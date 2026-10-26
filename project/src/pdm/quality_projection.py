"""Versioned model view of program-only line integrity material.

This is a model-input projection, not a new wire codec or a stored quality
context.  Complete contexts remain authoritative.  Only maps independently
reconstructable from the exact source lines in this request are replaced.
Historical callers must not opt into this contract implicitly.
"""

from copy import deepcopy
import hashlib
import json
import re

from .contracts import CaptureError
from .knowledge_quality import (
    _identity, _numbers, _ranges, _selected_lines, _source_scope_id,
    _validate_context, prepare_quality,
)


CONTRACT = "quality-model-projection-v1"
LINE_CONTRACT = "provided-source-line-sha256-v1"
RESOLUTION = "program_rebuilds_sha256_utf8_of_exact_provided_global_lines"
MARKER = "quality_projection"
PROJECTION_INSTRUCTIONS = (
    "quality_projection.contract=quality-model-projection-v1 是显式模型质量视图，不是原字段不变的编码。"
    "line_integrity 仅替代程序使用的逐行SHA映射，完整映射由程序从本包原始全局行还原核验，模型不要计算SHA。"
    "原文、全局行号、目标、全部统计、候选正文和完整审核反馈仍在本包；原evidence_id绑定保持。"
    "缺少必要同包行时保留原line_sha256，不能据摘要补读来源、推断事实或豁免任何质量声明。"
)
_SHA = re.compile(r"sha256-[0-9a-f]{64}\Z")
_CHILDREN = {"candidate", "revision_context", "quality_provenance", "model_context",
             "prior_candidates", "merge_inputs"}


def _fail(code="quality_projection_mismatch"):
    raise CaptureError(code, "模型质量投影未通过完整来源行、契约或材料绑定核验")


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail()


def _digest(value):
    return "sha256-" + hashlib.sha256(_canonical(value)).hexdigest()


def _version(contract):
    if contract != CONTRACT:
        _fail("unknown_quality_projection")


def _locations(value):
    """Visit only computation structures, never source metadata or feedback text."""
    if isinstance(value, dict):
        if "quality_context" in value:
            if value.get("quality_contract") != "skill-quality-v1":
                _fail()
            yield value, "quality_context"
        for key in sorted(_CHILDREN.intersection(value)):
            yield from _locations(value[key])
    elif isinstance(value, list):
        for child in value:
            yield from _locations(child)


def _sources(payload):
    result, paths = {}, set()
    supplied = payload.get("sources", [])
    if not isinstance(supplied, list):
        _fail()
    for source in supplied:
        identity = _identity(source)
        metadata = source.get("metadata", {})
        if any(key in source and key in metadata and source[key] != metadata[key]
               for key in ("source_id", "source_version")):
            _fail()
        path = source.get("path")
        if not isinstance(path, str) or not path or path in paths or identity in result:
            _fail()
        lines, _ = _selected_lines(source, None)
        # A body plus fragments must not conceal a conflicting second rendering.
        if "body" in source and "fragments" in source:
            _selected_lines(source, source)
        result[identity] = (source, lines)
        paths.add(path)
    return result


def _profile_lines(profile):
    hashes = profile.get("line_sha256")
    if (not isinstance(hashes, dict) or any(
            not isinstance(number, str) or not re.fullmatch(r"[1-9]\d*", number)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for number, digest in hashes.items())):
        _fail()
    numbers = {int(number) for number in hashes}
    if profile.get("provided_locators") != _ranges(numbers):
        _fail()
    fragments = profile.get("fragments")
    if not isinstance(fragments, list):
        _fail()
    fragment_numbers, ids = set(), set()
    for fragment in fragments:
        if (not isinstance(fragment, dict) or set(fragment) != {"fragment_id", "locators"}
                or not isinstance(fragment["fragment_id"], str) or not fragment["fragment_id"]
                or fragment["fragment_id"] in ids or not isinstance(fragment["locators"], list)):
            _fail()
        ids.add(fragment["fragment_id"])
        for locator in fragment["locators"]:
            # Bound expansion to the actual profile before materializing a range.
            selected = _numbers(locator)
            if len(selected) > len(numbers):
                _fail()
            fragment_numbers.update(selected)
    if fragment_numbers != numbers or profile.get("evidence_id") != _source_scope_id(profile):
        _fail()
    return numbers


def _rebuild(profile, provided, *, allow_partial):
    """Check every supplied line; return None only for a scoped missing input."""
    numbers = _profile_lines(profile)
    identity = _identity(profile)
    actual = provided.get(identity)
    if actual is None:
        if allow_partial:
            return None
        _fail()
    source, lines = actual
    if profile.get("path") != source["path"]:
        _fail()
    for number in numbers.intersection(lines):
        if profile["line_sha256"][str(number)] != hashlib.sha256(lines[number].encode("utf-8")).hexdigest():
            _fail()
    if not numbers.issubset(lines):
        if allow_partial:
            return None
        _fail()
    return {str(number): hashlib.sha256(lines[number].encode("utf-8")).hexdigest()
            for number in sorted(numbers)}


def _selected_sources(context, provided):
    """Recompute full quality from precisely these profiles, not extra input lines."""
    result = []
    for profile in context["sources"]:
        source, lines = provided[_identity(profile)]
        if (not profile["line_sha256"] and len(profile["fragments"]) == 1
                and profile["fragments"][0]["locators"] == []):
            # A valid empty/link-only body has a deterministic whole fragment
            # with no locator. Keep it distinct from a zero-fragment view.
            result.append({"path": source["path"], "metadata": deepcopy(source.get("metadata", {})),
                           "body": ""})
            continue
        fragments = []
        for fragment in profile["fragments"]:
            # Normal prepared fragments have one continuous locator. Split
            # fragments are scoped Review material and are not full contexts.
            if len(fragment["locators"]) != 1:
                _fail()
            locator = fragment["locators"][0]
            fragments.append({"fragment_id": fragment["fragment_id"], "locator": locator,
                              "lines": [{"line": number, "text": lines[number]}
                                        for number in _numbers(locator)]})
        result.append({"path": source["path"], "metadata": deepcopy(source.get("metadata", {})),
                       "fragments": fragments})
    return result


def _context(context, provided, *, restoring, unit_scope, current):
    context = deepcopy(context)
    _validate_context(context)
    scoped = context.get("scope_mode") in {"actual_source_fragments", "certificate_only"}
    if "scope_mode" in context and not scoped:
        _fail()
    if scoped and (not isinstance(context.get("scope"), dict)
                   or context["scope"].get("kind") not in {"object", "coverage", "global"}
                   or (context["scope"]["kind"] == "global") != (context["scope_mode"] == "certificate_only")):
        _fail()
    allow_partial = scoped or unit_scope
    complete = True
    identities = set()
    for profile in context["sources"]:
        if not isinstance(profile, dict):
            _fail()
        identity = _identity(profile)
        if identity in identities:
            _fail()
        identities.add(identity)
        if restoring and "line_integrity" in profile:
            descriptor = profile.pop("line_integrity")
            if ("line_sha256" in profile or not isinstance(descriptor, dict)
                    or set(descriptor) != {"contract", "sha256", "line_count", "resolution"}
                    or descriptor.get("contract") != LINE_CONTRACT
                    or descriptor.get("resolution") != RESOLUTION
                    or not isinstance(descriptor.get("sha256"), str) or not _SHA.fullmatch(descriptor["sha256"])
                    or type(descriptor.get("line_count")) is not int or descriptor["line_count"] < 0):
                _fail()
            actual = provided.get(identity)
            if actual is None or profile.get("path") != actual[0]["path"]:
                _fail()
            lines = actual[1]
            numbers = set()
            for locator in profile.get("provided_locators", []):
                selected = _numbers(locator)
                if len(selected) > len(lines):
                    _fail()
                numbers.update(selected)
            if not numbers.issubset(lines):
                _fail()
            hashes = {str(number): hashlib.sha256(lines[number].encode("utf-8")).hexdigest()
                      for number in sorted(numbers)}
            if descriptor["line_count"] != len(hashes) or descriptor["sha256"] != _digest(hashes):
                _fail()
            profile["line_sha256"] = hashes
        elif "line_integrity" in profile:
            _fail()
        hashes = _rebuild(profile, provided, allow_partial=allow_partial)
        complete = complete and hashes is not None
    if current and not allow_partial:
        if identities != set(provided) or any(
                {int(number) for number in profile["line_sha256"]} != set(provided[_identity(profile)][1])
                for profile in context["sources"]):
            _fail()
    if complete and not scoped:
        if prepare_quality(_selected_sources(context, provided)) != context:
            _fail()
    if not restoring:
        for profile in context["sources"]:
            hashes = _rebuild(profile, provided, allow_partial=allow_partial)
            if hashes:
                del profile["line_sha256"]
                profile["line_integrity"] = {"contract": LINE_CONTRACT, "sha256": _digest(hashes),
                                             "line_count": len(hashes), "resolution": RESOLUTION}
    return context


def _transform(payload, *, contract, restoring):
    _version(contract)
    if not isinstance(payload, dict):
        _fail()
    value = deepcopy(payload)
    if restoring:
        marker = value.pop(MARKER, None)
        if (not isinstance(marker, dict) or set(marker) != {"contract", "full_material_sha256"}
                or marker.get("contract") != contract
                or not isinstance(marker.get("full_material_sha256"), str)
                or not _SHA.fullmatch(marker["full_material_sha256"])):
            _fail()
    else:
        if MARKER in value:
            _fail()
        marker = {"contract": contract, "full_material_sha256": _digest(value)}
    locations = list(_locations(value))
    provided = _sources(value) if locations else {}
    unit_scope = (isinstance(value.get("review_scope"), dict)
                  and value["review_scope"].get("kind") in {"object", "coverage", "global"})
    for parent, key in locations:
        parent[key] = _context(parent[key], provided, restoring=restoring,
                               unit_scope=unit_scope, current=parent is value)
    if restoring:
        if marker["full_material_sha256"] != _digest(value):
            _fail()
    else:
        value[MARKER] = marker
    return value


def project_quality_payload(payload, *, contract=CONTRACT):
    """Return an explicit model-only view; never mutate persisted material.

    Unit Review contexts retain a full hash map when this same request lacks
    any required original line. No source is read or added by this function.
    """
    try:
        return _transform(payload, contract=contract, restoring=False)
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        _fail()


def restore_quality_payload(payload, *, contract=CONTRACT):
    """Rebuild every projected map and verify the complete original payload."""
    try:
        return _transform(payload, contract=contract, restoring=True)
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        _fail()
