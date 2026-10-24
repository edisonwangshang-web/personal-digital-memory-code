"""加工事务仅使用临时仓库与隔离 Markdown，不读真实个人资料。"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.contracts import CaptureError, markdown
from pdm.git_adapter import GitAdapter
from pdm.transaction import PipelineGit


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Isolated Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "fixture baseline")
        self.adapter = PipelineGit(self.repo)
        self.head = self.adapter.head()
        self.files = {
            "memory/records/fixture-record.md": markdown({"schema_version": "0.2", "id": "fixture-record"}, "隔离样例\r\n"),
            "memory/topics/fixture-topic.md": markdown({"schema_version": "0.2", "id": "fixture-topic"}, "隔离主题\n"),
        }

    def error(self, code, operation):
        with self.assertRaises(CaptureError) as context:
            operation()
        self.assertEqual(context.exception.code, code)

    def write(self):
        with self.adapter.lock():
            return self.adapter.write_files(self.files, self.head, "pipeline: isolated fixture")

    def pending(self):
        with patch.object(self.adapter, "_materialize", side_effect=OSError("isolated interruption")):
            self.error("transaction_recovery_required", self.write)
        return self.adapter.head()

    def recover(self):
        with self.adapter.lock():
            return self.adapter.recover()

    def partial(self, *, all_files=False):
        original = self.adapter._materialize
        count = 0

        def interrupt(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2 and not all_files:
                raise OSError("isolated next-file interruption")
            original(*args, **kwargs)
            if count == 2:
                raise OSError("isolated completion interruption")

        with patch.object(self.adapter, "_materialize", interrupt):
            self.error("transaction_recovery_required", self.write)
        return self.adapter.head()

    def test_multi_file_one_commit_exact_bytes_and_clean_index(self):
        commit = self.write()
        self.assertNotEqual(commit, self.head)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "2")
        self.assertEqual(git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines(), sorted(self.files))
        for relative, data in self.files.items():
            self.assertEqual(self.adapter.read(commit, relative), data)
            self.assertEqual((self.repo / relative).read_bytes(), data)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertFalse(self.adapter._journal_path().exists())

    def test_capture_write_scope_does_not_expand(self):
        self.error("path_rejected", lambda: GitAdapter(self.repo).path("memory/records/fixture.md"))
        self.error("invalid_transaction", lambda: self.adapter.write_commit("memory/records/fixture.md", b"fixture", self.head))
        source = "memory/sources/conversations/fixture/source.md"
        self.assertEqual(self.adapter.path(source), self.repo.resolve() / source)
        self.error("path_rejected", lambda: self.adapter.write_files({source: b"fixture"}, self.head, "fixture"))
        self.assertEqual(self.adapter.head(), self.head)

    def test_paths_reject_traversal_non_markdown_and_git_pathspec(self):
        invalid = ("../escape.md", "/tmp/escape.md", "memory/records/../topics/x.md", "memory/records/x.json",
                   "memory/records/a\\b.md", "memory//records/x.md", "memory/records/./x.md", "memory/records/*.md",
                   "memory/records/:x.md", "memory/records/.hidden.md", "memory/runs/captures/escape.json")
        for relative in invalid:
            with self.subTest(path=relative):
                self.error("path_rejected", lambda: self.adapter.path(relative))
        self.assertFalse((self.repo / "memory").exists())

    def test_symlink_parent_and_target_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.repo / "memory").symlink_to(outside, target_is_directory=True)
        self.error("path_rejected", lambda: self.adapter.path("memory/records/x.md"))
        (self.repo / "memory").unlink()
        target = self.repo / "memory/records/x.md"
        target.parent.mkdir(parents=True)
        target.symlink_to(outside / "x.md")
        self.error("path_rejected", lambda: self.adapter.path("memory/records/x.md"))
        self.assertEqual(list(outside.iterdir()), [])

    def test_create_only_rejects_existing_without_changing_history(self):
        commit = self.write()
        self.error("target_exists", lambda: self.adapter.write_files(self.files, commit, "attempt overwrite"))
        self.assertEqual(self.adapter.head(), commit)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_dirty_and_expected_head_conflicts_do_not_write(self):
        (self.repo / "draft.txt").write_text("isolated draft")
        self.error("dirty_worktree", self.write)
        (self.repo / "draft.txt").unlink()
        git(self.repo, "commit", "--allow-empty", "-m", "new fixture baseline")
        self.error("head_conflict", self.write)
        self.assertFalse((self.repo / "memory").exists())

    def test_cas_concurrent_commit_cleans_journal_and_temporary_index(self):
        original_run = self.adapter.run

        def concurrent(*args, **kwargs):
            if args[0] == "update-ref":
                git(self.repo, "commit", "--allow-empty", "-m", "concurrent fixture")
            return original_run(*args, **kwargs)

        with patch.object(self.adapter, "run", concurrent):
            self.error("git_failed", self.write)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "concurrent fixture")
        self.assertFalse((self.repo / "memory").exists())
        self.assertFalse(self.adapter._journal_path().exists())
        self.assertEqual(list((self.repo / ".git").glob("pdm-pipeline-*")), [])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_precommit_failure_leaves_no_partial_publication(self):
        original_run = self.adapter.run

        def fail(*args, **kwargs):
            if args[0] == "write-tree":
                raise CaptureError("fixture_failure", "isolated failure")
            return original_run(*args, **kwargs)

        with patch.object(self.adapter, "run", fail):
            self.error("fixture_failure", self.write)
        self.assertEqual(self.adapter.head(), self.head)
        self.assertFalse((self.repo / "memory").exists())
        self.assertEqual(list((self.repo / ".git").glob("pdm-pipeline-*")), [])

    def test_committed_materialization_failure_is_recoverable(self):
        commit = self.pending()
        self.assertNotEqual(commit, self.head)
        for relative, value in self.files.items():
            self.assertEqual(self.adapter.read(commit, relative), value)
            self.assertFalse((self.repo / relative).exists())
        self.error("transaction_recovery_required", lambda: self.adapter.write_files(self.files, commit, "no retry before recovery"))
        result = self.recover()
        self.assertEqual(result, {"status": "recovered", "commit_sha": commit, "files": 2})
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(self.recover(), {"status": "clean", "commit_sha": None, "files": 0})

    def test_recovery_preserves_unrelated_staged_and_unstaged_edits(self):
        self.pending()
        (self.repo / "staged.txt").write_text("isolated staged")
        git(self.repo, "add", "staged.txt")
        (self.repo / "draft.txt").write_text("isolated draft")
        before = git(self.repo, "diff", "--cached", "--", "staged.txt")
        self.recover()
        self.assertEqual(git(self.repo, "diff", "--cached", "--", "staged.txt"), before)
        self.assertEqual((self.repo / "draft.txt").read_text(), "isolated draft")
        self.assertNotIn("memory/", git(self.repo, "status", "--porcelain"))

    def test_recovery_preflights_all_targets_before_any_materialization(self):
        self.pending()
        relative = "memory/topics/fixture-topic.md"
        (self.repo / relative).parent.mkdir(parents=True)
        (self.repo / relative).write_bytes(b"user change")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual((self.repo / relative).read_bytes(), b"user change")
        self.assertFalse((self.repo / "memory/records/fixture-record.md").exists())
        self.assertTrue(self.adapter._journal_path().exists())

    def test_recovery_will_not_restore_subsequently_deleted_record(self):
        commit = self.pending()
        git(self.repo, "reset", "--hard", commit)
        git(self.repo, "rm", "memory/records/fixture-record.md")
        git(self.repo, "commit", "-m", "explicit fixture removal")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / "memory/records/fixture-record.md").exists())

    def test_recovery_rejects_other_branch_and_bad_journal(self):
        self.pending()
        git(self.repo, "checkout", "-b", "other-fixture")
        self.error("transaction_recovery_conflict", self.recover)
        git(self.repo, "checkout", "main")
        journal = json.loads(self.adapter._journal_path().read_text())
        journal["files"]["memory/records/fixture-record.md"]["sha256"] = "0" * 64
        self.adapter._journal_path().write_text(json.dumps(journal))
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / "memory/records/fixture-record.md").exists())

    def test_recovery_handles_original_commit_descendant_without_reset(self):
        commit = self.partial(all_files=True)
        (self.repo / "later.txt").write_text("unrelated later commit")
        git(self.repo, "add", "later.txt")
        git(self.repo, "commit", "-m", "later fixture change")
        later = self.adapter.head()
        self.assertEqual(self.recover()["commit_sha"], commit)
        self.assertEqual(self.adapter.head(), later)
        self.assertEqual((self.repo / "later.txt").read_text(), "unrelated later commit")

    def test_uncommitted_journal_is_aborted_without_replaying(self):
        self.pending()
        git(self.repo, "reset", "--mixed", self.head)
        result = self.recover()
        self.assertEqual(result, {"status": "aborted", "commit_sha": None, "files": 0})
        self.assertFalse((self.repo / "memory/records/fixture-record.md").exists())

    def test_journal_symlink_is_rejected_without_reading_target(self):
        outside = self.root / "private-fixture.txt"
        outside.write_text("not a transaction")
        self.adapter._journal_path().symlink_to(outside)
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(outside.read_text(), "not a transaction")

    def test_ambiguous_index_failure_pauses_instead_of_recreating_index(self):
        original_run = self.adapter.run
        count = 0

        def fail_once(*args, **kwargs):
            nonlocal count
            if args[0] == "update-index" and not kwargs.get("index"):
                count += 1
                if count == 1:
                    raise CaptureError("fixture_failure", "isolated index interruption")
            return original_run(*args, **kwargs)

        with patch.object(self.adapter, "run", fail_once):
            self.error("transaction_recovery_required", self.write)
        self.assertTrue((self.repo / "memory/records/fixture-record.md").exists())
        self.assertFalse((self.repo / "memory/topics/fixture-topic.md").exists())
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(git(self.repo, "ls-files", "--", "memory/records/fixture-record.md"), "")
        self.assertFalse((self.repo / "memory/topics/fixture-topic.md").exists())

    def test_existing_journal_is_never_removed_by_a_new_write(self):
        self.pending()
        before = self.adapter._journal_path().read_bytes()
        self.error("transaction_recovery_required", lambda: self.adapter.write_files(self.files, self.adapter.head(), "second fixture"))
        self.assertEqual(self.adapter._journal_path().read_bytes(), before)

    def test_unknown_schema_blocks_transaction_without_changing_memory(self):
        path = self.repo / "memory/staging/old-fixture.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(markdown({"schema_version": "pdm-1"}, "isolated incompatible fixture"))
        git(self.repo, "add", "memory/staging/old-fixture.md")
        git(self.repo, "commit", "-m", "fixture incompatible schema")
        head = self.adapter.head()
        self.error("incompatible_memory_schema", lambda: self.adapter.write_files(self.files, head, "blocked fixture"))
        self.assertEqual(self.adapter.head(), head)
        self.assertFalse((self.repo / "memory/records").exists())

    def test_crash_leftover_temporary_file_is_verified_and_removed(self):
        self.pending()
        journal = json.loads(self.adapter._journal_path().read_text())
        relative = "memory/records/fixture-record.md"
        temporary_name = self.adapter._temporary_name(relative, journal["files"][relative]["blob"])
        temporary = (self.repo / relative).parent / temporary_name
        temporary.parent.mkdir(parents=True)
        temporary.write_bytes(self.files[relative])
        self.recover()
        self.assertFalse(temporary.exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_partial_temporary_file_fails_closed_before_other_targets(self):
        self.pending()
        journal = json.loads(self.adapter._journal_path().read_text())
        relative = "memory/topics/fixture-topic.md"
        temporary_name = self.adapter._temporary_name(relative, journal["files"][relative]["blob"])
        temporary = (self.repo / relative).parent / temporary_name
        temporary.parent.mkdir(parents=True)
        temporary.write_bytes(b"partial fixture")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(temporary.read_bytes(), b"partial fixture")
        self.assertFalse((self.repo / "memory/records/fixture-record.md").exists())

    def test_completed_file_and_remaining_pending_file_can_recover(self):
        commit = self.partial()
        journal = json.loads(self.adapter._journal_path().read_text())
        self.assertEqual(journal["progress"]["memory/records/fixture-record.md"], "done")
        self.assertEqual(journal["progress"]["memory/topics/fixture-topic.md"], "pending")
        self.assertEqual(self.recover()["commit_sha"], commit)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_uncommitted_git_rm_after_partial_materialization_is_preserved(self):
        self.partial()
        relative = "memory/records/fixture-record.md"
        git(self.repo, "rm", relative)
        before = git(self.repo, "diff", "--cached")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / relative).exists())
        self.assertEqual(git(self.repo, "diff", "--cached"), before)
        self.assertFalse((self.repo / "memory/topics/fixture-topic.md").exists())

    def test_uncommitted_unlink_after_partial_materialization_is_preserved(self):
        self.partial()
        relative = "memory/records/fixture-record.md"
        (self.repo / relative).unlink()
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / relative).exists())
        self.assertFalse((self.repo / "memory/topics/fixture-topic.md").exists())

    def test_uncommitted_cached_removal_after_partial_materialization_is_preserved(self):
        self.partial()
        relative = "memory/records/fixture-record.md"
        git(self.repo, "rm", "--cached", relative)
        before = (self.repo / relative).read_bytes()
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(git(self.repo, "ls-files", "--", relative), "")
        self.assertEqual((self.repo / relative).read_bytes(), before)
        self.assertFalse((self.repo / "memory/topics/fixture-topic.md").exists())

    def test_link_then_unlink_before_progress_update_never_recreates_target(self):
        original_link = os.link

        def interrupted_link(*args, **kwargs):
            original_link(*args, **kwargs)
            os.unlink(args[1], dir_fd=kwargs["dst_dir_fd"])
            raise OSError("isolated link/log interruption")

        with patch("pdm.transaction.os.link", interrupted_link):
            self.error("transaction_recovery_required", self.write)
        journal = json.loads(self.adapter._journal_path().read_text())
        self.assertEqual(journal["progress"]["memory/records/fixture-record.md"], "linking")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / "memory/records/fixture-record.md").exists())

    def test_git_rm_between_index_success_and_done_log_is_preserved(self):
        original = self.adapter._progress

        def interrupted_progress(journal, relative, phase):
            if phase == "done":
                git(self.repo, "rm", relative)
                raise OSError("isolated index/log interruption")
            return original(journal, relative, phase)

        with patch.object(self.adapter, "_progress", interrupted_progress):
            self.error("transaction_recovery_required", self.write)
        journal = json.loads(self.adapter._journal_path().read_text())
        self.assertEqual(journal["progress"]["memory/records/fixture-record.md"], "indexing")
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / "memory/records/fixture-record.md").exists())

    def test_fifo_target_is_rejected_without_blocking_recovery(self):
        self.pending()
        relative = "memory/records/fixture-record.md"
        target = self.repo / relative
        target.parent.mkdir(parents=True)
        os.mkfifo(target)
        self.error("transaction_recovery_conflict", self.recover)
        self.assertTrue(target.exists())
        self.assertFalse((self.repo / "memory/topics/fixture-topic.md").exists())

    def test_fifo_temporary_file_is_rejected_without_blocking_recovery(self):
        self.pending()
        journal = json.loads(self.adapter._journal_path().read_text())
        relative = "memory/records/fixture-record.md"
        name = self.adapter._temporary_name(relative, journal["files"][relative]["blob"])
        target = (self.repo / relative).parent / name
        target.parent.mkdir(parents=True)
        os.mkfifo(target)
        self.error("transaction_recovery_conflict", self.recover)
        self.assertFalse((self.repo / relative).exists())

    def test_fifo_journal_is_rejected_without_blocking_recovery(self):
        os.mkfifo(self.adapter._journal_path())
        self.error("transaction_recovery_conflict", self.recover)
        self.assertEqual(self.adapter.head(), self.head)


if __name__ == "__main__":
    unittest.main()
