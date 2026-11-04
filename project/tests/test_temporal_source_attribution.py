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

    def test_literal_calendar_periods_match_their_own_source_periods(self):
        for body, original in (
            ("报告按2024 H1、2024 H2列示训练次数依次为3和5。", "人工报告：2024 H1、2024 H2。"),
            ("第2页按2025 q1、2025q2展示交付次数依次为4和6。", "人工分享：2025 Q1、2025 Q2。"),
            ("课程材料按2023 h 1展示练习次数为8。", "人工课程：2023 H1。"),
            ("2024 H1训练次数为3。", "人工报告：2024 H1。"),
            ("报告2024年H1记载5月观察次数为3。", "人工报告：2024年 H1，5月观察次数为3。"),
            ("测试覆盖率为42%，标注2023年4月。", "人工报告：2023年 4月，测试覆盖率42%。"),
        ):
            with self.subTest(body=body):
                self.valid(body, basis="source_time", original=original)

    def test_calendar_periods_do_not_borrow_a_year_or_another_period(self):
        for body, original in (
            ("报告按2024 H1列示训练次数为3。", "人工报告：2024 H2。"),
            ("分享按2025 Q1列示交付次数为4。", "人工分享：2025年说明及2026 Q1。"),
            ("课程材料按2023 Q2列示练习次数为8。", "人工课程：2023 Q1。"),
            ("报告按2024 H1、2024 H2列示次数。", "人工报告：2024 H1。"),
            ("报告按Q1列示训练次数为3。", "人工报告：2024 Q1。"),
            ("报告按2024 Q1/Q2列示训练次数。", "人工报告：2024 Q1。"),
            ("分享按2024 H1及Q2列示交付次数。", "人工分享：2024 H1。"),
            ("课程材料按2024 Q1.5列示练习次数。", "人工课程：2024 Q1。"),
            ("报告列示2023年5月训练次数。", "人工报告：2023年4月。"),
            ("报告列示2023年13月训练次数。", "人工报告：2023年13月。"),
            ("报告按2024年2月及3月列示训练次数。", "人工报告：2024年2月。"),
            ("报告列示2023年4月5日训练次数。", "人工报告：2023年4月6日。"),
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="source_time", original=original)
        for period in ("2024 H0", "2024 H3", "2024 H10", "2024 Q0", "2024 Q5", "2024 Q10"):
            with self.subTest(period=period):
                self.invalid("报告按" + period + "列示训练次数。", basis="source_time",
                    original="人工报告：" + period + "。")

    def test_calendar_periods_preserve_actual_current_and_future_checks(self):
        for body in (
            "报告按2024 H1列示训练次数，用户当前已经康复。",
            "分享按2024 H1列示交付次数。系统已经上线。",
            "课程材料按2024 H1列示练习次数，明天将会完成。",
            "报告按2024 H1介绍当前已经康复。",
            "分享按2024 H1介绍未来将会交付。",
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="source_time", original="人工资料：2024 H1。")

    def test_every_explicit_calendar_is_checked_before_weaker_fallbacks(self):
        wrong = (
            ("报告按2024 H1列示次数。", "人工报告：2024 H2。"),
            ("报告按2024年Q1列示次数。", "人工报告：2024年Q2。"),
            ("报告列示2023年5月训练次数。", "人工报告：2023年4月。"),
            ("报告列示2022.2.3训练次数。", "人工报告：2022.2.4。"),
            ("报告列示2021年2月29日训练次数。", "人工报告：2021年2月29日。"),
        )
        for body, original in wrong:
            for suffix in ("报告不构成用户已经康复的证明。", "栏目为‘未来规划’。", "报告当时介绍该结果。"):
                with self.subTest(body=body, suffix=suffix):
                    self.invalid(body + suffix, basis="source_time", original=original)
        for body, original in (
            ("报告按2024 H1列示2023年4月5日次数。", "人工报告：2024 H1及2023年4月6日。"),
            ("报告按2024 H1列示2023年2月30日次数。", "人工报告：2024 H1及2023年2月30日。"),
            ("报告引用2024年实验并按2024 Q1列示次数。", "人工报告：2024年实验及2024 Q2。"),
            ("报告于2022年2月3日列示2024 H1次数。", "人工报告：2022年2月3日及2024 H2。"),
            ("报告列示2022.2.3与2024 H1次数。", "人工报告：2022.2.3及2024 H2。"),
            ("报告按2024-02-05与2024-02-06列示次数。", "人工报告：2024-02-05。"),
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="source_time", original=original)
        self.valid("报告当时按2024 H1及2023年4月5日列示次数。",
            basis="source_time", original="人工报告：2024 H1及2023年4月5日。")
        self.valid("栏目为‘2024 H2’；报告当时介绍方法。", basis="source_time")
        self.invalid("报告列示3项训练；栏目为‘未来规划’。", basis="source_time")

    def test_invalid_calendar_components_remain_bounded_errors(self):
        for body in ("报告当时按0000 H1列示次数。", "报告当时按0000年4月列示次数。",
                     "报告当时按2024年" + "9" * 5000 + "月列示次数。"):
            with self.subTest(length=len(body)):
                self.invalid(body, basis="source_time", original=body)

    def test_numeric_gregorian_dates_are_bound_to_the_claims_own_quotes(self):
        for body, original in (
            ("来源报告的观察周期为2022.2.3-2022.3.4；样本为3组。", "人工观察周期：2022.2.3-2022.3.4，样本3组。"),
            ("报告列示测试期间2023/4/5-2023/5/6。", "人工测试期间：2023/04/05-2023/05/06。"),
            ("课程材料引用2020.2.29的练习记录。", "人工练习记录：2020年2月29日。"),
        ):
            with self.subTest(body=body):
                self.valid(body, basis="source_time", original=original)
        for body, original in (
            ("报告列示2022.2.3-2022.3.4。", "另一观察周期：2022.2.3-2022.3.5。"),
            ("报告列示2022/2/3。", "人工材料：2021/2/3。"),
            ("2022.2.3发生训练。", "人工材料：2022.2.3。"),
            ("报告列示2.3发生训练。", "人工材料：2022.2.3。"),
            ("报告列示2022.2发生训练。", "人工材料：2022.2.3。"),
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="source_time", original=original)
        for value in ("2021.2.29", "2022/13/1", "2022.2.30", "2022.2/3", "2022.2.003",
                      "20222.2.3", "2022.2.3.4", "2022..2.3"):
            with self.subTest(value=value):
                self.invalid("报告列示2022.1.1及" + value + "。", basis="source_time",
                    original="人工报告：2022.1.1及" + value + "。")

    def test_numeric_date_formats_do_not_authorize_current_or_future_claims(self):
        for body in (
            "报告列示2022.2.3，用户当前已经康复。",
            "材料引用2022/2/3。系统明天将会发布。",
            "课程材料列示2022.2.3，用户已经掌握课程。",
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="source_time", original="人工材料：2022.2.3。")

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

    def test_unknown_anaphoric_cutoff_uses_the_same_named_period(self):
        for body in (
            "报告当时记载：截至Q2累计观察8次；Q2年份未知。",
            "第4页记载分享当时的运营自述（分享日期及Q2年份未知）：覆盖7个测试组，Q2全量接入，截至该Q2累计评估需求800项。",
            "报告记载观察当时的训练情况（观察日期及H2年份未知）：覆盖3组训练，H2全量记录，截至此H2累计训练80次。",
            "课程材料记载授课当时的练习情况（授课日期及下半年年份未知）：覆盖5组练习，下半年全量记录，截至其下半年累计练习90次。",
            "报告当时记载：截至该 Q2累计观察8次；Q2年份未知。",
        ):
            with self.subTest(body=body):
                self.valid(body, basis="unknown")

    def test_unknown_anaphoric_cutoff_still_requires_local_source_and_period(self):
        for body in (
            "截至该Q2累计8项；Q2年份未知。",
            "报告当时截至该Q2累计8项；Q3年份未知。",
            "报告当时截至该Q2累计8项。Q2年份未知。",
            "报告当时截至该Q20累计8项；Q2年份未知。",
            "报告当时截至该Q2累计8项。",
            "报告当时截至该1月累计8项；11月年份未知。",
            "报告当时截至该Q1累计8项及截至该Q2累计9项；Q1年份未知。",
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="unknown")

    def test_anaphoric_cutoff_cannot_qualify_independent_state_claims(self):
        for body, basis in (
            ("报告当时截至该Q2累计8项；Q2年份未知，当前已经上线。", "unknown"),
            ("课程材料当时截至此H2累计8次；H2年份未知，但明天将会完成。", "unknown"),
            ("报告当时截至该Q2已经累计8项；Q2年份未知。", "unknown"),
            ("报告引用2021年研究，系统已经上线。", "source_time"),
            ("“不能作为”已经成为关键词。", None),
            ("“当前已经上线”均指非来源当时。", None),
        ):
            with self.subTest(body=body):
                self.invalid(body,
                    "quality_temporal_missing" if basis is None else "quality_temporal_mismatch",
                    basis=basis, original="人工资料载有2021年研究与来源时间说明。")

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

    def test_negative_inference_predicates_across_three_domains(self):
        for verb in ("宣称", "声称", "认定"):
            for statement in ("用户已经康复", "系统已经上线或用户已经采用方案", "用户已经掌握课程"):
                body = "不能据此" + verb + statement + "。"
                for basis in (None, "unknown"):
                    with self.subTest(verb=verb, statement=statement, basis=basis):
                        self.valid(body, basis=basis)

    def test_nonassertive_inference_denials_share_the_same_boundaries(self):
        for predicate in ("断言", "判断", "推断", "认定"):
            for state in ("用户现在已经康复", "系统将来实际发布", "用户已经掌握课程"):
                body = "不据此" + predicate + state + "。"
                for basis in (None, "unknown"):
                    with self.subTest(body=body, basis=basis):
                        self.valid(body, basis=basis)
        for body in (
            "并非本报告不据此断言用户已经康复。",
            "不能不据此断言系统已经上线。",
            "不是不据此认定用户已经掌握课程。",
            "‘不据此断言’已经加入词表。",
            "不据此断言已经康复可是用户已经康复。",
            "不据此判断已经完成，系统将来实际发布。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_source_presentations_and_explicit_column_names_are_metadata(self):
        for body in (
            "指南第3页“未来计划”栏列出运动安排。",
            "报告第6页‘未来规划’栏目列出测试方向。",
            "课程材料的栏目为‘当前练习’。",
            "课程材料的字段名称为‘正在学习’。",
        ):
            with self.subTest(body=body):
                self.valid(body)
        for body in (
            "报告第3页以未来计划介绍运动安排；计划提出日期及目标期限未知。",
            "材料以未来规划介绍测试方向；提出时间及完成期限未知。",
            "课程说明用未来展望描述练习项目；提出日期与目标日期未知。",
        ):
            with self.subTest(body=body):
                self.valid(body, basis="unknown")

    def test_conditional_applicability_and_input_evidence_limits_are_nonassertive(self):
        for body in (
            "如果未来训练负荷变化时采用备选方案。",
            "单项任务使用基础方法；多任务或未来部署需求增长时选择组合方法。",
            "若未来复习内容增加时可使用分层练习法。",
            "当前文字视图未保留足以独立确认表头对应的信息。",
            "当前笔记缺少用于核验动作次数的证据。",
            "目前文本无法支持题目与答案关系的独立确认。",
        ):
            with self.subTest(body=body):
                self.valid(body)

    def test_labeled_alternative_conditions_can_use_a_method_after_a_comma(self):
        for body in (
            "训练方案选择条件是：负荷增加或未来需求变化，采用分段训练。",
            "工具选择条件为：单项任务使用基础方法；多项任务或未来协作扩展，使用组合工具。",
            "练习方法适用条件：多主题或未来复习范围调整，选择分层练习。",
        ):
            with self.subTest(body=body):
                self.valid(body)

    def test_labeled_conditions_preserve_dates_actual_states_and_other_clauses(self):
        for body in (
            "多项任务或未来协作扩展，使用组合工具。",
            "工具选择条件是：未来将会采用新工具。",
            "训练方案选择条件是：未来暑假，采用训练方案。",
            "练习方法适用条件是：未来2028年5月，使用新教材。",
            "练习方法适用条件是：多主题或未来2028年范围调整，选择分层练习。",
            "‘选择条件是’为标签；多项任务或未来协作扩展，使用组合工具。",
            "“选择条件是：作为标签；多项任务或未来协作扩展，使用组合工具。",
            "”选择条件是：作为标签“；多项任务或未来协作扩展，使用组合工具。",
            "不是选择条件是：多项任务或未来协作扩展，使用组合工具。",
            "并非工具选择条件是：多项任务或未来协作扩展，使用组合工具。",
            "选择条件是：多项任务或未来已经上线的系统扩展，使用组合工具。",
            "选择条件是：多项任务或未来协作扩展，使用当前已经发布的工具。",
            "选择条件是：多项任务或未来协作扩展，使用工具并经过跨部门讨论及多项技术验证后系统已经上线。",
            "选择条件是：多主题或未来复习范围调整，选择分层练习。用户明天毕业。",
            "选择条件是：多项任务。多项任务或未来协作扩展，使用组合工具。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_bare_negative_inference_cues_are_nonassertive_across_three_domains(self):
        for verb in ("断言", "判断", "推断", "宣称", "声称", "认定"):
            for state in ("用户已经康复", "系统当前已经上线", "用户将来掌握课程"):
                for lead in ("", "材料", "这些是资料说明，"):
                    with self.subTest(verb=verb, state=state, lead=lead):
                        self.valid(lead + "不" + verb + state + "。")
                        self.valid(lead + "不" + verb + state + "。", basis="unknown")

    def test_bare_inference_denials_keep_subject_quote_and_negation_boundaries(self):
        for body in (
            "用户不断言已经康复。",
            "并非用户不断言已经康复。",
            "不是系统不声称当前已经上线。",
            "不能不断言已经康复。",
            "并非材料不断言用户已经掌握课程。",
            "用户当前已经康复的报告不断言恢复程度。",
            "‘不断言’已经加入词典。",
            "不认定已经完成可是系统已经上线。",
            "不断言已经完成，同时经过跨部门讨论及多项技术验证后系统已经上线。",
            "当前已经掌握课程，不断言水平提高。",
            "不断言已经康复，用户将来完成训练。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_incomplete_cognitive_time_descriptions_explicitly_preserve_unknowns(self):
        for body in (
            "报告只列训练间隔9日，但未完整明确两端日期。",
            "材料介绍测试安排，未充分说明具体时间。",
            "课程说明列出练习顺序，未完全确定起止日期。",
        ):
            with self.subTest(body=body):
                self.valid(body, basis="unknown")

    def test_incomplete_time_wording_does_not_cover_states_or_denied_gaps(self):
        for body in (
            "材料未完整明确训练安排。",
            "材料尚未完成。",
            "并非未完整明确两端日期。",
            "不是材料未充分说明具体时间。",
            "未完整明确两端日期，用户已经康复。",
            "未充分说明具体时间而系统当前已经上线。",
            "未完全确定起止日期，用户将来完成课程。",
            "‘未完整明确两端日期’已经加入词表。",
        ):
            with self.subTest(body=body):
                self.invalid(body, basis="unknown")

    def test_new_expressions_do_not_bypass_refs_calendars_or_system_facts(self):
        bodies = (
            "工具选择条件是：多项任务或未来协作扩展，使用组合工具。",
            "不断言系统已经上线。",
            "未完整明确两端日期。",
        )
        for body in bodies:
            with self.subTest(body=body):
                self.invalid(body, "quality_current_state_unverified", basis="current_state")
                self.invalid("报告按2027 H1列示3项结果；" + body,
                    basis="source_time", original="人工报告：2027 H2。")
                output, context = self.material(body, basis="unknown")
                output["quality"]["temporal_claims"][0]["source_refs"][0]["quote"] = "无关人工材料。"
                with self.assertRaises(CaptureError) as caught:
                    validate_quality_output(output, context)
                self.assertEqual("quality_source_reference_mismatch", caught.exception.code)

    def test_new_expressions_remain_v3_only(self):
        for extract, review in (("knowledge-extract@11", "knowledge-review@9"),
                                ("knowledge-extract@15", "knowledge-review@13")):
            bundle = resolve_skill_refs(extract, review)
            with self.subTest(extract=extract):
                self.valid("选择条件是：多项任务或未来协作扩展，使用组合工具。", bundle=bundle)
                self.invalid("不断言用户已经掌握课程。", "quality_temporal_missing", bundle=bundle)
                self.invalid("未完整明确两端日期。", basis="unknown", bundle=bundle)

    def test_conditional_or_evidence_frames_do_not_erase_actual_states(self):
        for body in (
            "未来部署将会采用工具。",
            "未来暑假时采用练习方法。",
            "如果未来将会康复时采用训练方案。",
            "若未来复习时使用当前已经发布的教材。",
            "多任务或未来部署时采用工具并系统后天发布。",
            "‘若未来部署时采用’已经成为词条。",
            "当前文字视图已经发布。",
            "当前材料已经写入。",
            "当前笔记已经同步。",
            "当前表格已经处理完成。",
            "当前材料尚未发布。",
            "当前笔记未保留用户当前已经康复的证据。",
            "当前表格缺少用于核验列名的信息且系统将会上线。",
            "‘当前表格缺少用于核验的信息’已经成为词条。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_metadata_excludes_only_the_name_not_actual_states(self):
        for body in (
            "未来规划包括测试流程。",
            "指南第3页“未来计划”栏列出用户当前已经康复。",
            "报告第6页‘未来规划’栏目已经启用。",
            "栏目为‘当前练习’，用户将来实际完成课程。",
            "报告第3页以未来计划介绍用户当前已经康复。",
            "材料以未来规划介绍系统明天将会发布；提出时间未知。",
            "课程说明用未来展望描述用户已经掌握课程。",
            "报告第3页当前已经发布并以未来规划介绍流程。",
            "第3页以未来规划介绍“当前已经上线”。",
            "‘第3页以未来规划介绍’已经成为课程术语。",
            "“栏目为‘未来规划’”已经加入注释。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_an_incidental_nontemporal_disclaimer_does_not_supply_time(self):
        for body in (
            "报告称训练样本为3组，不据此推断用户恢复程度。",
            "分享称测试覆盖率为42%，不据此判断其他平台质量。",
            "课程材料列出5项练习，不据此认定课程难度。",
        ):
            for basis in ("unknown", "source_time"):
                with self.subTest(body=body, basis=basis):
                    self.invalid(body, basis=basis)

    def test_every_future_predicate_preserves_its_independent_clause(self):
        for cue in ("不据此断言已经康复", "报告不构成已经完成的证明", "不能把未来计划认定为已实现"):
            for conjunction in ("且", "并", "同时"):
                for future in ("将来", "后天", "下周", "下月", "下年", "将要", "即将"):
                    body = cue + conjunction + "系统" + future + "发布。"
                    with self.subTest(body=body):
                        self.invalid(body, "quality_temporal_missing")
                self.invalid(cue + conjunction + "system will publish.", "quality_temporal_missing")

    def test_long_coordinated_prefixes_do_not_hide_real_states(self):
        for cue in ("报告不构成用户已经康复的证明", "不能把未来规划认定为已实现", "方案不能视为已经采用"):
            for conjunction in ("且", "并", "同时"):
                for state in ("系统已经上线", "用户当前已经康复", "用户后天将会完成课程"):
                    body = cue + conjunction + "经过跨团队沟通与多轮独立检查及额外验证以后" + state + "。"
                    with self.subTest(cue=cue, conjunction=conjunction, state=state):
                        self.invalid(body, "quality_temporal_missing")

    def test_material_relations_deny_evidence_or_attribution_not_the_state(self):
        for body in (
            "这不构成本轮已经读取记录的证明。",
            "报告不构成用户已经康复的证明。",
            "课程目录不表示用户当前已经掌握课程。",
            "方案不能视为用户已经采用方案。",
            "该要求是资料整理用途，不能视为用户已经实施方案。",
            "这是讲者的方向性判断，不构成用户决定或已经完成的转型。",
            "这是课程的学习建议，不构成用户决定或已经完成的学习。",
            "不能把未来阶段展示直接认定为已实现。",
            "不能把未来方案看作用户已经康复。",
            "不能把该份材料等同于用户已经掌握课程。",
            "材料写道“这不构成本轮已经读取记录的证明”。",
        ):
            for basis in (None, "unknown"):
                with self.subTest(body=body, basis=basis):
                    self.valid(body, basis=basis)

    def test_material_relations_preserve_real_subject_states_and_boundaries(self):
        for body in (
            "患者不能视为已经康复。",
            "系统不构成已经上线的产品。",
            "用户不表示已经掌握课程。",
            "不能视为已经康复。",
            "该要求是资料整理用途。不能视为已经实施方案。",
            "不能把当前已经发布的报告认定为已经完成。",
            "不能把未来将会完成的方案认定为已经实施。",
            "当前已经发布的报告不构成用户已经采用的证明。",
            "报告不构成已经康复的证明可是用户已经康复。",
            "方案不能视为已经采用却已经发布。",
            "不能把未来规划认定为已实现且系统已经上线。",
            "不能把未来规划认定为已实现，明天将会完成。",
            "“不构成”已经加入术语表。",
            "“不能视为”已经成为关键词。",
            "“不能把未来规划认定为”已经加入术语表。",
            "材料写道“不构成已经康复的证明”可是用户已经康复。",
            "并非不构成已经康复的证明。",
            "不是不能视为用户已经掌握课程。",
            "否认不能把未来方案认定为已实现。",
            "不能不构成已经完成的转型。",
            "并非报告不构成用户已经康复的证明。",
            "并非方案不能视为用户已经采用方案。",
            "不是这份材料不表示用户已经掌握课程。",
            "否认这不构成本轮已经读取记录的证明。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_negative_attribution_and_coordinated_material_subjects(self):
        for relation in ("转写为", "转述成", "改写为", "表述成"):
            for material, state in (
                ("训练方案", "用户已经康复"),
                ("测试用途", "系统已经上线"),
                ("课程目录", "用户已经掌握课程"),
            ):
                body = material + "不能" + relation + state + "，也不表示记录已经完成。"
                self.valid("不能把该份材料" + relation + state + "。")
                for basis in (None, "unknown"):
                    with self.subTest(body=body, basis=basis):
                        self.valid(body, basis=basis)

    def test_negative_proof_identity_requires_material_and_evidence_roles(self):
        for body in (
            "这是训练资料的判断，不是用户当前状态的核验。",
            "这是设计材料的建议，不是系统目前状态的验证。",
            "课程报告不是用户现在掌握程度的证明。",
            "这是讲者当时的诊断，不是用户或企业当前状态。",
            "统计期间未明确，不能视为当前能力保证。",
        ):
            with self.subTest(body=body):
                self.valid(body)
        for body in (
            "用户不是当前已经康复的人。",
            "系统不是当前已经发布的平台。",
            "报告不是系统已经发布后给出的验证。",
            "不是报告不是企业当前状态的核验。",
            "这是材料的判断。不是系统当前状态的核验。",
            "“不是当前状态的核验”已经成为词条。",
            "这是材料的判断，不是系统当前状态的验证但系统明天将会发布。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_attribution_coordination_does_not_inherit_arbitrary_negation(self):
        for body in (
            "患者不能转述为已经康复。",
            "系统不能转写为已经上线。",
            "报告不包含流程图，也不表示系统已经上线。",
            "当前已经发布的方案不能转写为用户已经采用。",
            "不能把当前已经发布的报告转述为用户已经采用。",
            "方案不能转述为已经实施。也不表示系统已经上线。",
            "方案不能转述为已经实施；也不表示用户已经学会。",
            "并非用途不能转写为用户已经采用，也不表示系统已经上线。",
            "方案不能转写为已经采用，也不表示已经完成且系统后天发布。",
            "“不能转写为”已经加入词表。",
            "材料写道“方案不能转写为已经实施，也不表示已经完成”已经成为条目。",
            "材料写道`方案不能转写为已经实施，也不表示已经完成`已经成为条目。",
            "用户不等于已经康复，也不表示记录已经完成。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_new_relations_and_periods_do_not_change_legacy_time_rules(self):
        for extract, review in (("knowledge-extract@11", "knowledge-review@9"),
                                ("knowledge-extract@15", "knowledge-review@13")):
            bundle = resolve_skill_refs(extract, review)
            with self.subTest(extract=extract):
                self.invalid("报告不构成用户已经康复的证明。", "quality_temporal_missing", bundle=bundle)
                self.invalid("报告按2024 H1列示训练次数。", basis="source_time",
                    original="人工报告：2024 H1。", bundle=bundle)

    def test_new_relations_do_not_supply_current_system_fact_authority(self):
        self.invalid("报告不构成已经完成的证明。当前已经上线。",
            "quality_current_state_unverified", basis="current_state")

    def test_negative_inference_predicates_preserve_quote_and_clause_boundaries(self):
        for body in (
            "“不能据此宣称”三个字已经加入术语表。",
            "“不能据此声称”已经成为关键词。",
            "“不能由此认定”已经加入课程词表。",
            "材料写道“不能据此认定已经完成”但当前已经完成。",
            "当前已经上线，不能据此宣称下一阶段完成。",
            "不能据此声称已经上线，当前已经上线。",
            "不能据此认定已经康复且用户当前已经康复。",
            "不能据此宣称已经学会。明天将会完成。",
            "并非不能据此宣称用户已经康复。",
            "不是不能据此声称当前已经上线。",
            "并不是不能由此认定用户已经学会。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_negative_inference_does_not_authorize_an_actual_current_system_fact(self):
        self.invalid("不能据此宣称已经完成。当前已经上线。",
            "quality_current_state_unverified", basis="current_state")

    def test_legacy_v2_does_not_gain_negative_inference_predicates(self):
        previous = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.invalid("不能据此宣称用户已经康复。", "quality_temporal_missing", bundle=previous)

    def test_bounded_material_reference_disclaimers_across_three_domains(self):
        for prefix in (
            "不能由这段历史说明判断", "无法从该份资料推断", "不能根据上述报告证明",
            "无法依据原文认定", "不能仅凭这些原始记录宣称", "无法只根据前述材料声称",
            "不能单由本次提供的文本确认", "无法凭该页相关说明确定", "不能从那一篇文档来断言",
        ):
            for state in ("用户当前已经康复", "系统当前已经上线", "用户目前已经掌握课程"):
                for basis in (None, "unknown"):
                    with self.subTest(prefix=prefix, state=state, basis=basis):
                        self.valid(prefix + state + "。", basis=basis)

    def test_material_reference_disclaimers_preserve_actual_assertions(self):
        for body in (
            "当前已经康复，不能由这份资料判断恢复程度。",
            "不能从该份资料推断当前状态，系统已经上线。",
            "无法依据原文认定已经学会；用户当前已经掌握课程。",
            "不能根据上述报告判断当前状态但明天将会完成。",
            "不能凭该记录证明当前状态且系统已经上线。",
            "“不能由这份资料判断”三个字已经加入备注。",
            "“无法依据原文认定”将会加入课程词表。",
            "材料写道“不能从原文判断已经完成”但当前已经完成。",
            "并非不能由这份资料判断用户已经康复。",
            "不是无法根据上述报告推断系统已经上线。",
            "并不是不能凭原文认定用户已经学会。",
            "并非 不能由这份资料判断用户已经康复。",
            "不是 无法根据上述报告推断系统已经上线。",
            "否认不能凭原文认定用户已经学会。",
            "不可能无法由该报告判断用户当前状态。",
            "不能不根据该资料判断当前状态。",
            "不能根据这份已经发布的报告判断系统当前状态。",
            "无法由该说明系统已经上线判断当前状态。",
            "不能凭某些随意的字词判断当前状态。",
            "不能由这段历史说明，判断当前状态。",
            "不能由这段\n历史说明判断当前状态。",
            "不能由这份资料判断当前状态可是系统已经上线。",
            "不能由这份资料判断当前状态却已经上线。",
        ):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_material_reference_does_not_cover_adversative_unknown_claims(self):
        for body in ("不能由这份资料判断当前状态可是系统已经上线。",
                     "不能由这份资料判断当前状态却已经上线。"):
            with self.subTest(body=body):
                self.invalid(body, basis="unknown")

    def test_denied_material_reference_does_not_count_as_an_unknown_disclaimer(self):
        for body in ("并非 不能由这份资料判断当前状态。", "否认不能从资料推断。"):
            for basis in ("unknown", "source_time"):
                with self.subTest(body=body, basis=basis):
                    self.invalid(body, basis=basis)

    def test_material_reference_disclaimer_does_not_bypass_source_validation(self):
        for field, value, code in (
            ("source_id", "src-outside-selected-material", "quality_source_reference_mismatch"),
            ("quote", "人工但不是所引原文。", "quality_source_reference_mismatch"),
            ("locator", "L999-L999", "quality_source_reference_mismatch"),
            ("source_version", None, "invalid_quality_output"),
        ):
            output, context = self.material("不能由这份资料判断当前恢复状态。", basis="unknown")
            output["quality"]["temporal_claims"][0]["source_refs"][0][field] = value
            with self.subTest(field=field), self.assertRaises(CaptureError) as caught:
                validate_quality_output(output, context)
            self.assertEqual(caught.exception.code, code)

    def test_material_reference_disclaimer_still_needs_current_system_fact_authority(self):
        self.invalid("不能由这份资料判断当前状态。当前已经上线。",
            "quality_current_state_unverified", basis="current_state")

    def test_legacy_v1_and_v2_do_not_gain_material_reference_disclaimers(self):
        for extract, review in (("knowledge-extract@11", "knowledge-review@9"),
                                ("knowledge-extract@15", "knowledge-review@13")):
            with self.subTest(extract=extract):
                self.invalid("不能由这份资料判断当前恢复状态。", "quality_temporal_missing",
                    bundle=resolve_skill_refs(extract, review))

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
