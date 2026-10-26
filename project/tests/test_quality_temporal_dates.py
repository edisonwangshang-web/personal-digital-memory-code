"""Artificial Chinese date expressions retain source and current-state guards."""

import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import prepare_quality, validate_quality_output
from quality_fixtures import add_quality
from test_knowledge_quality import extraction, ref, source


class SourceDateExpressionTests(unittest.TestCase):
    def output(self, original, statement, *, body=None, lines=(1,)):
        item = source(original, name="artificial-calendar")
        context = prepare_quality([item])
        output = add_quality(extraction(item, body or statement), context)
        refs = [ref(item, line) for line in lines]
        output["objects"][0]["sources"] = refs
        output["quality"]["temporal_claims"] = [{
            "record_id": "synthetic", "statement": statement, "basis": "source_time",
            "source_refs": refs, "system_fact_id": None,
        }]
        return output, context

    def valid(self, original, statement, **options):
        output, context = self.output(original, statement, **options)
        validate_quality_output(output, context)

    def invalid(self, original, statement, **options):
        output, context = self.output(original, statement, **options)
        with self.assertRaises(CaptureError) as raised:
            validate_quality_output(output, context)
        self.assertEqual(raised.exception.code, "quality_temporal_mismatch")

    def test_complete_chinese_dates_and_iso_citations_are_equivalent(self):
        for statement in (
            "2024年5月9日尚未提交。",
            "2024年05月09日尚未提交。",
            "2024 年 5 月 9 日尚未提交。",
        ):
            with self.subTest(statement=statement):
                self.valid("2024-05-09，人工材料尚未提交。", statement)
        self.valid("2024年5月9日，人工材料尚未提交。", "2024-05-09尚未提交。")

    def test_yearless_source_dates_apply_to_health_work_and_learning(self):
        cases = (
            ("2024-05-09,是,2", "含5月9日原表标为未完结的记录"),
            ("2024-05-09,是,2", "包含原表标为未完结的5月9日"),
            ("2024-05-09，人工材料尚未提交。", "工作记录：来源5月9日尚未提交材料。"),
            ("2024-05-09，人工课程尚未完成。", "学习记录：原文5月9日尚未完成课程。"),
        )
        for original, statement in cases:
            with self.subTest(statement=statement):
                body = "人工资料期间为2024-05-01至2024-05-09。" + statement
                self.valid(original, statement, body=body)

    def test_every_date_in_a_chinese_range_is_cited(self):
        self.valid("2024-05-01至2024-05-09，人工来源记录。",
                   "原表2024年5月1日至5月9日记录未完结状态。")
        self.invalid("2024-05-09，人工来源记录。",
                     "原表2024年5月1日至5月9日记录未完结状态。")

    def test_invalid_calendar_dates_years_and_unbound_dates_are_rejected(self):
        for statement in (
            "原表2024年2月30日尚未提交。",
            "原表2月30日尚未提交。",
            "原表13月9日尚未提交。",
            "原表2025年5月9日尚未提交。",
            "原表5月8日尚未提交。",
            "原表024年5月9日尚未提交。",
            "原表12024年5月9日尚未提交。",
            "原表2025年，5月9日尚未提交。",
        ):
            with self.subTest(statement=statement):
                self.invalid("2024-05-09，人工材料尚未提交。", statement)

    def test_month_day_must_have_one_complete_year_in_its_own_citations(self):
        self.invalid("2023-05-09，人工状态。\n2024-05-09，人工状态。",
                     "原表5月9日尚未提交。", lines=(1, 2))
        self.invalid("5月9日，人工状态。", "原表5月9日尚未提交。")
        self.invalid("2024-05-09，人工状态。", "5月9日尚未提交。")
        self.invalid("原始日期未知，人工状态。", "原表5月9日尚未提交。",
                     body="人工资料期间为2024-05-01至2024-05-09。原表5月9日尚未提交。")

    def test_dates_or_attribution_in_another_sentence_do_not_qualify_status(self):
        for statement in (
            "原表5月9日是资料日期。尚未提交。",
            "原表5月9日是资料日期；尚未提交。",
            "原表5月9日是资料日期。未完结。",
            "原表写有人工状态。5月9日尚未提交。",
            "原表写有人工状态；5月9日尚未提交。",
        ):
            with self.subTest(statement=statement):
                self.invalid("2024-05-09，人工材料尚未提交。", statement)

    def test_chinese_source_dates_cannot_authorize_current_system_state(self):
        for statement in (
            "原表5月9日是资料日期，当前已经导入。",
            "原表2024年5月9日是资料日期，目前已经导入。",
            "原表5月9日是资料日期，现在已经导入。",
            "原表5月9日是资料日期。现已提交。",
        ):
            with self.subTest(statement=statement):
                self.invalid("2024-05-09，人工材料尚未提交。", statement)

    def test_source_attribution_alone_is_not_a_new_time_exemption(self):
        self.invalid("2024-05-09，人工材料尚未提交。", "原表尚未提交。")
        self.invalid("2024-05-09，人工状态。", "原表标为未完结。")

    def test_new_date_support_keeps_exact_statement_and_reference_guards(self):
        output, context = self.output("2024-05-09，人工材料尚未提交。", "原表5月9日尚未提交。")
        output["quality"]["temporal_claims"][0]["source_refs"][0]["quote"] += "伪造"
        with self.assertRaises(CaptureError) as raised:
            validate_quality_output(output, context)
        self.assertEqual(raised.exception.code, "quality_source_reference_mismatch")


if __name__ == "__main__":
    unittest.main()
