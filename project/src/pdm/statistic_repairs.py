"""Materialize an explicitly reviewed missing-statistic mapping repair.

The model chooses the exact record excerpt and statistical identity. The v2
contract binds that choice in the actual Review; a later patch cannot replace
it. This module only completes the value and a source anchor from the identity.
It does not infer mappings from equal numbers, edit frozen knowledge, or verify
Git authority: callers first verify the complete revision context and current
controls, and rebuild the small context before accepting a stored patch.
"""

from copy import deepcopy
import hashlib

from .contracts import CaptureError, canonical
from .knowledge_quality import (build_review_checks, prepare_quality, project_quality, resolve_statistic,
                                validate_quality_output, validate_quality_review,
                                validate_review_repair_scope, _identity, _numbers,
                                _record_refs_cover, _selected_lines)

CONTRACT = "statistic-bindings-repair-v1"
SCHEMA = "0.15"
CONTRACT_V2 = "statistic-bindings-repair-v2"
SCHEMA_V2 = "0.20"


def _fail(code="invalid_statistic_repair"):
    raise CaptureError(code, "统计关联返工未匹配原审核、固定正文或准确来源范围；未改写知识")


def _hash(value):
    try:
        return "sha256-" + hashlib.sha256(canonical(value)).hexdigest()
    except (TypeError, ValueError, RecursionError):
        _fail()


def is_enabled(bundle):
    from .skill_support import statistic_repair_enabled
    return statistic_repair_enabled(bundle)


def _pairs(values, *, evidence_required=False):
    if not isinstance(values, list):
        _fail()
    pairs = []
    fields = {"stat_id", "record_id"} | ({"body_evidence"} if evidence_required else set())
    for value in values:
        if (not isinstance(value, dict) or set(value) != fields
                or any(not isinstance(value[key], str) or not value[key].strip()
                       for key in fields)):
            _fail()
        pairs.append((value["stat_id"], value["record_id"]))
    if len(set(pairs)) != len(pairs):
        _fail("statistic_repair_duplicate")
    return pairs


def _records(objects):
    if not isinstance(objects, list):
        _fail()
    records = {}
    for obj in objects:
        if not isinstance(obj, dict) or not isinstance(obj.get("path"), str):
            _fail()
        if not obj["path"].startswith("memory/records/"):
            continue
        metadata = obj.get("metadata")
        if (not isinstance(metadata, dict) or not isinstance(metadata.get("id"), str)
                or not isinstance(metadata.get("content_version"), str)
                or not isinstance(metadata.get("sources"), list) or not isinstance(obj.get("body"), str)
                or obj["path"] != "memory/records/" + metadata["id"] + ".md"
                or metadata["id"] in records):
            _fail()
        records[metadata["id"]] = obj
    if not records:
        _fail()
    return records


def _record_bindings(objects):
    return [{"record_id": rid, "path": obj["path"],
             "content_version": obj["metadata"]["content_version"],
             "body_sha256": _hash(obj["body"]), "sources_sha256": _hash(obj["metadata"]["sources"])}
            for rid, obj in sorted(_records(objects).items())]


def _statistic_scope(statistic, record):
    """A short declaration anchor never replaces the record's full sample."""
    locators = statistic["source_locators"] or statistic["evidence_locators"]
    _record_refs_cover({"sources": record["metadata"]["sources"]}, [
        {"source_id": statistic["source_id"], "source_version": statistic["source_version"],
         "locator": locator} for locator in locators])
    return locators


def _quality_view(output, objects):
    """Expose verified reused records to existing quality checks, without edits."""
    if not isinstance(output, dict) or not isinstance(output.get("objects"), list):
        _fail()
    records = _records(objects)
    emitted = set()
    for raw in output["objects"]:
        if (not isinstance(raw, dict) or raw.get("id") not in records
                or raw["id"] in emitted):
            _fail()
        old = records[raw["id"]]
        if raw.get("body") != old["body"] or raw.get("sources") != old["metadata"]["sources"]:
            _fail("statistic_repair_frozen_changed")
        emitted.add(raw["id"])
    view = deepcopy(output)
    view["objects"] = [{"id": rid, "body": obj["body"], "sources": deepcopy(obj["metadata"]["sources"])}
                       for rid, obj in records.items()]
    return view


def _review_checks(candidate, record):
    """Recover the actual ordinary or last decomposed Review unit's checks."""
    scope = None
    proof = record.get("decomposed_proof")
    if proof is not None:
        from .decomposed_review import _layout
        if not isinstance(proof, dict) or not isinstance(proof.get("nodes"), list) or not proof["nodes"]:
            _fail()
        payload = {"batch_context": candidate.get("batch_context"),
                   "prior_candidates": candidate.get("merge_inputs", [])}
        if "continuation_context" in candidate:
            payload["continuation_context"] = candidate["continuation_context"]
        units, _ = _layout(candidate, payload)
        scope = next((unit for unit in units if unit["unit_id"] == proof["nodes"][-1].get("unit_id")), None)
        if scope is None:
            _fail()
        if scope["kind"] != "object":
            return None, scope
    return build_review_checks(candidate, candidate["quality_context"], scope=scope), scope


def build_repair_context(revision_context, current_quality_context, *, sources=None,
                         source_views=None, controls_digest=None):
    """Bind a verified Review's exact missing pairs; unrelated repairs return None."""
    if not isinstance(revision_context, dict) or not isinstance(revision_context.get("candidate"), dict):
        _fail()
    candidate = revision_context["candidate"]
    if not is_enabled(candidate.get("skill_bundle")):
        return None
    from .skill_support import statistic_evidence_enabled
    reviewed_evidence = statistic_evidence_enabled(candidate.get("skill_bundle"))
    material = {key: value for key, value in revision_context.items() if key not in {"binding", "model_context"}}
    record = revision_context.get("review_record")
    if (revision_context.get("contract") != "review-feedback-v1"
            or revision_context.get("binding") != _hash(material)
            or candidate.get("binding") != _hash({key: value for key, value in candidate.items() if key != "binding"})
            or revision_context.get("candidate_binding") != candidate["binding"]
            or revision_context.get("source_run_id") != candidate.get("run_id")
            or not isinstance(record, dict) or record.get("binding") != candidate["binding"]
            or record.get("output") != revision_context.get("review_output")):
        _fail("statistic_repair_feedback_mismatch")
    output = record["output"]
    scope = (validate_review_repair_scope(output, evidence_required=True, candidate=candidate)
             if reviewed_evidence else validate_review_repair_scope(output))
    if scope["kind"] != "statistic_bindings":
        return None
    checks, reviewed_scope = _review_checks(candidate, record)
    if checks is None:
        return None
    if reviewed_evidence:
        validate_quality_review(output, checks, repair_scope_supported=True,
                                repair_evidence_supported=True, candidate=candidate,
                                review_quality_context=project_quality(current_quality_context, reviewed_scope))
    else:
        validate_quality_review(output, checks, repair_scope_supported=True)
    indexed_checks = {check["check_id"]: check for check in checks}
    if any(indexed_checks[check["check_id"]]["kind"] != "object_statistics_time"
           for check in output["checks"] if check["result"] != "passed"):
        _fail("statistic_repair_scope_mismatch")
    repairable_paths = {path for check in output["checks"] if check["result"] != "passed"
                        for path in indexed_checks[check["check_id"]]["object_paths"]}
    if (candidate.get("quality_context") != current_quality_context
            or revision_context.get("controls_digest") != candidate.get("controls_digest")
            or controls_digest is not None and controls_digest != revision_context.get("controls_digest")):
        _fail("statistic_repair_context_changed")
    if sources is not None and prepare_quality(sources, source_views) != current_quality_context:
        _fail("statistic_repair_context_changed")
    original = candidate.get("extraction_output")
    frozen = candidate.get("objects")
    view = _quality_view(original, frozen)
    validate_quality_output(view, current_quality_context)
    if (reviewed_evidence and candidate.get("batch_material", {}).get("draft_only") is not True
            and any(row["status"] == "deferred" for row in original["quality"]["coverage_disposition"])):
        # A stopped object Review only proves that object's repair scope. It
        # cannot freeze an unfinished final candidate as a metadata-only job.
        # Leave genuine evidence gaps and content repair to their existing
        # Review/pending paths; this function must not invent either verdict.
        return None
    records = _records(frozen)
    statistics = {stat["stat_id"]: stat for stat in current_quality_context["statistics"]}
    visible = project_quality(current_quality_context, reviewed_scope)
    visible_statistics = {stat["stat_id"] for stat in visible["statistics"]}
    visible_targets = {target["target_id"]: target for target in visible["targets"]}
    visible_sources = {(source["source_id"], source["source_version"], source["evidence_id"])
                       for source in visible["sources"]}
    summarized = {row["target_id"]: set(row["record_ids"])
                  for row in original["quality"]["coverage_disposition"] if row["status"] == "summarized"}
    existing = {(claim["stat_id"], claim["record_id"]) for claim in original["quality"]["statistic_claims"]}
    missing = sorted(_pairs(scope["missing_bindings"], evidence_required=reviewed_evidence))
    body_evidence = ({(entry["stat_id"], entry["record_id"]): entry["body_evidence"]
                      for entry in scope["missing_bindings"]} if reviewed_evidence else {})
    for sid, rid in missing:
        if (sid not in statistics or rid not in records or (sid, rid) in existing
                or rid not in summarized.get(statistics[sid]["target_id"], set())):
            _fail("statistic_repair_scope_mismatch")
        target = visible_targets.get(statistics[sid]["target_id"])
        if (sid not in visible_statistics or target is None
                or (target["source_id"], target["source_version"], target["source_scope_id"]) not in visible_sources
                or reviewed_scope is not None and records[rid]["path"] not in reviewed_scope["object_paths"]):
            _fail("statistic_repair_scope_mismatch")
        if records[rid]["path"] not in repairable_paths:
            # A Wiki's numerical check cannot quietly create a new repair
            # mandate for a separately reviewed record. Keep the full-output
            # path when the actual failed check did not cover this record.
            return None
        stat = resolve_statistic(current_quality_context, statistics[sid])
        if not any(ref.get("source_id") == stat["source_id"] and ref.get("source_version") == stat["source_version"]
                   for ref in records[rid]["metadata"]["sources"] if isinstance(ref, dict)):
            _fail("statistic_repair_scope_mismatch")
        _statistic_scope(stat, records[rid])
        if reviewed_evidence and body_evidence[(sid, rid)] not in records[rid]["body"]:
            _fail("statistic_repair_body_mismatch")
    context = {"contract": CONTRACT_V2 if reviewed_evidence else CONTRACT,
               "schema_version": SCHEMA_V2 if reviewed_evidence else SCHEMA,
               "source_run_id": candidate["run_id"], "revision_binding": revision_context["binding"],
               "candidate_binding": candidate["binding"], "stage": deepcopy(revision_context.get("stage")),
               "review": deepcopy(revision_context.get("review")),
               "controls_digest": revision_context["controls_digest"],
               "source_bindings_sha256": _hash(revision_context.get("source_bindings")),
               "quality_context_sha256": _hash(current_quality_context),
               "original_output_sha256": _hash(original), "frozen_objects_sha256": _hash(frozen),
               "record_bindings": _record_bindings(frozen),
               "missing_bindings": [{"stat_id": sid, "record_id": rid,
                    **({"body_evidence": body_evidence[(sid, rid)]} if reviewed_evidence else {})}
                    for sid, rid in missing]}
    context["binding"] = _hash(context)
    if reviewed_evidence and sources is not None:
        # Verify the already reviewed excerpts against the same numerical and
        # root-source checks a future patch must pass, before another call.
        # This is a pure materialization of copied data, not a model response.
        materialize_patch({"statistic_bindings": deepcopy(context["missing_bindings"])}, context,
            current_quality_context, sources, source_views, original,
            frozen_objects=frozen, controls_digest=controls_digest)
    return context


def _source_lines(sources, source_views, context):
    if prepare_quality(sources, source_views) != context:
        _fail("statistic_repair_context_changed")
    views = {} if source_views is None else {_identity(view): view for view in source_views}
    return {_identity(source): _selected_lines(source, views.get(_identity(source)))[0] for source in sources}


def verify_context(repair_context, revision_context, current_quality_context, **options):
    """Rebuild from the independently verified old computation, not a patch."""
    expected = build_repair_context(revision_context, current_quality_context, **options)
    if expected is None or repair_context != expected:
        _fail("statistic_repair_context_changed")
    return expected


def materialize_patch(raw_patch, repair_context, current_quality_context, sources,
                      source_views, original_output, *, frozen_objects=None, controls_digest=None):
    """Append the requested claims, retaining all supplied frozen output bytes."""
    if not isinstance(repair_context, dict):
        _fail()
    expected_fields = {"contract", "schema_version", "source_run_id", "revision_binding", "candidate_binding",
                       "stage", "review", "controls_digest", "source_bindings_sha256", "quality_context_sha256",
                       "original_output_sha256", "frozen_objects_sha256", "record_bindings", "missing_bindings", "binding"}
    reviewed_evidence = repair_context.get("contract") == CONTRACT_V2
    expected_contract = CONTRACT_V2 if reviewed_evidence else CONTRACT
    expected_schema = SCHEMA_V2 if reviewed_evidence else SCHEMA
    if (set(repair_context) != expected_fields or repair_context.get("contract") != expected_contract
            or repair_context.get("schema_version") != expected_schema
            or repair_context.get("binding") != _hash({key: value for key, value in repair_context.items() if key != "binding"})
            or repair_context["quality_context_sha256"] != _hash(current_quality_context)
            or repair_context["original_output_sha256"] != _hash(original_output)
            or controls_digest is not None and controls_digest != repair_context["controls_digest"]):
        _fail("statistic_repair_context_changed")
    if frozen_objects is None:
        _fail("statistic_repair_frozen_objects_required")
    if (repair_context["frozen_objects_sha256"] != _hash(frozen_objects)
            or repair_context["record_bindings"] != _record_bindings(frozen_objects)):
        _fail("statistic_repair_frozen_changed")
    if not isinstance(raw_patch, dict) or set(raw_patch) != {"statistic_bindings"} or not isinstance(raw_patch["statistic_bindings"], list):
        _fail("invalid_statistic_repair_patch")
    entries = {}
    for entry in raw_patch["statistic_bindings"]:
        if (not isinstance(entry, dict) or set(entry) != {"stat_id", "record_id", "body_evidence"}
                or any(not isinstance(entry[key], str) or not entry[key].strip() for key in entry)):
            _fail("invalid_statistic_repair_patch")
        pair = (entry["stat_id"], entry["record_id"])
        if pair in entries:
            _fail("statistic_repair_duplicate")
        entries[pair] = entry["body_evidence"]
    missing = _pairs(repair_context["missing_bindings"], evidence_required=reviewed_evidence)
    if set(entries) != set(missing):
        _fail("statistic_repair_scope_mismatch")
    if reviewed_evidence:
        reviewed = {(entry["stat_id"], entry["record_id"]): entry["body_evidence"]
                    for entry in repair_context["missing_bindings"]}
        if entries != reviewed:
            _fail("statistic_repair_review_body_mismatch")
    records = _records(frozen_objects)
    statistics = {stat["stat_id"]: stat for stat in current_quality_context["statistics"]}
    lines = _source_lines(sources, source_views, current_quality_context)
    result = deepcopy(original_output)
    _quality_view(result, frozen_objects)
    for sid, rid in missing:
        if sid not in statistics or rid not in records:
            _fail("statistic_repair_scope_mismatch")
        stat = resolve_statistic(current_quality_context, statistics[sid])
        record = records[rid]
        excerpt = entries[(sid, rid)]
        if excerpt not in record["body"]:
            _fail("statistic_repair_body_mismatch")
        identity = stat["source_id"], stat["source_version"]
        original_lines = lines.get(identity, {})
        covered = {number for ref in record["metadata"]["sources"]
                   if isinstance(ref, dict) and (ref.get("source_id"), ref.get("source_version")) == identity
                   for number in _numbers(ref.get("locator"))}
        sample = _statistic_scope(stat, record)
        eligible = sorted({number for locator in sample for number in _numbers(locator)}
                          & covered & set(original_lines))
        if not eligible:
            _fail("statistic_repair_source_mismatch")
        number = eligible[0]
        anchor = {"source_id": identity[0], "source_version": identity[1],
                  "locator": f"L{number}-L{number}", "quote": original_lines[number]}
        _record_refs_cover({"sources": record["metadata"]["sources"]}, [anchor])
        result["quality"]["statistic_claims"].append({"stat_id": sid, "record_id": rid,
            "value": stat["value"], "body_evidence": excerpt, "sources": [anchor]})
    validate_quality_output(_quality_view(result, frozen_objects), current_quality_context)
    return result
