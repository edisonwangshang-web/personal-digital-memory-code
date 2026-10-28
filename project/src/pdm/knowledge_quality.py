"""Deterministic, source-scoped quality material and independent Review gates.

This module does not extract semantic facts.  It inventories the text actually
provided, computes only explicitly defined descriptive quantities, and makes
the model's coverage, arithmetic and temporal commitments inspectable.
"""

import copy
import csv
import hashlib
import io
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

from .contracts import CaptureError, canonical


QUALITY_CONTRACT = "skill-quality-v1"
QUALITY_POLICY_VERSION = "source-quality-v1"
STATISTICS_POLICY_VERSION = "descriptive-v1"


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}
_REF_SCHEMA = _object({key: _STRING for key in ("source_id", "source_version", "locator", "quote")})
QUALITY_SCHEMA = _object({
    "coverage_disposition": {"type": "array", "items": _object({
        "target_id": _STRING,
        "status": {"type": "string", "enum": ["summarized", "deferred"]},
        "record_ids": _STRINGS,
        "body_evidence": {"type": "array", "items": _object({"record_id": _STRING, "text": _STRING})},
        "reason": _STRING,
    })},
    "statistic_claims": {"type": "array", "items": _object({
        "stat_id": _STRING, "record_id": _STRING, "value": _STRING,
        "body_evidence": _STRING, "sources": {"type": "array", "items": _REF_SCHEMA},
    })},
    "temporal_claims": {"type": "array", "items": _object({
        "record_id": _STRING, "statement": _STRING,
        "basis": {"type": "string", "enum": ["source_time", "current_state", "unknown"]},
        "source_refs": {"type": "array", "items": _REF_SCHEMA},
        "system_fact_id": {"type": ["string", "null"]},
    })},
})
QUALITY_REVIEW_CHECK_SCHEMA = _object({
    "check_id": _STRING, "scope_sha256": _STRING,
    "result": {"type": "string", "enum": ["passed", "needs_revision", "needs_confirmation"]},
    "reason": _STRING, "evidence_ids": _STRINGS,
})
REPAIR_SCOPE_SCHEMA = _object({
    "kind": {"type": "string", "enum": ["statistic_bindings", "content", "none"]},
    "missing_bindings": {"type": "array", "items": _object({"stat_id": _STRING, "record_id": _STRING})},
})
STATISTIC_BINDINGS_SCHEMA = _object({
    "statistic_bindings": {"type": "array", "items": _object({
        "stat_id": _STRING, "record_id": _STRING, "body_evidence": _STRING,
    })},
})
STATISTIC_REPAIR_CONTRACT = "statistic-bindings-repair-v1"

_DATE_COLUMNS = {"日期", "date", "Date"}
_ZONE_COLUMNS = {"时区", "timezone", "time_zone"}
_PARTIAL_COLUMNS = {"当日未完结", "partial_day", "incomplete_day"}
_STRUCTURE_COLUMNS = _DATE_COLUMNS | _ZONE_COLUMNS | _PARTIAL_COLUMNS
_TRUE = {"是", "true", "1", "yes"}
_FALSE = {"否", "false", "0", "no"}
_LITERAL_MARKERS = {"--", "—", "N/A", "NA", "null"}
# The header itself supplies the metric/unit.  Generic “实际”/“目标”, HRV
# status and duration strings deliberately have no inferred numerical unit.
_UNITS = {
    "步数": "步", "步数目标": "步", "steps": "steps",
    "睡眠得分": "分", "sleep_score": "score",
    "夜间身体电量变化": "设备分值", "平均压力": "设备分值",
    "身体电量每日最高": "设备分值", "身体电量每日最低": "设备分值",
    "上楼层数": "层", "下楼层数": "层", "楼层目标": "层",
    "强度活动分钟_实际": "分钟", "强度活动分钟_网页目标值": "分钟",
}
_UNIT_SUFFIXES = (("_bpm", "bpm"), ("_kcal", "kcal"), ("_kg", "kg"),
                  ("_百分数", "%"), ("_次每分", "次/分"))
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z")
_LOCATOR = re.compile(r"L([1-9]\d*)-L([1-9]\d*)\Z")
_TEMPORAL_WORDS = re.compile(r"当前|目前|尚未|已经|截至|现已|未导入|未提交|\bcurrently\b|\balready\b|\bnot yet\b", re.I)
_SOURCE_TIME_WORDS = re.compile(r"当时|导出时|导出说明|来源时间|原文.*时|截至|as of|at (?:export|source) time", re.I)
_UNKNOWN_TIME_WORDS = re.compile(r"未知|不明|无法确定|未核实|未确认|unknown|unverified", re.I)
_EXPLICIT_DATE = re.compile(r"(?<![0-9])[0-9]{4}-[0-9]{2}-[0-9]{2}(?![0-9])")
_CHINESE_DATE = re.compile(
    r"(?<![0-9])(?:([0-9]+)[ \t]*年[ \t]*)?([0-9]+)[ \t]*月[ \t]*([0-9]+)[ \t]*日(?![0-9])")
_CHINESE_YEAR = re.compile(r"(?<![0-9])[0-9]+[ \t]*年")
_SOURCE_DATE_ATTRIBUTION = re.compile(r"原表|源表|原文|来源")
_UNFINISHED_SOURCE_STATE = re.compile(r"未完结|未完成|未结束")
_CURRENT_TIME_WORDS = re.compile(r"当前|目前|现在|现已|\bcurrently\b|\bnow\b", re.I)
_SENTENCE_BOUNDARY = re.compile(r"[。！？!?；;\n]|\.(?:\s+|$)")


def _error(code, message, *, details=None):
    error = CaptureError(code, message)
    if details is not None:
        error.details = details
    raise error


def _hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _text(value, code="invalid_quality_output"):
    if not isinstance(value, str) or not value.strip():
        _error(code, "质量字段必须是非空文本")
    return value


def _keys(value, fields, code="invalid_quality_output"):
    if not isinstance(value, dict) or set(value) != set(fields):
        _error(code, "质量字段不完整或含未知字段")


def _list(value, code="invalid_quality_output"):
    if not isinstance(value, list):
        _error(code, "质量字段必须是列表")
    return value


def _schema(value, schema, code, *, _path=()):
    # Keep the original validation order. Only remember the innermost failure
    # path; the caller decides whether this is a temporal diagnostic.
    failure_path = _path
    try:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        valid = any((kind == "null" and value is None)
                    or (kind == "string" and isinstance(value, str))
                    or (kind == "array" and isinstance(value, list))
                    or (kind == "object" and isinstance(value, dict)) for kind in types)
        if not valid or ("enum" in schema and value not in schema["enum"]):
            _error(code, "质量输出字段类型或枚举无效")
        if isinstance(value, dict):
            if set(value) != set(schema["required"]):
                missing = next((key for key in schema["required"] if key not in value), None)
                extra = next((key for key in value if key not in schema["required"]), None)
                failure_path = (*_path, missing if missing is not None else extra)
            _keys(value, schema["required"], code)
            for key, child in schema["properties"].items():
                _schema(value[key], child, code, _path=(*_path, key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                _schema(item, schema["items"], code, _path=(*_path, index))
    except CaptureError as error:
        if not hasattr(error, "_quality_field_path"):
            error._quality_field_path = failure_path
        raise


def _unique_strings(value, code="invalid_quality_output"):
    values = _list(value, code)
    if any(not isinstance(item, str) or not item for item in values) or len(set(values)) != len(values):
        _error(code, "质量标识必须是无重复的非空字符串")
    return values


def _numbers(locator):
    match = _LOCATOR.fullmatch(locator if isinstance(locator, str) else "")
    if not match or len(match[1]) > 9 or len(match[2]) > 9 or int(match[1]) > int(match[2]):
        _error("invalid_quality_scope", "原文行范围无效")
    return range(int(match[1]), int(match[2]) + 1)


def _ranges(numbers):
    values = sorted(set(numbers))
    if not values:
        return []
    result, start, end = [], values[0], values[0]
    for number in values[1:]:
        if number == end + 1:
            end = number
        else:
            result.append(f"L{start}-L{end}")
            start = end = number
    return result + [f"L{start}-L{end}"]


def _decimal(value):
    if len(value) > 64 or not _NUMBER.fullmatch(value):
        return None
    try:
        result = Decimal(value)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _value(number):
    if number == number.to_integral_value():
        return str(number.to_integral_value())
    return format(number.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP), "f").rstrip("0").rstrip(".")


def _numeric_token_matches(tokens, expected):
    for token in tokens:
        if len(token) > 128:
            continue
        try:
            if Decimal(token) == Decimal(expected):
                return True
        except InvalidOperation:
            continue
    return False


def _unit(name):
    if name in _UNITS:
        return _UNITS[name]
    return next((unit for suffix, unit in _UNIT_SUFFIXES if name.endswith(suffix)), None)


def _identity(source):
    if not isinstance(source, dict) or not isinstance(source.get("metadata", {}), dict):
        _error("invalid_quality_scope", "资料不是合法来源对象")
    metadata = source.get("metadata", {})
    sid = source.get("source_id", metadata.get("source_id"))
    version = source.get("source_version", metadata.get("source_version"))
    if not isinstance(sid, str) or not sid or not isinstance(version, str) or not version:
        _error("invalid_quality_scope", "资料缺少固定来源身份")
    return sid, version


def _selected_lines(source, view):
    full = source.get("body")
    original = full.splitlines() if isinstance(full, str) else None
    if view is None and original is not None:
        lines = {number: text for number, text in enumerate(original, 1)}
        fragment = {"fragment_id": "whole-" + _hash([*_identity(source), lines]),
                    "locators": _ranges(lines)}
        return lines, [fragment]
    view = view or source
    fragments, lines = [], {}
    for fragment in _list(view.get("fragments"), "invalid_quality_scope"):
        if not isinstance(fragment, dict):
            _error("invalid_quality_scope", "原文片段无效")
        fid = _text(fragment.get("fragment_id"), "invalid_quality_scope")
        numbers = _numbers(fragment.get("locator"))
        supplied = _list(fragment.get("lines"), "invalid_quality_scope")
        if len(supplied) != len(numbers):
            _error("invalid_quality_scope", "原文片段与行范围不一致")
        for number, entry in zip(numbers, supplied):
            if not isinstance(entry, dict) or type(entry.get("line")) is not int or entry.get("line") != number or not isinstance(entry.get("text"), str):
                _error("invalid_quality_scope", "原文片段行号无效")
            text = entry["text"]
            if "\n" in text or "\r" in text:
                _error("invalid_quality_scope", "片段行不得包含隐藏换行")
            if number in lines and lines[number] != text:
                _error("invalid_quality_scope", "重叠原文片段不一致")
            if original is not None and (number > len(original) or original[number - 1] != text):
                _error("invalid_quality_scope", "片段不匹配原文")
            lines[number] = text
        fragments.append({"fragment_id": fid, "locators": [fragment["locator"]]})
    if len({item["fragment_id"] for item in fragments}) != len(fragments):
        _error("invalid_quality_scope", "原文片段标识重复")
    return lines, sorted(fragments, key=lambda item: item["fragment_id"])


def _source_scope_id(profile):
    return "source-scope-" + _hash({key: profile[key] for key in
                                   ("source_id", "source_version", "provided_locators", "fragments", "line_sha256")})[:32]


def _csv_rows(lines):
    """Return physically located CSV rows, or a reason for semantic review."""
    ordered = sorted(lines)
    if not ordered or ordered[0] != 1:
        return None, "header_not_provided"
    text = "\n".join(lines[number] for number in ordered) + "\n"
    first = lines[1].lstrip("\ufeff")
    delimiter = next((candidate for candidate in (",", "\t", ";") if candidate in first), None)
    if delimiter is None:
        return None, "not_csv"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
    result, previous = [], 0
    try:
        for values in reader:
            indices = ordered[previous:reader.line_num]
            previous = reader.line_num
            if not indices or indices != list(range(indices[0], indices[-1] + 1)):
                return None, "incomplete_multiline_row"
            result.append({"values": values, "lines": indices})
    except csv.Error:
        return None, "csv_parse_error"
    if not result or len(result[0]["values"]) < 2:
        return None, "not_csv"
    headers = [header.strip().lstrip("\ufeff") for header in result[0]["values"]]
    if any(not header for header in headers) or len(set(headers)) != len(headers):
        return None, "ambiguous_header"
    if any(len(row["values"]) != len(headers) for row in result[1:]):
        return None, "csv_column_mismatch"
    if not any(re.search(r"[A-Za-z\u4e00-\u9fff]", header) for header in headers):
        return None, "ambiguous_header"
    result[0]["values"] = headers
    return result, None


def _stat(target, operation, value, unit, rows, interval, policy, *, n=None, algorithm=None):
    numbers = [number for row in rows for number in row["lines"]]
    # Scope and rule definitions are shared instead of repeated in hundreds of
    # statistics.  The target supplies source identity; the scope supplies the
    # exact rows, denominator, interval and unit; operation selects the rule.
    scope = {"unit": unit, "valid_n": len(rows), "date_interval": interval,
             "source_locators": _ranges(numbers), "partial_policy": policy}
    scopes = target.setdefault("statistic_scopes", {})
    scope_id = next((key for key, saved in scopes.items() if saved == scope), None)
    if scope_id is None:
        scope_id = "scope-" + str(len(scopes))
        scopes[scope_id] = scope
    result = {"target_id": target["target_id"], "scope_id": scope_id,
              "operation": operation, "value": str(value)}
    result["stat_id"] = "stat-" + _hash({"statistic": result, "scope": scope,
                                       "source_id": target["source_id"], "source_version": target["source_version"],
                                       "evidence_locators": target["source_locators"],
                                       "policy": STATISTICS_POLICY_VERSION})[:32]
    return result


def resolve_statistic(context, statistic):
    """Resolve compact computation material without reading new source data."""
    target = next((item for item in context["targets"] if item["target_id"] == statistic["target_id"]), None)
    if target is None or statistic.get("scope_id") not in target.get("statistic_scopes", {}):
        _error("invalid_quality_context", "统计缺少绑定目标或计算范围")
    scope = target["statistic_scopes"][statistic["scope_id"]]
    material = {key: value for key, value in statistic.items() if key != "stat_id"}
    expected = "stat-" + _hash({"statistic": material, "scope": scope,
                                "source_id": target["source_id"], "source_version": target["source_version"],
                                "evidence_locators": target["source_locators"],
                                "policy": STATISTICS_POLICY_VERSION})[:32]
    if statistic.get("stat_id") != expected:
        _error("invalid_quality_context", "统计标识不匹配固定来源、范围和值")
    return {**statistic, **scope,
            "source_id": target["source_id"], "source_version": target["source_version"],
            "evidence_locators": target["source_locators"],
            "algorithm": context["statistic_definitions"]["operations"].get(statistic["operation"]),
            "statistics_policy": context["statistics_policy"]}


def _table_material(profile, rows):
    headers, data = rows[0]["values"], rows[1:]
    table_id = "table-" + _hash([profile["source_id"], profile["source_version"], rows[0]])
    date_column = next((i for i, name in enumerate(headers) if name in _DATE_COLUMNS), None)
    partial_column = next((i for i, name in enumerate(headers) if name in _PARTIAL_COLUMNS), None)
    zone_column = next((i for i, name in enumerate(headers) if name in _ZONE_COLUMNS), None)
    # These are literal markers in the explicitly identified Garmin CSV format.
    garmin = any(name in headers for name in ("睡眠得分", "睡眠需求", "身体电量每日最高"))
    known_missing = {"--"} if garmin else set()
    for row in data:
        row["date"] = None
        if date_column is not None:
            raw = row["values"][date_column].strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                try:
                    row["date"] = date.fromisoformat(raw)
                except ValueError:
                    pass
        row["partial"] = None
        if partial_column is not None:
            mark = row["values"][partial_column].strip().lower()
            row["partial"] = True if mark in _TRUE else False if mark in _FALSE else None
    dates = Counter(row["date"] for row in data if row["date"] is not None)
    duplicates = sorted(value.isoformat() for value, count in dates.items() if count > 1)
    invalid_dates = [number for row in data if date_column is not None and row["date"] is None for number in row["lines"]]
    interval = [min(dates).isoformat(), max(dates).isoformat()] if dates else None
    policy = ("exclude_partial_and_unknown_completion" if partial_column is not None
              else "completion_not_marked; no_claim_of_complete_days")
    table = {
        "table_id": table_id, "headers": headers, "header_locators": _ranges(rows[0]["lines"]),
        "structure_columns": [name for name in headers if name in _STRUCTURE_COLUMNS],
        "row_count": len(data), "date_interval": interval, "distinct_dates": len(dates),
        "duplicate_dates": duplicates, "invalid_date_locators": _ranges(invalid_dates),
        "partial_day_locators": _ranges(number for row in data if row["partial"] is True for number in row["lines"]),
        "unknown_completion_locators": _ranges(number for row in data if partial_column is not None and row["partial"] is None for number in row["lines"]),
        "timezones": sorted({row["values"][zone_column].strip() for row in data}) if zone_column is not None else [],
        "missing_marker_meanings": {marker: "export_missing" for marker in sorted(known_missing)},
        "partial_policy": policy,
    }
    targets, statistics = [], []
    for index, name in enumerate(headers):
        if name in _STRUCTURE_COLUMNS:
            continue
        values = [row["values"][index].strip() for row in data]
        unit = _unit(name)
        numeric = [_decimal(value) for value in values]
        blank = values.count("")
        missing = sum(value in known_missing for value in values)
        invalid = sum(bool(value) and value not in known_missing and number is None
                      for value, number in zip(values, numeric)) if unit else 0
        markers = dict(sorted(Counter(value for value in values if value in _LITERAL_MARKERS).items()))
        limitations = []
        if unit is None:
            limitations.append("metric_meaning_or_unit_not_registered")
        if blank + missing == len(values):
            limitations.append("no_observed_values")
        if duplicates:
            limitations.append("duplicate_dates_excluded")
        if invalid_dates:
            limitations.append("invalid_dates_excluded")
        if invalid:
            limitations.append("invalid_numeric_values_excluded")
        if table["partial_day_locators"] or table["unknown_completion_locators"]:
            limitations.append("incomplete_days_excluded")
        if len(table["timezones"]) > 1:
            limitations.append("mixed_timezones; no_segment_comparison")
        target = {
            "target_id": "metric-" + _hash([table_id, index, profile["provided_locators"]])[:32],
            "kind": "metric", "table_id": table_id, "column": name,
            "role": "target" if "目标" in name or name.lower().endswith("target") else "observation",
            "source_id": profile["source_id"], "source_version": profile["source_version"],
            "source_locators": profile["provided_locators"], "source_scope_id": profile["evidence_id"],
            "date_interval": interval, "unit": unit, "limitations": limitations,
            "counts": {"rows": len(data), "present": len(data) - blank - missing, "blank": blank,
                       "declared_missing": missing, "literal_markers": markers,
                       "numeric": sum(value is not None for value in numeric),
                       "zero": sum(value == 0 for value in numeric if value is not None),
                       "invalid_numeric": invalid},
        }
        targets.append(target)
        for operation, count in (("row_count", len(data)), ("present_count", target["counts"]["present"]),
                                 ("blank_count", blank), ("declared_missing_count", missing),
                                 ("zero_count", target["counts"]["zero"])):
            statistics.append(_stat(target, operation, count, "rows", data, interval,
                                    "counts_include_all_provided_rows; distinguish_partial_days",
                                    n=count, algorithm="literal_cell_count_v1"))
        valid = []
        for row, number in zip(data, numeric):
            if number is None or unit is None:
                continue
            if date_column is not None and (row["date"] is None or dates[row["date"]] != 1):
                continue
            if partial_column is not None and row["partial"] is not False:
                continue
            valid.append({**row, "number": number})
        target["descriptive_n"] = len(valid)
        if not valid:
            continue
        total = sum((row["number"] for row in valid), Decimal(0))
        for operation, value in (("mean", total / len(valid)),
                                 ("minimum", min(row["number"] for row in valid)),
                                 ("maximum", max(row["number"] for row in valid))):
            statistics.append(_stat(target, operation, _value(value), unit, valid, interval, policy,
                                    algorithm="arithmetic_mean_v1" if operation == "mean" else operation + "_v1"))
        # Compare two fixed calendar halves, never arbitrary model-picked dates.
        # Three valid days per half is a descriptive coverage threshold, not
        # statistical significance, a clinical threshold or a wear-time claim.
        if not interval or len(table["timezones"]) > 1:
            continue
        start, finish = map(date.fromisoformat, interval)
        midpoint = start + timedelta(days=(finish - start).days // 2)
        segments = ([row for row in valid if row["date"] <= midpoint],
                    [row for row in valid if row["date"] > midpoint])
        if any(len(segment) < 3 for segment in segments):
            target["limitations"].append("calendar_halves_need_3_valid_days_each")
            continue
        means = []
        for label, segment, bounds in (
                ("first_half_mean", segments[0], [start.isoformat(), midpoint.isoformat()]),
                ("second_half_mean", segments[1], [(midpoint + timedelta(days=1)).isoformat(), finish.isoformat()])):
            mean = sum((row["number"] for row in segment), Decimal(0)) / len(segment)
            means.append(mean)
            statistics.append(_stat(target, label, _value(mean), unit, segment, bounds, policy,
                                    algorithm="calendar_halves_min3_arithmetic_mean_v1"))
        statistics.append(_stat(target, "second_minus_first_mean", _value(means[1] - means[0]), unit,
                                valid, interval, policy, algorithm="calendar_halves_min3_difference_v1"))
    return table, targets, statistics


def prepare_quality(sources, source_views=None):
    """Inventory only actual input lines; never combine independent table counts."""
    sources = _list(sources, "invalid_quality_scope")
    identities = [_identity(source) for source in sources]
    if len(set(identities)) != len(identities):
        _error("invalid_quality_scope", "同一来源版本不得重复计数")
    views = None
    if source_views is not None:
        selected = _list(source_views, "invalid_quality_scope")
        keys = [_identity(view) for view in selected]
        if len(set(keys)) != len(keys) or set(keys) != set(identities):
            _error("invalid_quality_scope", "实际片段必须精确对应所选来源")
        views = dict(zip(keys, selected))
    result = {"contract": QUALITY_CONTRACT, "policy_version": QUALITY_POLICY_VERSION,
              "statistics_policy": STATISTICS_POLICY_VERSION, "sources": [], "targets": [],
              "statistics": [], "system_facts": [],
              "statistic_definitions": {
                  "resolution": "statistic.target_id -> target; statistic.scope_id -> target.statistic_scopes[scope_id]; "
                                "target binds source_id/source_version, scope binds unit/valid_n/date_interval/source_locators/partial_policy",
                  "missing_policy": "Exclude blank and explicitly declared missing; never impute zero. Unknown markers stay distinct.",
                  "duplicate_policy": "Exclude all rows for duplicate dates from numerical descriptions, retain them in literal counts.",
                  "rounding": "Decimal precision 128; round half up to 6 decimal places; strip trailing zeros.",
                  "count_denominator": "Counts scan valid_n provided rows; value is the selected literal cell count. A row is not proof of complete wear.",
                  "segmentation": "Inclusive date interval split after floor((end-start).days/2); at least 3 valid dates per half. Descriptive only.",
                  "operations": {
                      **{name: "literal_cell_count_v1" for name in
                         ("row_count", "present_count", "blank_count", "declared_missing_count", "zero_count")},
                      "mean": "arithmetic_mean_v1", "minimum": "minimum_v1", "maximum": "maximum_v1",
                      "first_half_mean": "calendar_halves_min3_arithmetic_mean_v1",
                      "second_half_mean": "calendar_halves_min3_arithmetic_mean_v1",
                      "second_minus_first_mean": "calendar_halves_min3_difference_v1",
                  },
              },
              "scope_note": "Only provided source versions and lines; overlapping views are deduplicated. "
                            "Tables are separate scopes, not independent corroboration or pooled measurements."}
    for source, identity in zip(sources, identities):
        lines, fragments = _selected_lines(source, None if views is None else views[identity])
        profile = {
            "source_id": identity[0], "source_version": identity[1], "path": source.get("path"),
            "source_type": source.get("metadata", {}).get("source_type", "unknown"),
            "original_time": source.get("metadata", {}).get("original_time"),
            "provided_locators": _ranges(lines), "fragments": fragments,
            "line_sha256": {str(number): hashlib.sha256(text.encode("utf-8")).hexdigest()
                            for number, text in sorted(lines.items())},
            "tables": [], "format": "text",
        }
        profile["evidence_id"] = _source_scope_id(profile)
        rows, reason = _csv_rows(lines)
        if rows is not None:
            profile["format"] = "csv"
            with localcontext() as decimal_context:
                decimal_context.prec = 128
                table, targets, statistics = _table_material(profile, rows)
            profile["tables"].append(table)
            result["targets"].extend(targets)
            result["statistics"].extend(statistics)
        else:
            profile["table_parse_status"] = reason
            # Prose coverage is an explicit semantic Review obligation.  Counts
            # cannot certify that every claim, speaker or exception was found.
            result["targets"].append({
                "target_id": "semantic-" + _hash([identity, profile["provided_locators"]])[:32],
                "kind": "semantic", "role": "claims_and_conditions",
                "source_id": identity[0], "source_version": identity[1],
                "source_locators": profile["provided_locators"], "source_scope_id": profile["evidence_id"],
                "limitations": [reason, "semantic_completeness_requires_independent_review"],
                "review_obligation": "Read provided fragments for speakers, claims, decisions, corrections, conditions and exceptions. "
                                     "Program inventory does not prove complete semantic coverage.",
            })
        result["sources"].append(profile)
    return result


def _validate_context(context):
    if not isinstance(context, dict) or context.get("contract") != QUALITY_CONTRACT or context.get("policy_version") != QUALITY_POLICY_VERSION:
        _error("invalid_quality_context", "质量契约或规则版本不匹配")
    for field in ("sources", "targets", "statistics", "system_facts"):
        _list(context.get(field), "invalid_quality_context")
    for field, key in (("targets", "target_id"), ("statistics", "stat_id")):
        ids = [item.get(key) for item in context[field] if isinstance(item, dict)]
        if len(ids) != len(context[field]) or any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
            _error("invalid_quality_context", "质量上下文存在重复或缺失标识")


def _record_map(output):
    records = {}
    for record in _list(output.get("objects")):
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not isinstance(record.get("body"), str):
            _error("invalid_quality_output", "质量映射缺少对应知识正文")
        if record["id"] in records:
            _error("invalid_quality_output", "知识标识重复")
        records[record["id"]] = record
    return records


def _body(records, record_id, excerpt):
    if not isinstance(record_id, str) or record_id not in records or not isinstance(excerpt, str) or not excerpt.strip() or excerpt not in records[record_id]["body"]:
        _error("quality_body_mapping_mismatch", "质量声明必须映射到实际候选正文")
    return records[record_id]


def _quality_refs(refs, context, target=None):
    refs = _list(refs)
    if not refs:
        _error("quality_source_reference_mismatch", "质量声明必须引用实际原文")
    sources = {(item["source_id"], item["source_version"]): item for item in context["sources"]}
    found = []
    for ref in refs:
        _keys(ref, ("source_id", "source_version", "locator", "quote"))
        if any(not isinstance(ref.get(key), str) for key in ref):
            _error("quality_source_reference_mismatch", "质量引文字段无效")
        source = sources.get((ref["source_id"], ref["source_version"]))
        if source is None:
            _error("quality_source_reference_mismatch", "质量引文超出本批来源")
        quoted = ref["quote"].splitlines()
        numbers = _numbers(ref["locator"])
        if len(numbers) > len(source["line_sha256"]) or len(quoted) != len(numbers) or any(source["line_sha256"].get(str(number)) != hashlib.sha256(text.encode("utf-8")).hexdigest()
                                             for number, text in zip(numbers, quoted)):
            _error("quality_source_reference_mismatch", "质量引文必须是所指原文完整行，含原始BOM")
        if target is not None:
            allowed = {number for locator in target["source_locators"] for number in _numbers(locator)}
            if (ref["source_id"], ref["source_version"]) != (target["source_id"], target["source_version"]) or not set(numbers).intersection(allowed):
                _error("quality_source_reference_mismatch", "统计引文未绑定该统计的实际原文")
        found.append(ref)
    return found


def _record_refs_cover(record, refs):
    """A declaration must survive publication in the record's own citations."""
    for ref in refs:
        wanted = _numbers(ref["locator"])
        intervals = []
        for actual in record.get("sources", []):
            if (isinstance(actual, dict) and actual.get("source_id") == ref["source_id"]
                    and actual.get("source_version") == ref["source_version"]):
                actual_range = _numbers(actual.get("locator"))
                intervals.append((actual_range.start, actual_range.stop))
        covered_end = wanted.start
        for start, end in sorted(intervals):
            if start <= covered_end:
                covered_end = max(covered_end, end)
        if covered_end < wanted.stop:
            _error("quality_source_reference_mismatch", "质量声明的原文依据未进入该正式知识的引用范围")


def _source_date_qualified(statement, refs):
    """Bind date expressions to cited dates, never the clock or another sentence.

    A yearless Chinese month/day additionally needs same-sentence source
    attribution and one unambiguous complete date in this claim's own quotes.
    """
    if _CURRENT_TIME_WORDS.search(statement):
        return False

    def complete_dates(text):
        values = [date.fromisoformat(value) for value in _EXPLICIT_DATE.findall(text)]
        for match in _CHINESE_DATE.finditer(text):
            year, month, day = match.groups()
            if year is not None:
                if len(year) != 4:
                    raise ValueError("invalid source date year")
                values.append(date(int(year), int(month), int(day)))
        return set(values)

    quoted = set()
    for ref in refs:
        # Preserve the existing treatment of malformed dates inside a larger
        # quotation: ignore that token, not another valid cited date.
        for match in list(_EXPLICIT_DATE.finditer(ref["quote"])) + list(_CHINESE_DATE.finditer(ref["quote"])):
            try:
                quoted.update(complete_dates(match.group()))
            except ValueError:
                continue
    stated = set()
    chinese = bool(_CHINESE_DATE.search(statement))
    for sentence in _SENTENCE_BOUNDARY.split(statement):
        try:
            dates = complete_dates(sentence)
        except ValueError:
            return False
        expressions = list(_CHINESE_DATE.finditer(sentence))
        partial = [match for match in expressions if match[1] is None]
        if partial:
            if not _SOURCE_DATE_ATTRIBUTION.search(sentence):
                return False
            full_spans = [match.span() for match in expressions if match[1] is not None]
            # Do not treat an unparsed, separate year as permission to guess
            # which year a short date means (e.g. "2025年，5月9日").
            if any(not any(start <= year.start() and year.end() <= end for start, end in full_spans)
                   for year in _CHINESE_YEAR.finditer(sentence)):
                return False
            for match in partial:
                try:
                    month, day = int(match[2]), int(match[3])
                except ValueError:
                    return False
                matches = {value for value in quoted if (value.month, value.day) == (month, day)}
                if len(matches) != 1:
                    return False
                dates.update(matches)
        if not dates <= quoted:
            return False
        if (_TEMPORAL_WORDS.search(sentence)
                or chinese and _UNFINISHED_SOURCE_STATE.search(sentence)) and not dates:
            return False
        stated.update(dates)
    return bool(stated)


def _temporal_details(output, field_path, *, claim_index=None, record_id=None,
                      statement=None, body_span=None):
    """Locate a failed declaration without including source or body text.

    JSON Pointer paths address the output passed to this validator. Body offsets count
    Unicode code points, starting at zero with an exclusive end; line and
    column numbers start at one.
    """
    pointer = lambda path: "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1")
                                          for part in path)
    details = {"field_path": pointer(field_path)}
    if claim_index is not None:
        details["claim_index"] = claim_index
    if not isinstance(record_id, str):
        return details
    details["record_id"] = record_id
    objects = output.get("objects") if isinstance(output, dict) else None
    if not isinstance(objects, list):
        return details
    matches = [(index, item["body"]) for index, item in enumerate(objects)
               if isinstance(item, dict) and item.get("id") == record_id
               and isinstance(item.get("body"), str)]
    if len(matches) != 1:
        return details
    index, body = matches[0]
    if body_span is None:
        if not isinstance(statement, str) or not statement or statement not in body:
            return details
        start = body.index(statement)
        body_span = (start, start + len(statement))
    start, end = body_span
    details["body_location"] = {
        "field_path": pointer(("objects", index, "body")), "start": start, "end": end,
        "line": body.count("\n", 0, start) + 1,
        "column": start - body.rfind("\n", 0, start),
    }
    return details


def validate_quality_output(output, context):
    """Reject absent/contradictory machine-verifiable commitments before Review."""
    _validate_context(context)
    if not isinstance(output, dict):
        _error("invalid_quality_output", "缺少质量输出")
    quality = output.get("quality")
    try:
        _schema(quality, QUALITY_SCHEMA, "invalid_quality_output", _path=("quality",))
    except CaptureError as error:
        path = getattr(error, "_quality_field_path", ())
        if path[:2] == ("quality", "temporal_claims"):
            index = path[2] if len(path) > 2 and isinstance(path[2], int) else None
            claims = quality.get("temporal_claims") if isinstance(quality, dict) else None
            claim = claims[index] if isinstance(claims, list) and index is not None else None
            error.details = _temporal_details(output, path, claim_index=index,
                record_id=claim.get("record_id") if isinstance(claim, dict) else None,
                statement=claim.get("statement") if isinstance(claim, dict) else None)
        raise
    _keys(quality, ("coverage_disposition", "statistic_claims", "temporal_claims"))
    records = _record_map(output)
    targets = {item["target_id"]: item for item in context["targets"]}
    seen = set()
    for item in _list(quality["coverage_disposition"]):
        _keys(item, ("target_id", "status", "record_ids", "body_evidence", "reason"))
        target_id = item["target_id"]
        if not isinstance(target_id, str) or target_id not in targets or target_id in seen:
            _error("quality_coverage_mismatch", "内容去向缺项、重复或超出本批资料")
        seen.add(target_id)
        ids = _unique_strings(item["record_ids"])
        evidence = _list(item["body_evidence"])
        if not isinstance(item["reason"], str):
            _error("invalid_quality_output", "处理理由必须为文本")
        if item["status"] == "summarized":
            if not ids or set(ids) - set(records) or not evidence:
                _error("quality_body_mapping_mismatch", "已总结指标必须绑定全部对应知识正文")
            mapped, excerpts = set(), set()
            target = targets[target_id]
            for entry in evidence:
                _keys(entry, ("record_id", "text"))
                record = _body(records, entry["record_id"], entry["text"])
                excerpt = (entry["record_id"], entry["text"])
                if entry["record_id"] not in ids or excerpt in excerpts:
                    _error("quality_body_mapping_mismatch", "正文摘录不得重复或指向未列出的知识")
                excerpts.add(excerpt)
                mapped.add(entry["record_id"])
                refs = record.get("sources", [])
                if not any(isinstance(ref, dict) and ref.get("source_id") == target["source_id"]
                           and ref.get("source_version") == target["source_version"] for ref in refs):
                    _error("quality_source_reference_mismatch", "内容去向对应知识未引用该来源版本")
            if mapped != set(ids):
                _error("quality_body_mapping_mismatch", "内容去向正文映射不完整")
        elif item["status"] == "deferred":
            if ids or evidence or len(item["reason"].strip()) < 12:
                _error("quality_deferral_unspecified", "暂缓须给具体证据限制，不能挂接已总结知识")
            if re.fullmatch(r"(?:资料不足|证据不足|暂不处理|无法判断|insufficient evidence|unknown)[。.! ]*", item["reason"].strip(), re.I):
                _error("quality_deferral_unspecified", "空泛暂缓理由不能代替指标去向")
        else:
            _error("invalid_quality_output", "内容去向状态无效")
    if seen != set(targets):
        _error("quality_coverage_mismatch", "每个指标或语义片段均需明确处理去向")
    statistics = {item["stat_id"]: item for item in context["statistics"]}
    summarized = {item["target_id"]: set(item["record_ids"]) for item in quality["coverage_disposition"]
                  if item["status"] == "summarized"}
    claimed = set()
    for claim in _list(quality["statistic_claims"]):
        _keys(claim, ("stat_id", "record_id", "value", "body_evidence", "sources"))
        sid = claim["stat_id"]
        if not isinstance(sid, str) or sid not in statistics or not isinstance(claim["record_id"], str):
            _error("quality_statistic_mismatch", "派生统计必须引用程序计算标识")
        key = (sid, claim["record_id"])
        if key in claimed:
            _error("quality_statistic_mismatch", "派生统计声明重复")
        claimed.add(key)
        stat = resolve_statistic(context, statistics[sid])
        if claim["value"] != stat["value"]:
            _error("quality_statistic_mismatch", "派生统计值不匹配程序计算")
        record = _body(records, claim["record_id"], claim["body_evidence"])
        if claim["record_id"] not in summarized.get(stat["target_id"], set()):
            _error("quality_statistic_mismatch", "统计声明必须属于该知识已总结的同一指标")
        numeric_tokens = re.findall(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", claim["body_evidence"])
        if not _numeric_token_matches(numeric_tokens, claim["value"]):
            _error("quality_statistic_mismatch", "统计声明值未出现在对应正文")
        refs = _quality_refs(claim["sources"], context, stat)
        _record_refs_cover(record, refs)
    temporal = set()
    facts = {fact.get("system_fact_id"): fact for fact in context["system_facts"] if isinstance(fact, dict)}
    for index, claim in enumerate(_list(quality["temporal_claims"])):
        field = None
        try:
            _keys(claim, ("record_id", "statement", "basis", "source_refs", "system_fact_id"))
            field = "record_id" if claim["record_id"] not in records else "statement"
            record = _body(records, claim["record_id"], claim["statement"])
            key = (claim["record_id"], claim["statement"])
            field = "statement"
            if key in temporal:
                _error("quality_temporal_mismatch", "时间状态声明重复")
            temporal.add(key)
            if claim["basis"] == "source_time":
                field = "system_fact_id"
                if claim["system_fact_id"] is not None:
                    _error("quality_temporal_mismatch", "来源当时状态须明确时间限定")
                field = "source_refs"
                refs = _quality_refs(claim["source_refs"], context)
                field = "statement"
                if not (_SOURCE_TIME_WORDS.search(claim["statement"])
                        or _source_date_qualified(claim["statement"], refs)):
                    _error("quality_temporal_mismatch", "来源当时状态须明确时间限定")
                field = "source_refs"
                _record_refs_cover(record, refs)
            elif claim["basis"] == "unknown":
                field = "system_fact_id" if claim["system_fact_id"] is not None else "statement"
                if claim["system_fact_id"] is not None or not _UNKNOWN_TIME_WORDS.search(claim["statement"]):
                    _error("quality_temporal_mismatch", "未知时间状态须在正文明确")
                field = "source_refs"
                _record_refs_cover(record, _quality_refs(claim["source_refs"], context))
            elif claim["basis"] == "current_state":
                fact = facts.get(claim["system_fact_id"]) if isinstance(claim["system_fact_id"], str) else None
                field = ("statement" if fact and fact.get("statement") != claim["statement"]
                         else "system_fact_id")
                if not fact or fact.get("statement") != claim["statement"] or not fact.get("binding"):
                    _error("quality_current_state_unverified", "当前系统状态必须绑定调用者核定的版本事实")
                field = "source_refs"
                if claim["source_refs"]:
                    _record_refs_cover(record, _quality_refs(claim["source_refs"], context))
            else:
                field = "basis"
                _error("quality_temporal_mismatch", "时间依据类型无效")
        except CaptureError as error:
            path = ("quality", "temporal_claims", index) + ((field,) if field is not None else ())
            error.details = _temporal_details(output, path, claim_index=index,
                record_id=claim.get("record_id") if isinstance(claim, dict) else None,
                statement=claim.get("statement") if isinstance(claim, dict) else None)
            raise
    # A small deterministic guard catches the concrete stale-state regression;
    # semantic Review is still mandatory for less obvious temporal language.
    for record_id, record in records.items():
        covered_spans = []
        for rid, statement in temporal:
            if rid == record_id:
                covered_spans.extend((match.start(), match.end())
                                     for match in re.finditer(re.escape(statement), record["body"]))
        for match in _TEMPORAL_WORDS.finditer(record["body"]):
            if not any(start <= match.start() and match.end() <= end for start, end in covered_spans):
                _error("quality_temporal_missing", "含当前/尚未等状态措辞的正文缺少时间声明",
                    details=_temporal_details(output, ("quality", "temporal_claims"),
                        record_id=record_id, body_span=match.span()))


def project_quality(context, scope=None):
    """Project Review material to the exact decomposed unit's visible scope."""
    _validate_context(context)
    if scope is None:
        return copy.deepcopy(context)
    if not isinstance(scope, dict):
        _error("invalid_quality_scope", "审核单元不是有效对象")
    kind = scope.get("kind")
    if kind not in {"object", "coverage", "global"}:
        _error("invalid_quality_scope", "审核单元类型无效")
    fragment_ids = set(_unique_strings(scope.get("fragment_ids", []), "invalid_quality_scope"))
    available = {fragment["fragment_id"] for source in context["sources"] for fragment in source["fragments"]}
    if fragment_ids - available:
        _error("invalid_quality_scope", "审核片段超出质量上下文")
    projected = {key: copy.deepcopy(value) for key, value in context.items()
                 if key not in {"sources", "targets", "statistics"}}
    projected.update(sources=[], targets=[], statistics=[],
                     scope={"kind": kind, "object_paths": list(scope.get("object_paths", [])),
                            "fragment_ids": sorted(fragment_ids)},
                     scope_mode="certificate_only" if kind == "global" else "actual_source_fragments")
    for source in context["sources"]:
        selected = [fragment for fragment in source["fragments"] if fragment["fragment_id"] in fragment_ids]
        if not selected:
            continue
        numbers = {number for fragment in selected for locator in fragment["locators"] for number in _numbers(locator)}
        view = copy.deepcopy(source)
        view["fragments"] = copy.deepcopy(selected)
        view["provided_locators"] = _ranges(numbers)
        view["line_sha256"] = {key: value for key, value in source["line_sha256"].items() if int(key) in numbers}
        view["evidence_id"] = _source_scope_id(view)
        if view["provided_locators"] != source["provided_locators"]:
            view["tables"] = [{"table_id": table["table_id"], "headers": table["headers"],
                               "header_locators": [locator for locator in table["header_locators"]
                                                   if set(_numbers(locator)).issubset(numbers)],
                               "scope_note": "Whole-table counts not supplied to this partial Review unit."}
                              for table in source["tables"]]
        projected["sources"].append(view)
    for target in context["targets"]:
        if kind == "global":
            projected["targets"].append({key: copy.deepcopy(target[key])
                                         for key in ("target_id", "kind", "source_id", "source_version")})
        else:
            selected_source = next((source for source in projected["sources"]
                                    if source["source_id"] == target["source_id"]
                                    and source["source_version"] == target["source_version"]), None)
            if selected_source is None:
                continue
            view = copy.deepcopy(target)
            view["source_scope_id"] = selected_source["evidence_id"]
            if selected_source["provided_locators"] != target["source_locators"]:
                for field in ("counts", "date_interval", "descriptive_n"):
                    view.pop(field, None)
                view["scope_note"] = "Only the selected fragments may be checked; whole-target totals require coverage certificates."
                numbers = {number for source in projected["sources"] if source["source_id"] == target["source_id"]
                           and source["source_version"] == target["source_version"]
                           for locator in source["provided_locators"] for number in _numbers(locator)}
                view["source_locators"] = _ranges(numbers)
            projected["targets"].append(view)
    for stat in context["statistics"]:
        if kind == "global":
            # Global certificates carry candidate declarations and prior Review
            # proof, not independently reusable source computation material.
            break
        resolved = resolve_statistic(context, stat)
        profile = next((source for source in projected["sources"]
                        if source["source_id"] == resolved["source_id"]
                        and source["source_version"] == resolved["source_version"]), None)
        if profile is None:
            continue
        available = {int(number) for number in profile["line_sha256"]}
        needed = {number for locator in resolved["source_locators"] for number in _numbers(locator)}
        target = next(item for item in context["targets"] if item["target_id"] == stat["target_id"])
        # Excluded rows establish missing/duplicate/partial-day decisions and
        # the claimed date interval too.  Sampling rows alone is insufficient.
        needed.update(number for locator in target["source_locators"] for number in _numbers(locator))
        full_profile = next(source for source in context["sources"] if source["source_id"] == resolved["source_id"]
                            and source["source_version"] == resolved["source_version"])
        for table in full_profile["tables"]:
            if table["table_id"] == target.get("table_id"):
                needed.update(number for locator in table["header_locators"] for number in _numbers(locator))
        if needed.issubset(available):
            projected["statistics"].append(copy.deepcopy(stat))
    # Partial units cannot retain hidden whole-table calculation scopes.
    for target in projected["targets"]:
        if "statistic_scopes" in target:
            used = {stat["scope_id"] for stat in projected["statistics"] if stat["target_id"] == target["target_id"]}
            target["statistic_scopes"] = {key: value for key, value in target["statistic_scopes"].items() if key in used}
    return projected


def _candidate_objects(candidate):
    if not isinstance(candidate, dict) or not isinstance(candidate.get("objects"), list):
        _error("invalid_quality_scope", "审核缺少候选对象")
    result = {}
    for obj in candidate["objects"]:
        if not isinstance(obj, dict) or not isinstance(obj.get("path"), str):
            _error("invalid_quality_scope", "审核对象缺少稳定路径")
        if obj["path"] in result:
            _error("invalid_quality_scope", "审核对象路径重复")
        result[obj["path"]] = obj
    return result


def build_review_checks(candidate, context, scope=None):
    """Freeze explicit obligations before the independent model is called."""
    _validate_context(context)
    objects = _candidate_objects(candidate)
    # These obligations belong to the new immutable Review revision only.
    # Rebuilding a historical @5 or earlier request must retain its exact
    # instructions, check IDs and scope hashes.
    bundle = candidate.get("skill_bundle")
    roles = bundle.get("skills", {}) if isinstance(bundle, dict) else {}
    review = roles.get("review") if isinstance(roles, dict) else None
    context_readability = isinstance(review, dict) and review.get("ref") in {"knowledge-review@6", "knowledge-review@7"}
    completion_policy = isinstance(review, dict) and review.get("ref") == "knowledge-review@10"
    batch_material = candidate.get("batch_material")
    draft_only = isinstance(batch_material, dict) and batch_material.get("draft_only") is True
    dispositions = {}
    if completion_policy:
        # Use the complete candidate, before a decomposed request projects its
        # declarations. A missing map must not silently disable this gate.
        try:
            declarations = candidate["extraction_output"]["quality"]["coverage_disposition"]
            dispositions = {item["target_id"]: item["status"] for item in declarations}
        except (KeyError, TypeError, AttributeError):
            _error("invalid_quality_scope", "完成度审核缺少候选的完整内容去向")
        if (not isinstance(declarations, list) or len(dispositions) != len(declarations)
                or set(dispositions) != {target["target_id"] for target in context["targets"]}
                or any(status not in {"summarized", "deferred"} for status in dispositions.values())):
            _error("invalid_quality_scope", "完成度审核须绑定全部候选内容去向")
    material = project_quality(context, scope)
    kind = "complete" if scope is None else scope["kind"]
    paths = sorted(objects) if scope is None else sorted(_unique_strings(scope.get("object_paths", []), "invalid_quality_scope"))
    if set(paths) - set(objects):
        _error("invalid_quality_scope", "审核对象超出候选")
    source_scopes = {source["evidence_id"]: source for source in material["sources"]}
    targets = [target["target_id"] for target in material["targets"]]
    checks = []

    def add(label, instruction, selected_paths, selected_targets, evidence, selected_scopes):
        evidence = sorted(set(evidence))
        allowed_results = None
        binding = {"kind": kind, "check_kind": label,
                   "objects": [{"path": path, "sha256": _hash(objects[path])} for path in selected_paths],
                   "source_scope_ids": sorted(selected_scopes), "target_ids": selected_targets,
                   "quality_sha256": _hash(material)}
        if completion_policy:
            binding["completion_policy"] = "draft-content-v1" if draft_only else "final-content-completion-v1"
            if label in {"metric_coverage", "semantic_coverage", "global_organization"}:
                selected_dispositions = {target: dispositions[target] for target in selected_targets}
                binding["declared_dispositions"] = selected_dispositions
                if not draft_only and "deferred" in selected_dispositions.values():
                    allowed_results = ["needs_revision", "needs_confirmation"]
                    binding["allowed_results"] = allowed_results
                    instruction += (
                        " This final scope still declares deferred content, so this check cannot pass. "
                        "Use needs_confirmation for a genuine unresolved evidence limitation; use needs_revision "
                        "when supplied, processable content was left undone. Explain the actual evidence and affected "
                        "scope; length, future work or a coverage label alone does not establish an evidence gap.")
                elif not draft_only:
                    instruction += (
                        " Confirm the declared summarized targets convey actual provided content in the cited final "
                        "records. A package description, task list or promise to finish later cannot substitute for "
                        "that content. Judge semantic evidence gaps from the supplied sources rather than keywords.")
        if context_readability:
            binding["reading_policy"] = "knowledge-context-v1"
            instruction += {
                "object_fidelity": (
                    " Read this document without navigation or the processing conversation: its own text must identify "
                    "the supplied source or source role, known time and actual scope. Qualify 'this batch', missing material "
                    "or unavailable statistics to the specific source or provided portion, not the whole collection. "
                    "Unknown names, dates or scope must stay unknown; repairable ambiguity requires needs_revision."),
                "object_statistics_time": (
                    " Distinguish same-name fields by source, subject or role, period, stated unit, sample denominator "
                    "and statistical meaning. Do not merge different measures or confuse literal observations with "
                    "derived statistics. Historical source statements do not establish current processing state."),
                "global_organization": (
                    " Each final record and Wiki must remain readable on its own, retaining main content rather than "
                    "only links. Cross-document supplementary relationships may use only actually supplied reviewed "
                    "related records, preserving exact identity/version and original scope, units and statistical meaning; "
                    "current candidate members still require their joint Review. Do not invent links, equivalent metrics "
                    "or claims that unprovided knowledge does not exist. Use only visible final objects and preceding "
                    "certificates; do not claim to have reread absent sources or related documents."),
            }.get(label, "")
        check = {"check_id": "quality-check-" + _hash(binding), "kind": label,
                 "scope_sha256": _hash(binding), "object_paths": selected_paths,
                 "source_scope_ids": sorted(selected_scopes), "target_ids": selected_targets,
                 "evidence_ids": evidence, "instruction": instruction}
        if allowed_results is not None:
            check["allowed_results"] = allowed_results
        checks.append(check)

    if kind in {"complete", "object"}:
        for path in paths:
            refs = objects[path].get("metadata", {}).get("sources", [])
            relevant = []
            for scope_id, source in source_scopes.items():
                supplied = {int(number) for number in source["line_sha256"]}
                if any(isinstance(ref, dict) and ref.get("source_id") == source["source_id"]
                       and ref.get("source_version") == source["source_version"]
                       and any(number in _numbers(ref.get("locator")) for number in supplied) for ref in refs):
                    relevant.append(scope_id)
            evidence = ["object:" + path] + relevant
            add("object_fidelity", "Compare this exact final text with the actual supplied source lines, including conditions, "
                "speaker and original meaning; missing source support requires revision.", [path], [], evidence, relevant)
            add("object_classification", "Check health/career/learning against the same facts; a valid enum is not semantic validation.",
                [path], [], evidence, relevant)
            add("object_statistics_time", "Check every numerical/descriptive claim, its coverage and units, structured stat IDs, "
                "and source-time/current/unknown qualifiers. Every derived numerical claim requires a declared program stat_id; "
                "undeclared calculations require needs_revision, while literal observations must cite raw evidence. "
                "Unstructured prose is also in scope; verify old operational statements "
                "are not asserted as current. Never infer cause, diagnosis or advice from descriptive statistics.",
                [path], [], evidence, relevant)
    if kind in {"complete", "coverage"}:
        for target in material["targets"]:
            relevant = [target["source_scope_id"]]
            add("metric_coverage" if target["kind"] == "metric" else "semantic_coverage",
                "Check this target's actual provided source scope against all final knowledge: summarized mappings must convey "
                "useful observed content, and deferral must name a real evidence limitation. Check missing/zero/partial/duplicate "
                "conditions. For prose independently look for omitted speakers, decisions, corrections, conditions and exceptions; "
                "a coverage list is not proof of complete meaning.",
                paths, [target["target_id"]], relevant + [target["target_id"]], relevant)
    if kind in {"complete", "global"}:
        add("global_organization", "Check final records and Wiki form a useful overview, with consistent classifications, time "
            "conditions and record relationships. Do not count overlapping exports as independent evidence. Global-only Review "
            "uses preceding coverage/object certificates and must not claim to have reread unavailable raw text.",
            paths, targets, ["object:" + path for path in paths] + targets, [])
    if not checks:
        _error("invalid_quality_scope", "审核范围没有可核验义务")
    return checks


def validate_review_repair_scope(output):
    """Validate the declared repair shape, not its factual or semantic merit."""
    if not isinstance(output, dict) or "repair_scope" not in output:
        _error("invalid_review_repair_scope", "新审核合同缺少明确返工范围")
    scope = output["repair_scope"]
    _schema(scope, REPAIR_SCOPE_SCHEMA, "invalid_review_repair_scope")
    kind, bindings = scope["kind"], scope["missing_bindings"]
    seen = set()
    for binding in bindings:
        pair = (binding["stat_id"], binding["record_id"])
        if any(not value.strip() for value in pair) or pair in seen:
            _error("invalid_review_repair_scope", "缺失统计关联须使用不重复的准确身份")
        seen.add(pair)
    if kind == "statistic_bindings":
        checks = output.get("checks", [])
        if (output.get("verdict") != "needs_revision" or not bindings
                or not any(isinstance(check, dict) and check.get("result") == "needs_revision" for check in checks)
                or any(isinstance(check, dict) and check.get("result") == "needs_confirmation" for check in checks)):
            _error("invalid_review_repair_scope", "只有明确纯统计关联遗漏的真实退回可要求局部返工")
    elif bindings or kind == "content" and output.get("verdict") != "needs_revision":
        _error("invalid_review_repair_scope", "返工范围与总审核结论不一致")
    return scope


def validate_quality_review(output, checks, *, repair_scope_supported=False):
    """Require every predeclared check, exact scope, evidence and consistent verdict."""
    fields = {"verdict", "reasons", "checks"} | ({"repair_scope"} if repair_scope_supported else set())
    if not isinstance(output, dict) or set(output) != fields:
        _error("invalid_quality_review", "独立审核缺少完整逐项质量结果")
    if repair_scope_supported:
        validate_review_repair_scope(output)
    expected = {check["check_id"]: check for check in checks}
    if not expected or len(expected) != len(checks):
        _error("invalid_quality_review", "必需审核项无效")
    seen, outcomes = set(), []
    for result in _list(output["checks"], "invalid_quality_review"):
        _schema(result, QUALITY_REVIEW_CHECK_SCHEMA, "invalid_quality_review")
        _keys(result, ("check_id", "scope_sha256", "result", "reason", "evidence_ids"), "invalid_quality_review")
        check_id = result["check_id"]
        if not isinstance(check_id, str) or check_id not in expected or check_id in seen:
            _error("quality_review_coverage_mismatch", "审核项缺失、重复或不属于本次范围")
        seen.add(check_id)
        check = expected[check_id]
        if result["scope_sha256"] != check["scope_sha256"]:
            _error("quality_review_scope_mismatch", "审核结果没有绑定实际候选与资料范围")
        if result["result"] not in {"passed", "needs_revision", "needs_confirmation"}:
            _error("invalid_quality_review", "审核结论无效")
        if "allowed_results" in check and result["result"] not in check["allowed_results"]:
            _error("quality_review_incomplete", "候选仍有暂缓内容，最终完成度检查不能通过",
                   details={"check_id": check_id, "target_ids": list(check["target_ids"]),
                            "allowed_results": list(check["allowed_results"])})
        if not isinstance(result["reason"], str) or len(result["reason"].strip()) < 8:
            _error("quality_review_reason_missing", "逐项审核必须给出可核对理由")
        evidence = _unique_strings(result["evidence_ids"], "invalid_quality_review")
        if not evidence or set(evidence) - set(check["evidence_ids"]):
            _error("quality_review_evidence_mismatch", "审核依据未对应实际可见材料")
        if (result["result"] == "passed" and check["kind"] != "global_organization"
                and (not check["source_scope_ids"] or not set(check["source_scope_ids"]).issubset(evidence))):
            _error("quality_review_evidence_mismatch", "通过的原文检查必须覆盖全部实际来源范围，不能以候选自证")
        outcomes.append(result["result"])
    if seen != set(expected):
        _error("quality_review_coverage_mismatch", "独立审核没有完成所有必需检查")
    verdict = ("needs_revision" if "needs_revision" in outcomes else
               "needs_confirmation" if "needs_confirmation" in outcomes else "passed")
    if output["verdict"] != verdict:
        _error("quality_review_verdict_mismatch", "总审核结论与逐项结果不一致")
    if repair_scope_supported and output["repair_scope"]["kind"] == "statistic_bindings":
        if any(result["result"] != "passed" and expected[result["check_id"]].get("kind") != "object_statistics_time"
               for result in output["checks"]):
            _error("invalid_review_repair_scope", "正文、覆盖、分类或整体关系问题不能伪装成只补统计关联")
    reasons = _list(output["reasons"], "invalid_quality_review")
    if not reasons or any(not isinstance(reason, str) or not reason.strip() for reason in reasons):
        _error("quality_review_reason_missing", "总审核结论须保留理由")
