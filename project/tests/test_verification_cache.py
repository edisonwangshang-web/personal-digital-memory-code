"""Read performance and live guards, using only disposable artificial data."""

import subprocess
from copy import deepcopy
from collections import Counter
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import test_revision_pipeline
from engine_fixtures import isolated_engine
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.evidence import ask_current, build_current_evidence
from pdm.git_adapter import GitAdapter
from pdm import model_calls
from pdm.skill_support import resolve_skill_refs
from pdm.pipeline import _committed, process_sources, publish
from pdm import pipeline
from pdm.revision_context import build_revision_context, verify_revision_context
from pdm.transaction import PipelineGit
from pdm.verification_cache import _fixed_command, immutable_git_read, immutable_read_scope


class ImmutableGitCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-artificial-read-cache-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        (self.repo / ".git").mkdir()
        self.other = self.repo / "other"
        (self.other / ".git").mkdir(parents=True)

    def test_only_explicit_immutable_commands_are_reused(self):
        sha, other = "a" * 40, "b" * 40
        accepted = [("show", sha + ":memory/records/artificial.md"),
                    ("ls-tree", "-r", "-z", "--name-only", sha, "--", "memory/records"),
                    ("merge-base", "--is-ancestor", sha, other),
                    ("rev-list", "--parents", "-n", "1", sha),
                    ("rev-parse", sha + "^"), ("cat-file", "-s", sha + ":memory/a.md")]
        rejected = [("status", "--porcelain"), ("show", "HEAD:memory/a.md"),
                    ("show", sha),
                    ("ls-tree", "-r", "--name-only", sha, "--", "memory/records"),
                    ("diff-tree", "--no-commit-id", "--no-renames", "-r", "-z", sha, other),
                    ("log", "--format=", "--name-only", "--diff-filter=D", sha, "--", "memory"),
                    ("log", "--full-history", "--format=%H", sha, "--", "memory/runs"),
                    ("log", "--ancestry-path", "--no-renames", "--diff-filter=D", sha + ".." + other, "--", "memory"),
                    ("log", "-1", "--format=%H", "--", "memory/a.md"),
                    ("log", "--format=%H", "HEAD", "--", "memory/a.md"),
                    ("log", "--since=yesterday", sha, "--", "memory"),
                    ("rev-parse", "HEAD"), ("update-ref", "HEAD", sha),
                    ("cat-file", "--batch"), ("log", "--all", "--", "memory")]
        for command in accepted:
            with self.subTest(command=command):
                self.assertTrue(_fixed_command(command))
        for command in rejected:
            with self.subTest(command=command):
                self.assertFalse(_fixed_command(command))

    def test_cache_is_scoped_bounded_and_separates_repositories(self):
        read = Mock(return_value=b"immutable")
        args = ("show", "a" * 40 + ":memory/a.md")
        with immutable_read_scope():
            self.assertEqual(immutable_git_read(self.repo, args, None, read), b"immutable")
            immutable_git_read(self.repo, args, None, read)
            immutable_git_read(self.other, args, None, read)
            immutable_git_read(self.repo, args, "private-index", read)
            self.assertEqual(read.call_count, 3)
        with immutable_read_scope():
            immutable_git_read(self.repo, args, None, read)
            self.assertEqual(read.call_count, 4)
        with patch("pdm.verification_cache.MAX_CACHE_BYTES", 1), immutable_read_scope():
            immutable_git_read(self.repo, args, None, read)
            immutable_git_read(self.repo, args, None, read)
            self.assertEqual(read.call_count, 6)

    def test_failures_and_mutable_reads_are_never_cached(self):
        read = Mock(side_effect=CaptureError("git_failed", "artificial failure"))
        with immutable_read_scope():
            for _ in range(2):
                with self.assertRaises(CaptureError):
                    immutable_git_read(self.repo, ("show", "a" * 40 + ":memory/a.md"), None, read)
        self.assertEqual(read.call_count, 2)
        read = Mock(side_effect=[b"first head", b"second head"])
        with immutable_read_scope():
            self.assertNotEqual(immutable_git_read(self.repo, ("rev-parse", "HEAD"), None, read),
                                immutable_git_read(self.repo, ("rev-parse", "HEAD"), None, read))

    def test_reinterpreted_graph_disables_hits_and_does_not_change_git_semantics(self):
        read = Mock(return_value=b"original")
        args = ("show", "a" * 40 + ":memory/a.md")
        with immutable_read_scope():
            self.assertEqual(immutable_git_read(self.repo, args, None, read), b"original")
            read.return_value = b"replacement"
            packed = self.repo / ".git/packed-refs"
            packed.write_text("b" * 40 + " refs/replace/" + "a" * 40 + "\n")
            self.assertEqual(immutable_git_read(self.repo, args, None, read), b"replacement")
            self.assertEqual(read.call_count, 2)
            packed.unlink()
            self.assertEqual(immutable_git_read(self.repo, args, None, read), b"original")
        for relative in ("shallow", "info/grafts", "refs/replace", "commondir"):
            target = self.repo / ".git" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("artificial graph override\n")
            with immutable_read_scope():
                immutable_git_read(self.repo, args, None, read)
                immutable_git_read(self.repo, args, None, read)
            target.unlink()
        self.assertEqual(read.call_count, 10)


class FrozenRequestBindingCacheTests(unittest.TestCase):
    def payload(self):
        return {"quality_contract": "skill-quality-v1",
                "skill_bundle": resolve_skill_refs("knowledge-extract@6", "knowledge-review@5"),
                "quality_context": {}, "sources": [{"content": "Artificial fixed input.", "value": [1]}]}

    def test_reuses_only_exact_inputs_and_returns_independent_binding(self):
        payload = self.payload()
        with patch("pdm.model_calls.render_model_request", wraps=model_calls.render_model_request) as render:
            with immutable_read_scope():
                first = model_calls.request_binding("extract", payload)
                saved = dict(first)
                first["sha256"] = "caller mutation"
                self.assertEqual(model_calls.request_binding("extract", deepcopy(payload)), saved)
                changed = deepcopy(payload)
                changed["sources"][0]["value"] = [1.0]
                self.assertNotEqual(model_calls.request_binding("extract", changed), saved)
                changed["sources"][0]["value"] = [True]
                self.assertNotEqual(model_calls.request_binding("extract", changed), saved)
                # Even when the wire renderer canonicalizes dict order, the
                # cache is conservative and renders a differently ordered input.
                reordered = dict(reversed(list(payload.items())))
                self.assertEqual(model_calls.request_binding("extract", reordered), saved)
                model_calls.request_binding("review", payload)
                self.assertEqual(render.call_count, 5)
            self.assertEqual(model_calls.request_binding("extract", payload), saved)
            self.assertEqual(render.call_count, 6)

    def test_unsupported_python_types_and_failed_rules_do_not_hit(self):
        payload = self.payload()
        with patch("pdm.model_calls.render_model_request", wraps=model_calls.render_model_request) as render, immutable_read_scope():
            model_calls.request_binding("extract", payload)
            unsupported = deepcopy(payload)
            unsupported["sources"][0]["value"] = (1,)
            with self.assertRaises(ValueError):
                model_calls.request_binding("extract", unsupported)
            invalid = deepcopy(payload)
            invalid["skill_bundle"]["skills"]["extract"]["files"][0]["content"] += "invalid bytes"
            for _ in range(2):
                with self.assertRaises(ValueError):
                    model_calls.request_binding("extract", invalid)
            self.assertEqual(render.call_count, 4)

    def test_legacy_prompt_changes_are_read_inside_the_same_scope(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-artificial-prompt-")
        self.addCleanup(temporary.cleanup)
        prompts = Path(temporary.name)
        prompt = prompts / "answer.md"
        prompt.write_text("Artificial first instructions.\n")
        with patch.object(model_calls, "PROMPTS", prompts), immutable_read_scope():
            first = model_calls.request_binding("answer", {"question": "Artificial question?"})
            prompt.write_text("Artificial changed instructions.\n")
            self.assertNotEqual(model_calls.request_binding("answer", {"question": "Artificial question?"}), first)

    def test_binding_cache_full_falls_back_without_skipping_render(self):
        payload = self.payload()
        with patch("pdm.verification_cache.MAX_CACHE_ENTRIES", 0), \
                patch("pdm.model_calls.render_model_request", wraps=model_calls.render_model_request) as render, immutable_read_scope():
            self.assertEqual(model_calls.request_binding("extract", payload), model_calls.request_binding("extract", payload))
            self.assertEqual(render.call_count, 2)


class EvidenceVerificationCacheTests(unittest.TestCase):
    def setUp(self):
        test_revision_pipeline.RevisionPipelineTests.setUp(self)
        isolated_engine(self)

    prepare = test_revision_pipeline.RevisionPipelineTests.prepare
    sources = test_revision_pipeline.RevisionPipelineTests.sources
    model = test_revision_pipeline.RevisionPipelineTests.model
    run_batch = test_revision_pipeline.RevisionPipelineTests.run_batch

    def publication(self, rejected_attempts=1):
        paths, _, projection = self.prepare()
        self.verdict = "needs_revision"
        old = self.run_batch(paths, projection, "auto")
        self.assertEqual(old["status"], "needs_revision", old)
        result = old
        for index in range(rejected_attempts):
            self.verdict = "passed" if index == rejected_attempts - 1 else "needs_revision"

            def repair(role, payload):
                response = self.model(role, payload)
                if role == "extract":
                    previous = payload["revision_context"]["candidate"]["objects"]
                    by_id = {obj["metadata"]["id"]: obj for obj in previous}
                    for obj in [*response["output"]["objects"], *response["output"]["wikis"]]:
                        obj["body"] = by_id[obj["id"]]["body"] + " The observation stays limited to the supplied artificial input."
                return response

            result = process_sources(self.repo, paths, related_runs=[],
                                     revision_from_run=result["run_id"], model_call=repair)
            self.assertEqual(result["status"], self.verdict, result)
        publish(self.repo, result["run_id"])
        return paths, old, result

    def test_revision_proof_reuses_git_work_without_changing_selected_evidence(self):
        paths, old, result = self.publication(rejected_attempts=3)
        git = PipelineGit(self.repo)
        head, calls_before = git.head(), self.number
        outputs, counts, seconds, blob_reads = [], [], [], []
        for limit in (0, 8192):
            with patch("pdm.verification_cache.MAX_CACHE_ENTRIES", limit), \
                    patch("pdm.git_adapter.subprocess.run", wraps=subprocess.run) as processes:
                start = time.monotonic()
                outputs.append(build_current_evidence(self.repo, paths, [result["run_id"]],
                    object_paths=["memory/records/record-" + paths[0].split("/")[-2][-10:] + ".md"],
                    compact_sources=True))
                counts.append(processes.call_count)
                seconds.append(time.monotonic() - start)
                blobs = Counter()
                for call in processes.call_args_list:
                    command = call.args[0]
                    # Count the actual raw-object subprocess boundary, without
                    # depending on the cache's own eligibility classifier.
                    if (len(command) == 5 and command[:4] == ["git", "-c", "commit.gpgsign=false", "show"]
                            and ":" in command[4]):
                        blobs[(str(call.kwargs["cwd"]), command[4])] += 1
                blob_reads.append(blobs)
        self.cache_measurement = {"artificial_attempts": 4, "uncached_git_processes": counts[0],
                                  "cached_git_processes": counts[1], "seconds": seconds}
        self.assertEqual(outputs[0], outputs[1])
        self.assertLess(counts[1], counts[0], counts)
        self.assertGreater(max(blob_reads[0].values()), 1)
        self.assertEqual(set(blob_reads[0]), set(blob_reads[1]))
        self.assertEqual(max(blob_reads[1].values()), 1)
        proof = outputs[1]["publications"][0]
        self.assertEqual(proof["run_id"], result["run_id"])
        self.assertEqual((git.head(), self.number), (head, calls_before))
        self.assertEqual(git.text("status", "--porcelain"), "")
        self.assertTrue((self.repo / old["review_path"]).is_file())

    def test_cached_blob_does_not_hide_changed_current_bytes(self):
        paths, _, _ = self.publication()
        git = PipelineGit(self.repo)
        with immutable_read_scope():
            original = _committed(git, paths[0])
            target = self.repo / paths[0]
            target.write_bytes(original + b"Artificial uncommitted alteration.\n")
            with self.assertRaises(CaptureError) as raised:
                _committed(git, paths[0])
            self.assertEqual(raised.exception.code, "saved_bytes_changed")

    def test_validation_does_not_collect_an_unused_authority_list(self):
        paths, old, _ = self.publication()
        git = PipelineGit(self.repo)
        context = build_revision_context(git, old["run_id"], source_paths=paths)
        before = git.head(), self.number
        with patch("pdm.pipeline._read_reviewed", wraps=pipeline._read_reviewed) as reviewed, \
                patch("pdm.pipeline._computation_snapshots", wraps=pipeline._computation_snapshots) as snapshots:
            self.assertEqual(verify_revision_context(git, context, source_paths=paths, include_snapshots=False), [])
            self.assertEqual(reviewed.call_count, 1)
            self.assertEqual(snapshots.call_count, 0)
            authority = verify_revision_context(git, context, source_paths=paths)
            self.assertTrue(authority)
            self.assertEqual(reviewed.call_count, 2)
            self.assertEqual(snapshots.call_count, 1)
        self.assertEqual((git.head(), self.number), before)

    def test_omitting_unused_snapshots_still_rejects_changed_review_bytes(self):
        paths, old, _ = self.publication()
        git = PipelineGit(self.repo)
        context = build_revision_context(git, old["run_id"], source_paths=paths)
        target = self.repo / old["review_path"]
        git.run("update-index", "--assume-unchanged", "--", old["review_path"])
        target.write_bytes(target.read_bytes() + b"Artificial hidden alteration.\n")
        with self.assertRaises(CaptureError) as raised:
            verify_revision_context(git, context, source_paths=paths, include_snapshots=False)
        self.assertEqual(raised.exception.code, "saved_bytes_changed")

    def test_real_git_replacement_and_packed_replacement_bypass_cached_sha(self):
        git = PipelineGit(self.repo)
        path, anchor = self.saved["path"], git.head()
        original = git.read(anchor, path)
        with immutable_read_scope():
            self.assertEqual(git.read(anchor, path), original)
            replaced = original + b"Artificial replacement-only bytes.\n"
            (self.repo / path).write_bytes(replaced)
            git.run("add", "--", path)
            tree = git.text("write-tree")
            replacement = git.text("commit-tree", tree, "-m", "artificial replacement tree")
            git.run("replace", anchor, replacement)
            self.assertEqual(git.head(), anchor)
            self.assertEqual(git.read(anchor, path), replaced)
            self.assertEqual(git.read(anchor, path), GitAdapter.run(git, "show", anchor + ":" + path))
            git.run("pack-refs", "--all", "--prune")
            self.assertEqual(git.read(anchor, path), replaced)
            self.assertEqual(git.read(anchor, path), GitAdapter.run(git, "show", anchor + ":" + path))

    def test_rename_configuration_does_not_hide_deleted_control_history(self):
        git = PipelineGit(self.repo)
        old, new = "memory/runs/controls/artificial-old.md", "memory/runs/controls/artificial-new.md"
        target = self.repo / old
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Artificial identical bytes for rename detection.\n")
        git.run("add", "--", old)
        git.run("commit", "-m", "artificial control history")
        git.run("mv", old, new)
        git.run("commit", "-m", "artificial unchanged rename")
        anchor = git.head()
        command = ("log", "--format=", "--name-only", "--diff-filter=D", anchor, "--", "memory/runs/controls")
        with immutable_read_scope():
            git.run("config", "diff.renames", "true")
            previous = git.run(*command)
            previous_show = git.run("show", anchor)
            self.assertEqual(previous.strip(), b"")
            git.run("config", "diff.renames", "false")
            self.assertEqual(git.head(), anchor)
            current = git.run(*command)
            current_show = git.run("show", anchor)
            self.assertIn(old.encode(), current)
            self.assertNotEqual(current, previous)
            self.assertEqual(current, GitAdapter.run(git, *command))
            self.assertNotEqual(current_show, previous_show)
            self.assertEqual(current_show, GitAdapter.run(git, "show", anchor))
            # Even a plain full-SHA history query changes with log.follow.
            git.run("config", "diff.renames", "true")
            follow = ("log", "--full-history", "--format=%H", anchor, "--", new)
            git.run("config", "log.follow", "false")
            not_followed = git.run(*follow)
            git.run("config", "log.follow", "true")
            followed = git.run(*follow)
            self.assertNotEqual(followed, not_followed)
            self.assertEqual(followed, GitAdapter.run(git, *follow))

    def test_answer_rebuilds_fresh_scope_after_concurrent_commit(self):
        paths, _, result = self.publication()
        source = paths[0].split("/")
        source_id, version = source[-2], source[-1][:-3]

        def changed_while_answering(role, payload):
            self.assertEqual(role, "answer")
            # No read lock is held during the actual model call. The new commit
            # must be detected even though all old immutable blobs still exist.
            capture(self.repo, {"source_key": "fixture:concurrent-cache-read", "source_type": "note",
                               "acquisition_status": "full_text", "body": "Artificial later input.\n"})
            return {"output": {"answer": "Artificial fact.[1]", "citations": [
                        {"source_id": source_id, "source_version": version, "locator": "L1-L1"}],
                        "limitations": []}, "model": "artificial", "invocation_id": "model-" + "f" * 32,
                    "elapsed_seconds": 0.01, "usage": None}

        with self.assertRaises(CaptureError) as raised:
            ask_current(self.repo, paths, "What artificial fact is recorded?", [result["run_id"]],
                        model_call=changed_while_answering)
        self.assertEqual(raised.exception.code, "evidence_changed")


if __name__ == "__main__":
    unittest.main()
