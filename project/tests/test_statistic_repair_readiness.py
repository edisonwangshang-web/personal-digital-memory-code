"""Synthetic Review evidence distinguishes a local patch from unfinished work."""

from copy import deepcopy
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import build_review_checks
from pdm.skill_support import resolve_skill_refs
from pdm.statistic_repairs import (_hash, build_repair_context, materialize_patch,
                                   verify_context, CONTRACT, CONTRACT_V2)
import test_statistic_repairs as legacy_fixtures
from quality_fixtures import passed_review


class StatisticRepairReadinessTests(unittest.TestCase):
    def setUp(self):
        self.f = legacy_fixtures.StatisticRepairsTests()
        self.f.setUp()
        self.f.bundle = resolve_skill_refs("knowledge-extract@13", "knowledge-review@11")
        self.missing = {**self.f.missing, "body_evidence": "扫描2行"}

    def revision(self, *, deferred=False, draft=False, reason=None, excerpt=None):
        output = deepcopy(self.f.output)
        if deferred:
            source = self.f.sources[1]["metadata"]["source_id"]
            targets = {target["target_id"] for target in self.f.quality["targets"]
                       if target["source_id"] == source}
            for row in output["quality"]["coverage_disposition"]:
                if row["target_id"] in targets:
                    row.update(status="deferred", record_ids=[], body_evidence=[],
                               reason=reason or "原文已经提供，但本次尚未完成该表资料的知识整理。")
        missing = {**self.missing}
        if excerpt is not None:
            missing["body_evidence"] = excerpt
        revision = self.f.make_revision(missing=missing, output=output, decomposed=True)
        candidate = revision["candidate"]
        candidate["batch_material"] = {"draft_only": draft}
        candidate["binding"] = _hash({key: value for key, value in candidate.items() if key != "binding"})
        # Adding the phase marker changes the predeclared checks. Rebuild the
        # synthetic Review rather than reusing an incompatible passed envelope.
        from pdm.decomposed_review import _layout
        units, _ = _layout(candidate, {"batch_context": candidate["batch_context"]})
        unit = next(unit for unit in units if unit["kind"] == "object"
                    and unit["object_paths"] == ["memory/records/table-0.md"])
        checks = build_review_checks(candidate, self.f.quality, scope=unit)
        output = passed_review(checks)
        check = next(check for check in checks if check["kind"] == "object_statistics_time")
        next(result for result in output["checks"] if result["check_id"] == check["check_id"]).update(
            result="needs_revision", reason="The provided correct text only lacks the specified statistical mapping.")
        output.update(verdict="needs_revision", repair_scope={"kind": "statistic_bindings",
                      "missing_bindings": [missing]})
        review = {"binding": candidate["binding"], "output": output,
                  "decomposed_proof": {"nodes": [{"unit_id": unit["unit_id"], "kind": "object",
                                                   "status": "needs_revision"}]}}
        revision.update(schema_version="0.20", candidate_binding=candidate["binding"],
                        review_record=review, review_output=deepcopy(output))
        revision["stage"]["sha256"] = _hash(candidate)
        revision["review"]["sha256"] = _hash(review)
        revision["binding"] = _hash({key: value for key, value in revision.items()
                                      if key not in {"binding", "model_context"}})
        return revision

    def context(self, revision):
        return build_repair_context(revision, self.f.quality, sources=self.f.sources)

    def materialize(self, context, *, excerpt="扫描2行"):
        return materialize_patch({"statistic_bindings": [{**self.missing, "body_evidence": excerpt}]},
            context, self.f.quality, self.f.sources, None, self.f.output, frozen_objects=self.f.frozen)

    def test_complete_candidate_allows_the_early_local_statistic_patch(self):
        revision = self.revision()
        before = deepcopy(revision), deepcopy(self.f.output), deepcopy(self.f.frozen)
        context = self.context(revision)
        self.assertEqual(context["contract"], CONTRACT_V2)
        self.assertEqual(context["schema_version"], "0.20")
        self.assertEqual(context["missing_bindings"], [self.missing])
        self.assertEqual(verify_context(context, revision, self.f.quality, sources=self.f.sources), context)
        result = self.materialize(context)
        self.assertEqual(result["objects"], self.f.output["objects"])
        self.assertEqual(result["wikis"], self.f.output["wikis"])
        self.assertEqual(result["quality"]["coverage_disposition"],
                         self.f.output["quality"]["coverage_disposition"])
        self.assertEqual(result["quality"]["statistic_claims"][0], {
            "stat_id": self.missing["stat_id"], "record_id": "table-0", "value": "2",
            "body_evidence": "扫描2行", "sources": [{
                "source_id": self.f.sources[0]["metadata"]["source_id"],
                "source_version": self.f.sources[0]["metadata"]["source_version"],
                "locator": "L2-L2", "quote": "2030-01-01,10"}]})
        self.assertEqual((revision, self.f.output, self.f.frozen), before)

    def test_unfinished_final_candidate_does_not_freeze_the_whole_draft_for_a_local_patch(self):
        revision = self.revision(deferred=True)
        before = deepcopy(revision)
        self.assertIsNone(self.context(revision))
        self.assertEqual(revision, before)
        self.assertEqual(revision["review_output"]["repair_scope"]["kind"], "statistic_bindings")

    def test_a_genuine_gap_keeps_the_existing_content_or_confirmation_path(self):
        revision = self.revision(deferred=True,
            reason="必要统计含义在所提供原文中未明确，需要用户确认实际指标定义。")
        self.assertIsNone(self.context(revision))
        self.assertEqual(revision["review_output"]["verdict"], "needs_revision")
        # Returning None does not fabricate a needs_confirmation verdict or
        # fill unavailable evidence. The existing semantic path decides that.
        self.assertTrue(any(row["status"] == "deferred"
                            for row in revision["candidate"]["extraction_output"]["quality"]["coverage_disposition"]))

    def test_a_partial_draft_keeps_its_local_repair_boundary(self):
        context = self.context(self.revision(deferred=True, draft=True))
        self.assertEqual(context["contract"], CONTRACT_V2)
        self.assertEqual(context["missing_bindings"], [self.missing])

    def test_patch_cannot_substitute_a_different_even_valid_explanation(self):
        context = self.context(self.revision())
        with self.assertRaises(CaptureError) as error:
            self.materialize(context, excerpt="人工观测表扫描2行，非空2行")
        self.assertEqual(error.exception.code, "statistic_repair_review_body_mismatch")

    def test_review_anchor_must_be_actual_correct_frozen_body_evidence(self):
        for excerpt in ("不存在的扫描2行", "空白0行"):
            with self.subTest(excerpt=excerpt), self.assertRaises(CaptureError):
                self.context(self.revision(excerpt=excerpt))

    def test_the_review_anchor_is_bound_against_local_rehashing(self):
        revision = self.revision()
        context = self.context(revision)
        context["missing_bindings"][0]["body_evidence"] = "非空2行"
        context["binding"] = _hash({key: value for key, value in context.items() if key != "binding"})
        with self.assertRaises(CaptureError) as error:
            verify_context(context, revision, self.f.quality, sources=self.f.sources)
        self.assertEqual(error.exception.code, "statistic_repair_context_changed")

    def test_new_review_cannot_omit_the_body_anchor(self):
        revision = self.revision()
        for output in (revision["review_output"], revision["review_record"]["output"]):
            del output["repair_scope"]["missing_bindings"][0]["body_evidence"]
        revision["binding"] = _hash({key: value for key, value in revision.items()
                                      if key not in {"binding", "model_context"}})
        with self.assertRaises(CaptureError):
            self.context(revision)

    def test_a_content_rejection_keeps_the_existing_full_output_path(self):
        revision = self.revision()
        for output in (revision["review_output"], revision["review_record"]["output"]):
            output["repair_scope"] = {"kind": "content", "missing_bindings": []}
        revision["review"]["sha256"] = _hash(revision["review_record"])
        revision["binding"] = _hash({key: value for key, value in revision.items()
                                      if key not in {"binding", "model_context"}})
        self.assertIsNone(self.context(revision))

    def test_equal_values_and_body_text_do_not_allow_cross_source_bindings(self):
        revision = self.revision()
        for output in (revision["review_output"], revision["review_record"]["output"]):
            output["repair_scope"]["missing_bindings"][0]["stat_id"] = self.f.stat(1, "row_count")["stat_id"]
        revision["review"]["sha256"] = _hash(revision["review_record"])
        revision["binding"] = _hash({key: value for key, value in revision.items()
                                      if key not in {"binding", "model_context"}})
        with self.assertRaises(CaptureError):
            self.context(revision)

    def test_legacy_contract_and_early_repair_remain_unchanged(self):
        output = deepcopy(self.f.output)
        source = self.f.sources[1]["metadata"]["source_id"]
        targets = {target["target_id"] for target in self.f.quality["targets"] if target["source_id"] == source}
        for row in output["quality"]["coverage_disposition"]:
            if row["target_id"] in targets:
                row.update(status="deferred", record_ids=[], body_evidence=[],
                           reason="旧合同保留原局部审核及草稿处理方式，不按新规则迁移。")
        for refs in (("knowledge-extract@11", "knowledge-review@9"),
                     ("knowledge-extract@12", "knowledge-review@10")):
            with self.subTest(refs=refs):
                self.f.bundle = resolve_skill_refs(*refs)
                context = self.context(self.f.make_revision(output=output, decomposed=True))
                self.assertEqual(context["contract"], CONTRACT)
                self.assertEqual(context["schema_version"], "0.15")
                self.assertEqual(context["missing_bindings"], [self.f.missing])


if __name__ == "__main__":
    unittest.main()
