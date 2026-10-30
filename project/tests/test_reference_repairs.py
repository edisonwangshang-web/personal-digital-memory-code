"""Artificial bad references are repaired without regenerating good knowledge."""

from copy import deepcopy
import unittest

from pdm.contracts import CaptureError
from pdm.extraction_feedback import _model_view
from pdm.model_calls import output_schema, _valid
from pdm.pipeline import _refs
from pdm.reference_repairs import build_context, materialize_patch
from pdm.skill_support import resolve_skill_refs


def fixture():
    reference = {"source_id": "src-artificial", "source_version": "sha256-artificial",
                 "locator": "L1-L1", "quote": "人工来源正文。"}
    bad = {**reference, "quote": "  人工来源正文。"}
    draft = {"objects": [{"id": "artificial-work", "body": "已有正确内容，不改写。", "sources": [bad, reference]}],
             "wikis": [{"id": "artificial-topic", "body": "现有主题正文。", "sources": [reference],
                        "record_ids": ["artificial-work"]}], "reuse_refs": [],
             "quality": {"coverage_disposition": [], "statistic_claims": [],
                         "temporal_claims": [{"record_id": "artificial-work", "statement": "来源当时的说明。",
                                              "basis": "source_time", "system_fact_id": None, "source_refs": [bad]}]}}
    paths = ["/objects/0/sources/0", "/quality/temporal_claims/0/source_refs/0"]
    feedback = {"contract": "extraction-validation-feedback-v1",
                "draft_contract": "current-draft-complete-quality-v2",
                "generation_invocations": ["model-artificial-original"],
                "source_run_id": "run-" + "a" * 32, "draft": draft,
                "validation_error": {"code": "invalid_quote", "message": "人工引文不匹配",
                                     "details": {"issues": [{"code": "invalid_quote", "field_path": p}
                                                            for p in paths], "issues_truncated": False}}}
    feedback["repair_context"] = build_context(feedback)
    patch = {"reference_repairs": [{"field_path": p, "reference": deepcopy(reference)} for p in paths]}
    return feedback, patch, reference


class ReferenceRepairTests(unittest.TestCase):
    def test_two_targeted_repairs_freeze_bodies_good_refs_and_complete_quality(self):
        feedback, patch, reference = fixture()
        original_feedback, original_patch = deepcopy(feedback), deepcopy(patch)
        expected = deepcopy(feedback["draft"])
        expected["objects"][0]["sources"][0] = reference
        expected["quality"]["temporal_claims"][0]["source_refs"][0] = reference
        rebuilt = materialize_patch(patch, feedback)
        self.assertEqual(rebuilt, expected)
        self.assertEqual(feedback, original_feedback)
        self.assertEqual(patch, original_patch)
        rebuilt["objects"][0]["body"] = "不得反向污染原稿"
        self.assertEqual(feedback, original_feedback)

    def test_missing_duplicate_extra_and_non_reference_paths_are_rejected(self):
        feedback, patch, reference = fixture()
        cases = [
            {"reference_repairs": patch["reference_repairs"][:1]},
            {"reference_repairs": [patch["reference_repairs"][0]] * 2},
            {"reference_repairs": patch["reference_repairs"] + [patch["reference_repairs"][0]]},
            {"reference_repairs": [{"field_path": "/objects/0/body", "reference": reference},
                                    patch["reference_repairs"][1]]},
            {**patch, "objects": []},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(CaptureError):
                materialize_patch(value, feedback)

    def test_context_binds_latest_draft_and_every_issue(self):
        for kind in ("body", "issues", "paths", "generations"):
            feedback, patch, _ = fixture()
            if kind == "body":
                feedback["draft"]["objects"][0]["body"] += "被篡改"
            elif kind == "issues":
                feedback["validation_error"]["details"]["issues"][0]["locator"] = "L9-L9"
            elif kind == "paths":
                feedback["repair_context"]["field_paths"].reverse()
            else:
                feedback["generation_invocations"] = ["model-unrelated"]
            with self.subTest(kind=kind), self.assertRaises(CaptureError) as raised:
                materialize_patch(patch, feedback)
            self.assertEqual(raised.exception.code, "reference_repair_context_changed")

    def test_mixed_content_faults_or_truncation_cannot_select_reference_only_mode(self):
        for code in ("quality_temporal_missing", "invalid_domains", "related_records_omitted"):
            feedback, _, _ = fixture()
            feedback["validation_error"]["details"]["issues"].append({"code": code, "field_path": "/objects/0/body"})
            self.assertIsNone(build_context(feedback))
        feedback, _, _ = fixture()
        feedback["validation_error"]["details"]["issues_truncated"] = True
        self.assertIsNone(build_context(feedback))
        feedback, _, _ = fixture()
        feedback["draft"]["quality"]["coverage_disposition"] = [{"target_id": "artificial-unfinished",
            "status": "deferred", "record_ids": [], "body_evidence": [], "reason": "真实缺少必要事实，不能编造内容"}]
        self.assertIsNone(build_context(feedback))

    def test_bad_repaired_quote_is_not_normalized_and_still_faces_existing_validator(self):
        feedback, patch, _ = fixture()
        patch["reference_repairs"][0]["reference"]["quote"] = "  人工来源正文。"
        rebuilt = materialize_patch(patch, feedback)
        self.assertEqual(rebuilt["objects"][0]["sources"][0]["quote"], "  人工来源正文。")
        with self.assertRaises(CaptureError) as raised:
            _refs(rebuilt["objects"][0]["sources"], [{"source_id": "src-artificial",
                  "source_version": "sha256-artificial", "body": "人工来源正文。\n"}])
        self.assertEqual(raised.exception.code, "invalid_quote")

    def test_new_output_schema_excludes_whole_draft_and_unauthorized_paths(self):
        feedback, patch, _ = fixture()
        payload = {"skill_bundle": resolve_skill_refs(), "quality_contract": "skill-quality-v1",
                   "quality_context": {"sources": [{"source_id": "src-artificial", "source_version": "sha256-artificial"}]},
                   "extraction_feedback": feedback}
        schema = output_schema("extract", payload)
        self.assertEqual(schema["required"], ["reference_repairs"])
        self.assertTrue(_valid(patch, schema))
        bad = deepcopy(patch)
        bad["reference_repairs"][0]["field_path"] = "/objects/0/body"
        self.assertFalse(_valid(bad, schema))
        bad = deepcopy(patch)
        bad["reference_repairs"][0]["reference"]["source_id"] = "src-unprovided"
        self.assertFalse(_valid(bad, schema))
        payload["skill_bundle"] = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        with self.assertRaises(ValueError):
            output_schema("extract", payload)

    def test_only_new_feedback_view_offers_narrow_patch(self):
        feedback, _, _ = fixture()
        diagnostic = {"response": {"output": feedback["draft"]},
                      "validation_context": {"generation_invocations": feedback["generation_invocations"]},
                      "validation_error": feedback["validation_error"]}
        legacy = _model_view(diagnostic, feedback["source_run_id"], complete=True)
        self.assertNotIn("repair_context", legacy)
        current = _model_view(diagnostic, feedback["source_run_id"], complete=True, stable=True)
        self.assertEqual(current["repair_context"], feedback["repair_context"])


if __name__ == "__main__":
    unittest.main()
