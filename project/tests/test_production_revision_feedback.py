"""New-contract feedback retries use real Git and artificial model responses."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_production
from engine_fixtures import isolated_engine, advance_engine
from pdm.batch_groups import verify_group_publication
from pdm.budget import BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.model_calls import MAX_INPUT_BYTES
from pdm.pipeline import _committed, _load, _read_reviewed
from pdm.production import _hash, prepare_production, run_production
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit
from quality_fixtures import add_quality, passed_review


class ProductionRevisionFeedbackTests(unittest.TestCase):
    setUp = test_production.ProductionTests.setUp
    state = test_production.ProductionTests.state
    attempts = test_production.ProductionTests.attempts

    def sources(self, count):
        return [capture(self.repo, {"source_key": f"artificial:feedback:{index}", "source_type": "note",
                   "acquisition_status": "full_text", "body": f"Artificial learning observation {index}.\n"})["path"]
                for index in range(count)]

    def transport(self, role, payload, timeout=120):
        response = test_production.ProductionTests.transport(self, role, payload, timeout)
        if role == "extract":
            if "revision_context" in payload:
                previous = payload["revision_context"]["candidate"]["objects"]
                old_records = {obj["metadata"]["id"]: obj for obj in previous if obj["metadata"]["type"] != "wiki"}
                for record in response["output"]["objects"]:
                    record["body"] = old_records[record["id"]]["body"] + " This artificial observation is limited to its supplied source."
                for topic in response["output"]["wikis"]:
                    topic["body"] += " This topic describes only the supplied artificial source."
            response["output"] = add_quality(response["output"], payload["quality_context"])
            response["output"]["reuse_refs"] = []
        else:
            response["output"] = passed_review(payload["required_checks"], payload.get("skill_bundle"))
            reject = getattr(self, "reject_reviews", 1)
            if reject:
                self.reject_reviews = reject - 1
                response["output"]["verdict"] = "needs_revision"
                response["output"]["reasons"] = ["Clarify the exact supplied-source scope before publication."]
                response["output"]["checks"][0].update(result="needs_revision", reason="The exact supplied-source scope needs clarification.")
        return response

    def prepare(self, name="feedback", bundle=None):
        paths = self.sources(1)
        prepared = prepare_production(self.repo, name, paths,
                    skill_bundle=bundle or resolve_skill_refs("knowledge-extract@10", "knowledge-review@8"))
        self.assertEqual(prepared["status"], "ready", prepared)
        return paths, prepared

    def test_rejected_slice_restarts_with_complete_feedback_then_publishes(self):
        paths, _ = self.prepare()
        first = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(first["status"], "needs_revision", first)
        git = PipelineGit(self.repo)
        frozen = {path: (self.repo / path).read_bytes() for path in (first["last_result"]["stage_path"], first["last_result"]["review_path"], *paths)}
        second = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(second["status"], "ready", second)
        self.assertEqual(self.roles, ["extract", "review"] * 2)
        revised_payload = [payload for role, payload in self.payloads if role == "extract"][-1]
        self.assertEqual(revised_payload["revision_context"]["source_run_id"], first["active_run_id"])
        self.assertEqual(revised_payload["revision_context"]["review_record"]["output"]["verdict"], "needs_revision")
        self.assertIn("Clarify the exact", revised_payload["revision_context"]["review_record"]["output"]["reasons"][0])
        attempt = self.attempts()[0]["attempts"][-1]
        self.assertEqual(attempt["attempt"], 2)
        self.assertEqual(attempt["revision_from_run"], first["active_run_id"])
        candidate, _, _ = _read_reviewed(git, second["final_run"])
        self.assertEqual(attempt["revision_context_sha256"], _hash(candidate["revision_context"]))
        published = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(published["status"], "published", published)
        proof = verify_group_publication(self.repo, published["publication"]["run_id"], source_paths=paths)
        self.assertEqual(len(proof["files"]), 2)
        self.assertEqual(self.roles, ["extract", "review"] * 2)
        self.assertEqual(frozen, {path: (self.repo / path).read_bytes() for path in frozen})

    def test_new_plan_and_skill_can_repair_old_rejection_without_resetting_attempt(self):
        code = isolated_engine(self)
        paths, original = self.prepare("old", resolve_skill_refs("knowledge-extract@9", "knowledge-review@7"))
        first = run_production(self.repo, "old", model_call=self.adapter, max_steps=1)
        old_state = deepcopy(self.state("old"))
        # A separate public engineering commit preserves the private sources.
        advance_engine(code)
        new = prepare_production(self.repo, "new", paths,
                  skill_bundle=resolve_skill_refs("knowledge-extract@10", "knowledge-review@8"))
        self.assertNotEqual(new["plan_id"], original["plan_id"])
        self.assertNotEqual(self.state("new")["plan"]["snapshot"]["versions"], old_state["plan"]["snapshot"]["versions"])
        second = run_production(self.repo, "new", model_call=self.adapter, max_steps=1)
        self.assertEqual(second["status"], "ready", second)
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 2)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), second["final_run"])
        self.assertEqual(candidate["batch_context"]["plan_id"], new["plan_id"])
        self.assertEqual(candidate["revision_context"]["candidate"]["batch_context"]["plan_id"], original["plan_id"])
        self.assertEqual(candidate["revision_context"]["source_run_id"], first["active_run_id"])
        published = run_production(self.repo, "new", model_call=self.adapter)
        self.assertEqual(published["status"], "published", published)
        proof = verify_group_publication(self.repo, published["publication"]["run_id"], source_paths=paths)
        self.assertEqual(proof["plan_id"], new["plan_id"])
        self.assertEqual(self.state("old"), old_state)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 4)

    def test_unknown_is_not_replayed_by_a_new_feedback_queue(self):
        paths, _ = self.prepare("old", resolve_skill_refs("knowledge-extract@9", "knowledge-review@7"))
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run_production(self.repo, "old", model_call=BudgetedModelCall(self.ledger, "worker", interrupted))
        prepare_production(self.repo, "new", paths, skill_bundle=resolve_skill_refs("knowledge-extract@10", "knowledge-review@8"))
        before = self.ledger.path.read_bytes()
        result = run_production(self.repo, "new", model_call=self.adapter)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 1)
        self.assertEqual(self.roles, [])

    def test_complete_feedback_capacity_is_checked_before_new_counter_or_audit_claim(self):
        self.prepare()
        first = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(first["status"], "needs_revision")
        before = deepcopy(self.attempts()), self.ledger.path.read_bytes(), self.number
        with patch("pdm.pipeline.preflight_processing", return_value={"status": "blocked_capacity", "request_bytes": MAX_INPUT_BYTES + 1,
                   "model_calls": 0, "attempted_calls": 0}) as checked:
            result = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "blocked_capacity", result)
        self.assertEqual(checked.call_args.kwargs["revision_from_run"], first["active_run_id"])
        self.assertEqual((self.attempts(), self.ledger.path.read_bytes(), self.number), before)

    def test_different_operator_run_cannot_mark_our_reservation_passed(self):
        self.prepare()
        first = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(first["status"], "needs_revision")
        before = self.ledger.path.read_bytes(), self.number
        foreign = "run-" + "f" * 32
        from pdm.pipeline import process_sources as actual_process
        def competing_operator(*args, **kwargs):
            if kwargs.get("_preflight_only"):
                return actual_process(*args, **kwargs)
            return {"status": "passed", "run_id": foreign, "attempted_calls": 0, "model_calls": 0}
        with patch("pdm.pipeline.process_sources", side_effect=competing_operator):
            result = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "blocked_context", result)
        self.assertEqual(result["last_result"]["error"], "queue_revision_run_mismatch")
        self.assertIsNone(result["final_run"])
        self.assertEqual(self.attempts()[0]["attempts"][-1]["status"], "blocked_context")
        self.assertEqual((self.ledger.path.read_bytes(), self.number), before)

    def test_legacy_queue_keeps_ordinary_retry_semantics(self):
        self.prepare("old", resolve_skill_refs("knowledge-extract@9", "knowledge-review@7"))
        self.reject_reviews = 1
        result = run_production(self.repo, "old", model_call=self.adapter, max_steps=2)
        self.assertEqual(result["status"], "ready", result)
        extracts = [payload for role, payload in self.payloads if role == "extract"]
        self.assertEqual(len(extracts), 2)
        self.assertTrue(all("revision_context" not in payload for payload in extracts))


if __name__ == "__main__":
    unittest.main()
