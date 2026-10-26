"""Artificial tables exercise deterministic mapping repair without model calls."""

from copy import deepcopy
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import build_review_checks, prepare_quality, resolve_statistic
from pdm.skill_support import resolve_skill_refs
from pdm.statistic_repairs import (_hash, build_repair_context, is_enabled,
                                   materialize_patch, verify_context)
from quality_fixtures import add_quality, passed_review


class StatisticRepairsTests(unittest.TestCase):
    def setUp(self):
        self.sources = [{"path": "artificial/table-" + str(index) + ".md",
                         "metadata": {"source_id": "src-" + str(index) * 64,
                                      "source_version": "sha256-" + str(index + 2) * 64,
                                      "source_type": "note", "original_time": None},
                         "body": "date,steps\n2030-01-01,10\n2030-01-02,20\n"}
                        for index in range(2)]
        self.quality = prepare_quality(self.sources)
        self.bundle = resolve_skill_refs("knowledge-extract@11", "knowledge-review@9")
        records, frozen = [], []
        for index, source in enumerate(self.sources):
            rid = "table-" + str(index)
            ref = {"source_id": source["metadata"]["source_id"],
                   "source_version": source["metadata"]["source_version"],
                   "locator": "L1-L3", "quote": source["body"].splitlines()[0]}
            raw = {"id": rid, "title": "人工观测表", "type": "knowledge", "source_kind": "external_source",
                   "domains": ["learning"], "sources": [ref], "valid_from": None, "valid_to": None,
                   "verification_status": "unverified", "lifecycle_status": "undetermined",
                   "body": "人工观测表扫描2行，非空2行，空白0行。"}
            records.append(raw)
            frozen.append({"path": "memory/records/" + rid + ".md", "body": raw["body"],
                           "metadata": {key: deepcopy(value) for key, value in raw.items() if key != "body"}
                                       | {"content_version": _hash(raw)}})
        self.output = add_quality({"objects": records, "wikis": [], "reuse_refs": []}, self.quality)
        self.frozen = frozen
        self.missing = {"stat_id": self.stat(0, "row_count")["stat_id"], "record_id": "table-0"}
        self.revision = self.make_revision()

    def stat(self, source_index, operation):
        identity = self.sources[source_index]["metadata"]["source_id"]
        return next(stat for stat in self.quality["statistics"]
                    if stat["operation"] == operation
                    and resolve_statistic(self.quality, stat)["source_id"] == identity)

    def make_revision(self, *, missing=None, output=None, frozen=None, check_kind="object_statistics_time",
                      decomposed=False, failed_record_id=None):
        pair = deepcopy(self.missing if missing is None else missing)
        candidate = {"run_id": "run-" + "1" * 32, "quality_contract": "skill-quality-v1",
                     "skill_bundle": self.bundle, "quality_context": deepcopy(self.quality),
                     "extraction_output": deepcopy(self.output if output is None else output),
                     "objects": deepcopy(self.frozen if frozen is None else frozen), "controls_digest": _hash([])}
        unit = None
        if decomposed:
            from pdm.decomposed_review import _layout
            candidate["batch_context"] = {"plan_id": "artificial-plan", "batch_id": "artificial-batch",
                "source_ranges": [{"path": source["path"], "source_id": source["source_id"],
                    "source_version": source["source_version"], "locator": locator,
                    "fragment_id": fragment["fragment_id"]}
                    for source in self.quality["sources"] for fragment in source["fragments"]
                    for locator in fragment["locators"]], "parent_batch_ids": []}
            units, _ = _layout(candidate, {"batch_context": candidate["batch_context"]})
            unit = next(scope for scope in units if scope["kind"] == "object"
                        and scope["object_paths"] == ["memory/records/table-0.md"])
        candidate["binding"] = _hash(candidate)
        checks = build_review_checks(candidate, self.quality, scope=unit)
        review_output = passed_review(checks)
        failed_path = "memory/records/" + (failed_record_id or ("table-0" if decomposed else pair["record_id"])) + ".md"
        bad = next(check for check in checks if check["kind"] == check_kind and failed_path in check["object_paths"])
        next(check for check in review_output["checks"] if check["check_id"] == bad["check_id"]).update(
            result="needs_revision", reason="The frozen text needs the explicitly listed missing statistic binding.")
        review_output.update(verdict="needs_revision", repair_scope={"kind": "statistic_bindings",
            "missing_bindings": [pair]})
        record = {"binding": candidate["binding"], "output": review_output}
        if unit is not None:
            record["decomposed_proof"] = {"nodes": [{"unit_id": unit["unit_id"], "kind": unit["kind"],
                                                      "status": "needs_revision"}]}
        context = {"schema_version": "0.15", "contract": "review-feedback-v1",
                   "source_run_id": candidate["run_id"], "candidate_binding": candidate["binding"],
                   "candidate": candidate, "review_record": record, "review_output": deepcopy(review_output),
                   "controls_digest": candidate["controls_digest"],
                   "source_bindings": [deepcopy(source["metadata"]) for source in self.sources],
                   "stage": {"path": "artificial/stage.md", "commit_sha": "a" * 40, "sha256": _hash(candidate)},
                   "review": {"path": "artificial/review.md", "commit_sha": "b" * 40, "sha256": _hash(record)}}
        context["binding"] = _hash(context)
        return context

    def context(self, revision=None, **options):
        return build_repair_context(self.revision if revision is None else revision, self.quality,
                                    sources=self.sources, **options)

    def patch(self, *, pair=None, excerpt="扫描2行"):
        return {"statistic_bindings": [{**(self.missing if pair is None else pair), "body_evidence": excerpt}]}

    def materialize(self, patch=None, context=None, **options):
        return materialize_patch(self.patch() if patch is None else patch,
            self.context() if context is None else context, self.quality, self.sources, None,
            options.pop("original_output", self.output),
            frozen_objects=options.pop("frozen_objects", self.frozen), **options)

    def expect_error(self, code, callback):
        with self.assertRaises(CaptureError) as error:
            callback()
        self.assertEqual(error.exception.code, code)

    def test_materialization_preserves_frozen_text_and_uses_the_exact_root_statistic(self):
        before = deepcopy(self.output), deepcopy(self.frozen), deepcopy(self.sources)
        result = self.materialize()
        self.assertEqual(result["objects"], self.output["objects"])
        self.assertEqual(result["wikis"], self.output["wikis"])
        self.assertEqual(result["reuse_refs"], self.output["reuse_refs"])
        self.assertEqual(result["quality"]["coverage_disposition"], self.output["quality"]["coverage_disposition"])
        self.assertEqual(result["quality"]["temporal_claims"], [])
        claim = result["quality"]["statistic_claims"][0]
        self.assertEqual((claim["stat_id"], claim["value"], claim["record_id"]),
                         (self.missing["stat_id"], "2", "table-0"))
        self.assertEqual(claim["sources"], [{"source_id": self.sources[0]["metadata"]["source_id"],
            "source_version": self.sources[0]["metadata"]["source_version"],
            "locator": "L2-L2", "quote": "2030-01-01,10"}])
        self.assertEqual((self.output, self.frozen, self.sources), before)
        context = self.context()
        self.assertNotIn("objects", context)
        self.assertNotIn("quality", context)
        self.assertNotIn("extraction_output", context)

    def test_existing_claims_are_not_changed_or_repaired_again(self):
        zero = self.stat(0, "zero_count")
        original = deepcopy(self.output)
        original["quality"]["statistic_claims"].append({"stat_id": zero["stat_id"], "record_id": "table-0",
            "value": "0", "body_evidence": "空白0行", "sources": [{
                "source_id": self.sources[0]["metadata"]["source_id"],
                "source_version": self.sources[0]["metadata"]["source_version"],
                "locator": "L2-L2", "quote": "2030-01-01,10"}]})
        context = self.context(self.make_revision(output=original))
        result = self.materialize(context=context, original_output=original)
        self.assertEqual(result["quality"]["statistic_claims"][:-1], original["quality"]["statistic_claims"])
        duplicate = self.make_revision(output=original,
            missing={"stat_id": zero["stat_id"], "record_id": "table-0"})
        self.expect_error("statistic_repair_scope_mismatch", lambda: self.context(duplicate))

    def test_equal_numeric_values_do_not_allow_a_different_source_or_record_mapping(self):
        other = {"stat_id": self.stat(1, "row_count")["stat_id"], "record_id": "table-0"}
        self.assertNotEqual(other["stat_id"], self.missing["stat_id"])
        self.expect_error("statistic_repair_scope_mismatch", lambda: self.context(self.make_revision(missing=other)))
        self.expect_error("statistic_repair_scope_mismatch", lambda: self.materialize(self.patch(pair=other)))

    def test_patch_requires_exact_requested_pairs_and_original_body_excerpts(self):
        for patch in ({"statistic_bindings": []}, {"statistic_bindings": [{**self.missing, "body_evidence": "扫描2行"},
                         {"stat_id": self.stat(0, "present_count")["stat_id"], "record_id": "table-0", "body_evidence": "非空2行"}]}):
            self.expect_error("statistic_repair_scope_mismatch", lambda: self.materialize(patch))
        duplicate = {"statistic_bindings": self.patch()["statistic_bindings"] * 2}
        self.expect_error("statistic_repair_duplicate", lambda: self.materialize(duplicate))
        self.expect_error("statistic_repair_body_mismatch", lambda: self.materialize(self.patch(excerpt="改写后的正文2行")))
        self.expect_error("quality_statistic_mismatch", lambda: self.materialize(self.patch(excerpt="空白0行")))
        illegal = {"statistic_bindings": [{**self.patch()["statistic_bindings"][0], "value": "2"}]}
        self.expect_error("invalid_statistic_repair_patch", lambda: self.materialize(illegal))

    def test_source_control_quality_and_frozen_changes_are_rejected(self):
        context = self.context()
        changed_source = deepcopy(self.sources)
        changed_source[0]["body"] = changed_source[0]["body"].replace(",10", ",11")
        self.expect_error("statistic_repair_context_changed", lambda: materialize_patch(
            self.patch(), context, self.quality, changed_source, None, self.output, frozen_objects=self.frozen))
        self.expect_error("statistic_repair_context_changed", lambda: self.materialize(
            context=context, controls_digest="sha256-" + "f" * 64))
        for field in ("body", "sources", "content_version"):
            frozen = deepcopy(self.frozen)
            if field == "body":
                frozen[0]["body"] += " extra text"
            elif field == "sources":
                frozen[0]["metadata"]["sources"][0]["locator"] = "L1-L1"
            else:
                frozen[0]["metadata"]["content_version"] = "sha256-" + "e" * 64
            self.expect_error("statistic_repair_frozen_changed", lambda: self.materialize(context=context, frozen_objects=frozen))
        changed_output = deepcopy(self.output)
        changed_output["objects"][0]["body"] += " extra text"
        self.expect_error("statistic_repair_context_changed", lambda: self.materialize(
            context=context, original_output=changed_output))

    def test_rehashed_context_cannot_expand_the_original_review_instruction(self):
        context = self.context()
        context["missing_bindings"].append({"stat_id": self.stat(1, "row_count")["stat_id"], "record_id": "table-1"})
        context["binding"] = _hash({key: value for key, value in context.items() if key != "binding"})
        self.expect_error("statistic_repair_context_changed", lambda: verify_context(
            context, self.revision, self.quality, sources=self.sources))

    def test_one_example_day_cannot_support_a_statistic_for_the_whole_sample(self):
        output, frozen = deepcopy(self.output), deepcopy(self.frozen)
        short = {**output["objects"][0]["sources"][0], "locator": "L2-L2", "quote": "2030-01-01,10"}
        output["objects"][0]["sources"] = [short]
        frozen[0]["metadata"]["sources"] = [deepcopy(short)]
        frozen[0]["metadata"]["content_version"] = _hash(output["objects"][0])
        incomplete = self.make_revision(output=output, frozen=frozen)
        self.expect_error("quality_source_reference_mismatch", lambda: self.context(incomplete))
        # Even a fabricated, self-consistent local metadata envelope cannot
        # turn the first in-range line into proof of the rest of the sample.
        context = self.context()
        context["original_output_sha256"] = _hash(output)
        context["frozen_objects_sha256"] = _hash(frozen)
        context["record_bindings"][0].update(content_version=frozen[0]["metadata"]["content_version"],
            sources_sha256=_hash(frozen[0]["metadata"]["sources"]))
        context["binding"] = _hash({key: value for key, value in context.items() if key != "binding"})
        self.expect_error("quality_source_reference_mismatch", lambda: self.materialize(
            context=context, original_output=output, frozen_objects=frozen))

    def test_reused_record_body_can_be_repaired_without_expanding_the_raw_output(self):
        original = deepcopy(self.output)
        original["objects"].pop()
        original["reuse_refs"] = [{"path": self.frozen[1]["path"],
                                    "content_version": self.frozen[1]["metadata"]["content_version"]}]
        pair = {"stat_id": self.stat(1, "row_count")["stat_id"], "record_id": "table-1"}
        context = self.context(self.make_revision(output=original, missing=pair))
        result = self.materialize(self.patch(pair=pair), context=context, original_output=original)
        self.assertEqual(result["objects"], original["objects"])
        self.assertEqual(result["reuse_refs"], original["reuse_refs"])
        self.assertEqual(result["quality"]["statistic_claims"][0]["record_id"], "table-1")

    def test_decomposed_feedback_cannot_repair_a_record_or_statistic_outside_its_actual_unit(self):
        local = self.make_revision(decomposed=True)
        context = self.context(local)
        result = self.materialize(context=context)
        self.assertEqual(result["quality"]["statistic_claims"][0]["record_id"], "table-0")
        # This pair is valid in the complete candidate, but the failed object
        # unit received only table-0 and never reviewed table-1's source/stat.
        outside = {"stat_id": self.stat(1, "row_count")["stat_id"], "record_id": "table-1"}
        rejected = self.make_revision(decomposed=True, missing=outside)
        self.expect_error("statistic_repair_scope_mismatch", lambda: self.context(rejected))

    def test_a_different_records_failed_check_keeps_the_full_output_repair_path(self):
        pair = {"stat_id": self.stat(1, "row_count")["stat_id"], "record_id": "table-1"}
        wrong_check = self.make_revision(missing=pair, failed_record_id="table-0")
        self.assertIsNone(self.context(wrong_check))

    def test_non_statistical_or_legacy_repairs_keep_the_normal_full_output_path(self):
        self.assertTrue(is_enabled(self.bundle))
        self.assertFalse(is_enabled(resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")))
        revised = deepcopy(self.revision)
        revised["candidate"]["skill_bundle"] = resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")
        self.assertIsNone(build_repair_context(revised, self.quality))
        context = deepcopy(self.revision)
        for key in ("review_record", "review_output"):
            output = context[key]["output"] if key == "review_record" else context[key]
            output["repair_scope"] = {"kind": "content", "missing_bindings": []}
        context["binding"] = _hash({key: value for key, value in context.items() if key not in {"binding", "model_context"}})
        self.assertIsNone(self.context(context))
        for key in ("review_record", "review_output"):
            output = context[key]["output"] if key == "review_record" else context[key]
            output["repair_scope"] = {"kind": "none", "missing_bindings": []}
        context["binding"] = _hash({key: value for key, value in context.items() if key not in {"binding", "model_context"}})
        self.assertIsNone(self.context(context))
        self.expect_error("invalid_review_repair_scope", lambda: self.context(
            self.make_revision(check_kind="object_fidelity")))


if __name__ == "__main__":
    unittest.main()
