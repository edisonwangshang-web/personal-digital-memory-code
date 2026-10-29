"""Synthetic CSV signatures keep calendar statistics and old proof bytes exact."""

import copy
import hashlib
import json
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import prepare_quality, project_quality
from pdm.skill_support import resolve_skill_refs
from test_knowledge_quality import source, statistics, view


SIMPLE = ('\ufeff"date","steps","partial_day"\n'
          '"2025-01-01","10","false"\n'
          '"2025-01-02","40","false"\n')


class QualityCsvBomPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.current = resolve_skill_refs("knowledge-extract@14", "knowledge-review@12")
        cls.previous = resolve_skill_refs("knowledge-extract@13", "knowledge-review@11")

    def prepare(self, body, **options):
        return prepare_quality([source(body)], skill_bundle=self.current, **options)

    def test_signature_before_quoted_header_recognizes_dates_without_fake_metric(self):
        item = source(SIMPLE)
        frozen = copy.deepcopy(item)
        context = prepare_quality([item], skill_bundle=self.current)
        self.assertEqual("source-quality-v2", context["policy_version"])
        table = context["sources"][0]["tables"][0]
        self.assertEqual(["date", "steps", "partial_day"], table["headers"])
        self.assertEqual(["2025-01-01", "2025-01-02"], table["date_interval"])
        self.assertEqual(2, table["distinct_dates"])
        self.assertEqual(["steps"], [target["column"] for target in context["targets"]])
        self.assertEqual(frozen, item)
        self.assertEqual(hashlib.sha256(SIMPLE.splitlines()[0].encode()).hexdigest(),
                         context["sources"][0]["line_sha256"]["1"])
        self.assertTrue(item["body"].startswith('\ufeff"date"'))

    def test_old_policy_reconstructs_exact_pre_fix_context(self):
        item = source(SIMPLE)
        previous = prepare_quality([item], skill_bundle=self.previous)
        self.assertEqual(prepare_quality([item]), previous)
        self.assertEqual("source-quality-v1", previous["policy_version"])
        self.assertEqual(['"date"', "steps", "partial_day"],
                         previous["sources"][0]["tables"][0]["headers"])
        raw = json.dumps(previous, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
        # Captured from the pre-fix source implementation, not recomputed by
        # a second call to the new implementation under test.
        self.assertEqual("d7fec71c450cae092de85f70e55491f952bfa5386234231f600f0c58a07522bd",
                         hashlib.sha256(raw).hexdigest())

    def test_context_policy_rebuild_preserves_new_or_historical_computation(self):
        for bundle in (self.current, self.previous):
            with self.subTest(pair=bundle["skills"]["extract"]["ref"]):
                context = prepare_quality([source(SIMPLE)], skill_bundle=bundle)
                self.assertEqual(context, prepare_quality([source(SIMPLE)],
                                 policy_version=context["policy_version"]))
                self.assertEqual(context, project_quality(context))

    def test_signature_fix_does_not_change_counts_for_already_supported_headers(self):
        for body in (SIMPLE.removeprefix("\ufeff"), SIMPLE.replace('"date"', 'date', 1)):
            with self.subTest(body=body.splitlines()[0]):
                old = prepare_quality([source(body)], skill_bundle=self.previous)
                new = self.prepare(body)
                old["policy_version"] = "source-quality-v2"
                self.assertEqual(old, new)

    def test_duplicate_and_invalid_calendar_rows_do_not_enter_descriptive_mean(self):
        body = ('\ufeff"date","steps","partial_day"\n'
                '"2025-01-01","10","false"\n'
                '"2025-01-01","20","false"\n'
                '"2025-02-30","30","false"\n'
                '"2025-01-02","40","false"\n')
        new = self.prepare(body)
        table = new["sources"][0]["tables"][0]
        self.assertEqual(["2025-01-01"], table["duplicate_dates"])
        self.assertEqual(["L4-L4"], table["invalid_date_locators"])
        target = new["targets"][0]
        self.assertEqual(4, target["counts"]["rows"])
        self.assertEqual(1, target["descriptive_n"])
        mean = next(row for row in statistics(new) if row["operation"] == "mean")
        self.assertEqual("40", mean["value"])
        self.assertEqual(["L5-L5"], mean["source_locators"])
        old = prepare_quality([source(body)], skill_bundle=self.previous)
        old_steps = next(target["target_id"] for target in old["targets"] if target["column"] == "steps")
        old_mean = next(row for row in statistics(old)
                        if row["operation"] == "mean" and row["target_id"] == old_steps)
        self.assertEqual("25", old_mean["value"])

    def test_partial_and_unknown_completion_still_exclude_rows(self):
        body = ('\ufeff"date","steps","partial_day"\n'
                '"2025-01-01","12","false"\n'
                '"2025-01-02","99","true"\n'
                '"2025-01-03","33","unknown"\n')
        context = self.prepare(body)
        table = context["sources"][0]["tables"][0]
        self.assertEqual(["L3-L3"], table["partial_day_locators"])
        self.assertEqual(["L4-L4"], table["unknown_completion_locators"])
        self.assertEqual(1, context["targets"][0]["descriptive_n"])
        self.assertEqual("12", next(row for row in statistics(context) if row["operation"] == "mean")["value"])

    def test_multiline_header_and_row_keep_physical_source_locators(self):
        body = '\ufeff"date","note\nlabel","steps"\n"2025-01-01","hello\nworld","12"\n'
        item = source(body)
        context = prepare_quality([item], skill_bundle=self.current)
        table = context["sources"][0]["tables"][0]
        self.assertEqual(["L1-L2"], table["header_locators"])
        self.assertEqual(["2025-01-01", "2025-01-01"], table["date_interval"])
        steps = next(target["target_id"] for target in context["targets"] if target["column"] == "steps")
        mean = next(row for row in statistics(context)
                    if row["operation"] == "mean" and row["target_id"] == steps)
        self.assertEqual(["L3-L4"], mean["source_locators"])
        self.assertEqual({str(number): hashlib.sha256(text.encode()).hexdigest()
                          for number, text in enumerate(body.splitlines(), 1)},
                         context["sources"][0]["line_sha256"])

    def test_literal_quotes_in_header_are_not_stripped_as_a_repair(self):
        body = '\ufeff"""date""","steps"\n"2025-01-01","10"\n'
        context = self.prepare(body)
        table = context["sources"][0]["tables"][0]
        self.assertEqual(['"date"', "steps"], table["headers"])
        self.assertIsNone(table["date_interval"])
        self.assertIn('"date"', [target["column"] for target in context["targets"]])

    def test_missing_header_fragment_does_not_gain_a_guessed_calendar(self):
        item = source(SIMPLE)
        selected = view(item, [("only-values", 2, 3)])
        context = prepare_quality([item], [selected], skill_bundle=self.current)
        profile = context["sources"][0]
        self.assertEqual("header_not_provided", profile["table_parse_status"])
        self.assertEqual([], profile["tables"])
        self.assertEqual("semantic", context["targets"][0]["kind"])

    def test_policy_cannot_be_unknown_or_conflict_with_the_bound_skill(self):
        cases = ({"policy_version": "source-quality-v999"},
                 {"policy_version": "source-quality-v1", "skill_bundle": self.current},
                 {"policy_version": "source-quality-v2", "skill_bundle": self.previous})
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaises(CaptureError) as error:
                    prepare_quality([source(SIMPLE)], **options)
                self.assertEqual("invalid_quality_context", error.exception.code)


if __name__ == "__main__":
    unittest.main()
