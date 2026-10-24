"""Synthetic-only tests of deterministic material and mandatory quality gates."""

import copy
import hashlib
import json
import unittest
from datetime import date, timedelta

from pdm.contracts import CaptureError
from pdm.knowledge_quality import (
    build_review_checks, prepare_quality, project_quality, resolve_statistic, validate_quality_output,
    validate_quality_review,
)
from quality_fixtures import add_quality, passed_review


def source(body, name="one", kind="note"):
    return {"source_id": "src-" + name, "source_version": "sha256-" + hashlib.sha256(body.encode()).hexdigest(),
            "path": f"memory/sources/{name}.md", "body": body,
            "metadata": {"source_id": "src-" + name,
                         "source_version": "sha256-" + hashlib.sha256(body.encode()).hexdigest(),
                         "source_type": kind, "original_time": None}}


def ref(item, line=1):
    return {"source_id": item["source_id"], "source_version": item["source_version"],
            "locator": f"L{line}-L{line}", "quote": item["body"].splitlines()[line - 1]}


def view(item, fragments):
    return {"path": item["path"], "metadata": item["metadata"],
            "fragments": [{"fragment_id": name, "locator": f"L{start}-L{end}",
                           "lines": [{"line": number, "text": item["body"].splitlines()[number - 1]}
                                     for number in range(start, end + 1)]}
                          for name, start, end in fragments]}


def extraction(item, body="这是一条人工测试记录。"):
    return {"objects": [{"id": "synthetic", "body": body, "sources": [ref(item)]}],
            "wiki": {"id": "synthetic-topic", "body": body, "sources": [ref(item)]}}


def candidate(output):
    return {"objects": [{"path": "memory/records/" + record["id"] + ".md",
                         "body": record["body"],
                         "metadata": {key: value for key, value in record.items() if key != "body"}}
                        for record in output["objects"]], "extraction_output": output}


def statistics(context):
    return [resolve_statistic(context, item) for item in context["statistics"]]


class KnowledgeQualityTests(unittest.TestCase):
    def assert_error(self, code, action):
        with self.assertRaises(CaptureError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def test_bom_counts_zero_missing_and_structural_columns_are_distinct(self):
        item = source("\ufeff日期,时区,当日未完结,睡眠得分,步数,步数目标\n"
                      "2025-01-01,Asia/Shanghai,否,80,0,100\n"
                      "2025-01-02,Asia/Shanghai,否,--,12,100\n"
                      "2025-01-03,Asia/Shanghai,是,,4,100\n")
        context = prepare_quality([item])
        table = context["sources"][0]["tables"][0]
        self.assertEqual(table["structure_columns"], ["日期", "时区", "当日未完结"])
        self.assertEqual(table["partial_day_locators"], ["L4-L4"])
        self.assertEqual(table["date_interval"], ["2025-01-01", "2025-01-03"])
        self.assertEqual(len(context["targets"]), 3)
        score = next(target for target in context["targets"] if target["column"] == "睡眠得分")
        self.assertEqual(score["counts"], {"rows": 3, "present": 1, "blank": 1, "declared_missing": 1,
                                          "literal_markers": {"--": 1}, "numeric": 1, "zero": 0, "invalid_numeric": 0})
        steps = next(target for target in context["targets"] if target["column"] == "步数")
        self.assertEqual(steps["counts"]["zero"], 1)
        self.assertEqual(steps["descriptive_n"], 2)
        mean = next(stat for stat in statistics(context) if stat["target_id"] == steps["target_id"] and stat["operation"] == "mean")
        self.assertEqual(mean["value"], "6")
        self.assertEqual(mean["valid_n"], 2)
        self.assertEqual(mean["source_locators"], ["L2-L3"])
        self.assertEqual(next(target for target in context["targets"] if target["column"] == "步数目标")["role"], "target")

    def test_duplicate_dates_invalid_dates_and_numeric_errors_are_not_averaged(self):
        item = source("日期,当日未完结,步数\n"
                      "2025-01-01,否,1\n2025-01-01,否,2\n"
                      "2025-02-30,否,3\n2025-01-03,否,broken\n"
                      "2025-01-04,否,0\n2025-01-05,?,9\n")
        context = prepare_quality([item])
        target = context["targets"][0]
        self.assertEqual(target["counts"]["invalid_numeric"], 1)
        self.assertEqual(target["descriptive_n"], 1)
        table = context["sources"][0]["tables"][0]
        self.assertEqual(table["duplicate_dates"], ["2025-01-01"])
        self.assertEqual(table["invalid_date_locators"], ["L4-L4"])
        self.assertEqual(table["unknown_completion_locators"], ["L7-L7"])
        mean = next(stat for stat in statistics(context) if stat["operation"] == "mean")
        self.assertEqual(mean["value"], "0")
        self.assertEqual(mean["source_locators"], ["L6-L6"])

    def test_unknown_units_and_unknown_markers_do_not_become_known_measurements(self):
        item = source("日期,HRV状态,实际,weight_kg\n"
                      "2025-01-01,42,8,--\n2025-01-02,--,0,12\n")
        context = prepare_quality([item])
        for target in context["targets"][:2]:
            self.assertIsNone(target["unit"])
            self.assertFalse(any(stat["operation"] == "mean" and stat["target_id"] == target["target_id"]
                                 for stat in context["statistics"]))
        weight = context["targets"][2]
        self.assertEqual(weight["counts"]["declared_missing"], 0)
        self.assertEqual(weight["counts"]["invalid_numeric"], 1)
        self.assertEqual(weight["counts"]["literal_markers"], {"--": 1})

    def test_same_export_tables_are_independent_scopes_without_pooling(self):
        first = source("日期,步数\n2025-01-01,10\n2025-01-02,20\n", "a")
        second = source(first["body"], "b")
        context = prepare_quality([first, second])
        self.assertEqual(len(context["sources"]), 2)
        self.assertEqual([target["counts"]["rows"] for target in context["targets"]], [2, 2])
        means = [stat for stat in statistics(context) if stat["operation"] == "mean"]
        self.assertEqual([stat["valid_n"] for stat in means], [2, 2])
        self.assertEqual(len({stat["stat_id"] for stat in means}), 2)
        self.assert_error("invalid_quality_scope", lambda: prepare_quality([first, first]))

    def test_calendar_halves_have_fixed_boundaries_minimum_samples_and_exact_difference(self):
        item = source("日期,当日未完结,步数\n" + "".join(
            f"2025-01-{day:02},否,{day}\n" for day in range(1, 9)))
        context = prepare_quality([item])
        stats = {stat["operation"]: stat for stat in statistics(context)}
        self.assertEqual(stats["first_half_mean"]["date_interval"], ["2025-01-01", "2025-01-04"])
        self.assertEqual(stats["first_half_mean"]["value"], "2.5")
        self.assertEqual(stats["second_half_mean"]["value"], "6.5")
        self.assertEqual(stats["second_minus_first_mean"]["value"], "4")
        shorter = source("日期,步数\n2025-01-01,1\n2025-01-02,2\n2025-01-03,3\n")
        short_context = prepare_quality([shorter])
        self.assertNotIn("first_half_mean", {stat["operation"] for stat in short_context["statistics"]})
        self.assertIn("calendar_halves_need_3_valid_days_each", short_context["targets"][0]["limitations"])

    def test_mixed_timezones_never_produce_calendar_comparisons(self):
        item = source("日期,时区,步数\n" + "".join(
            f"2025-01-{day:02},{'UTC' if day < 4 else 'Asia/Shanghai'},{day}\n" for day in range(1, 9)))
        context = prepare_quality([item])
        self.assertNotIn("first_half_mean", {stat["operation"] for stat in context["statistics"]})

    def test_selected_fragments_keep_global_lines_and_deduplicate_overlap(self):
        item = source("日期,步数\n2025-01-01,10\n2025-01-02,20\n2025-01-03,30\n")
        selected = view(item, [("header", 1, 1), ("row-three", 4, 4), ("overlap", 4, 4)])
        context = prepare_quality([item], [selected])
        profile = context["sources"][0]
        self.assertEqual(profile["provided_locators"], ["L1-L1", "L4-L4"])
        self.assertEqual(context["targets"][0]["counts"]["rows"], 1)
        mean = next(stat for stat in statistics(context) if stat["operation"] == "mean")
        self.assertEqual(mean["source_locators"], ["L4-L4"])
        self.assertEqual(mean["value"], "30")
        self.assertEqual(prepare_quality([selected]), context)

    def test_fragment_mismatch_and_missing_header_cannot_silently_expand_scope(self):
        item = source("日期,步数\n2025-01-01,10\n2025-01-02,20\n")
        selected = view(item, [("one-row", 3, 3)])
        context = prepare_quality([item], [selected])
        self.assertEqual(context["sources"][0]["table_parse_status"], "header_not_provided")
        self.assertEqual(context["targets"][0]["kind"], "semantic")
        self.assertEqual(context["statistics"], [])
        selected["fragments"][0]["lines"][0]["text"] = "2025-01-02,999"
        self.assert_error("invalid_quality_scope", lambda: prepare_quality([item], [selected]))

    def test_quoted_multiline_csv_rows_keep_physical_source_ranges(self):
        item = source('日期,步数,备注\n2025-01-01,10,"first\nsecond"\n2025-01-02,20,ordinary\n')
        context = prepare_quality([item])
        mean = next(stat for stat in statistics(context) if stat["operation"] == "mean")
        self.assertEqual(mean["source_locators"], ["L2-L4"])
        partial = view(item, [("header", 1, 1), ("start", 2, 2), ("later", 4, 4)])
        context = prepare_quality([item], [partial])
        self.assertEqual(context["statistics"], [])
        self.assertIn(context["sources"][0]["table_parse_status"], {"csv_parse_error", "incomplete_multiline_row"})

    def test_malformed_csv_is_semantic_review_scope_not_made_up_numeric_data(self):
        item = source('日期,步数\n2025-01-01,"broken\n')
        context = prepare_quality([item])
        self.assertEqual(context["sources"][0]["table_parse_status"], "csv_parse_error")
        self.assertEqual(context["statistics"], [])
        self.assertIn("semantic_completeness_requires_independent_review", context["targets"][0]["limitations"])

    def test_minimal_note_can_be_mapped_but_coverage_cannot_be_omitted(self):
        item = source("用户喜欢在安静的地方学习。")
        context = prepare_quality([item])
        output = add_quality(extraction(item), context)
        validate_quality_output(output, context)
        output["quality"]["coverage_disposition"].clear()
        self.assert_error("quality_coverage_mismatch", lambda: validate_quality_output(output, context))

    def test_coverage_duplicate_wrong_record_and_false_body_mapping_are_rejected(self):
        item = source("日期,步数\n2025-01-01,10\n")
        context = prepare_quality([item])
        good = add_quality(extraction(item), context)
        bad = copy.deepcopy(good)
        bad["quality"]["coverage_disposition"].append(copy.deepcopy(bad["quality"]["coverage_disposition"][0]))
        self.assert_error("quality_coverage_mismatch", lambda: validate_quality_output(bad, context))
        bad = copy.deepcopy(good)
        bad["quality"]["coverage_disposition"][0]["body_evidence"][0]["text"] = "正文不存在的说明"
        self.assert_error("quality_body_mapping_mismatch", lambda: validate_quality_output(bad, context))
        bad = copy.deepcopy(good)
        bad["quality"]["coverage_disposition"][0]["record_ids"] = ["invented"]
        self.assert_error("quality_body_mapping_mismatch", lambda: validate_quality_output(bad, context))

    def test_coverage_accepts_multiple_distinct_excerpts_for_one_record(self):
        item = source("资料说明记录范围，并保留缺测限制。")
        context = prepare_quality([item])
        output = add_quality(extraction(item, "记录保留资料范围。缺测值没有补零。"), context)
        disposition = output["quality"]["coverage_disposition"][0]
        disposition["body_evidence"] = [
            {"record_id": "synthetic", "text": "记录保留资料范围。"},
            {"record_id": "synthetic", "text": "缺测值没有补零。"},
        ]
        validate_quality_output(output, context)
        another = copy.deepcopy(output["objects"][0])
        another.update(id="another", body="另一条说明也保留来源。")
        output["objects"].append(another)
        disposition["record_ids"].append("another")
        disposition["body_evidence"].append({"record_id": "another", "text": another["body"]})
        validate_quality_output(output, context)

    def test_multiple_coverage_excerpts_still_require_exact_complete_unique_mapping(self):
        item = source("资料说明记录范围，并保留缺测限制。")
        context = prepare_quality([item])
        good = add_quality(extraction(item, "记录保留资料范围。缺测值没有补零。"), context)
        good["objects"].append({"id": "another", "body": "其他说明。", "sources": [ref(item)]})
        first = {"record_id": "synthetic", "text": "记录保留资料范围。"}
        second = {"record_id": "synthetic", "text": "缺测值没有补零。"}
        cases = [
            (["synthetic"], [first, second, first]),
            (["synthetic"], [first, {"record_id": "another", "text": "其他说明。"}]),
            (["synthetic"], [first, {"record_id": "absent", "text": "其他说明。"}]),
            (["synthetic", "another"], [first, second]),
            (["synthetic"], []),
            (["synthetic"], [first, {"record_id": "synthetic", "text": "正文中不存在的内容。"}]),
        ]
        for ids, evidence in cases:
            with self.subTest(ids=ids, evidence=evidence):
                bad = copy.deepcopy(good)
                bad["quality"]["coverage_disposition"][0].update(record_ids=ids, body_evidence=evidence)
                self.assert_error("quality_body_mapping_mismatch", lambda: validate_quality_output(bad, context))

    def test_deferred_requires_specific_reason_and_no_false_summary_mapping(self):
        item = source("日期,HRV状态\n2025-01-01,42\n")
        context = prepare_quality([item])
        output = add_quality(extraction(item), context)
        disposition = output["quality"]["coverage_disposition"][0]
        disposition.update(status="deferred", record_ids=[], body_evidence=[], reason="原文只有HRV状态原值，没有说明单位或算法。")
        validate_quality_output(output, context)
        disposition["reason"] = "证据不足"
        self.assert_error("quality_deferral_unspecified", lambda: validate_quality_output(output, context))

    def test_structured_statistics_require_exact_computation_value_and_source(self):
        item = source("日期,步数\n2025-01-01,10\n2025-01-02,20\n")
        context = prepare_quality([item])
        stat = next(stat for stat in context["statistics"] if stat["operation"] == "mean")
        output = add_quality(extraction(item, "选定两行步数均值为15步，完整佩戴情况未知。"), context)
        output["objects"][0]["sources"].append(ref(item, 2))
        claim = {"stat_id": stat["stat_id"], "record_id": "synthetic", "value": "15",
                 "body_evidence": output["objects"][0]["body"], "sources": [ref(item, 2)]}
        output["quality"]["statistic_claims"] = [claim]
        validate_quality_output(output, context)
        claim["value"] = "16"
        self.assert_error("quality_statistic_mismatch", lambda: validate_quality_output(output, context))
        claim["value"] = "15"
        claim["sources"][0]["quote"] = "2025-01-01,999"
        self.assert_error("quality_source_reference_mismatch", lambda: validate_quality_output(output, context))

    def test_numeric_substring_cannot_masquerade_as_correct_statistic(self):
        item = source("日期,步数\n2025-01-01,2\n")
        context = prepare_quality([item])
        stat = next(stat for stat in context["statistics"] if stat["operation"] == "mean")
        output = add_quality(extraction(item, "步数平均值为20步。"), context)
        output["objects"][0]["sources"].append(ref(item, 2))
        output["quality"]["statistic_claims"] = [{"stat_id": stat["stat_id"], "record_id": "synthetic",
            "value": "2", "body_evidence": output["objects"][0]["body"], "sources": [ref(item, 2)]}]
        self.assert_error("quality_statistic_mismatch", lambda: validate_quality_output(output, context))

    def test_statistic_source_must_survive_record_publication_and_match_summary_target(self):
        item = source("日期,步数\n2025-01-01,2\n")
        context = prepare_quality([item])
        stat = next(stat for stat in context["statistics"] if stat["operation"] == "mean")
        output = add_quality(extraction(item, "步数平均值为2步。"), context)
        output["quality"]["statistic_claims"] = [{"stat_id": stat["stat_id"], "record_id": "synthetic",
            "value": "2", "body_evidence": output["objects"][0]["body"], "sources": [ref(item, 2)]}]
        self.assert_error("quality_source_reference_mismatch", lambda: validate_quality_output(output, context))
        output["objects"][0]["sources"].append(ref(item, 2))
        validate_quality_output(output, context)
        output["quality"]["coverage_disposition"][0].update(
            status="deferred", record_ids=[], body_evidence=[], reason="原文列名口径存在额外限制，需要先确认。")
        self.assert_error("quality_statistic_mismatch", lambda: validate_quality_output(output, context))

    def test_malformed_structured_fields_raise_capture_error(self):
        item = source("日期,步数\n2025-01-01,2\n")
        context = prepare_quality([item])
        stat = next(stat for stat in context["statistics"] if stat["operation"] == "mean")
        output = add_quality(extraction(item, "步数平均值为2步。"), context)
        output["quality"]["statistic_claims"] = [{"stat_id": stat["stat_id"], "record_id": [],
            "value": "2", "body_evidence": output["objects"][0]["body"], "sources": [ref(item, 2)]}]
        self.assert_error("invalid_quality_output", lambda: validate_quality_output(output, context))

    def test_large_decimal_values_have_defined_precision_without_crashing(self):
        item = source("日期,步数\n2025-01-01,123456789012345678901234567.1\n"
                      "2025-01-02,123456789012345678901234567.2\n")
        context = prepare_quality([item])
        mean = next(stat for stat in statistics(context) if stat["operation"] == "mean")
        self.assertEqual(mean["value"], "123456789012345678901234567.15")

    def test_extreme_model_locator_is_rejected_without_materializing_range(self):
        item = source("日期,步数\n2025-01-01,2\n")
        context = prepare_quality([item])
        stat = next(stat for stat in context["statistics"] if stat["operation"] == "mean")
        output = add_quality(extraction(item, "步数平均值为2步。"), context)
        claim_ref = {**ref(item, 2), "locator": "L1-L999999999"}
        output["quality"]["statistic_claims"] = [{"stat_id": stat["stat_id"], "record_id": "synthetic",
            "value": "2", "body_evidence": output["objects"][0]["body"], "sources": [claim_ref]}]
        self.assert_error("quality_source_reference_mismatch", lambda: validate_quality_output(output, context))
        claim_ref["locator"] = "L1-L" + "9" * 10000
        self.assert_error("invalid_quality_scope", lambda: validate_quality_output(output, context))

    def test_extreme_exponent_in_model_body_is_rejected_as_wrong_statistic(self):
        item = source("日期,步数\n2025-01-01,2\n")
        context = prepare_quality([item])
        stat = next(stat for stat in context["statistics"] if stat["operation"] == "mean")
        output = add_quality(extraction(item, "步数平均值为1e" + "9" * 1000 + "步。"), context)
        output["quality"]["statistic_claims"] = [{"stat_id": stat["stat_id"], "record_id": "synthetic",
            "value": "2", "body_evidence": output["objects"][0]["body"], "sources": [ref(item, 2)]}]
        self.assert_error("quality_statistic_mismatch", lambda: validate_quality_output(output, context))

    def test_statistic_cannot_borrow_another_source_or_unprovided_row(self):
        first = source("日期,步数\n2025-01-01,10\n", "first")
        second = source("日期,步数\n2025-01-01,20\n", "second")
        context = prepare_quality([first, second])
        output = extraction(first, "第一份选定资料步数均值为10步。")
        output["objects"][0]["sources"].append(ref(second))
        output = add_quality(output, context)
        stat = next(stat for stat in context["statistics"] if stat["operation"] == "mean")
        output["quality"]["statistic_claims"] = [{"stat_id": stat["stat_id"], "record_id": "synthetic",
            "value": "10", "body_evidence": output["objects"][0]["body"], "sources": [ref(second, 2)]}]
        self.assert_error("quality_source_reference_mismatch", lambda: validate_quality_output(output, context))

    def test_old_operational_state_needs_explicit_source_time_and_qualified_body(self):
        item = source("数据未导入记忆仓库，也未提交Git。")
        context = prepare_quality([item])
        output = add_quality(extraction(item, item["body"]), context)
        self.assert_error("quality_temporal_missing", lambda: validate_quality_output(output, context))
        body = "导出说明写成当时，数据未导入记忆仓库，也未提交Git。"
        output = add_quality(extraction(item, body), context)
        output["quality"]["temporal_claims"] = [{"record_id": "synthetic", "statement": body,
            "basis": "source_time", "source_refs": [ref(item)], "system_fact_id": None}]
        validate_quality_output(output, context)
        output["quality"]["temporal_claims"][0]["statement"] = "未导入记忆仓库"
        self.assert_error("quality_temporal_mismatch", lambda: validate_quality_output(output, context))

    def test_source_time_accepts_explicit_dates_bound_to_true_source_lines(self):
        cases = [
            ("时间戳覆盖2025-01-01 00:00至2025-01-02 10:45。",
             "来源自述覆盖为2025-01-01 00:00至2025-01-02 10:45。"),
            ("2025-01-02,Asia/Shanghai,是,7",
             "2025-01-02标为当日未完结，原值7保留。"),
            ("2025-01-02尚未导入。",
             "2025-01-02尚未导入。"),
        ]
        for original, body in cases:
            with self.subTest(body=body):
                item = source(original)
                context = prepare_quality([item])
                output = add_quality(extraction(item, body), context)
                output["quality"]["temporal_claims"] = [{
                    "record_id": "synthetic", "statement": body, "basis": "source_time",
                    "source_refs": [ref(item)], "system_fact_id": None,
                }]
                validate_quality_output(output, context)
                output["quality"]["temporal_claims"][0]["source_refs"][0]["quote"] += "伪造"
                self.assert_error("quality_source_reference_mismatch",
                                  lambda: validate_quality_output(output, context))

    def test_source_date_cannot_be_invalid_unbound_or_qualify_other_sentences_or_current_state(self):
        item = source("2025-01-01记录了来源状态，文件尚未导入。")
        context = prepare_quality([item])
        for body in (
            "2025-02-30尚未导入。",
            "2025-01-02尚未导入。",
            "2025-01-01是资料日期。尚未导入。",
            "2025-01-01是资料日期；尚未导入。",
            "2025-01-01是资料日期，当前已经导入。",
            "2025-01-01是资料日期。目前已经导入。",
            "2025-01-01是资料日期，现在已经导入。",
        ):
            with self.subTest(body=body):
                output = add_quality(extraction(item, body), context)
                output["quality"]["temporal_claims"] = [{
                    "record_id": "synthetic", "statement": body, "basis": "source_time",
                    "source_refs": [ref(item)], "system_fact_id": None,
                }]
                self.assert_error("quality_temporal_mismatch",
                                  lambda: validate_quality_output(output, context))

    def test_current_state_disclaimer_still_needs_an_explicit_unknown_declaration(self):
        item = source("来源只说明文件下载后的处理情况。")
        context = prepare_quality([item])
        output = add_quality(extraction(item, "不能据此断言当前仓库状态。"), context)
        self.assert_error("quality_temporal_missing", lambda: validate_quality_output(output, context))
        body = "当前仓库状态未知，不能据此断言已经导入。"
        output = add_quality(extraction(item, body), context)
        output["quality"]["temporal_claims"] = [{
            "record_id": "synthetic", "statement": body, "basis": "unknown",
            "source_refs": [ref(item)], "system_fact_id": None,
        }]
        validate_quality_output(output, context)

    def test_current_state_never_comes_from_old_source_alone(self):
        item = source("导出说明中的旧操作状态。")
        context = prepare_quality([item])
        body = "截至commit abc，当前已导入该版本。"
        output = add_quality(extraction(item, body), context)
        claim = {"record_id": "synthetic", "statement": body, "basis": "current_state",
                 "source_refs": [], "system_fact_id": None}
        output["quality"]["temporal_claims"] = [claim]
        self.assert_error("quality_current_state_unverified", lambda: validate_quality_output(output, context))
        context["system_facts"] = [{"system_fact_id": "fact-fixture", "statement": body,
                                   "binding": {"commit_sha": "a" * 40, "path": item["path"]}}]
        claim["system_fact_id"] = "fact-fixture"
        validate_quality_output(output, context)
        context["system_facts"][0]["statement"] = "不同版本事实"
        self.assert_error("quality_current_state_unverified", lambda: validate_quality_output(output, context))

    def test_unknown_state_remains_explicitly_unknown(self):
        item = source("可能还未完成导入。")
        context = prepare_quality([item])
        body = "当前导入状态未知，不能据来源推断。"
        output = add_quality(extraction(item, body), context)
        output["quality"]["temporal_claims"] = [{"record_id": "synthetic", "statement": body,
            "basis": "unknown", "source_refs": [ref(item)], "system_fact_id": None}]
        validate_quality_output(output, context)

    def test_repeated_temporal_word_cannot_borrow_another_sentences_qualification(self):
        item = source("来源当时尚未处理。")
        context = prepare_quality([item])
        body = "当前入库状态未知。当前尚未导入记忆仓库。导出当时尚未处理。"
        output = add_quality(extraction(item, body), context)
        output["quality"]["temporal_claims"] = [
            {"record_id": "synthetic", "statement": "当前入库状态未知。", "basis": "unknown",
             "source_refs": [ref(item)], "system_fact_id": None},
            {"record_id": "synthetic", "statement": "导出当时尚未处理。", "basis": "source_time",
             "source_refs": [ref(item)], "system_fact_id": None},
        ]
        self.assert_error("quality_temporal_missing", lambda: validate_quality_output(output, context))

    def test_check_ids_bind_actual_candidate_and_scope(self):
        item = source("这是人工隔离资料。")
        context = prepare_quality([item])
        output = add_quality(extraction(item), context)
        original = candidate(output)
        checks = build_review_checks(original, context)
        validate_quality_review(passed_review(checks), checks)
        changed = copy.deepcopy(original)
        changed["objects"][0]["body"] = "changed"
        changed_checks = build_review_checks(changed, context)
        self.assertNotEqual(checks[0]["scope_sha256"], changed_checks[0]["scope_sha256"])
        self.assert_error("quality_review_coverage_mismatch",
                          lambda: validate_quality_review(passed_review(checks), changed_checks))

    def test_review_missing_duplicate_wrong_scope_empty_evidence_and_reason_are_rejected(self):
        item = source("这是人工隔离资料。")
        context = prepare_quality([item])
        checks = build_review_checks(candidate(add_quality(extraction(item), context)), context)
        mutations = [
            ("quality_review_coverage_mismatch", lambda out: out["checks"].pop()),
            ("quality_review_coverage_mismatch", lambda out: out["checks"].append(copy.deepcopy(out["checks"][0]))),
            ("quality_review_scope_mismatch", lambda out: out["checks"][0].update(scope_sha256="wrong")),
            ("quality_review_evidence_mismatch", lambda out: out["checks"][0].update(evidence_ids=[])),
            ("quality_review_reason_missing", lambda out: out["checks"][0].update(reason="通过")),
            ("quality_review_evidence_mismatch", lambda out: out["checks"][0].update(evidence_ids=["object:" + checks[0]["object_paths"][0]])),
        ]
        for code, mutate in mutations:
            with self.subTest(code=code):
                review = passed_review(checks)
                mutate(review)
                self.assert_error(code, lambda: validate_quality_review(review, checks))

    def test_review_aggregate_verdict_cannot_mask_failed_checks(self):
        item = source("这是人工隔离资料。")
        context = prepare_quality([item])
        checks = build_review_checks(candidate(add_quality(extraction(item), context)), context)
        review = passed_review(checks)
        review["checks"][0]["result"] = "needs_revision"
        self.assert_error("quality_review_verdict_mismatch", lambda: validate_quality_review(review, checks))
        review["verdict"] = "needs_revision"
        validate_quality_review(review, checks)
        review["checks"][0]["result"] = "needs_confirmation"
        review["verdict"] = "needs_confirmation"
        validate_quality_review(review, checks)

    def test_object_check_cannot_borrow_another_objects_source_scope(self):
        first, second = source("人工学习资料。", "a"), source("人工工作资料。", "b")
        context = prepare_quality([first, second])
        output = extraction(first)
        output["objects"][0]["id"] = "first"
        other = extraction(second)["objects"][0]
        other["id"] = "second"
        output["objects"].append(other)
        checks = build_review_checks(candidate(add_quality(output, context)), context)
        first_check = next(check for check in checks if check["kind"] == "object_fidelity"
                           and check["object_paths"] == ["memory/records/first.md"])
        self.assertEqual(first_check["source_scope_ids"], [context["sources"][0]["evidence_id"]])
        review = passed_review(checks)
        returned = next(check for check in review["checks"] if check["check_id"] == first_check["check_id"])
        returned["evidence_ids"] = [context["sources"][1]["evidence_id"]]
        self.assert_error("quality_review_evidence_mismatch", lambda: validate_quality_review(review, checks))

    def test_missing_object_source_cannot_pass_on_candidate_alone(self):
        first, second = source("人工学习资料。", "a"), source("人工工作资料。", "b")
        context = prepare_quality([first, second],
                                  [view(first, [("a", 1, 1)]), view(second, [("b", 1, 1)])])
        obj = candidate(extraction(first))
        checks = build_review_checks(obj, context, {"kind": "object",
                                     "object_paths": ["memory/records/synthetic.md"], "fragment_ids": ["b"]})
        self.assertTrue(all(check["source_scope_ids"] == [] for check in checks))
        self.assert_error("quality_review_evidence_mismatch", lambda: validate_quality_review(passed_review(checks), checks))

    def test_multisource_object_review_must_account_for_each_source_scope(self):
        first, second = source("人工学习资料。", "a"), source("人工工作资料。", "b")
        context = prepare_quality([first, second])
        output = extraction(first)
        output["objects"][0]["sources"].append(ref(second))
        checks = build_review_checks(candidate(add_quality(output, context)), context)
        review = passed_review(checks)
        self.assertEqual(len(checks[0]["source_scope_ids"]), 2)
        review["checks"][0]["evidence_ids"] = [checks[0]["source_scope_ids"][0]]
        self.assert_error("quality_review_evidence_mismatch", lambda: validate_quality_review(review, checks))

    def test_many_row_fragments_share_source_scope_and_fit_review_material_budget(self):
        names = ["日期", "当日未完结"] + [f"sample_{index}_kg" for index in range(12)]
        body = ",".join(names) + "\n" + "".join(
            ",".join([(date(2025, 1, 1) + timedelta(days=day)).isoformat(), "否"]
                     + [str(day + index) for index in range(12)]) + "\n" for day in range(80))
        item = source(body)
        fragments = [("fragment-" + hashlib.sha256(str(number).encode()).hexdigest(), number, number)
                     for number in range(1, 82)]
        context = prepare_quality([item], [view(item, fragments)])
        output = add_quality(extraction(item), context)
        checks = build_review_checks(candidate(output), context)
        self.assertTrue(all(target["source_scope_id"] == context["sources"][0]["evidence_id"]
                            and "fragment_ids" not in target for target in context["targets"]))
        self.assertTrue(all("fragment_ids" not in check for check in checks))
        size = len(json.dumps({"quality_context": context, "required_checks": checks}, ensure_ascii=False).encode())
        self.assertLess(size, 120000, size)
        validate_quality_review(passed_review(checks), checks)

    def test_decomposed_projection_does_not_disclose_unread_table_totals(self):
        item = source("日期,步数\n2025-01-01,10\n2025-01-02,20\n")
        selected = view(item, [("header", 1, 1), ("a", 2, 2), ("b", 3, 3)])
        context = prepare_quality([item], [selected])
        scope = {"kind": "coverage", "object_paths": ["memory/records/synthetic.md"], "fragment_ids": ["header", "a"]}
        projected = project_quality(context, scope)
        self.assertEqual(projected["sources"][0]["provided_locators"], ["L1-L2"])
        self.assertNotIn("3", projected["sources"][0]["line_sha256"])
        self.assertNotIn("counts", projected["targets"][0])
        self.assertEqual(projected["statistics"], [])
        checks = build_review_checks(candidate(add_quality(extraction(item), context)), context, scope)
        self.assertTrue(all(check["kind"] == "metric_coverage" for check in checks))
        self.assertTrue(all("b" not in check["evidence_ids"] for check in checks))
        validate_quality_review(passed_review(checks), checks)

    def test_global_projection_is_certificate_only_and_checks_organization(self):
        item = source("这是人工隔离资料。")
        context = prepare_quality([item], [view(item, [("all", 1, 1)])])
        scope = {"kind": "global", "object_paths": ["memory/records/synthetic.md"], "fragment_ids": []}
        projected = project_quality(context, scope)
        self.assertEqual(projected["scope_mode"], "certificate_only")
        self.assertEqual(projected["sources"], [])
        self.assertEqual(projected["statistics"], [])
        checks = build_review_checks(candidate(add_quality(extraction(item), context)), context, scope)
        self.assertEqual([check["kind"] for check in checks], ["global_organization"])
        validate_quality_review(passed_review(checks), checks)

    def test_global_reply_table_never_creates_unresolvable_statistics(self):
        item = source("日期,步数\n2025-01-01,2\n")
        context = prepare_quality([item], [view(item, [("reply", 1, 2)])])
        projected = project_quality(context, {"kind": "global", "object_paths": [], "fragment_ids": ["reply"]})
        self.assertEqual(projected["statistics"], [])
        self.assertTrue(projected["sources"])

    def test_statistics_projection_requires_actual_header_and_value_rows(self):
        item = source("日期,步数\n2025-01-01,2\n")
        context = prepare_quality([item], [view(item, [("header", 1, 1), ("value", 2, 2)])])
        projected = project_quality(context, {"kind": "object", "object_paths": [], "fragment_ids": ["value"]})
        self.assertEqual(projected["statistics"], [])

    def test_statistics_projection_also_requires_excluded_rows_and_date_basis(self):
        item = source("日期,当日未完结,步数\n2025-01-01,否,2\n2025-01-02,否,4\n2025-01-03,是,100\n")
        context = prepare_quality([item], [view(item, [("seen", 1, 3), ("excluded", 4, 4)])])
        projected = project_quality(context, {"kind": "object", "object_paths": [], "fragment_ids": ["seen"]})
        self.assertEqual(projected["statistics"], [])
        self.assertEqual(projected["targets"][0]["statistic_scopes"], {})

    def test_compact_statistic_binding_detects_modified_shared_scope_or_source(self):
        item = source("日期,步数\n2025-01-01,2\n")
        context = prepare_quality([item])
        stat = next(stat for stat in context["statistics"] if stat["operation"] == "mean")
        changed = copy.deepcopy(context)
        changed["targets"][0]["statistic_scopes"][stat["scope_id"]]["valid_n"] = 99
        self.assert_error("invalid_quality_context", lambda: resolve_statistic(changed, stat))
        changed = copy.deepcopy(context)
        changed["targets"][0]["source_version"] = "sha256-different"
        self.assert_error("invalid_quality_context", lambda: resolve_statistic(changed, stat))


if __name__ == "__main__":
    unittest.main()
