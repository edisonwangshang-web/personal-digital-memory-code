"""Deferred-first Review and the narrow parser-policy comparison use fake data."""

from copy import deepcopy
import unittest

import test_knowledge_quality as fixtures
from quality_fixtures import add_quality
from test_revision_coverage_priority import revision_material
from pdm.contracts import CaptureError
from pdm.decomposed_review import _layout, _source_layout
from pdm.knowledge_quality import prepare_quality
from pdm.revision_context import pending_coverage_targets, revision_coverage_regressions
from pdm.skill_support import resolve_skill_refs


NEW = ("knowledge-extract@14", "knowledge-review@12")
OLD = ("knowledge-extract@13", "knowledge-review@11")


def parser_revision():
    source = fixtures.source('\ufeff"date",steps,score\n2025-01-01,10,60\n2025-01-02,20,70\n',
                             "artificial-policy-upgrade")
    view = fixtures.view(source, [("artificial-upgrade-complete-table", 1, 3)])
    record = {"id": "artificial-observation", "body": "Artificial supplied observation summary.",
              "sources": [fixtures.ref(source)]}

    def candidate(pair):
        bundle = resolve_skill_refs(*pair)
        quality = prepare_quality([source], [view], skill_bundle=bundle)
        value = fixtures.candidate(add_quality({"objects": [record]}, quality))
        value.update(quality_contract="skill-quality-v1", skill_bundle=bundle,
                     quality_context=quality, controls_digest="sha256-" + "1" * 64,
                     source_views=[view], batch_context={"source_ranges": [
                         {"fragment_id": "artificial-upgrade-complete-table", "path": source["path"],
                          "source_id": source["source_id"], "source_version": source["source_version"]}]},
                     related_runs=[], merge_inputs=[])
        return value

    old, current = candidate(OLD), candidate(NEW)
    current["revision_context"] = {"candidate": old}
    steps = next(target["target_id"] for target in current["quality_context"]["targets"]
                 if target.get("column") == "steps")
    row = next(row for row in current["extraction_output"]["quality"]["coverage_disposition"]
               if row["target_id"] == steps)
    row.update(status="deferred", record_ids=[], body_evidence=[],
               reason="The artificial supplied observations still need content repair.")
    return current, old, steps


class PendingCoveragePriorityTests(unittest.TestCase):
    def test_initial_final_candidate_with_deferred_data_reviews_complete_coverage_first(self):
        candidate, payload, _ = revision_material(NEW)
        candidate.pop("revision_context")
        before = deepcopy((candidate, payload))
        self.assertEqual(revision_coverage_regressions(candidate), [])
        self.assertEqual(len(pending_coverage_targets(candidate)), 28)
        for layout in (_layout, _source_layout):
            units, _ = layout(candidate, payload)
            self.assertEqual(units[0]["kind"], "coverage")
            self.assertEqual(units[-1]["kind"], "global")
            self.assertEqual(len([unit for unit in units if unit["kind"] == "object"]), 5)
        first = _source_layout(candidate, payload)[0][0]
        for source in payload["sources"][1:]:
            self.assertTrue({fragment["fragment_id"] for fragment in source["fragments"]}
                            <= set(first["fragment_ids"]))
        self.assertEqual((candidate, payload), before)

    def test_still_deferred_after_revision_does_not_wait_for_a_regression(self):
        candidate, payload, _ = revision_material(NEW)
        previous = deepcopy(candidate)
        previous.pop("revision_context")
        candidate["revision_context"] = {"candidate": previous}
        self.assertEqual(revision_coverage_regressions(candidate), [])
        self.assertEqual(_source_layout(candidate, payload)[0][0]["kind"], "coverage")

    def test_genuine_gap_and_unfinished_work_both_receive_independent_coverage_review(self):
        for reason in ("Artificial source omitted the necessary observation.",
                       "Artificial source is available but the draft left it unfinished."):
            candidate, payload, _ = revision_material(NEW)
            candidate.pop("revision_context")
            for row in candidate["extraction_output"]["quality"]["coverage_disposition"]:
                if row["status"] == "deferred":
                    row["reason"] = reason
            units, _ = _source_layout(candidate, payload)
            self.assertEqual(units[0]["kind"], "coverage")
            self.assertNotIn("verdict", candidate)

    def test_completed_content_and_old_initial_contract_keep_old_layout(self):
        candidate, payload, old = revision_material(NEW)
        self.assertEqual(pending_coverage_targets(old), [])
        self.assertEqual(_source_layout(old, payload)[0][0]["kind"], "object")
        for pair in (OLD, ("knowledge-extract@12", "knowledge-review@10")):
            candidate, payload, _ = revision_material(pair)
            candidate.pop("revision_context")
            self.assertEqual(pending_coverage_targets(candidate), [])
            self.assertEqual(_source_layout(candidate, payload)[0][0]["kind"], "object")

    def test_pending_inventory_corruption_fails_before_review(self):
        candidate, payload, _ = revision_material(NEW)
        candidate.pop("revision_context")
        candidate["extraction_output"]["quality"]["coverage_disposition"].pop()
        with self.assertRaises(CaptureError) as error:
            _source_layout(candidate, payload)
        self.assertEqual(error.exception.code, "revision_coverage_context_mismatch")

    def test_local_draft_does_not_gain_final_deferred_priority(self):
        candidate, payload, _ = revision_material(NEW)
        candidate.pop("revision_context")
        candidate["batch_material"] = {"draft_only": True}
        self.assertEqual(pending_coverage_targets(candidate), [])
        self.assertEqual(_source_layout(candidate, payload)[0][0]["kind"], "object")


class ParserPolicyCoverageComparisonTests(unittest.TestCase):
    def test_only_actual_columns_are_compared_and_old_false_date_metric_is_not_copied(self):
        candidate, previous, steps = parser_revision()
        before = deepcopy((candidate, previous))
        self.assertEqual(len(previous["quality_context"]["targets"]), 3)
        self.assertEqual(len(candidate["quality_context"]["targets"]), 2)
        rows = revision_coverage_regressions(candidate)
        self.assertEqual([row["target_id"] for row in rows], [steps])
        self.assertEqual(rows[0]["previous_record_ids"], ["artificial-observation"])
        self.assertEqual(pending_coverage_targets(candidate), [steps])
        self.assertEqual((candidate, previous), before)

    def test_new_policy_does_not_invent_regression_when_mapped_columns_remain_done(self):
        candidate, _, _ = parser_revision()
        for row in candidate["extraction_output"]["quality"]["coverage_disposition"]:
            row.update(status="summarized", record_ids=["artificial-observation"],
                       body_evidence=[{"record_id": "artificial-observation", "text": "Artificial summary."}])
        self.assertEqual(revision_coverage_regressions(candidate), [])

    def test_changed_raw_source_controls_views_or_target_cannot_use_parser_bridge(self):
        for change in ("controls", "views", "ranges", "source", "line_hash", "column", "old_pair", "new_pair"):
            candidate, _, _ = parser_revision()
            if change == "controls":
                candidate["controls_digest"] = "sha256-" + "9" * 64
            elif change == "views":
                candidate["source_views"] = []
            elif change == "ranges":
                candidate["batch_context"] = {"source_ranges": []}
            elif change == "source":
                candidate["quality_context"]["sources"][0]["source_version"] = "sha256-" + "9" * 64
            elif change == "line_hash":
                candidate["quality_context"]["sources"][0]["line_sha256"]["1"] = "9" * 64
            elif change == "column":
                candidate["quality_context"]["targets"][0]["column"] = "another-column"
            elif change == "old_pair":
                candidate["revision_context"]["candidate"]["skill_bundle"] = resolve_skill_refs(
                    "knowledge-extract@12", "knowledge-review@10")
            else:
                candidate["skill_bundle"] = resolve_skill_refs(*OLD)
            with self.subTest(change=change), self.assertRaises(CaptureError) as error:
                revision_coverage_regressions(candidate)
            self.assertEqual(error.exception.code, "revision_coverage_context_mismatch")


if __name__ == "__main__":
    unittest.main()
