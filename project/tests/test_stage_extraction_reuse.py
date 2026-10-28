"""Small stage-reuse witnesses; all sources, returns and model calls are artificial."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_stage_reuse_pipeline as fixture
from pdm.batching import _context, _views
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.contracts import CaptureError, canonical
from pdm.pipeline import _load_sources, execution_versions
from pdm.production import _hash
from pdm.stage_extraction_reuse import prepare, read
from pdm.transaction import PipelineGit


class StageExtractionReuseTests(unittest.TestCase):
    setUp = fixture.StageReusePipelineTests.setUp
    state = fixture.StageReusePipelineTests.state
    attempts = fixture.StageReusePipelineTests.attempts
    sources = fixture.StageReusePipelineTests.sources
    transport = fixture.StageReusePipelineTests.transport
    prepare_queue = fixture.StageReusePipelineTests.prepare
    prepare = fixture.StageReusePipelineTests.prepare
    run_queue = fixture.StageReusePipelineTests.run_queue
    one_work = fixture.StageReusePipelineTests.one_work
    block_original = fixture.StageReusePipelineTests.block_original
    upgrade_target = fixture.StageReusePipelineTests.upgrade_target
    assert_no_formal = fixture.StageReusePipelineTests.assert_no_formal

    def target(self, *, prior_failures=0):
        self.block_original(prior_failures=prior_failures)
        self.upgrade_target()
        self.git = PipelineGit(self.repo)
        state = self.state("new")
        plan = state["plan"]
        batch = plan["batches"][0]
        self.payload = deepcopy(self.old["payload"])
        self.payload.pop("extraction_feedback", None)
        sources = _load_sources(self.repo, self.paths, allow_versions=True, purpose="history", max_sources=256)
        self.payload.update(batch_context=_context(plan["plan_id"], batch["batch_id"], plan["fragments"], batch["parent_batch_ids"]),
                            sources=_views(sources, plan["fragments"]))
        self.versions = execution_versions(self.git)
        self.work = _hash(self.one_work()["identity"])
        self.number_before = self.number
        self.reuse_adapter = self.adapter.for_operation("batch:" + self.work + ":" + str(len(self.one_work()["attempts"]) + 1))

    def witness(self, **changes):
        options = {"target_payload": self.payload, "target_versions": self.versions,
                   "work_id": self.work, "target_attempt": len(self.one_work()["attempts"]) + 1,
                   "model_call": self.reuse_adapter}
        options.update(changes)
        return prepare(self.git, self.old["row"]["run_id"], **options)

    def assert_capture_error(self, action):
        with self.assertRaises(CaptureError):
            action()

    def test_small_witness_reads_original_raw_and_payload_without_mutation(self):
        self.target(prior_failures=1)
        before = self.git.head(), self.ledger.path.read_bytes(), deepcopy(self.one_work()), self.number
        witness = self.witness()
        material = read(self.git, witness, target_payload=self.payload, target_versions=self.versions, model_call=self.reuse_adapter)
        self.assertEqual(material["candidate"], self.old["candidate"])
        self.assertEqual(material["original_payload"], self.old["payload"])
        raw = self.old["candidate"].get("raw_extraction_output", self.old["candidate"]["extraction_output"])
        self.assertEqual(material["response"]["output"], raw)
        self.assertEqual(material["response"]["invocation_id"], self.old["candidate"]["extraction"]["invocation_id"])
        self.assertEqual(witness["target"]["attempt"], 3)
        self.assertLess(len(canonical(witness)), 12000)
        self.assertNotIn("output", witness)
        self.assertNotIn("original_payload", witness)
        self.assertEqual(len(material["snapshots"]), 7)
        self.assertEqual((self.git.head(), self.ledger.path.read_bytes(), self.one_work(), self.number), before)

    def test_historical_read_uses_frozen_observation_without_an_external_ledger(self):
        self.target()
        witness = self.witness()
        self.ledger.path.unlink()
        material = read(self.git, witness, target_payload=self.payload, target_versions=self.versions, historical=True)
        self.assertEqual(material["candidate"]["run_id"], self.old["row"]["run_id"])
        self.assertIn("not_server_signature", witness["authority"])
        self.assertEqual(self.number, self.number_before)

    def test_changed_source_fragment_and_quality_scope_are_rejected(self):
        self.target()
        witness = self.witness()
        for field in ("source", "quality", "control", "rule"):
            with self.subTest(field=field):
                payload = deepcopy(self.payload)
                if field == "source":
                    payload["sources"][0]["fragments"][0]["lines"][0]["text"] += " Artificial change."
                elif field == "quality":
                    payload["quality_context"]["statistics_policy"] += "-changed"
                elif field == "control":
                    payload["controls"]["digest"] = "sha256-" + "f" * 64
                else:
                    payload["rules_version"] = "sha256-" + "f" * 64
                self.assert_capture_error(lambda: read(self.git, witness, target_payload=payload,
                    target_versions=self.versions, model_call=self.reuse_adapter))
        self.assertEqual(self.number, self.number_before)

    def test_wrong_authorized_source_is_rejected_before_any_original_body_load(self):
        self.target()
        payload = deepcopy(self.payload)
        payload["sources"][0]["path"] = self.saved["path"]
        with patch("pdm.pipeline._load_sources", side_effect=AssertionError("Unselected source body loaded")) as loader:
            with self.assertRaises(CaptureError) as caught:
                self.witness(target_payload=payload)
        self.assertEqual(caught.exception.code, "evidence_source_mismatch")
        loader.assert_not_called()
        self.assertEqual(self.number, self.number_before)

    def test_arbitrary_plan_and_batch_identity_changes_are_not_ignored(self):
        self.target()
        for key in ("plan_id", "batch_id"):
            payload = deepcopy(self.payload)
            payload["batch_context"][key] = ("plan-" if key == "plan_id" else "batch-") + "f" * 64
            self.assert_capture_error(lambda: self.witness(target_payload=payload))
        self.assertEqual(self.number, self.number_before)

    def test_different_pool_or_unknown_original_extract_cannot_authorize_reuse(self):
        self.target()
        ledger = BudgetLedger.create(self.ledger.path.with_name("stage-other.json"), None, 0,
                                    "artificial-stage-other-grant", mode="audit")
        adapter = BudgetedModelCall(ledger, "other", self.transport)
        self.assert_capture_error(lambda: self.witness(model_call=adapter))
        original = self.ledger.operation

        def changed(operation):
            value = original(operation)
            if operation == self.old["row"]["operation_id"]:
                value["calls"]["extract"]["status"] = "unknown"
            return value

        with patch.object(self.ledger, "operation", side_effect=changed):
            self.assert_capture_error(self.witness)
        self.assertEqual(self.number, self.number_before)

    def test_same_pool_with_the_wrong_target_operation_is_rejected(self):
        self.target()
        self.assert_capture_error(lambda: self.witness(model_call=self.adapter.for_operation("wrong-target")))
        self.assertEqual(self.number, self.number_before)

    def test_tampered_response_identity_and_ref_are_rejected(self):
        self.target()
        original = self.witness()
        for field in ("response", "payload", "ref", "invocation"):
            witness = deepcopy(original)
            if field == "response":
                witness["response_sha256"] = "sha256-" + "f" * 64
            elif field == "payload":
                witness["original_payload_sha256"] = "f" * 64
            elif field == "ref":
                witness["snapshots"]["stage"]["sha256"] = "sha256-" + "f" * 64
            else:
                witness["completed_extract"]["invocation_id"] = "model-" + "f" * 32
            self.assert_capture_error(lambda: read(self.git, witness, target_payload=self.payload,
                target_versions=self.versions, model_call=self.reuse_adapter))
        self.assertEqual(self.number, self.number_before)

    def test_four_used_slots_reject_a_fifth_and_missing_claim_stays_zero_call(self):
        self.target()
        witness = self.witness()
        self.assert_capture_error(lambda: read(self.git, witness, target_payload=self.payload,
            target_versions=self.versions, model_call=self.reuse_adapter, require_claim=True))
        self.assert_capture_error(lambda: self.witness(target_attempt=5))
        self.assertEqual(self.number, self.number_before)


if __name__ == "__main__":
    unittest.main()
