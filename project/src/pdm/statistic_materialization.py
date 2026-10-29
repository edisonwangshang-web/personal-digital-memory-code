"""Fill deterministic fields of explicitly selected ordinary statistic claims.

The native model still selects the statistic, record and verbatim body excerpt.
This transformation never chooses facts, changes text or invents associations.
Raw output and the materialized candidate retain separate immutable bindings.
"""

from copy import deepcopy
import hashlib

from .contracts import CaptureError, canonical
from .knowledge_quality import (_body, _identity, _numbers, _record_map,
                                _record_refs_cover, _selected_lines,
                                prepare_quality, resolve_statistic)
from .statistic_repairs import _statistic_scope


CONTRACT = "ordinary-statistic-claims-v1"
SCHEMA = "0.22"
MARKER = "statistic_claims_materialization"


def _fail(code="invalid_statistic_materialization"):
    raise CaptureError(code, "普通统计声明未匹配明确标识、固定正文及完整来源范围")


def _hash(value):
    try:
        return "sha256-" + hashlib.sha256(canonical(value)).hexdigest()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail()


def materialize_statistic_claims(raw_output, quality_context, sources, source_views=None,
                                 *, reused_objects=()):
    """Return an independent full output and reproducible program-only marker."""
    if (not isinstance(raw_output, dict) or not isinstance(raw_output.get("quality"), dict)
            or not isinstance(raw_output["quality"].get("statistic_claims"), list)
            or not isinstance(quality_context, dict)):
        _fail()
    if prepare_quality(sources, source_views,
                       policy_version=quality_context.get("policy_version")) != quality_context:
        _fail("statistic_materialization_context_changed")
    records = _record_map(raw_output)
    for obj in reused_objects:
        if (not isinstance(obj, dict) or not isinstance(obj.get("metadata"), dict)
                or not isinstance(obj.get("body"), str)):
            _fail()
        metadata = obj["metadata"]
        if metadata.get("type") == "wiki":
            continue
        record = {**metadata, "body": obj["body"]}
        identifier = record.get("id")
        if (not isinstance(identifier, str) or identifier in records
                or obj.get("path") != "memory/records/" + identifier + ".md"):
            _fail()
        records[identifier] = record
    statistics = {item["stat_id"]: item for item in quality_context["statistics"]}
    views = {} if source_views is None else {_identity(item): item for item in source_views}
    lines = {_identity(item): _selected_lines(item, views.get(_identity(item)))[0]
             for item in sources}
    result, seen, claims = deepcopy(raw_output), set(), []
    for entry in raw_output["quality"]["statistic_claims"]:
        if (not isinstance(entry, dict) or set(entry) != {"stat_id", "record_id", "body_evidence"}
                or any(not isinstance(entry[key], str) or not entry[key].strip() for key in entry)):
            _fail("invalid_short_statistic_claim")
        pair = entry["stat_id"], entry["record_id"]
        if pair in seen:
            _fail("duplicate_short_statistic_claim")
        seen.add(pair)
        if entry["stat_id"] not in statistics:
            _fail("statistic_materialization_statistic_missing")
        record = _body(records, entry["record_id"], entry["body_evidence"])
        stat = resolve_statistic(quality_context, statistics[entry["stat_id"]])
        # A one-line anchor documents the value. The record must nevertheless
        # retain every source line of the statistic's actual fixed sample.
        sample = _statistic_scope(stat, {"metadata": record})
        original_lines = lines.get((stat["source_id"], stat["source_version"]), {})
        available = {number for locator in sample for number in _numbers(locator)}
        if not available or not available.issubset(original_lines):
            _fail("statistic_materialization_source_missing")
        number = min(available)
        anchor = {"source_id": stat["source_id"], "source_version": stat["source_version"],
                  "locator": f"L{number}-L{number}", "quote": original_lines[number]}
        _record_refs_cover(record, [anchor])
        claims.append({**deepcopy(entry), "value": stat["value"], "sources": [anchor]})
    result["quality"]["statistic_claims"] = claims
    marker = {"contract": CONTRACT, "schema_version": SCHEMA,
              "raw_output_sha256": _hash(raw_output),
              "quality_context_sha256": _hash(quality_context),
              "records_sha256": _hash([{key: record.get(key) for key in ("id", "body", "sources")}
                                        for _, record in sorted(records.items())]),
              "materialized_output_sha256": _hash(result)}
    return result, marker
