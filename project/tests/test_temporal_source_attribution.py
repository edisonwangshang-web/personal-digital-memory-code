"""Artificial three-domain source periods, literals and bounded exclusions."""

from copy import deepcopy
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import (SOURCE_QUALITY_V3, collect_temporal_diagnostics,
    prepare_quality, validate_quality_output)
from pdm.skill_support import resolve_skill_refs
from test_knowledge_quality import ref, source


class TemporalSourceAttributionTests(unittest.TestCase):
    def setUp(self):
        self.bundle = resolve_skill_refs("knowledge-extract@16", "knowledge-review@14")

    def material(self, body, *, basis=None, original="人工资料用于检验来源时间。", bundle=None):
        item = source(original, "artificial-attribution-time")
        context = prepare_quality([item], skill_bundle=bundle or self.bundle)
        record_id = "artificial-time-attribution"
        output = {"objects": [{"id": record_id, "body": body, "sources": [ref(item)]}],
            "quality": {"coverage_disposition": [{"target_id": row["target_id"],
                "status": "summarized", "record_ids": [record_id],
                "body_evidence": [{"record_id": record_id, "text": body}], "reason": ""}
                for row in context["targets"]], "statistic_claims": [], "temporal_claims": []}}
        if basis is not None:
            output["quality"]["temporal_claims"] = [{"record_id": record_id, "statement": body,
                "basis": basis, "source_refs": [ref(item)], "system_fact_id": None}]
        return output, context

    def valid(self, body, **options):
        output, context = self.material(body, **options)
        before = deepcopy((output, context))
        validate_quality_output(output, context)
        self.assertEqual([], collect_temporal_diagnostics(output, context))
        self.assertEqual(before, (output, context))

    def invalid(self, body, code="quality_temporal_mismatch", **options):
        output, context = self.material(body, **options)
        with self.assertRaises(CaptureError) as caught:
            validate_quality_output(output, context)
        self.assertEqual(code, caught.exception.code)
        diagnostics = collect_temporal_diagnostics(output, context)
        if context["policy_version"] != SOURCE_QUALITY_V3:
            self.assertEqual([], diagnostics)
            return
        self.assertEqual(code, diagnostics[0]["code"])
        self.assertIn("field_path", diagnostics[0]["details"])

    def test_attributed_display_periods_across_three_domains(self):
        for body in (
            "报告在该页所述时期记载训练负荷有所下降；报告日期未知。",
            "讲者对其展示区间自述交付周期缩短；分享具体日期未知。",
            "课程材料在其所述时期记载练习覆盖有所提升；授课日期未知。",
        ):
            with self.subTest(body=body):
                self.valid(body, basis="source_time")

    def test_a_period_without_source_attribution_is_not_a_source_time(self):
        self.invalid("展示区间的负荷有所下降。", basis="source_time")

    def test_historical_year_is_bound_to_this_claims_own_quote(self):
        for body, original in (
            ("报告引用2020年训练实验，称负荷变化；未独立核验。", "人工报告载有2020 年训练实验。"),
            ("分享引用2021年业务实验，称交付改善；这是分享转述。", "人工分享载有2021年业务实验。"),
            ("课程材料引用2022年练习研究，称方法有效；未独立核验。", "人工课程引用2022年练习研究。"),
            ("报告引用2021年训练实验。", "人工报告于2021年5月9日载有训练实验。"),
        ):
            with self.subTest(body=body):
                self.valid(body, basis="source_time", original=original)
        self.invalid("分享引用2023年业务实验，称交付改善。", basis="source_time",
                     original="人工分享载有2021年业务实验。")

    def test_a_literal_year_without_source_attribution_is_not_permission(self):
        self.invalid("2021年训练后指标改善。", basis="source_time", original="人工资料2021年。")

    def test_year_or_period_cannot_qualify_independent_current_or_future_state(self):
        for body in (
            "报告引用2021年实验。当前已经恢复。",
            "分享引用2021年实验，当前已经上线。",
            "课程材料在其所述时期记载练习变化。明天将会完成。",
            "讲者对其展示区间自述交付改善，当前已经上线。",
            "报告引用2021年研究，系统已经上线。",
            "报告引用其所述区间，用户已经康复。",
            "报告引用2021年研究，说明书已经更新。",
            "报告引用2021年研究，描述文件已经提交。",
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="source_time", original="人工资料载有2021年实验。")

    def test_unknown_year_can_belong_to_a_named_historical_cutoff(self):
        for body in (
            "报告当时记载：截至Q1累计观察8次；Q1年份未知。",
            "分享在该页所述时期自述：截至H2累计形成6项；H2年份未知。",
            "课程材料当时记载：截至下半年累计练习9次；下半年年份未知。",
            "来源当时记载：截至11月累计记录5项；11月所属年份不明。",
            "报告当时截至Q1累计8项及截至Q2累计9项；Q1年份未知，Q2年份未知。",
        ):
            with self.subTest(body=body):
                self.valid(body, basis="unknown")

    def test_unknown_cutoff_requires_matching_local_period_and_source_scope(self):
        for body in (
            "截至Q2累计8项；Q2年份未知。",
            "报告当时截至Q2累计8项；Q3年份未知。",
            "报告当时截至Q2累计8项。Q2年份未知。",
            "报告当时截至Q20累计8项；Q2年份未知。",
            "报告当时截至Q2累计8项。",
            "报告当时截至1月累计8项；11月年份未知。",
            "报告当时截至Q1累计8项及截至Q2累计9项；Q1年份未知。",
            "报告当时截至Q2累计8项. Q2年份未知。",
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="unknown")

    def test_unknown_cutoff_cannot_hide_current_future_or_completed_predicates(self):
        for body in (
            "报告当时截至Q2累计8项；Q2年份未知。当前已经恢复。",
            "分享当时截至Q2累计8项；Q2年份未知，当前已经上线。",
            "课程材料当时截至Q2累计8项；Q2年份未知，但明天将会完成。",
            "报告当时截至Q2已经累计8项；Q2年份未知。",
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="unknown")

    def test_narrow_inference_exclusions_do_not_assert_the_outcome(self):
        for body in (
            "一次训练不等于用户已经康复。",
            "方案不能作为当前系统状态。",
            "课程目录不能外推为当前已经学会。",
            "草稿不能被用作当前发布状态。",
            "报告引用“这不等于用户已经康复”作为限制说明。",
            "材料写道“这不能作为当前平台状态”。",
            "课程说明“目录不能外推为当前已经学会”。",
        ):
            with self.subTest(body=body):
                self.valid(body)

    def test_exclusion_does_not_erase_actual_preceding_or_following_claims(self):
        for body in (
            "当前已经康复，不等于以后没有风险。",
            "方案不等于已经上线而当前已经上线。",
            "课程目录不能外推为当前已经学会。明天将会完成。",
            "不能作为当前状态且当前已经发布。",
            "草稿不能外推为当前已经发布且系统当前已经上线。",
            "目录不等于已经学会并用户当前已经毕业。",
            "不能作为当前状态同时系统明天将会发布。",
            "“不等于”三个字已经加入术语表。",
            "“不能作为”已经成为关键词。",
            "‘不等于’三个字已经加入术语表。",
            "'不能作为'已经成为关键词。",
            "报告写道“这不等于已经完成”但当前已经完成。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_explicitly_historical_quoted_labels_are_metadata(self):
        for body in (
            "以下“当前负荷”“正在调整”均指报告当时。",
            "以下“当前方案”“正在试点”均指分享当时。",
            "以下“当前练习”“正在学习”均指材料当时。",
        ):
            with self.subTest(body=body):
                self.valid(body)

    def test_quotation_alone_is_not_a_historical_or_nonassertive_exemption(self):
        for body in (
            "用户说“当前已经恢复”。",
            "以下“当前已经上线”均指来源当时。当前已经上线。",
            "以下“正在学习”均指材料当时，但明天将会完成。",
            "“当前已经上线”均指非来源当时。",
            "用户说“当前已经上线”指的不是来源当时。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_real_negative_state_and_unqualified_unknown_claims_remain_rejected(self):
        self.invalid("该任务尚未完成。", "quality_temporal_missing")
        self.invalid("说明称本机执行了识别。", basis="unknown")

    def test_current_system_state_still_needs_exact_system_fact(self):
        self.invalid("当前已经上线。", "quality_current_state_unverified", basis="current_state")

    def test_legacy_v2_contract_does_not_gain_new_v3_exceptions(self):
        previous = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.invalid("一次训练不等于用户已经康复。", "quality_temporal_missing", bundle=previous)


if __name__ == "__main__":
    unittest.main()
