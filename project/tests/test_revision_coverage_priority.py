"""Current-draft coverage repair and early Review use artificial sources only."""

from copy import deepcopy
import hashlib
import unittest

import test_decomposed_review as legacy
import test_decomposed_source_material as material_fixtures
import test_knowledge_quality as fixtures
from quality_fixtures import add_quality, passed_review
from pdm.contracts import CaptureError, quality_schema_version
from pdm.decomposed_review import (
    REQUEST_CONTRACT, _layout, _request, _source_layout,
)
from pdm.knowledge_quality import prepare_quality
from pdm.model_calls import ModelCallError, render_model_request
from pdm.pipeline import _candidate_binding, _document
from pdm.revision_context import revision_coverage_regressions
from pdm.skill_support import resolve_skill_refs


def revision_material(pair=("knowledge-extract@13", "knowledge-review@11")):
    """Eight prior objects cover 67 targets; five retained objects omit 28."""
    def table(count):
        return "date," + ",".join("observation_" + str(index) for index in range(count)) + "\n" + "\n".join(
            f"2025-01-0{day}," + ",".join(str(day + index) for index in range(count))
            for day in (1, 2)) + "\n"
    bodies = [table(39), table(26), "Artificial dictionary qualifies the new work observations.\n",
              "Artificial learning note retains its explicitly unresolved condition.\n"]
    sources = [fixtures.source(body, "revision-priority-" + str(index)) for index, body in enumerate(bodies)]
    views = [fixtures.view(source, [(f"revision-fragment-{index}-{line}", line, line)
                                   for line in range(1, len(source["body"].splitlines()) + 1)])
             for index, source in enumerate(sources)]
    context = prepare_quality(sources, source_views=views)
    old_records = [{"id": "old-observation-" + str(index), "body": "Artificial old bounded summary " + str(index),
                    "sources": [fixtures.ref(sources[0])]} for index in range(4)]
    new_records = [{"id": "new-observations", "body": "Artificial new observation summary.",
                    "sources": [fixtures.ref(sources[1])]},
                   {"id": "new-scope", "body": "Artificial new scope and evidence qualifications.",
                    "sources": [fixtures.ref(source) for source in sources[2:]]}]
    output = add_quality({"objects": old_records + new_records}, context)
    previous = fixtures.candidate(output)
    version = "sha256-" + "0" * 64
    for obj in previous["objects"]:
        obj["metadata"]["content_version"] = version
    for name, records, selected in (("old-topic", old_records, sources[:1]),
                                    ("new-topic", new_records, sources[1:])):
        previous["objects"].append({"path": "memory/topics/" + name + ".md",
            "body": "Artificial topic retains the bounded supplied observations.",
            "metadata": {"id": name, "content_version": version,
                         "sources": [fixtures.ref(source) for source in selected],
                         "records": [{"id": record["id"], "content_version": version} for record in records]}})
    bundle = resolve_skill_refs(*pair)
    previous.update(quality_contract="skill-quality-v1", quality_context=context,
                    skill_bundle=bundle, binding="sha256-" + "1" * 64)
    candidate = deepcopy(previous)
    candidate["objects"] = [obj for obj in candidate["objects"] if not obj["metadata"]["id"].startswith("new-")]
    candidate["extraction_output"]["objects"] = deepcopy(old_records)
    old_identity = sources[0]["source_id"]
    target_map = {target["target_id"]: target for target in context["targets"]}
    for row in candidate["extraction_output"]["quality"]["coverage_disposition"]:
        if target_map[row["target_id"]]["source_id"] != old_identity:
            row.update(status="deferred", record_ids=[], body_evidence=[],
                       reason="The supplied synthetic content is present but was left unfinished in this revision.")
    candidate["revision_context"] = {"candidate": previous}
    payload = {"sources": views, "quality_contract": "skill-quality-v1", "skill_bundle": bundle,
               "quality_context": context, "batch_context": {"source_ranges": [
                   {"fragment_id": fragment["fragment_id"], "path": view["path"],
                    "source_id": view["metadata"]["source_id"],
                    "source_version": view["metadata"]["source_version"]}
                   for view in views for fragment in view["fragments"]]}}
    return candidate, payload, previous


class RevisionCoveragePriorityTests(unittest.TestCase):
    def test_28_regressed_targets_get_complete_source_review_before_old_objects(self):
        candidate, payload, previous = revision_material()
        before = deepcopy((candidate, payload))
        self.assertEqual(len(previous["objects"]), 8)
        self.assertEqual(len(candidate["objects"]), 5)
        regressions = revision_coverage_regressions(candidate)
        self.assertEqual(len(regressions), 28)
        self.assertTrue(all(row["previous_status"] == "summarized" and row["current_status"] == "deferred"
                            for row in regressions))
        units, _ = _source_layout(candidate, payload)
        self.assertEqual(units[0]["kind"], "coverage")
        self.assertEqual(units[-1]["kind"], "global")
        new_table = payload["sources"][1]
        first_ids = set(units[0]["fragment_ids"])
        self.assertTrue({fragment["fragment_id"] for fragment in new_table["fragments"]} <= first_ids)
        for note in payload["sources"][2:]:
            self.assertTrue({fragment["fragment_id"] for fragment in note["fragments"]} <= first_ids)
        self.assertEqual(len([unit for unit in units if unit["kind"] == "object"]), 5)
        self.assertEqual(set().union(*(set(unit["fragment_ids"]) for unit in units if unit["kind"] == "coverage")),
                         {row["fragment_id"] for row in payload["batch_context"]["source_ranges"]})
        self.assertEqual((candidate, payload), before)

    def test_coarse_coverage_layout_also_retains_every_unit_and_global_last(self):
        candidate, payload, _ = revision_material()
        reordered, _ = _layout(candidate, payload)
        legacy_candidate = deepcopy(candidate)
        legacy_candidate.pop("revision_context")
        ordinary, _ = _layout(legacy_candidate, payload)
        self.assertEqual(reordered[0]["kind"], "coverage")
        self.assertEqual(reordered[-1]["kind"], "global")
        self.assertEqual({unit["unit_id"] for unit in reordered}, {unit["unit_id"] for unit in ordinary})

    def test_legal_record_merge_and_rewriting_preserve_target_coverage(self):
        candidate, payload, previous = revision_material()
        candidate = deepcopy(previous)
        candidate["revision_context"] = {"candidate": previous}
        merged = deepcopy(next(obj for obj in candidate["objects"] if obj["metadata"]["id"] == "new-observations"))
        scope = next(obj for obj in candidate["objects"] if obj["metadata"]["id"] == "new-scope")
        merged["metadata"]["id"] = "merged-new-content"
        merged["path"] = "memory/records/merged-new-content.md"
        merged["metadata"]["sources"] += scope["metadata"]["sources"]
        merged["body"] = "Artificial rewritten work and learning content, with the original bounded qualifications."
        candidate["objects"] = [obj for obj in candidate["objects"]
                                if obj["metadata"]["id"] not in {"new-observations", "new-scope"}] + [merged]
        topic = next(obj for obj in candidate["objects"] if obj["metadata"]["id"] == "new-topic")
        topic["metadata"]["records"] = [{"id": "merged-new-content", "content_version": merged["metadata"]["content_version"]}]
        for row in candidate["extraction_output"]["quality"]["coverage_disposition"]:
            if any(identifier.startswith("new-") for identifier in row["record_ids"]):
                row["record_ids"] = ["merged-new-content"]
                row["body_evidence"] = [{"record_id": "merged-new-content", "text": merged["body"]}]
        self.assertEqual(len(candidate["objects"]), 7)
        self.assertEqual(revision_coverage_regressions(candidate), [])
        self.assertEqual(_source_layout(candidate, payload)[0][0]["kind"], "object")

    def test_no_regression_and_initial_extraction_keep_the_normal_order(self):
        candidate, payload, previous = revision_material()
        for selected in (previous, {**deepcopy(previous), "revision_context": {"candidate": previous}}):
            self.assertEqual(revision_coverage_regressions(selected), [])
            units, _ = _source_layout(selected, payload)
            self.assertEqual([unit["kind"] for unit in units], ["object"] * 8 + ["coverage"] * 2 + ["global"])

    def test_changed_target_context_and_missing_or_duplicate_declarations_fail_closed(self):
        candidate, _, _ = revision_material()
        for change in ("source", "statistics", "missing", "duplicate"):
            altered = deepcopy(candidate)
            if change == "source":
                altered["quality_context"]["targets"][0]["source_version"] = "sha256-" + "f" * 64
            elif change == "statistics":
                altered["quality_context"]["statistics"] = []
            elif change == "missing":
                altered["extraction_output"]["quality"]["coverage_disposition"].pop()
            else:
                altered["extraction_output"]["quality"]["coverage_disposition"].append(
                    deepcopy(altered["extraction_output"]["quality"]["coverage_disposition"][0]))
            with self.subTest(change=change), self.assertRaises(CaptureError) as raised:
                revision_coverage_regressions(altered)
            self.assertEqual(raised.exception.code, "revision_coverage_context_mismatch")

    def test_old_contracts_do_not_gain_new_regression_or_layout_rules(self):
        for pair in (("knowledge-extract@11", "knowledge-review@9"),
                     ("knowledge-extract@12", "knowledge-review@10")):
            candidate, payload, _ = revision_material(pair)
            self.assertEqual(revision_coverage_regressions(candidate), [])
            self.assertEqual([unit["kind"] for unit in _source_layout(candidate, payload)[0]],
                             ["object"] * 5 + ["coverage"] * 2 + ["global"])
        expected = {
            ("knowledge-extract@11", "knowledge-review@9"): "62e008afcb4040f49e267812fa8e65c2a30279810ccabb5eeb51f9ad35b5241d",
            ("knowledge-extract@12", "knowledge-review@10"): "5a4d2f9e546843c24cc5b7d6bd875e436fc386768a405df7c9e73a38de6e214f",
        }
        for pair, digest in expected.items():
            candidate, payload = material_fixtures.material(pair)
            plan = material_fixtures.request_plan(candidate, payload, REQUEST_CONTRACT)
            rendered = [render_model_request("review", _request(plan, unit, candidate, payload))
                        for unit in plan["units"]]
            self.assertEqual(hashlib.sha256(b"\x00".join(rendered)).hexdigest(), digest)


class RevisionCoverageReviewLifecycleTests(unittest.TestCase):
    setUp = legacy.DecomposedReviewTests.setUp
    sources = legacy.DecomposedReviewTests.sources
    prepare = legacy.DecomposedReviewTests.prepare
    run_review = legacy.DecomposedReviewTests.run_review
    verify = legacy.DecomposedReviewTests.verify

    def model(self, role, payload):
        response = legacy.DecomposedReviewTests.model(self, role, payload)
        if role == "review":
            self.assertEqual(payload["review_scope"]["kind"], "coverage")
            if getattr(self, "unknown_review", False):
                raise ModelCallError("cli_turn_failed")
            verdict = getattr(self, "coverage_verdict", "needs_confirmation")
            output = passed_review(payload["required_checks"])
            output["repair_scope"] = {"kind": "none", "missing_bindings": []}
            for check, result in zip(payload["required_checks"], output["checks"]):
                if "allowed_results" in check:
                    result.update(result=verdict, reason="Artificial coverage Review identifies the actual evidence gap or unfinished supplied content.")
            self.assertTrue(any(check["result"] == verdict for check in output["checks"]))
            if verdict == "needs_revision":
                output["repair_scope"]["kind"] = "content"
            output.update(verdict=verdict, reasons=["Artificial coverage Review preserves the actual qualification without fabricating content."])
            response["output"] = output
        return response

    def freeze_regression(self):
        legacy.DecomposedReviewTests.freeze(self)
        bundle = resolve_skill_refs("knowledge-extract@13", "knowledge-review@11")
        quality = {"quality_contract": "skill-quality-v1", "skill_bundle": bundle,
                   "quality_context": prepare_quality(self.inputs, source_views=self.payload["sources"])}
        self.payload.update(deepcopy(quality))
        self.candidate.update(deepcopy(quality))
        self.candidate["extraction_output"] = add_quality(self.candidate["extraction_output"], quality["quality_context"])
        previous = deepcopy(self.candidate)
        previous["run_id"] = "run-" + "b" * 32
        previous["binding"] = _candidate_binding(previous)
        row = self.candidate["extraction_output"]["quality"]["coverage_disposition"][0]
        row.update(status="deferred", record_ids=[], body_evidence=[],
                   reason="Artificial source leaves the observation's actual status unresolved and needs a user's clarification.")
        self.candidate["revision_context"] = {"candidate": previous}
        self.candidate["binding"] = _candidate_binding(self.candidate)
        with self.git.lock():
            self.stage = self.git.write_changes(
                {self.stage_path: _document("candidate", self.candidate, quality_schema_version(bundle))},
                {self.stage_path: (self.repo / self.stage_path).read_bytes()}, self.git.check(),
                "artificial coverage revision candidate")

    def assert_early_stop(self, verdict):
        self.coverage_verdict = verdict
        self.freeze_regression()
        result = self.run_review()
        self.assertEqual(result["status"], verdict, result)
        self.assertEqual(result["attempted_calls"], 1)
        self.assertEqual([node["kind"] for node in result["proof"]["nodes"]], ["coverage"])
        self.assertTrue(self.verify(result))
        before = self.git.head(), self.ledger.path.read_bytes(), self.number
        reopened = self.run_review()
        self.assertEqual(reopened["proof"], result["proof"])
        self.assertEqual(reopened["attempted_calls"], 0)
        self.assertEqual((self.git.head(), self.ledger.path.read_bytes(), self.number), before)

    def test_real_evidence_gap_still_reaches_confirmation_and_reopens_without_new_calls(self):
        self.assert_early_stop("needs_confirmation")

    def test_unfinished_supplied_content_returns_revision_before_old_object_reviews(self):
        self.assert_early_stop("needs_revision")

    def test_unknown_prioritized_coverage_is_not_reissued_or_skipped(self):
        self.freeze_regression()
        self.unknown_review = True
        first = self.run_review()
        self.assertEqual(first["status"], "unknown", first)
        self.assertEqual(first["attempted_calls"], 1)
        before = self.git.head(), self.ledger.path.read_bytes(), self.number
        for _ in range(2):
            result = self.run_review()
            self.assertEqual(result["status"], "unknown", result)
            self.assertEqual(result["attempted_calls"], 0)
        self.assertEqual((self.git.head(), self.ledger.path.read_bytes(), self.number), before)


if __name__ == "__main__":
    unittest.main()
