"""Control invariants over disposable Git repositories and artificial sources."""

from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository

from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.controls import object_policy, plan_control, read_controls, record_control, source_id_policy, source_policy
from pdm.pipeline import load_source, process_sources, publish


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial baseline")
        self.empty_digest = read_controls(self.repo)["digest"]
        self.sources = [capture(self.repo, {"source_key": f"fixture:controls:{number}", "source_type": "note",
                        "acquisition_status": "full_text", "body": text}) for number, text in enumerate(
                        ("旧人工数值：2。\n", "用户纠正：旧数值错误，实际是1。\n", "无关人工内容。\n"))]
        self.paths = [source["path"] for source in self.sources]
        self.calls = 0

    def correction(self, **changes):
        return {"request_key": "fixture:correction", "action": "corrects", "source_paths": [self.paths[0]],
                "replacement_source_paths": [self.paths[1]], "basis_source_path": self.paths[1], **changes}

    def assert_code(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def model(self, role, payload):
        self.calls += 1
        if role == "extract":
            refs = [{"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                     "locator": "L1-L1", "quote": source["body"].splitlines()[0]} for source in payload["sources"] if source["body"]]
            output = {"objects": [{"id": "state-artificial", "type": "state", "title": "人工状态",
                       "source_kind": "user_statement", "sources": refs, "domains": [], "valid_from": None,
                       "valid_to": None, "verification_status": "unverified", "lifecycle_status": "undetermined",
                       "body": "人工数值。"}], "wiki": {"id": "topic-artificial", "title": "人工主题",
                       "sources": refs, "body": "人工主题正文。"}}
        else:
            output = {"verdict": "passed", "reasons": ["隔离样例。"]}
        return {"output": output, "invocation_id": "model-" + f"{self.calls:032x}",
                "model": "artificial", "elapsed_seconds": 0.01, "usage": None}

    def publish_old(self, paths=None):
        result = process_sources(self.repo, paths or [self.paths[0]], model_call=self.model)
        self.assertEqual(result["status"], "passed")
        return publish(self.repo, result["run_id"])

    def test_digest_excludes_head_and_historical_snapshot_cannot_undo_current_guard(self):
        before = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(read_controls(self.repo)["digest"], self.empty_digest)
        record_control(self.repo, self.correction())
        current = read_controls(self.repo)
        self.assertNotEqual(current["digest"], self.empty_digest)
        self.assertEqual(read_controls(self.repo, at_commit=before)["digest"], self.empty_digest)
        self.assert_code("head_conflict", lambda: read_controls(self.repo, head=before))
        self.assertTrue(source_policy(current, self.paths[0])["excluded"])
        self.assertFalse(source_policy(current, self.paths[0], purpose="history")["excluded"])
        self.assertFalse(source_policy(current, self.paths[2])["excluded"])

    def test_current_body_is_blocked_before_open_while_explicit_history_remains_available(self):
        record_control(self.repo, self.correction())
        from pdm import pipeline
        original = pipeline._committed
        def guarded(adapter, path, commit=None):
            if path == self.paths[0]:
                self.fail("Corrected body opened in current mode")
            return original(adapter, path, commit)
        with patch("pdm.pipeline._committed", side_effect=guarded):
            self.assert_code("source_excluded", lambda: load_source(self.repo, self.paths[0]))
        self.assertIn("旧人工数值", load_source(self.repo, self.paths[0], purpose="history")["body"])
        self.assertIn("无关", load_source(self.repo, self.paths[2])["body"])

    def test_request_key_retries_are_idempotent_and_conflicting_key_rejected(self):
        first = record_control(self.repo, self.correction())
        head = git(self.repo, "rev-parse", "HEAD")
        second = record_control(self.repo, self.correction())
        self.assertEqual(first["control_id"], second["control_id"])
        self.assertTrue(second["reused"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assert_code("control_key_conflict", lambda: record_control(self.repo, self.correction(action="supersedes")))

    def test_control_count_limit_preserves_reads_and_idempotent_retries(self):
        with patch("pdm.controls.MAX_CONTROLS", 1):
            first = record_control(self.repo, self.correction())
            head = git(self.repo, "rev-parse", "HEAD")
            controls = read_controls(self.repo)
            before = {str(path.relative_to(self.repo)): path.read_bytes()
                      for path in (self.repo / "memory").rglob("*") if path.is_file()}
            repeated = record_control(self.repo, self.correction())
            self.assertEqual(repeated["control_id"], first["control_id"])
            self.assertTrue(repeated["reused"])
            self.assert_code("control_key_conflict", lambda: record_control(
                self.repo, self.correction(action="supersedes")))
            self.assert_code("control_limit", lambda: record_control(self.repo, {
                "request_key": "fixture:over-capacity", "action": "delete", "source_paths": [self.paths[2]]}))
            self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
            self.assertEqual(read_controls(self.repo), controls)
            self.assertIn("无关人工内容", load_source(self.repo, self.paths[2])["body"])
            after = {str(path.relative_to(self.repo)): path.read_bytes()
                     for path in (self.repo / "memory").rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_control_count_limit_rejects_deletion_without_removing_current_views(self):
        published = self.publish_old()
        with patch("pdm.controls.MAX_CONTROLS", 1):
            record_control(self.repo, {"request_key": "fixture:first-control", "action": "delete",
                                      "source_paths": [self.paths[2]]})
            head = git(self.repo, "rev-parse", "HEAD")
            objects = {path: (self.repo / path).read_bytes() for path in published["files"]}
            self.assert_code("control_limit", lambda: record_control(self.repo, {
                "request_key": "fixture:over-capacity-deletion", "action": "delete",
                "source_paths": [self.paths[0]], "object_paths": sorted(objects)}))
            self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
            self.assertEqual({path: (self.repo / path).read_bytes() for path in objects}, objects)
            self.assertEqual(len(read_controls(self.repo)["events"]), 1)
            self.assertIn("旧人工数值", load_source(self.repo, self.paths[0])["body"])
            self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_replacement_cycle_is_rejected_without_new_commit(self):
        record_control(self.repo, self.correction())
        head = git(self.repo, "rev-parse", "HEAD")
        self.assert_code("control_cycle", lambda: record_control(self.repo, self.correction(
            request_key="fixture:cycle", source_paths=[self.paths[1]], replacement_source_paths=[self.paths[0]])))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_delete_suppresses_all_source_versions_including_history_without_removing_raw_history(self):
        raw = (self.repo / self.paths[0]).read_bytes()
        result = record_control(self.repo, {"request_key": "fixture:delete", "action": "delete", "source_paths": [self.paths[0]]})
        self.assertEqual(result["plan"]["source_suppression"], "all_versions")
        current = read_controls(self.repo)
        self.assertTrue(source_id_policy(current, self.sources[0]["source_id"])["excluded"])
        alternate = self.paths[0].replace(self.sources[0]["source_version"], "sha256-" + "f" * 64)
        self.assertTrue(source_policy(current, alternate, purpose="history")["excluded"])
        for purpose in ("current", "history"):
            self.assert_code("source_excluded", lambda: load_source(self.repo, self.paths[0], purpose=purpose))
        self.assertEqual((self.repo / self.paths[0]).read_bytes(), raw)
        self.assertIn("旧人工数值", git(self.repo, "show", "HEAD:" + self.paths[0]))

    def test_delete_requires_explicit_source_and_all_dependent_current_views(self):
        published = self.publish_old()
        objects = sorted(published["files"])
        payload = {"request_key": "fixture:delete-objects", "action": "delete", "object_paths": [objects[0]]}
        plan = plan_control(self.repo, payload)
        self.assertEqual(plan["missing_source_paths"], [self.paths[0]])
        self.assertEqual(plan["missing_object_paths"], [objects[1]])
        head = git(self.repo, "rev-parse", "HEAD")
        error = self.assert_code("control_dependencies", lambda: record_control(self.repo, payload))
        self.assertEqual(error.details["missing_source_paths"], [self.paths[0]])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        payload["source_paths"] = [self.paths[0]]
        self.assert_code("control_dependencies", lambda: record_control(self.repo, payload))
        payload["object_paths"] = objects
        result = record_control(self.repo, payload)
        self.assertFalse(result["reused"])
        self.assertTrue(all(not (self.repo / path).exists() for path in objects))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        for path in objects:
            self.assertIn("人工", git(self.repo, "show", head + ":" + path))
            self.assertTrue(object_policy(read_controls(self.repo), path, purpose="history")["excluded"])

    def test_corrected_object_only_blocks_its_bound_old_content_version(self):
        published = self.publish_old()
        path = sorted(published["files"])[0]
        result = record_control(self.repo, self.correction(object_paths=[path]))
        snapshot = read_controls(self.repo)
        old = snapshot["events"][0]["object_versions"][path]
        self.assertTrue(object_policy(snapshot, path, content_version=old)["excluded"])
        self.assertFalse(object_policy(snapshot, path, content_version="sha256-" + "f" * 64)["excluded"])
        self.assertFalse(object_policy(snapshot, path, content_version=old, purpose="history")["excluded"])
        self.assertNotIn("旧人工数值", (self.repo / result["path"]).read_text())

    def test_unknown_uncommitted_or_modified_control_is_not_ignored(self):
        target = self.repo / "memory/runs/controls/unknown.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("PRIVATE_ARTIFICIAL_CONTROL_SENTINEL")
        self.assert_code("unsupported_controls", lambda: read_controls(self.repo))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "artificial unknown control")
        self.assert_code("unsupported_controls", lambda: read_controls(self.repo))

    def test_committed_control_modification_or_removal_fails_closed(self):
        saved = record_control(self.repo, self.correction())
        path = self.repo / saved["path"]
        path.write_bytes(path.read_bytes() + b"\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "artificial changed control")
        self.assert_code("control_modified", lambda: read_controls(self.repo))
        path.unlink()
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "artificial removed control")
        self.assert_code("control_removed", lambda: read_controls(self.repo))


if __name__ == "__main__":
    unittest.main()
