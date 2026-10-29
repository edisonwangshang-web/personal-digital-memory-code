"""Synthetic-only BOM policy crosses model projections and reviewed patches."""

from copy import deepcopy
import hashlib
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import (build_review_checks, prepare_quality,
                                   validate_quality_review)
from pdm.quality_projection import project_quality_payload, restore_quality_payload
from pdm.skill_support import resolve_skill_refs
from pdm.statistic_repairs import (CONTRACT_V2, _hash, build_repair_context,
                                   materialize_patch, verify_context)
from quality_fixtures import add_quality, passed_review
import test_statistic_repairs as repair_fixtures
from test_knowledge_quality import source


BODY = '\ufeff"date","steps"\n"2030-01-01","10"\n"2030-01-02","20"\n'


def full_payload(bundle):
    item = source(BODY)
    return {"sources": [item], "quality_contract": "skill-quality-v1",
            "skill_bundle": bundle,
            "quality_context": prepare_quality([item], skill_bundle=bundle),
            "objects": [{"body": "明确标记的人工样例正文。"}]}


def repair_fixture(bundle):
    # Reuse the existing pure calculation fixture's genuine check/binding
    # construction, with actual BOM CSV and new immutable policy material.
    fixture = repair_fixtures.StatisticRepairsTests()
    fixture.setUp()
    fixture.bundle = bundle
    for item in fixture.sources:
        item["body"] = BODY
        item["metadata"]["source_version"] = "sha256-" + hashlib.sha256(BODY.encode()).hexdigest()
    fixture.quality = prepare_quality(fixture.sources, skill_bundle=bundle)
    records, frozen = [], []
    for index, item in enumerate(fixture.sources):
        record = deepcopy(fixture.output["objects"][index])
        record["sources"] = [{"source_id": item["metadata"]["source_id"],
                              "source_version": item["metadata"]["source_version"],
                              "locator": "L1-L3", "quote": BODY.splitlines()[0]}]
        records.append(record)
        frozen.append({"path": "memory/records/" + record["id"] + ".md",
                       "body": record["body"],
                       "metadata": {key: deepcopy(value) for key, value in record.items() if key != "body"}
                                   | {"content_version": _hash(record)}})
    fixture.output = add_quality({"objects": records, "wikis": [], "reuse_refs": []}, fixture.quality)
    fixture.frozen = frozen
    fixture.missing = {"stat_id": fixture.stat(0, "row_count")["stat_id"],
                       "record_id": "table-0", "body_evidence": "扫描2行"}
    return fixture


class Dev37PolicyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.current = resolve_skill_refs("knowledge-extract@14", "knowledge-review@12")
        cls.previous = resolve_skill_refs("knowledge-extract@13", "knowledge-review@11")

    def test_projection_roundtrip_rebuilds_new_calendar_and_unchanged_quoted_evidence(self):
        original = full_payload(self.current)
        frozen = deepcopy(original)
        projected = project_quality_payload(original)
        self.assertEqual("source-quality-v2", projected["quality_context"]["policy_version"])
        profile = projected["quality_context"]["sources"][0]
        self.assertNotIn("line_sha256", profile)
        self.assertEqual(3, profile["line_integrity"]["line_count"])
        self.assertEqual(BODY, projected["sources"][0]["body"])
        self.assertEqual(["2030-01-01", "2030-01-02"], profile["tables"][0]["date_interval"])
        self.assertEqual(original, restore_quality_payload(projected))
        self.assertEqual(frozen, original)

    def test_nested_historical_context_keeps_its_old_computation_in_a_new_request(self):
        old, new = full_payload(self.previous), full_payload(self.current)
        new["revision_context"] = {"candidate": {"quality_contract": "skill-quality-v1",
            "quality_context": deepcopy(old["quality_context"]), "objects": deepcopy(old["objects"])}}
        projected = project_quality_payload(new)
        context = projected["revision_context"]["candidate"]["quality_context"]
        self.assertEqual("source-quality-v1", context["policy_version"])
        self.assertIsNone(context["sources"][0]["tables"][0]["date_interval"])
        self.assertEqual(['"date"', "steps"], context["sources"][0]["tables"][0]["headers"])
        self.assertEqual(new, restore_quality_payload(projected))

    def test_policy_substitution_cannot_relabel_old_or_new_context(self):
        for bundle, policy in ((self.previous, "source-quality-v2"),
                               (self.current, "source-quality-v1")):
            original = full_payload(bundle)
            original["quality_context"]["policy_version"] = policy
            with self.subTest(policy=policy), self.assertRaises(CaptureError):
                project_quality_payload(original)
        projected = project_quality_payload(full_payload(self.current))
        projected["quality_context"]["policy_version"] = "source-quality-v1"
        with self.assertRaises(CaptureError):
            restore_quality_payload(projected)

    def test_new_policy_reviewed_statistic_patch_rebuilds_exact_source_scope(self):
        fixture = repair_fixture(self.current)
        revision = fixture.make_revision(missing=fixture.missing, decomposed=True)
        before = deepcopy((revision, fixture.output, fixture.sources))
        repair = build_repair_context(revision, fixture.quality, sources=fixture.sources)
        self.assertEqual(CONTRACT_V2, repair["contract"])
        self.assertEqual([fixture.missing], repair["missing_bindings"])
        self.assertEqual(repair, verify_context(repair, revision, fixture.quality, sources=fixture.sources))
        result = materialize_patch({"statistic_bindings": [fixture.missing]}, repair,
            fixture.quality, fixture.sources, None, fixture.output, frozen_objects=fixture.frozen)
        self.assertEqual(fixture.output["objects"], result["objects"])
        self.assertEqual(fixture.output["wikis"], result["wikis"])
        self.assertEqual(fixture.output["quality"]["coverage_disposition"],
                         result["quality"]["coverage_disposition"])
        claim = result["quality"]["statistic_claims"][0]
        self.assertEqual((fixture.missing["stat_id"], "2", "table-0"),
                         (claim["stat_id"], claim["value"], claim["record_id"]))
        self.assertEqual('"2030-01-01","10"', claim["sources"][0]["quote"])
        self.assertEqual("L2-L2", claim["sources"][0]["locator"])
        self.assertEqual(before, (revision, fixture.output, fixture.sources))

    def test_old_policy_cannot_freeze_statistic_patch_after_calendar_policy_change(self):
        fixture = repair_fixture(self.previous)
        revision = fixture.make_revision(missing=fixture.missing, decomposed=True)
        new_quality = prepare_quality(fixture.sources, skill_bundle=self.current)
        before = deepcopy(revision)
        self.assertIsNone(build_repair_context(revision, new_quality, sources=fixture.sources))
        self.assertEqual(before, revision)

    def test_new_policy_patch_rejects_source_or_policy_tampering(self):
        fixture = repair_fixture(self.current)
        revision = fixture.make_revision(missing=fixture.missing, decomposed=True)
        repair = build_repair_context(revision, fixture.quality, sources=fixture.sources)
        for change in ("source-bom", "policy"):
            sources, quality, scope = deepcopy(fixture.sources), deepcopy(fixture.quality), deepcopy(repair)
            if change == "source-bom":
                sources[0]["body"] = sources[0]["body"].removeprefix("\ufeff")
            else:
                quality["policy_version"] = "source-quality-v1"
                scope["quality_context_sha256"] = _hash(quality)
                scope["binding"] = _hash({key: value for key, value in scope.items() if key != "binding"})
            with self.subTest(change=change), self.assertRaises(CaptureError):
                materialize_patch({"statistic_bindings": [fixture.missing]}, scope,
                    quality, sources, None, fixture.output, frozen_objects=fixture.frozen)

    def test_new_review_cannot_pass_a_final_unfinished_target(self):
        fixture = repair_fixture(self.current)
        revision = fixture.make_revision(missing=fixture.missing)
        candidate = revision["candidate"]
        row = candidate["extraction_output"]["quality"]["coverage_disposition"][0]
        row.update(status="deferred", record_ids=[], body_evidence=[],
                   reason="资料已提供，但人工测试候选还没有整理该项。")
        checks = build_review_checks(candidate, fixture.quality)
        unfinished = [check for check in checks if "allowed_results" in check]
        self.assertEqual({"metric_coverage", "global_organization"},
                         {check["kind"] for check in unfinished})
        with self.assertRaises(CaptureError) as error:
            validate_quality_review(passed_review(checks), checks)
        self.assertEqual("quality_review_incomplete", error.exception.code)


if __name__ == "__main__":
    unittest.main()
