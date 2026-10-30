"""Artificial v3 time assertions and prose/CSV admission, with legacy controls."""

from copy import deepcopy
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import (SOURCE_QUALITY_V3, collect_temporal_diagnostics,
    prepare_quality, project_quality, validate_quality_output, build_review_checks, validate_quality_review)
from pdm.skill_support import resolve_skill_refs
from test_knowledge_quality import source, ref, view


class StableTemporalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.current = resolve_skill_refs("knowledge-extract@16", "knowledge-review@14")
        self.previous = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")

    def material(self, body, *, basis=None, original="人工来源在导出当时尚未提交。", bundle=None):
        item = source(original, "artificial-stable-time")
        context = prepare_quality([item], skill_bundle=self.current if bundle is None else bundle)
        output = {"objects": [{"id": "artificial-time", "body": body, "sources": [ref(item)]}],
            "quality": {"coverage_disposition": [{"target_id": target["target_id"],
                "status": "summarized", "record_ids": ["artificial-time"],
                "body_evidence": [{"record_id": "artificial-time", "text": body}], "reason": ""}
                for target in context["targets"]], "statistic_claims": [], "temporal_claims": []}}
        if basis is not None:
            output["quality"]["temporal_claims"] = [{"record_id": "artificial-time",
                "statement": body, "basis": basis, "source_refs": [ref(item)], "system_fact_id": None}]
        return output, context

    def valid(self, body, **options):
        output, context = self.material(body, **options)
        before = deepcopy((output, context))
        validate_quality_output(output, context)
        self.assertEqual([], collect_temporal_diagnostics(output, context))
        self.assertEqual(before, (output, context))
        return output, context

    def invalid(self, body, code="quality_temporal_mismatch", **options):
        output, context = self.material(body, **options)
        with self.assertRaises(CaptureError) as caught:
            validate_quality_output(output, context)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual(code, collect_temporal_diagnostics(output, context)[0]["code"])
        return caught.exception

    def test_narrow_evidence_disclaimers_work_across_three_domains(self):
        for body in (
            "本文仅整理来源方法，不证明用户当前健康状态。",
            "这些是文档方案，不代表用户已经承诺或本系统当前状态。",
            "课程目录不能据此断言用户已经完成学习。",
            "这些记录不构成用户当前健康状态的证据。",
            "这份方案不构成个人职责或本系统当前状态的依据。",
            "课程目录不能作为用户已经完成学习的依据。",
            "单份样本无法证明用户当前健康状态。",
            "文档无法据此证明系统已经上线。",
        ):
            with self.subTest(body=body):
                self.valid(body)

    def test_literal_field_names_are_not_time_assertions(self):
        for body in ("字段名为“当前阶段”，只说明一个标签。",
                     "“已经完成”是字段名称，不是执行回执。",
                     '列名"当前练习项目"只是原文字段名。',
                     "字段名为“当前，已经完成”，只是字面名称。"):
            with self.subTest(body=body):
                self.valid(body)

    def test_disclaimer_or_literal_name_cannot_hide_an_actual_later_assertion(self):
        for body in ("本文不证明当前状态，但当前已经全部发布。",
                     "本记录不代表已经完成。现在已经全部完成。",
                     "字段名为“当前状态”，目前已经写入资料。",
                     "不能据此断言当前状态且明天将会完成发布。",
                     "本文不证明当前状态而当前已经发布。",
                     "本文不证明当前状态同时当前已经发布。",
                     "这份说明不构成用户当前状态的证据但当前已经发布。",
                     "目录不能作为已经完成学习的依据，现在已经完成。",
                     "资料无法证明当前状态而当前已经提交。",
                     "当前已经发布，不构成其他主张的证据。"):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_real_negative_facts_still_require_a_temporal_declaration(self):
        for body in ("用户尚未提交材料。", "这项课程尚未完成。", "原文未导入资料。"):
            with self.subTest(body=body):
                self.invalid(body, "quality_temporal_missing")

    def test_historical_and_unknown_claims_cannot_hide_current_or_future_clauses(self):
        for basis, body in (
            ("source_time", "导出当时尚未提交。当前已经全部发布。"),
            ("source_time", "导出当时尚未提交，但当前已经全部发布。"),
            ("source_time", "导出当时尚未提交并且明天将会发布。"),
            ("unknown", "当前状态未知。未来将会全部发布。"),
            ("unknown", "当前状态未知，但当前已经发布。"),
            ("unknown", "当前已经发布时间未知。"),
            ("unknown", "当前是否已经导入未知而当前已经发布。"),
            ("unknown", "当前是否已经导入未知当前已经发布。"),
            ("source_time", "文档当时称不证明当前状态，当前已经发布。"),
        ):
            with self.subTest(basis=basis, body=body):
                error = self.invalid(body, basis=basis)
                self.assertEqual("/quality/temporal_claims/0/statement", error.details["field_path"])
                location = error.details["body_location"]
                self.assertIn("/objects/0/body", location["field_path"])

    def test_valid_source_time_and_unknown_statements_remain_valid(self):
        self.valid("导出当时尚未提交。", basis="source_time")
        self.valid("导出当时，尚未提交。", basis="source_time")
        self.valid("导出当时尚未提交，也尚未导入。", basis="source_time")
        self.valid("来源当时称：当前阶段尚未完成。", basis="source_time")
        self.valid("文档当时称，目前尚未提交。", basis="source_time")
        self.valid("当前状态未知。", basis="unknown")
        self.valid("当前是否已经完成未知。", basis="unknown")
        self.valid("这些文字不证明当前状态。", basis="unknown")

    def test_supported_calendar_dates_stay_bound_to_their_own_source(self):
        self.valid("原表2025年1月1日尚未完成。", basis="source_time",
                   original="2025-01-01，人工来源尚未完成。")
        self.invalid("原表2025年1月2日尚未完成。", basis="source_time",
                     original="2025-01-01，人工来源尚未完成。")
        self.invalid("2025-01-01是来源日期。当前已经导入。", basis="source_time",
                     original="2025-01-01，人工来源尚未完成。")

    def test_current_state_still_needs_an_exact_bound_system_fact(self):
        self.invalid("当前已经发布。", "quality_current_state_unverified", basis="current_state")
        output, context = self.material("当前已经发布。", basis="current_state")
        fact = {"system_fact_id": "artificial-system-fact", "statement": "当前已经发布。",
                "binding": "artificial-bound-version"}
        context["system_facts"] = [fact]
        output["quality"]["temporal_claims"][0]["system_fact_id"] = fact["system_fact_id"]
        validate_quality_output(output, context)
        output["quality"]["temporal_claims"][0]["statement"] = "当前已经"
        self.assertEqual("quality_current_state_unverified",
                         collect_temporal_diagnostics(output, context)[0]["code"])

    def test_collector_reports_independent_time_errors_without_mutating_inputs(self):
        output, context = self.material("当时尚未完成。当前已经发布。", basis="source_time")
        other = {"id": "artificial-other", "body": "当前已经完成。", "sources": output["objects"][0]["sources"]}
        output["objects"].append(other)
        before = deepcopy((output, context))
        errors = collect_temporal_diagnostics(output, context)
        self.assertEqual(["quality_temporal_mismatch", "quality_temporal_missing"],
                         [error["code"] for error in errors])
        self.assertEqual(["artificial-time", "artificial-other"],
                         [error["details"]["record_id"] for error in errors])
        self.assertEqual(before, (output, context))

    def test_collector_handles_invalid_time_shape_and_ignores_legacy_policy(self):
        output, context = self.material("人工资料说明。")
        output["quality"]["temporal_claims"] = [None]
        self.assertEqual("invalid_quality_output", collect_temporal_diagnostics(output, context)[0]["code"])
        self.assertEqual([], collect_temporal_diagnostics({}, prepare_quality([source("人工旧资料。")])))

    def test_legacy_policy_retains_its_frozen_time_contract(self):
        output, context = self.material("本文不能据此断言当前状态。", bundle=self.previous)
        with self.assertRaises(CaptureError) as caught:
            validate_quality_output(output, context)
        self.assertEqual("quality_temporal_missing", caught.exception.code)
        output, context = self.material("导出当时尚未提交。当前已经全部发布。",
                                       basis="source_time", bundle=self.previous)
        validate_quality_output(output, context)

    def test_new_review_keeps_the_final_deferred_completion_gate(self):
        output, context = self.material("人工资料内容尚需整理。")
        row = output["quality"]["coverage_disposition"][0]
        row.update(status="deferred", record_ids=[], body_evidence=[], reason="Artificial material is unfinished.")
        candidate = {"objects": [{"path": "memory/records/artificial-time.md",
                        "body": output["objects"][0]["body"],
                        "metadata": {key: value for key, value in output["objects"][0].items() if key != "body"}}],
            "extraction_output": output, "quality_contract": "skill-quality-v1", "skill_bundle": self.current}
        checks = build_review_checks(candidate, context)
        for check in checks:
            if check["kind"] in {"semantic_coverage", "global_organization"}:
                self.assertEqual(["needs_revision", "needs_confirmation"], check["allowed_results"])
        review = {"verdict": "passed", "reasons": ["Artificial invalid all-passed result."],
            "checks": [{"check_id": check["check_id"], "scope_sha256": check["scope_sha256"],
                "result": "passed", "reason": "Artificial all-passed must not override deferred content.",
                "evidence_ids": check["evidence_ids"]} for check in checks],
            "repair_scope": {"kind": "none", "missing_bindings": []}}
        with self.assertRaises(CaptureError) as caught:
            validate_quality_review(review, checks, repair_scope_supported=True)
        self.assertEqual("quality_review_incomplete", caught.exception.code)


class StableCsvAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.current = resolve_skill_refs("knowledge-extract@16", "knowledge-review@14")
        self.previous = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")

    def prepare(self, text, **options):
        return prepare_quality([source(text)], skill_bundle=self.current, **options)

    def test_single_punctuation_note_is_semantic_instead_of_an_empty_table(self):
        for text in ("Artificial project note, this is prose.\n", "人工说明;仅为一条笔记。\n",
                     "人工甲,人工乙。\n"):
            with self.subTest(text=text):
                context = self.prepare(text)
                self.assertEqual(SOURCE_QUALITY_V3, context["policy_version"])
                self.assertEqual("text", context["sources"][0]["format"])
                self.assertEqual("semantic", context["targets"][0]["kind"])
                self.assertEqual([], context["statistics"])
                old = prepare_quality([source(text)], skill_bundle=self.previous)
                self.assertEqual("csv", old["sources"][0]["format"])
                self.assertTrue(all(target["counts"]["rows"] == 0 for target in old["targets"]))

    def test_explicit_empty_csv_headers_keep_their_zero_row_table(self):
        for text in ('\ufeff"date","steps"\n', 'date,steps\n', '"title","status"\n'):
            with self.subTest(text=text):
                context = self.prepare(text)
                self.assertEqual("csv", context["sources"][0]["format"])
                self.assertTrue(context["targets"])
                self.assertTrue(all(target["counts"]["rows"] == 0 for target in context["targets"]))

    def test_valid_csv_bom_and_source_hashes_keep_their_original_bytes(self):
        text = '\ufeff"date","steps"\n"2025-01-01","4"\n"2025-01-02","8"\n'
        item = source(text)
        before = deepcopy(item)
        context = prepare_quality([item], skill_bundle=self.current)
        old = prepare_quality([item], skill_bundle=self.previous)
        old["policy_version"] = SOURCE_QUALITY_V3
        self.assertEqual(old, context)
        self.assertEqual(before, item)
        self.assertEqual(["date", "steps"], context["sources"][0]["tables"][0]["headers"])

    def test_v3_policy_reconstruction_projection_and_conflicting_skill_guard(self):
        item = source("人工笔记,不是表格。\n")
        current = self.prepare(item["body"])
        self.assertEqual(current, prepare_quality([item], policy_version=SOURCE_QUALITY_V3))
        self.assertEqual(current, project_quality(current))
        with self.assertRaises(CaptureError):
            prepare_quality([item], skill_bundle=self.previous, policy_version=SOURCE_QUALITY_V3)

    def test_missing_header_and_multiline_csv_still_preserve_physical_line_bounds(self):
        item = source('date,steps,note\n2025-01-01,4,"one\ntwo"\n')
        current = prepare_quality([item], skill_bundle=self.current)
        self.assertEqual("csv", current["sources"][0]["format"])
        self.assertEqual(["L1-L3"], current["targets"][0]["source_locators"])
        self.assertTrue(any(scope["source_locators"] == ["L2-L3"]
                            for scope in current["targets"][0]["statistic_scopes"].values()))
        fragment = view(item, [("only-row", 2, 3)])
        limited = prepare_quality([item], [fragment], skill_bundle=self.current)
        self.assertEqual("header_not_provided", limited["sources"][0]["table_parse_status"])
        self.assertEqual("semantic", limited["targets"][0]["kind"])


if __name__ == "__main__":
    unittest.main()
