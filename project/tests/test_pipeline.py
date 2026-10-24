"""Artificial sources only: exercise publication gates and recovery in temporary Git."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError, markdown, parse_markdown
from pdm.model_calls import ModelCallError
from pdm.pipeline import load_source, process, publish, verify_publication
from pdm.transaction import PipelineGit


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Isolated Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "isolated baseline")
        self.saved = capture(self.repo, {"source_key": "fixture:one", "source_type": "conversation",
            "platform": "isolated", "original_url": None, "acquisition_status": "full_text",
            "original_time": None, "body": "用户：我想整理隔离样例。\n尚未完成。\n"})
        self.roles = []
        self.contexts = []
        self.verdict = "passed"
        self.number = 0

    def model(self, role, payload, timeout=120):
        self.roles.append(role)
        self.contexts.append(payload)
        self.number += 1
        source = payload["sources"][0]
        ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
               "locator": "L1-L2", "quote": source["body"].splitlines()[0]}
        if role == "extract":
            output = {"objects": [{"id": "goal-isolated", "type": "goal", "title": "整理隔离样例",
                "source_kind": "user_statement", "sources": [ref], "domains": [], "valid_from": None, "valid_to": None,
                "verification_status": "unverified", "lifecycle_status": "undetermined", "body": "用户希望整理隔离样例，尚未完成。"}],
                "wiki": {"id": "topic-isolated", "title": "隔离主题", "body": "目标尚未完成。", "sources": [ref]}}
        else:
            output = {"verdict": self.verdict, "reasons": ["忠实保留目标及尚未完成状态"] if self.verdict == "passed" else ["需要补充或判断隔离样例"]}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def assert_error(self, code, action):
        with self.assertRaises(CaptureError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def run_process(self):
        return process(self.repo, self.saved["path"], model_call=self.model)

    def commit_change(self, path, data):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        git(self.repo, "add", path)
        git(self.repo, "commit", "-m", "isolated intervening change")

    def test_fixed_source_independent_review_and_atomic_idempotent_publish(self):
        result = self.run_process()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(self.contexts[1]["sources"][0]["body"], "用户：我想整理隔离样例。\n尚未完成。\n")
        self.assertIn("candidate", self.contexts[1])
        self.assertFalse((self.repo / "memory/records").exists())
        published = publish(self.repo, result["run_id"], result["head"])
        self.assertEqual(published["status"], "published_local")
        changed = git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", published["commit_sha"]).splitlines()
        self.assertEqual(set(changed), {published["manifest_path"], "memory/records/goal-isolated.md", "memory/topics/topic-isolated.md"})
        metadata, _ = parse_markdown((self.repo / "memory/records/goal-isolated.md").read_bytes())
        self.assertEqual(metadata["schema_version"], "0.2")
        self.assertEqual(metadata["review_status"], "passed")
        self.assertIsNone(metadata["valid_from"])
        self.assertEqual(publish(self.repo, result["run_id"])["head"], published["head"])
        self.assertEqual(verify_publication(self.repo, result["run_id"])["commit_sha"], published["commit_sha"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_review_nonpass_never_publishes_and_confirmation_stays_open(self):
        for verdict in ("needs_revision", "needs_confirmation"):
            with self.subTest(verdict=verdict):
                self.verdict = verdict
                result = self.run_process()
                self.assertEqual(result["status"], verdict)
                self.assert_error("review_not_passed", lambda: publish(self.repo, result["run_id"]))
                if verdict == "needs_confirmation":
                    _, body = parse_markdown((self.repo / result["pending_path"]).read_bytes())
                    self.assertIsNone(json.loads(body)["user_reply"])
                    self.assertEqual(json.loads(body)["status"], "open")
        self.assertFalse((self.repo / "memory/records").exists())

    def test_same_invocation_is_rejected_and_failure_preserves_stage(self):
        def same_call(role, payload):
            response = self.model(role, payload)
            response["invocation_id"] = "model-" + "1" * 32
            return response
        result = process(self.repo, self.saved["path"], model_call=same_call)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "review_not_independent")
        self.assertTrue((self.repo / result["stage_path"]).is_file())
        self.assertTrue((self.repo / result["failure_path"]).is_file())
        self.assertFalse((self.repo / "memory/records").exists())

    def test_extraction_timeout_preserves_source_and_safe_retry(self):
        def failed(role, payload):
            raise ModelCallError("model_timeout", "model-" + "0" * 32)
        result = process(self.repo, self.saved["path"], model_call=failed)
        self.assertEqual(result["error"], "model_timeout")
        _, failure = parse_markdown((self.repo / result["failure_path"]).read_bytes())
        failure = json.loads(failure)
        self.assertEqual(failure["call"]["invocation_id"], "model-" + "0" * 32)
        self.assertEqual(failure["attempted_calls"], 1)
        self.assertEqual(failure["call"]["elapsed_seconds"], 0.0)
        self.assertTrue((self.repo / self.saved["path"]).is_file())
        self.assertEqual(self.run_process()["status"], "passed")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_invalid_quote_and_path_injection_never_reach_review(self):
        def bad_quote(role, payload):
            response = self.model(role, payload)
            response["output"]["objects"][0]["sources"][0]["quote"] = "不存在的原话"
            return response
        result = process(self.repo, self.saved["path"], model_call=bad_quote)
        self.assertEqual(result["error"], "invalid_quote")
        self.assertEqual(self.roles, ["extract"])
        self.roles.clear()
        def bad_id(role, payload):
            response = self.model(role, payload)
            response["output"]["objects"][0]["id"] = "../../escape"
            return response
        result = process(self.repo, self.saved["path"], model_call=bad_id)
        self.assertEqual(result["error"], "invalid_object_id")
        self.assertFalse((self.repo / "escape.md").exists())

    def test_candidate_tampering_and_unrelated_head_change_invalidate_release(self):
        result = self.run_process()
        git(self.repo, "commit", "--allow-empty", "-m", "isolated external commit")
        self.assert_error("publication_head_changed", lambda: publish(self.repo, result["run_id"]))
        result = self.run_process()
        meta, body = parse_markdown((self.repo / result["stage_path"]).read_bytes())
        value = json.loads(body)
        value["objects"][0]["body"] = "篡改隔离内容"
        self.commit_change(result["stage_path"], markdown(meta, json.dumps(value, ensure_ascii=False)))
        self.assert_error("candidate_mismatch", lambda: publish(self.repo, result["run_id"]))
        self.assertFalse((self.repo / "memory/records").exists())

    def test_rules_or_program_version_change_invalidates_release(self):
        result = self.run_process()
        with patch("pdm.pipeline.execution_versions", return_value={"program_sha256": "changed"}):
            self.assert_error("processing_context_changed", lambda: publish(self.repo, result["run_id"]))
        self.assertFalse((self.repo / "memory/records").exists())

    def test_target_collision_and_deleted_target_are_never_overwritten(self):
        self.commit_change("memory/records/goal-isolated.md", markdown({"schema_version": "0.2"}, "人工隔离资料"))
        self.assert_error("target_exists_or_deleted", self.run_process)
        git(self.repo, "rm", "memory/records/goal-isolated.md")
        git(self.repo, "commit", "-m", "isolated explicit removal")
        self.assert_error("target_exists_or_deleted", self.run_process)
        self.assertFalse((self.repo / "memory/records/goal-isolated.md").exists())

    def test_capture_receipt_mismatch_and_deleted_source_fail_closed(self):
        original = (self.repo / self.saved["receipt_path"]).read_bytes()
        meta, _ = parse_markdown(original)
        meta["commit_sha"] = self.saved["head"]
        self.commit_change(self.saved["receipt_path"], markdown(meta))
        self.assert_error("source_mismatch", lambda: load_source(self.repo, self.saved["path"]))
        git(self.repo, "rm", self.saved["path"])
        git(self.repo, "commit", "-m", "isolated source removal")
        self.assert_error("source_deleted", lambda: load_source(self.repo, self.saved["path"], self.saved["head"]))

    def test_existing_controls_block_processing_and_reading(self):
        self.commit_change("memory/runs/controls/delete.md", markdown({"schema_version": "0.2"}, "isolated control"))
        self.assert_error("unsupported_controls", self.run_process)
        self.assert_error("unsupported_controls", lambda: load_source(self.repo, self.saved["path"]))
        self.assertEqual(self.roles, [])

    def test_missing_receipt_is_repaired_without_republication(self):
        result = self.run_process()
        original = PipelineGit.write_files
        def stop_receipt(adapter, files, head, message):
            if any(path.endswith("/receipt.md") for path in files):
                raise CaptureError("isolated_failure", "isolated receipt interruption")
            return original(adapter, files, head, message)
        with patch.object(PipelineGit, "write_files", stop_receipt):
            self.assert_error("isolated_failure", lambda: publish(self.repo, result["run_id"]))
        first = verify_publication(self.repo, result["run_id"])
        self.assertFalse(first["receipt_saved"])
        recovered = publish(self.repo, result["run_id"])
        self.assertEqual(recovered["commit_sha"], first["commit_sha"])
        self.assertTrue(recovered["receipt_saved"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        git(self.repo, "rm", recovered["receipt_path"])
        git(self.repo, "commit", "-m", "isolated explicit receipt removal")
        self.assert_error("publication_receipt_deleted", lambda: publish(self.repo, result["run_id"]))

    def test_postpublication_modification_cannot_keep_reviewed_status(self):
        result = self.run_process()
        publish(self.repo, result["run_id"])
        path = "memory/topics/topic-isolated.md"
        self.commit_change(path, (self.repo / path).read_bytes() + b"unreviewed change")
        self.assert_error("publication_mismatch", lambda: verify_publication(self.repo, result["run_id"]))

    def test_month_precision_and_invalid_time_are_checked(self):
        def month(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                response["output"]["objects"][0]["valid_from"] = "2026-10"
            return response
        result = process(self.repo, self.saved["path"], model_call=month)
        self.assertEqual(result["status"], "passed")
        def bad_time(role, payload):
            response = self.model(role, payload)
            response["output"]["objects"][0]["valid_from"] = "2026-02-30"
            return response
        result = process(self.repo, self.saved["path"], model_call=bad_time)
        self.assertEqual(result["error"], "invalid_model_output")

    def test_empty_review_reasons_and_nonfinite_metrics_do_not_publish(self):
        def empty_reasons(role, payload):
            response = self.model(role, payload)
            if role == "review":
                response["output"]["reasons"] = []
            return response
        result = process(self.repo, self.saved["path"], model_call=empty_reasons)
        self.assertEqual(result["error"], "invalid_model_output")
        def invalid_metrics(role, payload):
            response = self.model(role, payload)
            response["elapsed_seconds"] = float("nan")
            return response
        result = process(self.repo, self.saved["path"], model_call=invalid_metrics)
        self.assertEqual(result["error"], "invalid_model_invocation")
        _, failure = parse_markdown((self.repo / result["failure_path"]).read_bytes())
        self.assertIsNone(json.loads(failure)["call"]["elapsed_seconds"])
        self.assertFalse((self.repo / "memory/records").exists())

    def test_persisted_duplicate_keys_are_rejected(self):
        result = self.run_process()
        original = (self.repo / result["stage_path"]).read_bytes()
        duplicate_header = original.replace(b'{"kind":"candidate",', b'{"kind":"candidate","kind":"candidate",', 1)
        self.commit_change(result["stage_path"], duplicate_header)
        self.assert_error("unknown_memory_schema", lambda: publish(self.repo, result["run_id"]))
        duplicate_body = original.replace(b'{"base_head":', b'{"run_id":"duplicate-key","base_head":', 1)
        self.commit_change(result["stage_path"], duplicate_body)
        self.assert_error("invalid_pipeline_data", lambda: publish(self.repo, result["run_id"]))

    def test_crlf_quotes_preserve_source_bytes(self):
        body = "用户：保留换行。\r\n第二行隔离样例。\r\n"
        saved = capture(self.repo, {"source_key": "fixture:crlf", "source_type": "conversation",
            "platform": "isolated", "acquisition_status": "full_text", "body": body})
        def exact(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                response["output"]["objects"][0]["sources"][0]["quote"] = body
            return response
        result = process(self.repo, saved["path"], model_call=exact)
        self.assertEqual(result["status"], "passed")
        def normalized(role, payload):
            response = exact(role, payload)
            response["output"]["objects"][0]["sources"][0]["quote"] = body.replace("\r\n", "\n")
            return response
        result = process(self.repo, saved["path"], model_call=normalized)
        self.assertEqual(result["error"], "invalid_quote")


if __name__ == "__main__":
    unittest.main()
