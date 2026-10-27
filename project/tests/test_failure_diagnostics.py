"""Synthetic extraction diagnostics stay in the selected data repository."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine_fixtures import isolated_engine
from git_fixtures import configure_test_repository
from quality_fixtures import add_quality, passed_review
from test_pipeline import git
from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.cli import main
from pdm.contracts import CaptureError, canonical
from pdm.model_calls import ModelCallError, request_binding
from pdm.pipeline import (_extraction_diagnostic, _hash, _load, _stage_path,
                          process_sources, publish, verify_publication)
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class ExtractionFailureDiagnosticTests(unittest.TestCase):
    MARKER = "ARTIFICIAL_PRIVATE_DIAGNOSTIC_ONLY_7341"
    DEBUG = "ARTIFICIAL_HOST_DEBUG_MUST_NOT_BE_RETAINED"
    SOURCE = "导出说明记载，当时尚未提交人工材料。"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-diagnostic-data-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = self.directory / "data"
        self.repo.mkdir(mode=0o700)
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Diagnostic Fixture")
        git(self.repo, "config", "user.email", "diagnostic@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial diagnostic baseline")
        self.saved = capture(self.repo, {
            "source_key": "artificial:diagnostic-note", "source_type": "note",
            "acquisition_status": "full_text", "original_time": "2024-05-09",
            "body": self.SOURCE + "\n",
        })
        self.code = isolated_engine(self)
        self.engine_head = git(self.code, "rev-parse", "HEAD")
        self.bundle = resolve_skill_refs("knowledge-extract@11", "knowledge-review@9")
        self.ledger = BudgetLedger.create(self.directory / "audit.json", None, 0,
            "artificial-diagnostic-authority", mode="audit")
        self.adapter = BudgetedModelCall(self.ledger, "diagnostic-operation", self.transport)
        self.mode = "missing"
        self.calls = []
        self.returned = []

    def transport(self, role, payload, timeout=120):
        self.calls.append((role, deepcopy(payload)))
        invocation = "model-" + f"{len(self.calls):032x}"
        if self.mode == "timeout":
            raise ModelCallError("timeout", invocation, 1.0)
        if role == "extract":
            source = payload["sources"][0]
            reference = {"source_id": source["metadata"]["source_id"],
                "source_version": source["metadata"]["source_version"],
                "locator": "L1-L1", "quote": self.SOURCE}
            statement = "当前已经提交人工材料。" if self.mode == "mismatch" else self.SOURCE
            body = "虚构诊断样例。\n" + statement + "\n" + self.MARKER
            output = {"objects": [{"id": "diagnostic-record", "type": "knowledge",
                "title": "人工时间说明", "source_kind": "external_source", "sources": [reference],
                "domains": ["learning"], "valid_from": None, "valid_to": None,
                "verification_status": "supported", "lifecycle_status": "historical", "body": body}],
                "wiki": {"id": "diagnostic-topic", "title": "人工资料概览", "body": statement,
                         "sources": [reference]}, "reuse_refs": []}
            if self.mode == "capacity":
                # Twenty otherwise shaped records plus their exact quality
                # excerpts form a returned envelope above the old file cap.
                # This remains the same missing-time validation failure, not
                # a malformed host response or a new model invocation.
                output["objects"] = [{**deepcopy(output["objects"][0]),
                    "id": f"diagnostic-record-{index}", "body": body + "x" * 28000}
                    for index in range(20)]
            output = add_quality(output, payload["quality_context"])
            if self.mode in {"mismatch", "valid"}:
                output["quality"]["temporal_claims"] = [{"record_id": "diagnostic-record",
                    "statement": statement, "basis": "source_time", "source_refs": [reference],
                    "system_fact_id": None}]
        else:
            output = passed_review(payload["required_checks"], payload.get("skill_bundle"))
        response = {"output": output, "model": "artificial-diagnostic-model", "usage": None,
            "elapsed_seconds": 0.01, "invocation_id": invocation,
            "host_debug": self.DEBUG, "hidden_reasoning": self.DEBUG}
        self.returned.append(response)
        return response

    def process(self, **options):
        return process_sources(self.repo, [self.saved["path"]], model_call=self.adapter,
                               skill_bundle=self.bundle, **options)

    def failure(self, result):
        self.assertEqual(result["status"], "failed", result)
        self.assertTrue((self.repo / result["failure_path"]).is_file())
        return _load((self.repo / result["failure_path"]).read_bytes(), "failure")

    def assert_no_stage_or_review(self, result):
        self.assertFalse((self.repo / _stage_path(result["run_id"])).exists())
        self.assertFalse((self.repo / f"memory/runs/processes/{result['run_id']}/review.md").exists())
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
            "memory/records", "memory/topics"), "")
        self.assertEqual([role for role, _ in self.calls], ["extract"])

    def assert_saved_response(self, result, code):
        failure = self.failure(result)
        self.assertEqual(failure["error"], code)
        diagnostic = failure["extraction_diagnostic"]
        self.assertEqual(diagnostic["status"], "unvalidated_not_publishable")
        self.assertEqual(diagnostic["validation_target"], "response.output")
        expected = {k: deepcopy(self.returned[0][k]) for k in
                    ("output", "model", "invocation_id", "elapsed_seconds", "usage")}
        self.assertEqual(diagnostic["response"], expected)
        self.assertEqual(diagnostic["response_sha256"], _hash(expected))
        self.assertEqual(diagnostic["response_bytes"], len(canonical(expected)))
        self.assertEqual(diagnostic["request_binding"], request_binding(*self.calls[0]))
        self.assertEqual(diagnostic["validation_error"]["code"], code)
        self.assertEqual(failure["call"]["invocation_id"], expected["invocation_id"])
        audit = self.ledger.operation("diagnostic-operation")["calls"]["extract"]
        self.assertEqual((audit["status"], audit["invocation_id"]),
                         ("completed", expected["invocation_id"]))
        raw = (self.repo / result["failure_path"]).read_text()
        self.assertNotIn(self.DEBUG, raw)
        self.assertNotIn('"host_debug"', raw)
        self.assertNotIn('"hidden_reasoning"', raw)
        self.assertEqual(git(self.code, "rev-parse", "HEAD"), self.engine_head)
        self.assertEqual(git(self.code, "status", "--porcelain"), "")
        self.assert_no_stage_or_review(result)
        return diagnostic

    def test_missing_temporal_declaration_retains_response_and_exact_body_location(self):
        result = self.process()
        diagnostic = self.assert_saved_response(result, "quality_temporal_missing")
        details = diagnostic["validation_error"]["details"]
        self.assertEqual((details["record_id"], details["field_path"]),
                         ("diagnostic-record", "/quality/temporal_claims"))
        self.assertNotIn("claim_index", details)
        body = diagnostic["response"]["output"]["objects"][0]["body"]
        start = body.index("尚未")
        self.assertEqual(details["body_location"], {"field_path": "/objects/0/body",
            "start": start, "end": start + 2, "line": 2,
            "column": start - body.index("\n")})
        saved_bytes = (self.repo / result["failure_path"]).read_bytes()
        self.returned[0]["output"]["objects"][0]["body"] = "artificial caller mutation"
        self.assertEqual((self.repo / result["failure_path"]).read_bytes(), saved_bytes)

    def test_unqualified_source_time_points_to_the_actual_claim_statement(self):
        self.mode = "mismatch"
        result = self.process()
        diagnostic = self.assert_saved_response(result, "quality_temporal_mismatch")
        details = diagnostic["validation_error"]["details"]
        self.assertEqual((details["record_id"], details["claim_index"], details["field_path"]),
            ("diagnostic-record", 0, "/quality/temporal_claims/0/statement"))
        body = diagnostic["response"]["output"]["objects"][0]["body"]
        statement = diagnostic["response"]["output"]["quality"]["temporal_claims"][0]["statement"]
        start = body.index(statement)
        self.assertEqual(details["body_location"], {"field_path": "/objects/0/body",
            "start": start, "end": start + len(statement), "line": 2, "column": 1})

    def test_valid_temporal_declaration_still_reviews_and_publishes(self):
        self.mode = "valid"
        result = self.process()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual([role for role, _ in self.calls], ["extract", "review"])
        self.assertFalse((self.repo / f"memory/runs/processes/{result['run_id']}/failure.md").exists())
        published = publish(self.repo, result["run_id"])
        self.assertEqual(published["status"], "published_local")
        self.assertEqual(verify_publication(self.repo, result["run_id"])["commit_sha"], published["commit_sha"])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 2)

    def test_cli_and_audit_do_not_echo_the_private_diagnostic_response(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("pdm.cli._model_budget", return_value=self.adapter), \
                redirect_stdout(output), redirect_stderr(errors):
            status = main(["process", "--repo", str(self.repo), "--source", self.saved["path"],
                "--extract-skill", "knowledge-extract@11", "--review-skill", "knowledge-review@9",
                "--budget-ledger", str(self.ledger.path), "--operation-id", "diagnostic-operation"])
        self.assertEqual(status, 4, errors.getvalue())
        result = json.loads(output.getvalue())
        self.assert_saved_response(result, "quality_temporal_missing")
        for value in (output.getvalue(), errors.getvalue(), self.ledger.path.read_text()):
            self.assertNotIn(self.MARKER, value)
            self.assertNotIn(self.SOURCE, value)
            self.assertNotIn(self.DEBUG, value)

    def test_failure_storage_error_propagates_and_does_not_refund_or_replay(self):
        original = PipelineGit.write_files

        def stop_failure_write(git_adapter, files, expected_head, message):
            if any(path.endswith("/failure.md") for path in files):
                raise CaptureError("artificial_diagnostic_write_failed", "Artificial failure write stopped.")
            return original(git_adapter, files, expected_head, message)

        with patch.object(PipelineGit, "write_files", stop_failure_write):
            with self.assertRaises(CaptureError) as raised:
                self.process()
        self.assertEqual(raised.exception.code, "artificial_diagnostic_write_failed")
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)
        self.assertEqual(self.ledger.operation("diagnostic-operation")["calls"]["extract"]["status"], "completed")
        self.assertEqual(list(self.repo.glob("memory/runs/processes/*/failure.md")), [])
        before = self.ledger.path.read_bytes(), len(self.calls)
        with self.assertRaises(BudgetBlocked):
            self.process()
        self.assertEqual((self.ledger.path.read_bytes(), len(self.calls)), before)

    def test_saved_diagnostic_is_not_a_candidate_or_automatic_recovery_authority(self):
        result = self.process()
        self.assert_saved_response(result, "quality_temporal_missing")
        before = self.ledger.path.read_bytes(), len(self.calls), (self.repo / result["failure_path"]).read_bytes()
        with self.assertRaises(CaptureError):
            publish(self.repo, result["run_id"])
        with self.assertRaises(BudgetBlocked):
            self.process()
        self.assertEqual((self.ledger.path.read_bytes(), len(self.calls),
                          (self.repo / result["failure_path"]).read_bytes()), before)
        self.assert_no_stage_or_review(result)

    def test_unknown_timeout_does_not_invent_a_diagnostic_response(self):
        self.mode = "timeout"
        result = self.process()
        failure = self.failure(result)
        self.assertEqual(failure["error"], "timeout")
        self.assertNotIn("extraction_diagnostic", failure)
        self.assertEqual(self.ledger.operation("diagnostic-operation")["calls"]["extract"]["status"], "unknown")
        self.assertEqual(self.returned, [])
        self.assert_no_stage_or_review(result)

    def test_engineering_checkout_preserves_only_basic_failure_not_raw_response(self):
        # This is a disposable artificial engineering checkout, never the real
        # public repository. Even an accidentally combined data/code caller
        # must not persist a returned private body into its engineering tree.
        self.repo = self.code
        self.saved = capture(self.repo, {
            "source_key": "artificial:engineering-diagnostic-note", "source_type": "note",
            "acquisition_status": "full_text", "original_time": "2024-05-09",
            "body": self.SOURCE + "\n",
        })
        result = self.process()
        failure = self.failure(result)
        self.assertEqual(failure["error"], "quality_temporal_missing")
        self.assertEqual(failure["extraction_diagnostic"], {"status": "not_saved_engineering_root"})
        raw = (self.repo / result["failure_path"]).read_text()
        self.assertNotIn(self.MARKER, raw)
        self.assertNotIn(self.DEBUG, raw)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)
        self.assert_no_stage_or_review(result)

    def test_oversize_response_keeps_basic_failure_without_claiming_raw_retention(self):
        self.mode = "capacity"
        result = self.process()
        failure = self.failure(result)
        self.assertEqual(failure["error"], "quality_temporal_missing")
        diagnostic = failure["extraction_diagnostic"]
        self.assertEqual(diagnostic["status"], "not_saved_capacity")
        self.assertNotIn("response", diagnostic)
        expected = {k: self.returned[0][k] for k in
                    ("output", "model", "invocation_id", "elapsed_seconds", "usage")}
        self.assertEqual(diagnostic["response_sha256"], _hash(expected))
        self.assertEqual(diagnostic["response_bytes"], len(canonical(expected)))
        self.assertGreater(diagnostic["response_bytes"], 1024 * 1024)
        self.assertEqual(diagnostic["validation_error"]["code"], "quality_temporal_missing")
        self.assertLess(len((self.repo / result["failure_path"]).read_bytes()), 1024 * 1024)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)
        self.assert_no_stage_or_review(result)

    def test_checked_materialized_view_is_distinct_from_the_visible_raw_response(self):
        raw = {"output": {"statistic_bindings": []}, "model": "artificial-diagnostic-model",
               "invocation_id": "model-" + "c" * 32, "elapsed_seconds": 0.01, "usage": None}
        checked = {"objects": [{"id": "materialized-artificial", "body": "当时尚未完成人工测试。"}],
                   "quality": {"temporal_claims": []}}
        body = checked["objects"][0]["body"]
        start = body.index("尚未")
        details = {"record_id": "materialized-artificial", "field_path": "/quality/temporal_claims",
                   "body_location": {"field_path": "/objects/0/body", "start": start,
                                     "end": start + 2, "line": 1, "column": start + 1}}
        error = CaptureError("quality_temporal_missing", "人工时间声明缺失", details=details)
        error._diagnostic_validation_output = checked
        expected_raw, expected_checked, expected_details = deepcopy(raw), deepcopy(checked), deepcopy(details)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
        diagnostic = _extraction_diagnostic(PipelineGit(self.repo), error, raw)
        self.assertEqual(diagnostic["validation_target"], "validated_output")
        self.assertEqual(diagnostic["response"], expected_raw)
        self.assertEqual(diagnostic["response_sha256"], _hash(expected_raw))
        self.assertEqual(diagnostic["validated_output"], expected_checked)
        self.assertEqual(diagnostic["validation_error"]["details"], expected_details)
        self.assertNotIn("objects", diagnostic["response"]["output"])
        location = diagnostic["validation_error"]["details"]["body_location"]
        self.assertEqual(location["field_path"], "/objects/0/body")
        self.assertEqual(diagnostic["validated_output"]["objects"][0]["body"]
                         [location["start"]:location["end"]], "尚未")
        raw["output"]["statistic_bindings"].append({"artificial": True})
        checked["objects"][0]["body"] = "artificial caller mutation"
        details["record_id"] = "changed"
        self.assertEqual(diagnostic["response"], expected_raw)
        self.assertEqual(diagnostic["validated_output"], expected_checked)
        self.assertEqual(diagnostic["validation_error"]["details"], expected_details)
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes()), before)
        self.assertEqual(self.calls, [])

    def test_nonfinite_returned_values_have_no_diagnostic_or_side_effects(self):
        error = CaptureError("quality_temporal_missing", "人工时间声明缺失")
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
        for label, value in (("nan", float("nan")), ("positive_infinity", float("inf")),
                             ("negative_infinity", float("-inf"))):
            with self.subTest(value=label):
                raw = {"output": {"artificial_nonfinite_value": value},
                       "model": "artificial-diagnostic-model", "usage": None,
                       "invocation_id": "model-" + "d" * 32, "elapsed_seconds": 0.01}
                self.assertIsNone(_extraction_diagnostic(PipelineGit(self.repo), error, raw))
                self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes()), before)
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")
                self.assertEqual(list(self.repo.glob("memory/runs/processes/*/failure.md")), [])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
