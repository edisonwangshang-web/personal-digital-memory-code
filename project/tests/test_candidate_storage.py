"""Artificial candidate envelopes in temporary Git; never publish facts/models."""

import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.contracts import CaptureError, canonical, markdown
from pdm.skill_support import resolve_skill_refs
from pdm.storage_limits import (
    LEGACY_FILE_BYTES, MAX_CANDIDATE_BYTES, candidate_storage_sizes,
    read_limit, validate_candidate_bytes, validate_file_bytes,
)
from pdm.transaction import PipelineGit


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True).stdout.decode().strip()


class CandidateStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "commit", "--allow-empty", "-m", "artificial baseline")
        self.adapter = PipelineGit(self.repo)
        self.head = self.adapter.head()
        self.run = "run-" + "a" * 32
        self.path = "memory/staging/" + self.run + ".md"
        self.bundle = resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")

    def candidate(self, *, run=None, padding=LEGACY_FILE_BYTES + 100):
        # A resource-envelope fixture only: it has no publication authority.
        return {"run_id": run or self.run, "quality_contract": "skill-quality-v1",
                "skill_bundle": copy.deepcopy(self.bundle), "objects": [{"body": "人工当前候选正文。"}],
                "revision_context": {"artificial_history": "H" * padding}}

    def saved(self, value, *, schema="0.12", kind="candidate", header_run=None):
        return markdown({"schema_version": schema, "kind": kind, "run_id": header_run or value["run_id"]},
                        canonical(value).decode() + "\n")

    def write(self, files, head=None):
        with self.adapter.lock():
            return self.adapter.write_files(files, head or self.adapter.head(), "artificial candidate storage")

    def change(self, before, after):
        with self.adapter.lock():
            return self.adapter.write_changes({self.path: after}, {self.path: before},
                                              self.adapter.head(), "artificial conditional candidate")

    def recover(self):
        with self.adapter.lock():
            return self.adapter.recover()

    def assert_rejected(self, action):
        with self.assertRaises(CaptureError):
            action()

    def test_valid_large_candidate_commits_and_current_historical_bytes_match(self):
        value = self.candidate()
        data = self.saved(value)
        self.assertGreater(len(data), LEGACY_FILE_BYTES)
        self.assertLess(candidate_storage_sizes(value)["core_bytes"], LEGACY_FILE_BYTES)
        self.assertEqual(validate_candidate_bytes(data)["total_bytes"], len(data))
        commit = self.write({self.path: data})
        self.assertEqual(self.adapter._existing(self.path), data)
        self.assertEqual(self.adapter.read(commit, self.path), data)
        self.write({"memory/runs/processes/artificial-note.md": b"artificial unrelated marker\n"})
        historical = self.adapter.read(commit, self.path)
        validate_file_bytes(self.path, historical)
        self.assertEqual(historical, data)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertFalse(self.adapter._journal_path().exists())

    def test_legacy_fake_role_other_kind_and_other_paths_do_not_get_exception(self):
        value = self.candidate()
        fake = copy.deepcopy(value)
        fake["skill_bundle"]["skills"]["extract"]["ref"] = "knowledge-extract@9"
        legacy = copy.deepcopy(value)
        legacy["skill_bundle"] = resolve_skill_refs("knowledge-extract@9", "knowledge-review@7")
        fake_lineage = copy.deepcopy(value)
        fake_lineage["revision_context"] = value["revision_context"]["artificial_history"]
        cases = [(self.path, self.saved(legacy, schema="0.11")),
                 (self.path, self.saved(fake)), (self.path, self.saved(fake_lineage)),
                 (self.path, self.saved(value, kind="review")),
                 (self.path, self.saved(value, header_run="run-" + "b" * 32)),
                 ("memory/records/artificial.md", self.saved(value)),
                 ("memory/runs/processes/artificial.md", self.saved(value)),
                 ("memory/staging/artificial.md", self.saved(value)),
                 ("memory/staging/run-" + "b" * 32 + ".md", self.saved(value))]
        for path, data in cases:
            with self.subTest(path=path, bytes=len(data)):
                self.assert_rejected(lambda: self.write({path: data}, self.head))
                self.assertEqual(self.adapter.head(), self.head)
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")
                self.assertFalse(self.adapter._journal_path().exists())
        self.assertEqual(read_limit("memory/records/artificial.md"), LEGACY_FILE_BYTES)
        self.assertEqual(read_limit(self.path), MAX_CANDIDATE_BYTES)

    def test_current_body_and_full_history_caps_remain_separate(self):
        current = self.candidate(padding=100)
        current["objects"][0]["body"] = "X" * LEGACY_FILE_BYTES
        too_much_history = self.candidate(padding=MAX_CANDIDATE_BYTES)
        for value in (current, too_much_history):
            with self.subTest(core=candidate_storage_sizes(value)["core_bytes"]):
                self.assert_rejected(lambda: self.write({self.path: self.saved(value)}))
                self.assertEqual(self.adapter.head(), self.head)

    def test_duplicate_header_body_keys_and_nan_are_rejected(self):
        data = self.saved(self.candidate())
        bad = [data.replace(b'"kind":"candidate"', b'"kind":"candidate","kind":"candidate"', 1),
               data.replace(b'"objects":', b'"objects":[],"objects":', 1),
               data.replace(b'"objects":', b'"invalid":NaN,"objects":', 1)]
        for value in bad:
            with self.subTest(bytes=len(value)):
                self.assert_rejected(lambda: self.write({self.path: value}))
                self.assertEqual(self.adapter.head(), self.head)

    def test_create_transaction_fault_recovers_full_large_blob(self):
        data = self.saved(self.candidate())
        with patch.object(self.adapter, "_materialize", side_effect=OSError("artificial interruption")):
            self.assert_rejected(lambda: self.write({self.path: data}))
        committed = self.adapter.head()
        self.assertNotEqual(committed, self.head)
        self.assertFalse((self.repo / self.path).exists())
        result = self.recover()
        self.assertEqual(result["status"], "recovered")
        self.assertEqual((self.repo / self.path).read_bytes(), data)
        self.assertEqual(self.adapter.read(committed, self.path), data)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_conditional_large_candidate_recovers_before_and_after_snapshots(self):
        before = self.saved(self.candidate())
        after = self.saved(self.candidate(padding=LEGACY_FILE_BYTES + 500))
        first = self.write({self.path: before})
        with patch.object(self.adapter, "_materialize_change", side_effect=OSError("artificial interruption")):
            self.assert_rejected(lambda: self.change(before, after))
        committed = self.adapter.head()
        self.assertEqual((self.repo / self.path).read_bytes(), before)
        self.assertEqual(self.recover()["status"], "recovered")
        self.assertEqual((self.repo / self.path).read_bytes(), after)
        self.assertEqual(self.adapter.read(first, self.path), before)
        self.assertEqual(self.adapter.read(committed, self.path), after)
        self.assertFalse(list((self.repo / "memory/staging").glob(".pdm-change-*")))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_recovery_preserves_external_invalid_large_payload_and_journal(self):
        before = self.saved(self.candidate())
        after = self.saved(self.candidate(padding=LEGACY_FILE_BYTES + 500))
        self.write({self.path: before})
        with patch.object(self.adapter, "_materialize_change", side_effect=OSError("artificial interruption")):
            self.assert_rejected(lambda: self.change(before, after))
        tampered = before.replace(b'"schema_version":"0.12"', b'"schema_version":"0.11"', 1)
        (self.repo / self.path).write_bytes(tampered)
        journal = self.adapter._journal_path().read_bytes()
        self.assert_rejected(self.recover)
        self.assertEqual((self.repo / self.path).read_bytes(), tampered)
        self.assertEqual(self.adapter._journal_path().read_bytes(), journal)

    def test_recovery_checks_aggregate_cap_before_any_materialization(self):
        other_run = "run-" + "b" * 32
        files = {self.path: self.saved(self.candidate()),
                 "memory/staging/" + other_run + ".md": self.saved(self.candidate(run=other_run))}
        with patch.object(self.adapter, "_materialize", side_effect=OSError("artificial interruption")):
            self.assert_rejected(lambda: self.write(files))
        # Exercise the same unchanged total policy with a smaller test budget.
        with patch("pdm.transaction.MAX_TOTAL_BYTES", 2 * LEGACY_FILE_BYTES):
            self.assert_rejected(self.recover)
        self.assertTrue(self.adapter._journal_path().exists())
        self.assertTrue(all(not (self.repo / name).exists() for name in files))
        self.assertEqual(self.recover()["status"], "recovered")


if __name__ == "__main__":
    unittest.main()
