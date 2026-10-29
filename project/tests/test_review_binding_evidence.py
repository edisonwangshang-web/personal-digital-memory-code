"""Synthetic-only Review anchors and versioned statistical obligations."""

from copy import deepcopy
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import validate_quality_review
from pdm.model_calls import _valid, output_schema
from pdm.skill_support import resolve_skill_refs
from pdm.statistic_repairs import _review_checks
import test_statistic_repair_readiness as readiness_fixtures


class ReviewBindingEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = readiness_fixtures.StatisticRepairReadinessTests()
        self.fixture.setUp()
        self.revision = self.fixture.revision()
        self.candidate = self.revision["candidate"]
        self.output = self.revision["review_record"]["output"]
        self.checks, _ = _review_checks(self.candidate, self.revision["review_record"])

    def validate(self, output, candidate=None):
        return validate_quality_review(output, self.checks, repair_scope_supported=True,
            repair_evidence_supported=True, candidate=candidate or self.candidate)

    def test_exact_anchor_is_accepted_without_changing_candidate_or_review(self):
        before = deepcopy((self.output, self.candidate))
        self.validate(self.output)
        self.assertEqual((self.output, self.candidate), before)

    def test_unprovided_text_or_a_same_value_statistic_of_another_record_is_rejected(self):
        for field, value in (("body_evidence", "Text not present in the fixed record."),
                             ("stat_id", "unprovided-statistic"), ("record_id", "table-1")):
            output = deepcopy(self.output)
            output["repair_scope"]["missing_bindings"][0][field] = value
            with self.subTest(field=field), self.assertRaises(CaptureError):
                self.validate(output)

    def test_a_review_cannot_request_readding_an_existing_binding(self):
        candidate = deepcopy(self.candidate)
        item = self.output["repair_scope"]["missing_bindings"][0]
        candidate["extraction_output"]["quality"]["statistic_claims"].append(
            {"stat_id": item["stat_id"], "record_id": item["record_id"]})
        with self.assertRaises(CaptureError):
            self.validate(self.output, candidate)

    def test_a_same_target_statistic_not_visible_in_this_review_is_rejected(self):
        with self.assertRaises(CaptureError):
            validate_quality_review(self.output, self.checks, repair_scope_supported=True,
                repair_evidence_supported=True, candidate=self.candidate,
                review_quality_context={"statistics": []})

    def test_new_schema_requires_the_body_anchor_and_old_schema_keeps_its_pair(self):
        payload = {"quality_contract": "skill-quality-v1", "skill_bundle": resolve_skill_refs(),
                   "quality_context": self.candidate["quality_context"]}
        schema = output_schema("review", payload)
        self.assertTrue(_valid(self.output, schema))
        pair_only = deepcopy(self.output)
        pair_only["repair_scope"]["missing_bindings"][0].pop("body_evidence")
        self.assertFalse(_valid(pair_only, schema))
        payload["skill_bundle"] = resolve_skill_refs("knowledge-extract@12", "knowledge-review@10")
        self.assertTrue(_valid(pair_only, output_schema("review", payload)))
        self.assertFalse(_valid(self.output, output_schema("review", payload)))

    def test_shared_denominator_rule_is_predeclared_for_the_actual_record_targets(self):
        check = next(check for check in self.checks if check["kind"] == "object_statistics_time")
        self.assertIn("statistic-bindings-v2", check["instruction"])
        self.assertIn("collective denominator", check["instruction"])
        self.assertIn("single-metric", check["instruction"])
        target = next(stat["target_id"] for stat in self.candidate["quality_context"]["statistics"]
                      if stat["stat_id"] == self.fixture.missing["stat_id"])
        self.assertIn(target, check["target_ids"])


if __name__ == "__main__":
    unittest.main()
