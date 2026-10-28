"""Fixed failed extraction feedback, with artificial inputs and a real audit/Git chain."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_production
from pdm.batch_groups import verify_group_publication
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.extraction_feedback import (_model_view, authority_refs, load_feedback,
                                     prepare_feedback)
from pdm.model_calls import MAX_INPUT_BYTES, ModelCallError
from pdm.pipeline import _committed, _load, _read_reviewed, _run_path
from pdm.production import prepare_production, run_production
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit
from quality_fixtures import add_quality, passed_review


class ExtractionValidationFeedbackTests(unittest.TestCase):
    setUp = test_production.ProductionTests.setUp
    attempts = test_production.ProductionTests.attempts

    def sources(self):
        return [capture(self.repo, {"source_key": "artificial:validator-feedback", "source_type": "note",
                    "acquisition_status": "full_text", "body": "Artificial learning observation.\n"})["path"]]

    def prepare(self, name="feedback", *, old=False, paths=None):
        paths = paths or self.sources()
        bundle = resolve_skill_refs("knowledge-extract@11" if old else "knowledge-extract@12",
                                    "knowledge-review@9" if old else "knowledge-review@10")
        result = prepare_production(self.repo, name, paths, skill_bundle=bundle)
        self.assertEqual(result["status"], "ready", result)
        return paths

    def transport(self, role, payload, timeout=120):
        response = test_production.ProductionTests.transport(self, role, payload, timeout)
        if role == "extract":
            response["output"] = add_quality(response["output"], payload["quality_context"])
            response["output"]["reuse_refs"] = []
            extract_number = self.roles.count("extract")
            if extract_number <= getattr(self, "failed_extracts", 1):
                response["output"]["quality"]["coverage_disposition"][0]["body_evidence"] = []
        else:
            response["output"] = passed_review(payload["required_checks"], payload.get("skill_bundle"))
        return response

    def first_failure(self):
        result = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["last_result"]["error"], "quality_body_mapping_mismatch")
        self.assertEqual(self.roles, ["extract"])
        return result

    def assert_error(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_failed_draft_and_exact_error_feed_next_extraction_then_independent_review_and_publish(self):
        paths = self.prepare()
        first = self.first_failure()
        git = PipelineGit(self.repo)
        failure_path = _run_path(first["active_run_id"], "failure")
        frozen_failure = _committed(git, failure_path)
        diagnostic = _load(frozen_failure, "failure")["extraction_diagnostic"]
        second = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(second["status"], "ready", second)
        self.assertEqual(self.roles, ["extract", "extract", "review"])
        payload = [value for role, value in self.payloads if role == "extract"][-1]
        feedback = payload["extraction_feedback"]
        self.assertNotIn("revision_context", payload)
        self.assertEqual(feedback["source_run_id"], first["active_run_id"])
        self.assertEqual(feedback["validation_error"], diagnostic["validation_error"])
        self.assertEqual(feedback["draft"]["objects"], diagnostic["response"]["output"]["objects"])
        self.assertEqual(set(feedback["draft"]["quality"]), {"coverage_disposition"})
        self.assertNotIn("response", feedback)
        self.assertNotIn("payload", feedback)
        attempts = self.attempts()[0]["attempts"]
        self.assertEqual([item["attempt"] for item in attempts], [1, 2])
        self.assertEqual(attempts[0]["status"], "failed")
        candidate, _, _ = _read_reviewed(git, second["final_run"])
        ref = candidate["extraction_feedback_ref"]
        self.assertEqual(ref, attempts[1]["extraction_feedback_ref"])
        self.assertEqual({entry["path"] for entry in authority_refs(git, ref)},
                         {_run_path(first["active_run_id"], name) for name in ("failure", "started", "engine")})
        published = run_production(self.repo, "feedback", model_call=self.adapter)
        self.assertEqual(published["status"], "published", published)
        # Historical publication verification uses committed proof, not an external audit file.
        self.ledger.path.unlink()
        proof = verify_group_publication(self.repo, published["publication"]["run_id"], source_paths=paths)
        self.assertEqual(len(proof["files"]), 2)
        self.assertEqual(_committed(git, failure_path), frozen_failure)
        self.assertEqual(self.roles, ["extract", "extract", "review"])

    def test_missing_complete_raw_stops_before_new_counter_or_call(self):
        self.prepare()
        with patch("pdm.pipeline._extraction_diagnostic", return_value={"status": "not_saved_capacity"}):
            self.first_failure()
        before = deepcopy(self.attempts()), self.ledger.path.read_bytes(), self.number
        result = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "blocked_context", result)
        self.assertEqual(result["last_result"]["error"], "extraction_feedback_unavailable")
        self.assertEqual((self.attempts(), self.ledger.path.read_bytes(), self.number), before)

    def test_feedback_candidate_keeps_original_request_when_review_never_started(self):
        paths = self.prepare()
        review_launch_failed = []

        def transport(role, payload, timeout=120, on_start=None):
            if role == "review" and not review_launch_failed:
                review_launch_failed.append(True)
                raise ModelCallError("codex_not_found", "model-" + "f" * 32)
            on_start("model-" + f"{self.number + 1:032x}")
            return self.transport(role, payload, timeout)

        adapter = BudgetedModelCall(self.ledger, "feedback-prelaunch-worker")
        with patch("pdm.model_calls._call_transport", side_effect=transport):
            first = run_production(self.repo, "feedback", model_call=adapter, max_steps=1)
            self.assertEqual(first["last_result"]["error"], "quality_body_mapping_mismatch")
            second = run_production(self.repo, "feedback", model_call=adapter, max_steps=1)
            self.assertEqual(second["last_result"]["error"], "codex_not_found")
            prior = self.attempts()[0]["attempts"][1]
            self.assertNotIn("review", self.ledger.operation(prior["operation_id"])["calls"])
            git = PipelineGit(self.repo)
            stage_path = "memory/staging/" + prior["run_id"] + ".md"
            stage_bytes = _committed(git, stage_path)
            prior_candidate = _load(stage_bytes, "candidate")
            result = run_production(self.repo, "feedback", model_call=adapter)

        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "extract", "review"])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 3)
        attempts = self.attempts()[0]["attempts"]
        self.assertEqual([item["attempt"] for item in attempts], [1, 2, 3])
        self.assertEqual([item["status"] for item in attempts], ["failed", "failed", "passed"])
        candidate, _, _ = _read_reviewed(git, result["final_run"])
        self.assertEqual(candidate["extraction_reused_from"]["run_id"], prior["run_id"])
        self.assertEqual(candidate["extraction_feedback_ref"], prior_candidate["extraction_feedback_ref"])
        self.assertEqual(_committed(git, stage_path), stage_bytes)
        self.ledger.path.unlink()
        proof = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=paths)
        self.assertEqual(len(proof["files"]), 2)

    def test_original_ledger_call_must_still_be_exactly_completed(self):
        self.prepare()
        self.first_failure()
        old = self.attempts()[0]["attempts"][0]
        original_operation = self.ledger.operation

        def unknown_operation(operation_id):
            operation = original_operation(operation_id)
            if operation_id == old["operation_id"]:
                operation["calls"]["extract"]["status"] = "unknown"
            return operation

        before = deepcopy(self.attempts()), self.ledger.path.read_bytes(), self.number
        with patch.object(self.ledger, "operation", side_effect=unknown_operation):
            self.assert_error("extraction_feedback_audit_mismatch",
                              lambda: prepare_feedback(PipelineGit(self.repo), old, self.adapter))
        self.assertEqual((self.attempts(), self.ledger.path.read_bytes(), self.number), before)

    def test_different_audit_pool_cannot_authorize_existing_failed_output(self):
        self.prepare()
        self.first_failure()
        other = BudgetLedger.create(self.ledger.path.with_name("other-audit.json"), None, 24,
                                   "artificial-different-feedback-grant", mode="audit")
        adapter = BudgetedModelCall(other, "worker", self.transport)
        before = deepcopy(self.attempts()), self.ledger.path.read_bytes(), other.path.read_bytes(), self.number
        self.assert_error("extraction_feedback_audit_mismatch", lambda: prepare_feedback(
            PipelineGit(self.repo), self.attempts()[0]["attempts"][0], adapter))
        self.assertEqual((self.attempts(), self.ledger.path.read_bytes(), other.path.read_bytes(), self.number), before)

    def test_later_edits_cannot_replace_the_fixed_failed_output(self):
        self.prepare()
        first = self.first_failure()
        git = PipelineGit(self.repo)
        path = _run_path(first["active_run_id"], "failure")
        git.path(path).write_bytes(_committed(git, path) + b"\n")
        git.run("add", "--", path)
        git.run("commit", "-m", "artificial failure edit")
        before = deepcopy(self.attempts()), self.ledger.path.read_bytes(), self.number
        result = run_production(self.repo, "feedback", model_call=self.adapter)
        self.assertEqual(result["status"], "blocked_context", result)
        self.assertEqual(result["last_result"]["error"], "extraction_feedback_changed")
        self.assertEqual((self.attempts(), self.ledger.path.read_bytes(), self.number), before)

    def test_full_feedback_scope_is_frozen_except_control_head(self):
        self.prepare()
        self.first_failure()
        git = PipelineGit(self.repo)
        ref = prepare_feedback(git, self.attempts()[0]["attempts"][0], self.adapter)
        failure = _load(_committed(git, _run_path(ref["source_run_id"], "failure")), "failure")
        versions = failure["extraction_diagnostic"]["validation_context"]["versions"]
        payload = deepcopy(self.payloads[0][1])
        payload["controls"]["head"] = "f" * 40
        self.assertEqual(load_feedback(git, ref, payload, versions)["source_run_id"], ref["source_run_id"])
        payload["sources"][0]["fragments"][0]["lines"][0]["text"] += " Changed artificial source."
        self.assert_error("extraction_feedback_scope_changed", lambda: load_feedback(git, ref, payload, versions))
        payload = deepcopy(self.payloads[0][1])
        payload["controls"]["digest"] = "sha256-" + "f" * 64
        self.assert_error("extraction_feedback_scope_changed", lambda: load_feedback(git, ref, payload, versions))
        changed_versions = deepcopy(versions)
        changed_versions["program_commit"] = "f" * 40
        self.assert_error("extraction_feedback_scope_changed", lambda: load_feedback(
            git, ref, self.payloads[0][1], changed_versions))

    def test_capacity_preflight_includes_feedback_without_claiming_an_attempt(self):
        self.prepare()
        self.first_failure()
        before = deepcopy(self.attempts()), self.ledger.path.read_bytes(), self.number
        with patch("pdm.pipeline.preflight_processing", return_value={"status": "blocked_capacity",
                   "request_bytes": MAX_INPUT_BYTES + 1, "model_calls": 0, "attempted_calls": 0}) as checked:
            result = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "blocked_capacity", result)
        self.assertIn("extraction_feedback_ref", checked.call_args.kwargs)
        self.assertEqual((self.attempts(), self.ledger.path.read_bytes(), self.number), before)

    def test_four_failed_extractions_end_work_across_queue_rename(self):
        paths = self.prepare()
        self.failed_extracts = 10
        result = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=8)
        self.assertEqual(result["status"], "attempt_limit", result)
        self.assertEqual(self.roles, ["extract"] * 4)
        self.assertEqual([item["attempt"] for item in self.attempts()[0]["attempts"]], [1, 2, 3, 4])
        self.assertTrue(all(item["status"] == "failed" for item in self.attempts()[0]["attempts"]))
        self.prepare("renamed", paths=paths)
        again = run_production(self.repo, "renamed", model_call=self.adapter)
        self.assertEqual(again["status"], "attempt_limit", again)
        self.assertEqual(self.roles, ["extract"] * 4)
        self.assertEqual(len(self.attempts()), 1)

    def test_unknown_first_call_is_never_replayed_as_validation_feedback(self):
        self.prepare()

        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_production(self.repo, "feedback", model_call=BudgetedModelCall(self.ledger, "worker", interrupted))
        before = deepcopy(self.attempts()), self.ledger.path.read_bytes()
        result = run_production(self.repo, "feedback", model_call=self.adapter)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual((self.attempts(), self.ledger.path.read_bytes()), before)
        self.assertEqual(self.roles, [])

    def test_old_contract_does_not_gain_new_feedback(self):
        self.prepare(old=True)
        self.first_failure()
        second = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(second["status"], "ready", second)
        self.assertTrue(all("extraction_feedback" not in payload for role, payload in self.payloads if role == "extract"))

    def test_model_view_only_includes_the_error_related_quality_claim(self):
        diagnostic = {"response": {"output": {"objects": [], "wikis": [], "reuse_refs": [],
                       "quality": {"coverage_disposition": [{"unused": True}], "statistic_claims": [{"unused": True}],
                                   "temporal_claims": [{"record_id": "artificial-a"}, {"record_id": "artificial-b"}]}}},
                      "validation_error": {"code": "quality_temporal_mismatch", "message": "Artificial exact error",
                                           "details": {"field_path": "/quality/temporal_claims/1", "claim_index": 1}}}
        view = _model_view(diagnostic, "run-" + "1" * 32)
        self.assertEqual(view["draft"]["quality"], {"temporal_claims": [{"record_id": "artificial-b"}]})
        self.assertEqual(view["validation_error"], diagnostic["validation_error"])

    def test_model_view_uses_the_materialized_draft_targeted_by_the_exact_error(self):
        diagnostic = {"response": {"output": {"statistic_bindings": [{"stat_id": "artificial"}]}},
                      "validated_output": {"objects": [{"id": "artificial-record", "body": "Artificial body."}],
                                           "wikis": [], "reuse_refs": [],
                                           "quality": {"temporal_claims": [{"record_id": "artificial-record"}]}},
                      "validation_target": "validated_output",
                      "validation_error": {"code": "quality_temporal_mismatch", "message": "Artificial error",
                                           "details": {"claim_index": 0}}}
        view = _model_view(diagnostic, "run-" + "2" * 32)
        self.assertEqual(view["draft"]["objects"], diagnostic["validated_output"]["objects"])
        self.assertNotIn("statistic_bindings", view["draft"])
        self.assertEqual(view["draft"]["quality"], {"temporal_claims": [{"record_id": "artificial-record"}]})

    def test_missing_statistic_feedback_omits_unrelated_existing_claims(self):
        diagnostic = {"response": {"output": {"objects": [], "wikis": [], "reuse_refs": [],
                       "quality": {"statistic_claims": [{"record_id": "artificial-old", "stat_id": "stat-old"}]}}},
                      "validation_error": {"code": "reuse_statistic_claims_missing", "message": "Artificial missing",
                                           "details": {"field_path": "/quality/statistic_claims", "missing_claims": [
                                               {"record_id": "artificial-new", "stat_id": "stat-new"}]}}}
        view = _model_view(diagnostic, "run-" + "3" * 32)
        self.assertEqual(view["draft"]["quality"], {"statistic_claims": []})
        self.assertEqual(view["validation_error"], diagnostic["validation_error"])


if __name__ == "__main__":
    unittest.main()
