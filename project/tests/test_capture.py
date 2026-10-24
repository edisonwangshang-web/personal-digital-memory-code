"""全部材料为隔离样例，仅在临时 Git 仓库运行。"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError, MAX_SOURCE_BYTES, parse_markdown, source_contract
from pdm.git_adapter import GitAdapter


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Isolated Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "fixture baseline")
        self.payload = {
            "source_key": "fixture-conversation-1", "source_type": "conversation",
            "platform": "isolated-fixture", "original_url": None,
            "acquisition_status": "full_text", "body": "用户：隔离样例。\n助手：保留顺序。\n",
            "original_time": None,
        }

    def assert_error(self, code, operation):
        with self.assertRaises(CaptureError) as context:
            operation()
        self.assertEqual(context.exception.code, code)

    def test_capture_versions_receipt_and_idempotency(self):
        baseline = git(self.repo, "rev-parse", "HEAD")
        first = capture(self.repo, self.payload, baseline)
        again = capture(self.repo, dict(self.payload), first["head"])
        self.assertEqual(first["head"], again["head"])
        self.assertTrue(again["reused"])
        self.assertEqual(first["remote_status"], "not_requested")
        self.assertEqual(first["knowledge_status"], "unprocessed")
        metadata, body = parse_markdown((self.repo / first["path"]).read_bytes())
        self.assertEqual(body, self.payload["body"])
        self.assertIsNone(metadata["original_time"])
        self.assertTrue(metadata["captured_at"].endswith("+00:00"))
        receipt, _ = parse_markdown((self.repo / first["receipt_path"]).read_bytes())
        self.assertEqual(receipt["commit_sha"], first["commit_sha"])
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "3")
        updated = capture(self.repo, {**self.payload, "body": "用户：新的隔离内容。"})
        self.assertEqual(first["source_id"], updated["source_id"])
        self.assertNotEqual(first["source_version"], updated["source_version"])
        self.assertTrue((self.repo / first["path"]).exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_same_content_after_clock_changes_has_same_version(self):
        with patch("pdm.capture.timestamp", return_value="2026-01-01T00:00:00+00:00"):
            first = capture(self.repo, self.payload)
        with patch("pdm.capture.timestamp", return_value="2027-01-01T00:00:00+00:00"):
            second = capture(self.repo, self.payload)
        self.assertEqual(first["source_version"], second["source_version"])
        self.assertEqual(first["head"], second["head"])

    def test_bad_paths_and_symlink_are_rejected(self):
        adapter = GitAdapter(self.repo)
        for path in ("../escape.md", "memory/records/no.md", "memory/sources/inbox/../../records/no.md", "/tmp/no.md"):
            self.assert_error("path_rejected", lambda: adapter.path(path))
        outside = self.root / "outside"
        outside.mkdir()
        (self.repo / "memory").symlink_to(outside, target_is_directory=True)
        git(self.repo, "add", "memory")
        git(self.repo, "commit", "-m", "fixture symlink")
        self.assert_error("path_rejected", lambda: capture(self.repo, self.payload))
        self.assertEqual(list(outside.iterdir()), [])

    def test_untrusted_key_never_becomes_a_path(self):
        result = capture(self.repo, {**self.payload, "source_key": "../../records/escape"})
        self.assertNotIn("..", result["path"])
        self.assertTrue(result["path"].startswith("memory/sources/conversations/src-"))
        self.assertFalse((self.repo / "memory/records").exists())

    def test_dirty_and_head_conflict_are_rejected_without_write(self):
        baseline = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "unfinished.txt").write_text("fixture")
        self.assert_error("dirty_worktree", lambda: capture(self.repo, self.payload))
        (self.repo / "unfinished.txt").unlink()
        git(self.repo, "commit", "--allow-empty", "-m", "concurrent fixture change")
        self.assert_error("head_conflict", lambda: capture(self.repo, self.payload, baseline))
        self.assertFalse((self.repo / "memory").exists())

    def test_missing_receipt_retry_preserves_source_commit(self):
        write_commit = GitAdapter.write_commit

        def fail_receipt(adapter, path, content, head):
            if path.startswith("memory/runs/"):
                raise CaptureError("fixture_failure", "模拟原文提交后回执中断")
            return write_commit(adapter, path, content, head)

        with patch.object(GitAdapter, "write_commit", fail_receipt):
            self.assert_error("fixture_failure", lambda: capture(self.repo, self.payload))
        source_commit = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        result = capture(self.repo, self.payload, source_commit)
        self.assertTrue(result["reused"])
        self.assertEqual(result["commit_sha"], source_commit)
        self.assertEqual(git(self.repo, "rev-list", "--count", "HEAD"), "3")

    def test_remote_failure_then_successful_push_and_readback(self):
        first = capture(self.repo, self.payload, remote="missing")
        self.assertTrue(first["local_saved"])
        self.assertEqual(first["remote_status"], "unconfirmed")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        bare = self.root / "remote.git"
        git(self.root, "init", "--bare", str(bare))
        configure_test_repository(bare)
        git(self.repo, "remote", "add", "fixture", str(bare))
        result = capture(self.repo, self.payload, first["head"], remote="fixture")
        self.assertEqual(result["remote_status"], "confirmed")
        self.assertEqual(result["head"], first["head"])
        self.assertEqual(git(bare, "rev-parse", "refs/heads/main"), result["head"])
        self.assertEqual(git(bare, "show", result["commit_sha"] + ":" + result["path"]),
                         (self.repo / result["path"]).read_text().strip())

    def test_invalid_and_oversize_input(self):
        self.assert_error("invalid_input", lambda: capture(self.repo, {**self.payload, "target_path": "memory/records/x"}))
        self.assert_error("invalid_input", lambda: capture(self.repo, {**self.payload, "acquisition_status": "link_only"}))
        self.assert_error("input_too_large", lambda: capture(self.repo, {**self.payload, "body": "x" * MAX_SOURCE_BYTES}))
        self.assertFalse((self.repo / "memory").exists())

    def test_header_requires_opening_delimiter(self):
        self.assert_error("invalid_saved_file", lambda: parse_markdown(b'{"schema_version":"0.1"}\n---\nbody'))

    def test_url_userinfo_is_rejected_before_persistence(self):
        for url in ("https://user:secret@example.invalid/source", "https://token@example.invalid/source"):
            self.assert_error("invalid_input", lambda: capture(self.repo, {**self.payload, "original_url": url}))
        self.assertFalse((self.repo / "memory").exists())

    def test_original_time_preserves_precision_and_validates_known_time(self):
        for value in ("2026-10-15T17:30:00", "2026-13", "2026-02-30", "yesterday", ""):
            with self.subTest(invalid=value):
                self.assert_error("invalid_input", lambda: source_contract({**self.payload, "original_time": value}))
        for value in (None, "2026-10", "2026-10-15", "2024-02-29", "2026-10-15T17:30:00+08:00"):
            with self.subTest(valid=value):
                data, _, _ = source_contract({**self.payload, "original_time": value})
                self.assertEqual(data["original_time"], value)
        known_time = "2026-10-15T17:30:00+08:00"
        result = capture(self.repo, {**self.payload, "original_time": known_time})
        metadata, _ = parse_markdown((self.repo / result["path"]).read_bytes())
        self.assertEqual(metadata["original_time"], known_time)

    def test_push_uses_saved_commit_even_if_head_advances(self):
        result = capture(self.repo, self.payload)
        git(self.repo, "commit", "--allow-empty", "-m", "unrelated later change")
        bare = self.root / "remote.git"
        git(self.root, "init", "--bare", str(bare))
        configure_test_repository(bare)
        git(self.repo, "remote", "add", "fixture", str(bare))
        GitAdapter(self.repo).sync("fixture", {result["path"]: (self.repo / result["path"]).read_bytes()}, result["head"])
        self.assertEqual(git(bare, "rev-parse", "refs/heads/main"), result["head"])
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD"), result["head"])

    def test_compare_and_swap_rejects_concurrent_commit(self):
        original_run = GitAdapter.run

        def run_with_concurrent_commit(adapter, *args, **kwargs):
            if args[0] == "update-ref":
                git(self.repo, "commit", "--allow-empty", "-m", "concurrent fixture update")
            return original_run(adapter, *args, **kwargs)

        with patch.object(GitAdapter, "run", run_with_concurrent_commit):
            self.assert_error("git_failed", lambda: capture(self.repo, self.payload))
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "concurrent fixture update")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(list(self.repo.glob("memory/sources/**/*.md")), [])

    def test_crlf_body_is_preserved_despite_text_attributes(self):
        (self.repo / ".gitattributes").write_text("* text=auto\n")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-m", "fixture text normalization")
        body = "用户：隔离样例。\r\n助手：保留顺序。\r\n"
        result = capture(self.repo, {**self.payload, "body": body})
        saved = GitAdapter(self.repo).read(result["commit_sha"], result["path"])
        self.assertEqual(parse_markdown(saved)[1], body)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_deleted_source_is_not_silently_recreated(self):
        result = capture(self.repo, self.payload)
        git(self.repo, "rm", result["path"])
        git(self.repo, "commit", "-m", "fixture explicit removal")
        head = git(self.repo, "rev-parse", "HEAD")
        self.assert_error("source_missing", lambda: capture(self.repo, self.payload))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertFalse((self.repo / result["path"]).exists())


if __name__ == "__main__":
    unittest.main()
