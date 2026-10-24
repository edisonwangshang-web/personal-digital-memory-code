"""Controlled current/historical views using artificial Git data and model stubs."""

from datetime import datetime
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.controls import read_controls, record_control, source_policy
from pdm.evidence import ask_current, build_current_evidence
from pdm.pipeline import process_sources, publish


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class CurrentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial baseline")
        self.sources = [capture(self.repo, {"source_key": f"fixture:current:{number}", "source_type": "note",
                        "acquisition_status": "full_text", "body": text}) for number, text in enumerate(
                        ("OLD_ARTIFICIAL_ERROR_VALUE_TWO\n", "用户：旧值错误，人工实际值为一。\n", "UNSELECTED_ARTIFICIAL_SENTINEL\n"))]
        self.paths = [source["path"] for source in self.sources]
        self.calls = 0
        self.payloads = []

    def control(self, **changes):
        return record_control(self.repo, {"request_key": "fixture:current-control", "action": "corrects",
                             "source_paths": [self.paths[0]], "replacement_source_paths": [self.paths[1]], **changes})

    def assert_code(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def model(self, role, payload):
        self.calls += 1
        self.payloads.append((role, payload))
        if role == "extract":
            source = next((source for source in payload["sources"] if source["path"] == self.paths[1]), payload["sources"][0])
            ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                   "locator": "L1-L1", "quote": source["body"].splitlines()[0]}
            output = {"objects": [{"id": "state-artificial", "type": "state", "title": "人工状态",
                       "source_kind": "user_statement", "sources": [ref], "domains": [], "valid_from": None,
                       "valid_to": None, "verification_status": "unverified", "lifecycle_status": "undetermined",
                       "body": "已纠正人工知识正文。" if source["path"] == self.paths[1] else "OLD_REVIEWED_ARTIFICIAL_ERROR"}],
                       "wiki": {"id": "topic-artificial", "title": "人工主题", "sources": [ref],
                                "body": "已纠正人工主题。" if source["path"] == self.paths[1] else "OLD_REVIEWED_ARTIFICIAL_TOPIC"}}
        elif role == "review":
            output = {"verdict": "passed", "reasons": ["隔离样例。"]}
        else:
            entries = payload["evidence"]["sources"]
            output = {"answer": "只根据所提供人工资料回答。", "citations": [
                      {"source_id": entry["source"]["source_id"], "source_version": entry["source"]["source_version"],
                       "locator": "L1-L1"} for entry in entries if entry["lines"]], "limitations": ["受限资料。"]}
        return {"output": output, "invocation_id": "model-" + f"{self.calls:032x}",
                "model": "artificial", "elapsed_seconds": 0.01, "usage": None}

    def test_current_excludes_error_before_body_open_and_preserves_unprocessed_replacement(self):
        self.control()
        from pdm import evidence
        original = evidence.load_source
        def selected(repo, path, *args, **kwargs):
            if path == self.paths[0]:
                self.fail("Current view opened corrected raw body")
            return original(repo, path, *args, **kwargs)
        head = git(self.repo, "rev-parse", "HEAD")
        before = {str(path.relative_to(self.repo)): path.read_bytes() for path in (self.repo / "memory").rglob("*.md")}
        with patch("pdm.evidence.load_source", side_effect=selected):
            result = ask_current(self.repo, self.paths[:2], "现在人工值是什么？", model_call=self.model)
        selected_payload = self.payloads[-1][1]["evidence"]
        self.assertNotIn("OLD_ARTIFICIAL_ERROR_VALUE_TWO", json.dumps(selected_payload))
        self.assertNotIn("UNSELECTED_ARTIFICIAL_SENTINEL", json.dumps(selected_payload))
        self.assertEqual(result["coverage"]["read_source_paths"], [self.paths[1]])
        self.assertEqual(selected_payload["sources"][0]["knowledge_status"], "unprocessed_or_unverified")
        self.assertEqual(result["coverage"]["excluded_sources"][0]["reason"], "corrects")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(before, {str(path.relative_to(self.repo)): path.read_bytes() for path in (self.repo / "memory").rglob("*.md")})

    def test_replacement_outside_scope_is_a_gap_and_never_implicitly_read(self):
        self.control()
        with patch("pdm.evidence.load_source", side_effect=AssertionError("No source body is authorized here")):
            selected = build_current_evidence(self.repo, [self.paths[0]])
        self.assertEqual(selected["sources"], [])
        self.assertEqual(selected["coverage"]["missing_update_paths"], [self.paths[1]])
        self.assertFalse(selected["coverage"]["current_complete"])

    def test_history_can_read_corrected_sources_but_deletion_always_blocks_history(self):
        self.control()
        old = build_current_evidence(self.repo, self.paths[:2], history=True)
        self.assertIn("OLD_ARTIFICIAL_ERROR_VALUE_TWO", json.dumps(old))
        self.assertFalse(old["coverage"]["current_complete"])
        record_control(self.repo, {"request_key": "fixture:delete-after-correction", "action": "delete", "source_paths": [self.paths[0]]})
        current = build_current_evidence(self.repo, self.paths[:2], history=True)
        self.assertNotIn("OLD_ARTIFICIAL_ERROR_VALUE_TWO", json.dumps(current))
        self.assertEqual(current["coverage"]["excluded_sources"][0]["reason"], "delete")

    def test_citation_to_excluded_error_is_rejected(self):
        self.control()
        def hallucinate(role, payload):
            output = self.model(role, payload)
            output["output"]["citations"] = [{"source_id": self.sources[0]["source_id"],
                                              "source_version": self.sources[0]["source_version"], "locator": "L1-L1"}]
            return output
        self.assert_code("invalid_citation", lambda: ask_current(self.repo, self.paths[:2], "人工？", model_call=hallucinate))

    def test_budget_reports_entire_unread_sources_without_body_truncation(self):
        huge = capture(self.repo, {"source_key": "fixture:huge-current", "source_type": "note",
                       "acquisition_status": "full_text", "body": "HUGE_ARTIFICIAL_SENTINEL" * 10000})
        selected = build_current_evidence(self.repo, [self.paths[1], huge["path"]], max_bytes=7500)
        self.assertIn(huge["path"], selected["coverage"]["unread_source_paths"])
        self.assertTrue(selected["coverage"]["budget_exhausted"])
        self.assertFalse(selected["coverage"]["current_complete"])
        self.assertNotIn("HUGE_ARTIFICIAL_SENTINEL", json.dumps(selected))
        self.assertLessEqual(len(json.dumps(selected, ensure_ascii=False).encode()), 7500)

    def test_same_source_new_version_is_reported_using_receipts_without_reading_its_body(self):
        new = capture(self.repo, {"source_key": "fixture:current:0", "source_type": "note",
                                 "acquisition_status": "full_text", "body": "UNSELECTED_SAME_SOURCE_NEW_VERSION\n"})
        from pdm import evidence
        original = evidence.load_source
        def selected_only(repo, path, *args, **kwargs):
            if path == new["path"]:
                self.fail("An unselected newer version body was opened")
            return original(repo, path, *args, **kwargs)
        with patch("pdm.evidence.load_source", side_effect=selected_only):
            selected = build_current_evidence(self.repo, [self.paths[0]])
        self.assertEqual(selected["coverage"]["missing_update_paths"], [new["path"]])
        self.assertFalse(selected["coverage"]["current_complete"])
        self.assertNotIn("UNSELECTED_SAME_SOURCE_NEW_VERSION", json.dumps(selected))
        latest = build_current_evidence(self.repo, [new["path"]])
        self.assertEqual(latest["coverage"]["missing_update_paths"], [])
        self.assertTrue(latest["coverage"]["current_complete"])

    def test_malformed_unselected_new_receipt_fails_closed_without_loading_its_source(self):
        new = capture(self.repo, {"source_key": "fixture:current:0", "source_type": "note",
                                 "acquisition_status": "full_text", "body": "UNSELECTED_NEW_BODY\n"})
        receipt = self.repo / new["receipt_path"]
        receipt.write_bytes(receipt.read_bytes() + b"unexpected receipt body")
        git(self.repo, "add", str(receipt))
        git(self.repo, "commit", "-m", "artificial corrupt receipt")
        with patch("pdm.evidence.load_source", side_effect=AssertionError("Must fail before any source body")):
            self.assert_code("receipt_mismatch", lambda: build_current_evidence(self.repo, [self.paths[0]]))

    def test_old_publication_is_withheld_and_new_reviewed_update_is_current(self):
        old = process_sources(self.repo, [self.paths[0]], model_call=self.model)
        old_pub = publish(self.repo, old["run_id"])
        self.control(object_paths=sorted(old_pub["files"]))
        before = build_current_evidence(self.repo, self.paths[:2], [old["run_id"]])
        self.assertEqual(before["publications"], [])
        self.assertNotIn("OLD_REVIEWED_ARTIFICIAL_ERROR", json.dumps(before))
        updated = process_sources(self.repo, self.paths[:2], related_runs=[old["run_id"]], model_call=self.model)
        self.assertEqual(updated["status"], "passed")
        publish(self.repo, updated["run_id"])
        selected = build_current_evidence(self.repo, self.paths[:2], [updated["run_id"]])
        self.assertEqual(selected["knowledge_status"], "published")
        self.assertEqual(len(selected["publications"][0]["documents"]), 2)
        serialized = json.dumps(selected, ensure_ascii=False)
        self.assertIn("已纠正人工知识正文", serialized)
        self.assertNotIn("OLD_ARTIFICIAL_ERROR_VALUE_TWO", serialized)
        self.assertNotIn("OLD_REVIEWED_ARTIFICIAL_ERROR", serialized)

    def test_current_view_can_select_old_and_updated_runs_without_reintroducing_old_formal_body(self):
        old = process_sources(self.repo, [self.paths[0]], model_call=self.model)
        publish(self.repo, old["run_id"])
        updated = process_sources(self.repo, self.paths[:2], related_runs=[old["run_id"]], model_call=self.model)
        publish(self.repo, updated["run_id"])
        selected = build_current_evidence(self.repo, self.paths[:2], [old["run_id"], updated["run_id"]])
        self.assertEqual(len(selected["publications"]), 1)
        self.assertEqual(selected["publications"][0]["run_id"], updated["run_id"])
        self.assertIn({"run_id": old["run_id"], "reason": "historical_publication"}, selected["coverage"]["unread_runs"])
        self.assertNotIn("OLD_REVIEWED_ARTIFICIAL_ERROR", json.dumps(selected))

    def test_reused_formal_objects_keep_verified_original_review_origin(self):
        old = process_sources(self.repo, [self.paths[0]], model_call=self.model)
        publish(self.repo, old["run_id"])
        reused = process_sources(self.repo, [self.paths[0]], related_runs=[old["run_id"]], model_call=self.model)
        publication = publish(self.repo, reused["run_id"])
        self.assertTrue(publication["object_origins"])
        selected = build_current_evidence(self.repo, [self.paths[0]], [reused["run_id"]])
        self.assertEqual(len(selected["publications"][0]["documents"]), 2)
        self.assertEqual(selected["publications"][0]["object_origins"], publication["object_origins"])

    def test_future_supersession_keeps_old_source_and_unknown_time_is_incomplete(self):
        self.control(action="supersedes", effective_from="9999-12")
        selected = build_current_evidence(self.repo, self.paths[:2])
        self.assertEqual(len(selected["sources"]), 2)
        self.assertTrue(selected["coverage"]["not_yet_effective_source_paths"])
        self.assertTrue(any("未来上下文" in text for text in selected["limitations"]))
        snapshot = read_controls(self.repo)
        at = datetime(2026, 10, 15, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertFalse(source_policy(snapshot, self.paths[0], as_of=at)["excluded"])
        self.assertTrue(source_policy(snapshot, self.paths[0], as_of=at)["not_yet_effective"])
        self.control(request_key="fixture:unknown-supersession", action="supersedes", effective_from=None)
        selected = build_current_evidence(self.repo, self.paths[:2])
        self.assertFalse(selected["coverage"]["current_complete"])
        self.assertTrue(selected["coverage"]["temporal_undetermined_source_paths"])

    def test_future_replacement_only_reports_unselected_current_predecessor_without_opening_it(self):
        self.control(action="supersedes", effective_from="9999-12")
        from pdm import evidence
        original = evidence.load_source
        def selected_only(repo, path, *args, **kwargs):
            if path == self.paths[0]:
                self.fail("Unselected current predecessor body was opened")
            return original(repo, path, *args, **kwargs)
        with patch("pdm.evidence.load_source", side_effect=selected_only):
            selected = build_current_evidence(self.repo, [self.paths[1]])
        self.assertEqual(selected["coverage"]["read_source_paths"], [self.paths[1]])
        self.assertEqual(selected["coverage"]["missing_update_paths"], [self.paths[0]])
        self.assertFalse(selected["coverage"]["current_complete"])
        self.assertNotIn("OLD_ARTIFICIAL_ERROR_VALUE_TWO", json.dumps(selected))
        complete = build_current_evidence(self.repo, self.paths[:2])
        self.assertEqual(complete["coverage"]["missing_update_paths"], [])
        self.assertTrue(complete["coverage"]["current_complete"])

    def test_supersession_date_precision_uses_shanghai_day_and_corrections_are_immediate(self):
        self.control(action="supersedes", effective_from="2026-10-16")
        snapshot = read_controls(self.repo)
        early = datetime(2026, 10, 15, 15, 30, tzinfo=ZoneInfo("UTC"))  # Shanghai 23:30, Oct 15.
        later = datetime(2026, 10, 15, 16, 30, tzinfo=ZoneInfo("UTC"))  # Shanghai 00:30, Oct 16.
        self.assertFalse(source_policy(snapshot, self.paths[0], as_of=early)["excluded"])
        self.assertTrue(source_policy(snapshot, self.paths[0], as_of=early)["not_yet_effective"])
        self.assertTrue(source_policy(snapshot, self.paths[0], as_of=later)["excluded"])
        self.control(request_key="fixture:corrects-future-validity", action="corrects", effective_from="9999-12")
        self.assertTrue(source_policy(read_controls(self.repo), self.paths[0], as_of=early)["excluded"])

    def test_model_side_effects_invalidate_fixed_current_evidence(self):
        def mutate(role, payload):
            result = self.model(role, payload)
            capture(self.repo, {"source_key": "fixture:concurrent", "source_type": "note",
                               "acquisition_status": "full_text", "body": "并发人工输入。"})
            return result
        self.assert_code("evidence_changed", lambda: ask_current(self.repo, [self.paths[1]], "人工？", model_call=mutate))


if __name__ == "__main__":
    unittest.main()
