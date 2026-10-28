"""Synthetic final-content gates; no real sources or model calls."""

from copy import deepcopy
import unittest

import test_knowledge_quality as fixtures
from pdm.contracts import CaptureError
from pdm.knowledge_quality import (
    build_review_checks, prepare_quality, validate_quality_output, validate_quality_review,
)
from quality_fixtures import add_quality, passed_review


class FinalContentCompletionTests(unittest.TestCase):
    def incomplete_package(self):
        description = fixtures.source("这份人工资料包含一份说明和两项监测值。", "description")
        data = fixtures.source("日期,步数,resting_heart_rate_bpm\n2025-01-01,10,56\n", "measurements")
        context = prepare_quality([description, data])
        output = fixtures.extraction(description, "人工资料范围说明。")
        output["quality"] = {"coverage_disposition": [], "statistic_claims": [], "temporal_claims": []}
        for target in context["targets"]:
            item = {"target_id": target["target_id"], "status": "deferred", "record_ids": [],
                    "body_evidence": [], "reason": "该项内容还没有完成整理，需要在后续处理后补充。"}
            if target["source_id"] == description["source_id"]:
                item.update(status="summarized", record_ids=["synthetic"],
                            body_evidence=[{"record_id": "synthetic", "text": "人工资料范围说明。"}], reason="")
            output["quality"]["coverage_disposition"].append(item)
        validate_quality_output(output, context)
        return fixtures.candidate(output), context

    @staticmethod
    def review_version(candidate, version="10"):
        result = deepcopy(candidate)
        # The builder receives an already-validated candidate in production.
        # This helper-level test supplies only the role selector it consumes.
        result["skill_bundle"] = {"skills": {"review": {"ref": "knowledge-review@" + version}}}
        return result

    def assert_error(self, code, action):
        with self.assertRaises(CaptureError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    @staticmethod
    def nonpassing_review(checks, verdict):
        result = passed_review(checks)
        result["verdict"] = verdict
        for entry, check in zip(result["checks"], checks):
            if "allowed_results" in check:
                entry["result"] = verdict
                entry["reason"] = "The synthetic source limitation or unfinished content remains unresolved."
        return result

    def test_description_cannot_make_deferred_new_measurements_complete(self):
        candidate, context = self.incomplete_package()
        checks = build_review_checks(self.review_version(candidate), context)
        incomplete = [check for check in checks if "allowed_results" in check]
        self.assertEqual([check["kind"] for check in incomplete],
                         ["metric_coverage", "metric_coverage", "global_organization"])
        self.assertTrue(all(check["allowed_results"] == ["needs_revision", "needs_confirmation"]
                            for check in incomplete))
        error = self.assert_error("quality_review_incomplete",
                                  lambda: validate_quality_review(passed_review(checks), checks))
        self.assertTrue(error.details["target_ids"])

    def test_unfinished_content_and_genuine_gap_keep_nonpassing_outcomes(self):
        for key, domain in (("health", "健康"), ("career", "事业"), ("learning", "学习")):
            with self.subTest(domain=domain):
                source = fixtures.source(f"用户提交了{domain}话题，但没有提供所述参考材料。", key)
                context = prepare_quality([source])
                output = fixtures.extraction(source, "一条人工待补充资料的范围说明。")
                output["quality"] = {
                    "coverage_disposition": [
                        {"target_id": target["target_id"], "status": "deferred", "record_ids": [],
                         "body_evidence": [], "reason": "来源明确没有提供所述参考材料，需要用户补充对应正文。"}
                        for target in context["targets"]],
                    "statistic_claims": [], "temporal_claims": [],
                }
                validate_quality_output(output, context)
                checks = build_review_checks(self.review_version(fixtures.candidate(output)), context)
                for verdict in ("needs_revision", "needs_confirmation"):
                    review = self.nonpassing_review(checks, verdict)
                    validate_quality_review(review, checks)
                    self.assertEqual(review["verdict"], verdict)

    def test_legacy_review_nine_checks_and_behavior_remain_unchanged(self):
        candidate, context = self.incomplete_package()
        legacy = build_review_checks(self.review_version(candidate, "9"), context)
        self.assertEqual(legacy, build_review_checks(candidate, context))
        self.assertFalse(any("allowed_results" in check for check in legacy))
        validate_quality_review(passed_review(legacy), legacy)

    def test_draft_and_object_checks_do_not_require_final_completion(self):
        candidate, context = self.incomplete_package()
        draft = self.review_version(candidate)
        draft["batch_material"] = {"draft_only": True}
        checks = build_review_checks(draft, context)
        self.assertFalse(any("allowed_results" in check for check in checks))
        validate_quality_review(passed_review(checks), checks)
        scope = {"kind": "object", "object_paths": [obj["path"] for obj in candidate["objects"]],
                 "fragment_ids": [fragment["fragment_id"] for source in context["sources"]
                                  for fragment in source["fragments"]]}
        checks = build_review_checks(self.review_version(candidate), context, scope)
        self.assertFalse(any("allowed_results" in check for check in checks))
        validate_quality_review(passed_review(checks), checks)

    def test_decomposed_coverage_and_global_gate_the_complete_declarations(self):
        candidate, context = self.incomplete_package()
        for kind in ("coverage", "global"):
            with self.subTest(kind=kind):
                scope = {"kind": kind, "object_paths": [obj["path"] for obj in candidate["objects"]],
                         "fragment_ids": [] if kind == "global" else [
                             fragment["fragment_id"] for source in context["sources"]
                             for fragment in source["fragments"]]}
                checks = build_review_checks(self.review_version(candidate), context, scope)
                self.assertTrue(any("allowed_results" in check for check in checks))
                self.assert_error("quality_review_incomplete",
                                  lambda: validate_quality_review(passed_review(checks), checks))
                validate_quality_review(self.nonpassing_review(checks, "needs_confirmation"), checks)

    def test_final_declarations_are_bound_even_when_record_text_does_not_change(self):
        source = fixtures.source("人工材料只陈述一件已经发生过的测试事件。")
        context = prepare_quality([source])
        output = add_quality(fixtures.extraction(source, "测试事件的人工记录。"), context)
        output["quality"]["temporal_claims"] = []
        complete = self.review_version(fixtures.candidate(output))
        checks = build_review_checks(complete, context)
        validate_quality_review(passed_review(checks), checks)
        deferred = deepcopy(complete)
        declaration = deferred["extraction_output"]["quality"]["coverage_disposition"][0]
        declaration.update(status="deferred", record_ids=[], body_evidence=[],
                           reason="此处为人工对抗输入，将同一正文故意标成尚未处理。")
        changed = build_review_checks(deferred, context)
        self.assertEqual(complete["objects"], deferred["objects"])
        self.assertNotEqual([check["scope_sha256"] for check in checks],
                            [check["scope_sha256"] for check in changed])
        self.assert_error("quality_review_coverage_mismatch",
                          lambda: validate_quality_review(passed_review(checks), changed))

    def test_new_contract_missing_dispositions_cannot_disable_the_gate(self):
        candidate, context = self.incomplete_package()
        candidate = self.review_version(candidate)
        candidate["extraction_output"]["quality"]["coverage_disposition"] = []
        self.assert_error("invalid_quality_scope", lambda: build_review_checks(candidate, context))


if __name__ == "__main__":
    unittest.main()
