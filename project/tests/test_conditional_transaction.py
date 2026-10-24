"""条件事务只使用隔离 Git 仓库，验证 CAS、删除与恢复的实际边界。"""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.contracts import CaptureError, markdown
from pdm.transaction import PipelineGit


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


def document(identifier, text):
    return markdown({"schema_version": "0.2", "id": identifier}, text)


class ConditionalTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Isolated Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        self.update = "memory/records/update.md"
        self.delete = "memory/topics/delete.md"
        self.create = "memory/runs/controls/create.md"
        self.before = {self.update: document("update", "before\r\n"),
                       self.delete: document("delete", "delete before\n"), self.create: None}
        self.after = {self.update: document("update", "after\n"), self.delete: None,
                      self.create: document("control", "isolated control\n")}
        for relative, value in self.before.items():
            if value is not None:
                path = self.repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
        git(self.repo, "add", "memory")
        git(self.repo, "commit", "-m", "isolated baseline")
        self.adapter = PipelineGit(self.repo)
        self.head = self.adapter.head()

    def error(self, code, operation):
        with self.assertRaises(CaptureError) as context:
            operation()
        self.assertEqual(context.exception.code, code)

    def write(self):
        with self.adapter.lock():
            return self.adapter.write_changes(self.after, self.before, self.head, "isolated conditional changes")

    def recover(self):
        with self.adapter.lock():
            return self.adapter.recover()

    def pending(self):
        with patch.object(self.adapter, "_materialize_change", side_effect=OSError("isolated interruption")):
            self.error("transaction_recovery_required", self.write)
        return self.adapter.head()

    def partial(self, *, all_files=False):
        original = self.adapter._materialize_change
        count = 0

        def interrupted(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2 and not all_files:
                raise OSError("isolated next-file interruption")
            original(*args, **kwargs)
            if all_files and count == len(self.after):
                raise OSError("isolated post-materialization interruption")

        with patch.object(self.adapter, "_materialize_change", interrupted):
            self.error("transaction_recovery_required", self.write)
        return self.adapter.head()

    def assert_before_worktree(self):
        for relative, expected in self.before.items():
            path = self.repo / relative
            self.assertEqual(path.read_bytes() if path.exists() else None, expected)

    def assert_after_worktree(self):
        for relative, expected in self.after.items():
            path = self.repo / relative
            self.assertEqual(path.read_bytes() if path.exists() else None, expected)

    def test_create_update_delete_share_one_commit_and_exact_bytes(self):
        commit = self.write()
        self.assertEqual(git(self.repo, "show", "-s", "--format=%P", commit), self.head)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(git(self.repo, "diff-tree", "--no-commit-id", "--no-renames", "--name-status", "-r", commit).splitlines(),
                         ["M\t" + self.update, "A\t" + self.create, "D\t" + self.delete])
        self.assert_after_worktree()
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertFalse(self.adapter._journal_path().exists())
        self.assertFalse(list(self.repo.rglob(".pdm-change-*")))

    def test_equal_values_can_guard_a_path_but_entire_noop_is_rejected(self):
        self.after[self.delete] = self.before[self.delete]
        self.write()
        self.assertEqual((self.repo / self.delete).read_bytes(), self.before[self.delete])
        head = self.adapter.head()
        self.error("invalid_transaction", lambda: self.adapter.write_changes(self.after, self.after, head, "noop"))
        self.assertEqual(self.adapter.head(), head)

    def test_new_roots_and_single_pending_file_are_explicitly_allowed(self):
        files = {path: document("fixture", "isolated") for path in (
            "memory/runs/pending/item.md", "memory/runs/schedules/state.md", "memory/pending.md")}
        commit = self.adapter.write_changes(files, dict.fromkeys(files), self.head, "isolated new roots")
        for relative, value in files.items():
            self.assertEqual(self.adapter.read(commit, relative), value)

    def test_protected_sources_and_captures_reject_before_git_mutations(self):
        for relative in ("memory/sources/conversations/x/source.md", "memory/runs/captures/x.md",
                         "memory/other.md", "memory/runs/controllers/x.md"):
            with self.subTest(path=relative):
                original = self.adapter.run
                writes = []

                def observed(*args, **kwargs):
                    if args[0] in {"hash-object", "read-tree", "update-index", "commit-tree", "update-ref"}:
                        writes.append(args)
                    return original(*args, **kwargs)

                with patch.object(self.adapter, "run", observed):
                    self.error("path_rejected", lambda: self.adapter.write_changes(
                        {self.update: self.after[self.update], relative: b"blocked"},
                        {self.update: self.before[self.update], relative: None}, self.head, "blocked"))
                self.assertEqual(writes, [])
        self.assert_before_worktree()

    def test_wrong_expected_value_and_missing_state_do_not_write(self):
        for replacement in (None, b"wrong before"):
            expected = {**self.before, self.update: replacement}
            self.error("transaction_conflict", lambda: self.adapter.write_changes(self.after, expected, self.head, "wrong"))
        self.assertEqual(self.adapter.head(), self.head)
        self.assert_before_worktree()

    def test_invalid_maps_and_nonbytes_are_rejected(self):
        cases = ((self.after, {}), ({self.update: "text"}, {self.update: self.before[self.update]}),
                 ({self.update: None}, {self.update: "text"}), ({}, {}))
        for after, before in cases:
            with self.subTest(after=after):
                self.error("invalid_transaction", lambda: self.adapter.write_changes(after, before, self.head, "invalid"))
        self.assert_before_worktree()

    def test_ignored_existing_create_and_hidden_modified_target_are_detected(self):
        exclude = self.repo / ".git/info/exclude"
        exclude.write_text("memory/runs/controls/\n")
        target = self.repo / self.create
        target.parent.mkdir(parents=True)
        ignored = document("ignored", "external file")
        target.write_bytes(ignored)
        self.error("transaction_conflict", self.write)
        self.assertEqual(target.read_bytes(), ignored)
        target.unlink()
        git(self.repo, "update-index", "--skip-worktree", self.update)
        hidden = document("update", "hidden edit")
        (self.repo / self.update).write_bytes(hidden)
        self.error("transaction_conflict", self.write)
        self.assertEqual((self.repo / self.update).read_bytes(), hidden)

    def test_all_paths_preflight_before_any_git_mutation(self):
        outside = self.root / "outside"
        outside.mkdir()
        target = self.repo / self.create
        target.parent.mkdir(parents=True)
        target.symlink_to(outside / "private")
        (self.repo / ".git/info/exclude").write_text("memory/runs/controls/\n")
        with patch.object(self.adapter, "_write_journal") as journal:
            self.error("path_rejected", self.write)
            journal.assert_not_called()
        self.assert_before_except_create()
        self.assertFalse(list(outside.iterdir()))

    def assert_before_except_create(self):
        self.assertEqual((self.repo / self.update).read_bytes(), self.before[self.update])
        self.assertEqual((self.repo / self.delete).read_bytes(), self.before[self.delete])

    def test_stale_head_and_dirty_index_are_rejected(self):
        git(self.repo, "commit", "--allow-empty", "-m", "concurrent")
        self.error("head_conflict", self.write)
        self.head = self.adapter.head()
        (self.repo / self.update).write_bytes(document("update", "staged external"))
        git(self.repo, "add", self.update)
        before = git(self.repo, "diff", "--cached")
        self.error("dirty_worktree", self.write)
        self.assertEqual(git(self.repo, "diff", "--cached"), before)

    def test_cas_concurrent_commit_preserves_old_files_and_cleans_journal(self):
        original = self.adapter.run

        def raced(*args, **kwargs):
            if args[0] == "update-ref":
                git(self.repo, "commit", "--allow-empty", "-m", "concurrent")
            return original(*args, **kwargs)

        with patch.object(self.adapter, "run", raced):
            self.error("git_failed", self.write)
        self.assert_before_worktree()
        self.assertFalse(self.adapter._journal_path().exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_caller_mapping_mutation_does_not_change_frozen_transaction(self):
        original, expected = self.adapter.run, dict(self.after)

        def mutated(*args, **kwargs):
            if args[0] == "read-tree":
                self.after[self.update] = b"caller callback mutation"
                self.before[self.update] = b"caller callback before mutation"
            return original(*args, **kwargs)

        with patch.object(self.adapter, "run", mutated):
            self.write()
        for relative, value in expected.items():
            target = self.repo / relative
            self.assertEqual(target.read_bytes() if target.exists() else None, value)

    def test_unknown_successful_cas_keeps_journal_and_can_recover(self):
        original = self.adapter.run

        def lost_result(*args, **kwargs):
            value = original(*args, **kwargs)
            if args[0] == "update-ref":
                raise CaptureError("git_failed", "isolated lost CAS response")
            return value

        with patch.object(self.adapter, "run", lost_result):
            self.error("transaction_recovery_required", self.write)
        self.assert_before_worktree()
        self.assertTrue(self.adapter._journal_path().exists())
        self.assertEqual(self.recover()["status"], "recovered")
        self.assert_after_worktree()

    def test_pending_v3_binds_before_after_hashes_and_recovers(self):
        commit = self.pending()
        journal = json.loads(self.adapter._journal_path().read_text())
        self.assertEqual(journal["version"], 3)
        for relative, expected in self.before.items():
            self.assertEqual(journal["files"][relative]["before_sha256"],
                             None if expected is None else hashlib.sha256(expected).hexdigest())
        self.assertEqual(self.recover(), {"status": "recovered", "commit_sha": commit, "files": 3})
        self.assert_after_worktree()
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_recovery_checks_every_path_before_any_changes(self):
        self.pending()
        (self.repo / self.delete).write_bytes(b"external deletion target edit")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual((self.repo / self.update).read_bytes(), self.before[self.update])
        self.assertFalse((self.repo / self.create).exists())
        self.assertEqual((self.repo / self.delete).read_bytes(), b"external deletion target edit")

    def test_recovery_preserves_unrelated_staged_and_unstaged_edits(self):
        self.pending()
        (self.repo / "staged.txt").write_text("staged")
        git(self.repo, "add", "staged.txt")
        (self.repo / "draft.txt").write_text("unstaged")
        staged = git(self.repo, "diff", "--cached", "--", "staged.txt")
        self.recover()
        self.assertEqual(git(self.repo, "diff", "--cached", "--", "staged.txt"), staged)
        self.assertEqual((self.repo / "draft.txt").read_text(), "unstaged")
        self.assert_after_worktree()

    def test_external_deletion_after_completed_update_is_never_restored(self):
        self.partial()
        (self.repo / self.update).unlink()
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / self.update).exists())
        self.assertFalse((self.repo / self.create).exists())
        self.assertEqual((self.repo / self.delete).read_bytes(), self.before[self.delete])

    def test_external_cached_removal_is_never_recreated(self):
        self.partial()
        git(self.repo, "rm", "--cached", self.update)
        before = git(self.repo, "diff", "--cached")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(git(self.repo, "diff", "--cached"), before)
        self.assertFalse((self.repo / self.create).exists())

    def test_completed_delete_with_external_recreation_is_not_deleted_again(self):
        self.partial(all_files=True)
        (self.repo / self.delete).write_bytes(b"external replacement")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual((self.repo / self.delete).read_bytes(), b"external replacement")

    def test_other_branch_and_malformed_journal_are_rejected(self):
        self.pending()
        git(self.repo, "checkout", "-b", "other")
        self.error("transaction_recovery_conflict", self.recover)
        git(self.repo, "checkout", "main")
        journal = json.loads(self.adapter._journal_path().read_text())
        journal["progress"][self.update] = []
        self.adapter._journal_path().write_text(json.dumps(journal))
        self.error("transaction_recovery_conflict", self.recover)
        self.assert_before_worktree()

    def test_tampered_before_digest_is_rejected(self):
        self.pending()
        journal = json.loads(self.adapter._journal_path().read_text())
        journal["files"][self.update]["before_sha256"] = "0" * 64
        self.adapter._journal_path().write_text(json.dumps(journal))
        self.error("transaction_recovery_conflict", self.recover)
        self.assert_before_worktree()

    def test_uncommitted_valid_pending_journal_aborts_without_replay(self):
        self.pending()
        git(self.repo, "reset", "--mixed", self.head)
        self.assertEqual(self.recover(), {"status": "aborted", "commit_sha": None, "files": 0})
        self.assert_before_worktree()

    def test_unauthorized_commit_diff_is_rejected_before_materialization(self):
        self.pending()
        # HEAD has after bytes, so construct an amended tree without touching the worktree.
        index = self.root / "amended-index"
        self.adapter.run("read-tree", self.adapter.head(), index=str(index))
        blob = self.adapter.text("hash-object", "-w", "--stdin")
        self.adapter.run("update-index", "--add", "--cacheinfo", f"100644,{blob},rogue.txt", index=str(index))
        tree = self.adapter.run("write-tree", index=str(index)).decode().strip()
        commit = self.adapter.text("commit-tree", tree, "-p", self.head, "-m", "unauthorized")
        journal = json.loads(self.adapter._journal_path().read_text())
        journal["commit"] = commit
        self.adapter._journal_path().write_text(json.dumps(journal))
        self.adapter.run("update-ref", "HEAD", commit)
        self.error("transaction_recovery_conflict", self.recover)
        self.assert_before_worktree()

    def test_ambiguous_link_window_pauses_even_when_new_bytes_exist(self):
        original = self.adapter._progress

        def interrupted(journal, relative, phase):
            if phase == "linked":
                raise OSError("isolated link/log interruption")
            return original(journal, relative, phase)

        with patch.object(self.adapter, "_progress", interrupted):
            self.error("transaction_recovery_required", self.write)
        journal = json.loads(self.adapter._journal_path().read_text())
        self.assertEqual(journal["progress"][self.update], "linking")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / self.create).exists())

    def test_ambiguous_index_failure_does_not_rewrite_old_index(self):
        with patch.object(self.adapter, "_change_index", side_effect=OSError("isolated index failure")):
            self.error("transaction_recovery_required", self.write)
        before = git(self.repo, "ls-files", "--stage")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(git(self.repo, "ls-files", "--stage"), before)

    def test_confirmed_index_success_before_log_can_recover(self):
        original = self.adapter._change_index

        def interrupted(*args):
            original(*args)
            raise OSError("isolated index response loss")

        with patch.object(self.adapter, "_change_index", interrupted):
            self.error("transaction_recovery_required", self.write)
        self.recover()
        self.assert_after_worktree()
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_concurrent_target_creation_is_preserved_without_blind_replace(self):
        original = os.link

        def raced(source, destination, **kwargs):
            if str(source).startswith(".pdm-change-after-"):
                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                     0o600, dir_fd=kwargs["dst_dir_fd"])
                os.write(descriptor, b"external concurrent file")
                os.close(descriptor)
            return original(source, destination, **kwargs)

        with patch("pdm.transaction.os.link", raced):
            self.error("transaction_recovery_required", self.write)
        self.assertEqual((self.repo / self.update).read_bytes(), b"external concurrent file")
        parent = (self.repo / self.update).parent
        self.assertEqual([path.read_bytes() for path in parent.glob(".pdm-change-before-*")], [self.before[self.update]])
        self.assertEqual([path.read_bytes() for path in parent.glob(".pdm-change-after-*")], [self.after[self.update]])
        self.assertFalse((self.repo / self.create).exists())
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual((self.repo / self.update).read_bytes(), b"external concurrent file")

    def test_atomic_capture_preserves_concurrent_inode_replacement(self):
        original = self.adapter._move_noreplace
        external = b"EXTERNAL_CONCURRENT_REPLACEMENT"

        def raced(parent, source, destination):
            if source == "update.md":
                replacement = self.root / "external-replacement"
                replacement.write_bytes(external)
                os.replace(replacement, self.repo / self.update)
            return original(parent, source, destination)

        with patch.object(self.adapter, "_move_noreplace", raced):
            self.error("transaction_recovery_required", self.write)
        snapshots = list((self.repo / self.update).parent.glob(".pdm-change-before-*"))
        new_snapshots = list((self.repo / self.update).parent.glob(".pdm-change-after-*"))
        self.assertEqual([path.read_bytes() for path in snapshots], [external])
        self.assertEqual([path.read_bytes() for path in new_snapshots], [self.after[self.update]])
        self.assertFalse((self.repo / self.update).exists())
        self.assertFalse((self.repo / self.create).exists())
        self.assertEqual((self.repo / self.delete).read_bytes(), self.before[self.delete])
        journal = self.adapter._journal_path().read_bytes()
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(self.adapter._journal_path().read_bytes(), journal)
        self.assertEqual(snapshots[0].read_bytes(), external)
        self.assertEqual(new_snapshots[0].read_bytes(), self.after[self.update])

    def test_delete_atomic_capture_preserves_concurrent_replacement(self):
        original = self.adapter._move_noreplace
        external = b"EXTERNAL_DELETE_TARGET_REPLACEMENT"

        def raced(parent, source, destination):
            if source == "delete.md":
                replacement = self.root / "external-delete-replacement"
                replacement.write_bytes(external)
                os.replace(replacement, self.repo / self.delete)
            return original(parent, source, destination)

        with patch.object(self.adapter, "_move_noreplace", raced):
            self.error("transaction_recovery_required", lambda: self.adapter.write_changes(
                {self.delete: None}, {self.delete: self.before[self.delete]}, self.head, "isolated conditional delete"))
        snapshots = list((self.repo / self.delete).parent.glob(".pdm-change-before-*"))
        self.assertEqual([path.read_bytes() for path in snapshots], [external])
        self.assertFalse((self.repo / self.delete).exists())
        self.assertEqual((self.repo / self.update).read_bytes(), self.before[self.update])
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(snapshots[0].read_bytes(), external)

    def test_atomic_capture_rejects_equal_bytes_with_different_inode(self):
        original = self.adapter._move_noreplace

        def raced(parent, source, destination):
            if source == "update.md":
                replacement = self.root / "same-byte-replacement"
                replacement.write_bytes(self.before[self.update])
                os.replace(replacement, self.repo / self.update)
            return original(parent, source, destination)

        with patch.object(self.adapter, "_move_noreplace", raced):
            self.error("transaction_recovery_required", self.write)
        self.error("transaction_recovery_conflict", self.recover)
        snapshots = list((self.repo / self.update).parent.glob(".pdm-change-before-*"))
        self.assertEqual([path.read_bytes() for path in snapshots], [self.before[self.update]])
        self.assertFalse((self.repo / self.create).exists())

    def test_atomic_capture_interruption_preserves_old_snapshot_without_replay(self):
        original = self.adapter._move_noreplace

        def interrupted(parent, source, destination):
            original(parent, source, destination)
            if source == "update.md":
                raise OSError("isolated atomic capture response loss")

        with patch.object(self.adapter, "_move_noreplace", interrupted):
            self.error("transaction_recovery_required", self.write)
        snapshots = list((self.repo / self.update).parent.glob(".pdm-change-before-*"))
        self.assertEqual([path.read_bytes() for path in snapshots], [self.before[self.update]])
        self.assertFalse((self.repo / self.update).exists())
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / self.update).exists())

    def test_unavailable_atomic_move_blocks_before_git_side_effects(self):
        original, mutations = self.adapter.run, []

        def observed(*args, **kwargs):
            if args[0] in {"hash-object", "read-tree", "update-index", "write-tree", "commit-tree", "update-ref"}:
                mutations.append(args[0])
            return original(*args, **kwargs)

        with patch.object(self.adapter, "_move_noreplace", side_effect=CaptureError("atomic_move_unavailable", "isolated")), \
                patch.object(self.adapter, "run", observed):
            self.error("atomic_move_unavailable", self.write)
        self.assertEqual(mutations, [])
        self.assertEqual(self.adapter.head(), self.head)
        self.assertFalse(self.adapter._journal_path().exists())
        self.assertFalse(list(self.repo.rglob(".pdm-move-probe-*")))
        self.assert_before_worktree()

    def test_recovery_missing_atomic_move_keeps_pending_journal_unchanged(self):
        self.pending()
        journal = self.adapter._journal_path().read_bytes()
        with patch.object(self.adapter, "_move_noreplace", side_effect=CaptureError("atomic_move_unavailable", "isolated")):
            self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(self.adapter._journal_path().read_bytes(), journal)
        self.assert_before_worktree()
        self.assertFalse(list(self.repo.rglob(".pdm-move-probe-*")))

    def test_unrelated_descendant_can_finish_completed_transaction(self):
        commit = self.partial(all_files=True)
        (self.repo / "later.txt").write_text("later")
        git(self.repo, "add", "later.txt")
        git(self.repo, "commit", "-m", "unrelated later commit")
        later = self.adapter.head()
        self.assertEqual(self.recover()["commit_sha"], commit)
        self.assertEqual(self.adapter.head(), later)
        self.assert_after_worktree()

    def test_descendant_target_change_is_not_overwritten(self):
        self.partial(all_files=True)
        replacement = document("update", "external committed update")
        (self.repo / self.update).write_bytes(replacement)
        git(self.repo, "add", self.update)
        git(self.repo, "commit", "-m", "later target update")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual((self.repo / self.update).read_bytes(), replacement)


if __name__ == "__main__":
    unittest.main()
