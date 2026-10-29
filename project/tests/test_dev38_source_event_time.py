"""Artificial definite-past source events retain strict current-state checks."""
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import prepare_quality, validate_quality_output
from quality_fixtures import add_quality
import test_knowledge_quality as fixtures


class SourceEventTimeTests(unittest.TestCase):
    def material(self, statement, *, policy="source-quality-v2"):
        item = fixtures.source("人工来源：采集和导出均仅记述该来源的历史事件。\n", "event")
        context = prepare_quality([item], policy_version=policy)
        output = add_quality(fixtures.extraction(item, statement), context)
        output["quality"]["temporal_claims"] = [{"record_id": "synthetic",
            "statement": statement, "basis": "source_time",
            "source_refs": [fixtures.ref(item)], "system_fact_id": None}]
        return output, context

    def rejected(self, statement, *, policy="source-quality-v2", code="quality_temporal_mismatch"):
        output, context = self.material(statement, policy=policy)
        with self.assertRaises(CaptureError) as caught:
            validate_quality_output(output, context)
        self.assertEqual(code, caught.exception.code)

    def test_definite_past_export_acquisition_and_reading_are_source_time(self):
        for event in ("该次导出", "当次采集", "那次读取", "上次导出", "前次采集"):
            with self.subTest(event=event):
                output, context = self.material(event + "没有取得可核验文件。")
                validate_quality_output(output, context)

    def test_legacy_policy_retains_its_exact_rejection_boundary(self):
        self.rejected("该次导出没有取得可核验文件。", policy="source-quality-v1")

    def test_new_branch_does_not_accept_current_or_ongoing_assertions(self):
        for word in ("当前", "目前", "现在", "现已", "正在", "此刻", "currently"):
            with self.subTest(word=word):
                self.rejected("该次导出" + word + "处于执行状态。")

    def test_new_branch_does_not_accept_future_or_planned_events(self):
        for word in ("将", "会", "预计", "预期", "将会", "将要", "即将", "未来", "明天", "计划", "打算", "准备", "will"):
            with self.subTest(word=word):
                self.rejected("该次采集" + word + "取得文件。")

    def test_this_or_unspecified_event_does_not_prove_historical_time(self):
        for statement in ("本次导出没有取得文件。", "此次读取没有取得文件。", "没有取得下载文件。"):
            with self.subTest(statement=statement):
                self.rejected(statement)

    def test_historical_event_still_requires_bound_source_reference(self):
        output, context = self.material("该次导出没有取得文件。")
        output["quality"]["temporal_claims"][0]["source_refs"][0]["quote"] = "并不存在的原文"
        with self.assertRaises(CaptureError):
            validate_quality_output(output, context)

    def test_historical_event_does_not_bypass_current_state_system_fact(self):
        output, context = self.material("该次导出没有取得文件。")
        output["quality"]["temporal_claims"][0].update(basis="current_state", system_fact_id="made-up")
        with self.assertRaises(CaptureError) as caught:
            validate_quality_output(output, context)
        self.assertEqual("quality_current_state_unverified", caught.exception.code)

    def test_legacy_qualified_negative_current_comparison_keeps_old_acceptance(self):
        for policy in ("source-quality-v1", "source-quality-v2"):
            with self.subTest(policy=policy):
                output, context = self.material("导出当时尚未入库，不证明现在状态。", policy=policy)
                validate_quality_output(output, context)


if __name__ == "__main__":
    unittest.main()
