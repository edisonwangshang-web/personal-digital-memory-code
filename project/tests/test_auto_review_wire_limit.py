"""Artificial encoder-capacity failures may select existing Review units only."""

import copy
import unittest
from unittest.mock import patch

from pdm.contracts import CaptureError
from pdm.knowledge_quality import prepare_quality
from pdm.model_calls import render_model_request
from pdm.pipeline import _review_payload, _use_decomposed_review
from pdm.skill_support import resolve_skill_refs
from pdm.skill_wire import MAX_BYTES, SharedJSONError
from test_quality_projection import source


class AutoReviewWireLimitTests(unittest.TestCase):
    def material(self):
        item = source("Artificial source for a general knowledge record.\n")
        context = prepare_quality([item])
        bundle = resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")
        candidate = {
            "review_strategy": "auto", "quality_contract": "skill-quality-v1",
            "skill_bundle": bundle, "quality_context": context,
            "objects": [{"path": "memory/records/artificial.md",
                         "metadata": {"sources": []}, "body": "Artificial knowledge."}],
            "extraction_output": {"quality": {}},
        }
        payload = {"sources": [item], "quality_contract": "skill-quality-v1",
                   "skill_bundle": bundle, "quality_context": context,
                   "revision_context": {"feedback": "Artificial complete semantic feedback."}}
        return candidate, payload

    def test_actual_shared_limit_selects_units_without_changing_request(self):
        candidate, payload = self.material()
        self.assertFalse(_use_decomposed_review(candidate, payload))
        candidate["revision_context"] = {"candidate": {"history": "x" * MAX_BYTES}}
        original = copy.deepcopy((candidate, payload))
        request = _review_payload(candidate, payload)
        with self.assertRaisesRegex(SharedJSONError, "^shared_json_limit$"):
            render_model_request("review", request)
        self.assertTrue(_use_decomposed_review(candidate, payload))
        self.assertEqual((candidate, payload), original)
        self.assertEqual(_review_payload(candidate, payload), request)

    def test_invalid_skill_and_noncapacity_wire_errors_still_block(self):
        candidate, payload = self.material()
        payload["quality_contract"] = "unknown-artificial-contract"
        with self.assertRaises(CaptureError) as rejected:
            _use_decomposed_review(candidate, payload)
        self.assertEqual(rejected.exception.code, "invalid_skill_request")
        candidate, payload = self.material()
        with patch("pdm.model_calls.request_binding", side_effect=SharedJSONError("shared_json_cycle")):
            with self.assertRaises(CaptureError) as rejected:
                _use_decomposed_review(candidate, payload)
        self.assertEqual(rejected.exception.code, "invalid_skill_request")


if __name__ == "__main__":
    unittest.main()
