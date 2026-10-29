"""Artificial complete-request limits and frozen historical rendering only."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pdm import model_calls
from pdm.budget import BudgetLedger
from pdm.knowledge_quality import prepare_quality
from pdm.skill_support import resolve_skill_refs
from test_quality_projection import source


class Dev37RequestCapacityTests(unittest.TestCase):
    def material(self, kind="extract"):
        item = source("Artificial learning evidence.\nBlank cells stay blank.\n",
                      "capacity-compatibility")
        payload = {
            "quality_contract": "skill-quality-v1",
            "skill_bundle": resolve_skill_refs("knowledge-extract@13", "knowledge-review@11"),
            "sources": [item], "quality_context": prepare_quality([item]),
            "revision_context": {"candidate": {"body": "Current complete artificial draft.\n"},
                "review_record": {"output": {"verdict": "needs_revision",
                    "reasons": ["Keep the exact unresolved artificial feedback."]}}},
        }
        if kind == "patch":
            payload["quality_repair_context"] = {
                "contract": "statistic-bindings-repair-v2", "missing_bindings": [{
                    "stat_id": "artificial-stat", "record_id": "artificial-record",
                    "body_evidence": "Exact artificial body evidence."}]}
        return payload

    def sized(self, role, payload, size):
        payload = deepcopy(payload)
        payload["artificial_padding"] = ""
        overhead = len(model_calls.render_model_request(role, payload))
        payload["artificial_padding"] = "x" * (size - overhead)
        self.assertEqual(size, len(model_calls.render_model_request(role, payload)))
        return payload

    def output(self, kind, payload):
        if kind == "review":
            return {"verdict": "passed", "reasons": ["Artificial transport only."],
                    "checks": [], "repair_scope": {"kind": "none", "missing_bindings": []}}
        if kind == "patch":
            return {"statistic_bindings": deepcopy(payload["quality_repair_context"]["missing_bindings"])}
        return {"objects": [], "wiki": None, "reuse_refs": [], "quality": {
            "coverage_disposition": [{"target_id": item["target_id"], "status": "deferred",
                "record_ids": [], "body_evidence": [], "reason": "Artificial transport only."}
                for item in payload["quality_context"]["targets"]],
            "statistic_claims": [], "temporal_claims": []}}

    def test_complete_extract_review_and_patch_keep_every_byte_at_the_new_native_boundary(self):
        self.assertEqual(384_000, model_calls.MAX_INPUT_BYTES)
        with tempfile.TemporaryDirectory() as temporary:
            ledger = BudgetLedger.create(Path(temporary) / "ledger.json", 20, 0,
                                         "artificial capacity validation only")
            for kind in ("extract", "review", "patch"):
                role = "review" if kind == "review" else "extract"
                for size in (323_370, 384_000):
                    with self.subTest(kind=kind, size=size):
                        payload = self.sized(role, self.material(kind), size)
                        frozen = deepcopy(payload)
                        output = self.output(kind, payload)
                        events = [{"type": "item.completed", "item": {
                            "type": "agent_message", "text": json.dumps(output)}},
                            {"type": "turn.completed"}]
                        operation = f"artificial-{kind}-{size}"
                        with patch.object(model_calls.shutil, "which", return_value="/artificial/codex"), \
                                patch.object(model_calls.subprocess, "Popen", return_value=Mock()) as launch, \
                                patch.object(model_calls, "_events", return_value=events) as stream:
                            result = model_calls.call_model(role, payload, ledger=ledger,
                                                            operation_id=operation)
                        launch.assert_called_once()
                        self.assertEqual(stream.call_args.args[1], model_calls.render_model_request(role, payload))
                        self.assertEqual(len(stream.call_args.args[1]), size)
                        self.assertEqual(result["output"], output)
                        self.assertEqual(payload, frozen)
                        self.assertEqual(ledger.operation(operation)["calls"][role]["status"], "completed")

    def test_one_byte_over_is_rejected_before_any_native_reservation_for_all_processing_roles(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = BudgetLedger.create(Path(temporary) / "ledger.json", 20, 0,
                                         "artificial rejected capacity validation")
            for kind in ("extract", "review", "patch"):
                role = "review" if kind == "review" else "extract"
                payload = self.sized(role, self.material(kind), 384_001)
                before = ledger.path.read_bytes()
                operation = "oversize-" + kind
                with self.subTest(kind=kind), \
                        patch.object(model_calls.shutil, "which", return_value="/artificial/codex"), \
                        patch.object(model_calls.subprocess, "Popen") as launch, \
                        self.assertRaises(model_calls.ModelCallError) as raised:
                    model_calls.call_model(role, payload, ledger=ledger, operation_id=operation)
                self.assertEqual(raised.exception.code, "input_limit")
                launch.assert_not_called()
                self.assertEqual(ledger.path.read_bytes(), before)
                self.assertIsNone(ledger.operation(operation))

    def test_previous_full_review_and_patch_rendering_retain_their_precise_bytes(self):
        expected = {
            "extract": (53_238, "sha256-8f7ca345bea9f5f21e129d48c18f55d9e440d9a085bc046667224eede2ef03e6"),
            "review": (47_738, "sha256-931caffa0fbcd4598bbbfc5c6a8923eef9a2d4784fab146137656144c94d9d70"),
            "patch": (53_434, "sha256-3204b187adeb15e5ec43e3c7c44c616ca1d656ebb77f431d7dbfcfe1acdaecac"),
        }
        for kind, (size, digest) in expected.items():
            role = "review" if kind == "review" else "extract"
            with self.subTest(kind=kind):
                self.assertEqual(model_calls.request_binding(role, self.material(kind)),
                                 {"bytes": size, "sha256": digest})

    def test_new_pair_retains_the_current_full_and_statistic_repair_schemas(self):
        payload = self.material()
        payload["skill_bundle"] = resolve_skill_refs("knowledge-extract@14", "knowledge-review@12")
        extract = model_calls.output_schema("extract", payload)
        review = model_calls.output_schema("review", payload)
        self.assertIn("reuse_refs", extract["required"])
        self.assertIn("repair_scope", review["required"])
        fields = review["properties"]["repair_scope"]["properties"]["missing_bindings"]["items"]["properties"]
        self.assertEqual({"stat_id", "record_id", "body_evidence"}, set(fields))


if __name__ == "__main__":
    unittest.main()
