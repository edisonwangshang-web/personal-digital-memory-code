"""Bounded candidate history storage, separate from model input capacity.

Only a registered, exactly paired quality candidate may use the larger envelope.
The stable 0.23/16/14 pair and historical pairs keep separate contracts. Its current computation remains within the legacy file cap;
only the three explicit, separately verified lineage fields are discounted.
This resource check never grants publication or recovery authority.
"""

import json
import re

from .contracts import CaptureError, markdown, quality_schema_version


LEGACY_FILE_BYTES = 1024 * 1024
MAX_CANDIDATE_BYTES = 4 * LEGACY_FILE_BYTES
_RUN = re.compile(r"run-[0-9a-f]{32}\Z")
_STAGE = re.compile(r"memory/staging/(run-[0-9a-f]{32})\.md\Z")
_LINEAGE = frozenset({"revision_context", "revision_revalidation", "revision_resume"})


def _fail(code="invalid_candidate_storage"):
    raise CaptureError(code, "候选存储未通过路径、固定角色、版本或有限历史大小核验")


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _constant(value):
    _fail()


def read_limit(relative):
    """A provisional read bound; the complete bytes still need validation."""
    return MAX_CANDIDATE_BYTES if isinstance(relative, str) and _STAGE.fullmatch(relative) else LEGACY_FILE_BYTES


def candidate_storage_sizes(candidate):
    """Return canonical envelope bounds, also usable for pre-call lower bounds."""
    if not isinstance(candidate, dict) or not isinstance(candidate.get("run_id"), str) or not _RUN.fullmatch(candidate["run_id"]):
        _fail()
    schema = quality_schema_version(candidate.get("skill_bundle"))
    header = {"schema_version": schema if schema in {"0.15", "0.18", "0.20", "0.21", "0.22", "0.23"} else "0.12",
              "kind": "candidate", "run_id": candidate["run_id"]}
    current = {key: value for key, value in candidate.items() if key not in _LINEAGE}
    return {"core_bytes": len(markdown(header, _canonical(current).decode("utf-8") + "\n")),
            "total_bytes": len(markdown(header, _canonical(candidate).decode("utf-8") + "\n"))}


def validate_candidate_bytes(data, expected_run_id=None):
    """Validate only the oversized exception; ordinary small data is unchanged.

    Callers still perform their existing semantic, schema and computation proof
    checks. Complete bundle validation here reads no files or credentials.
    """
    if not isinstance(data, bytes):
        _fail()
    if len(data) <= LEGACY_FILE_BYTES:
        return {"core_bytes": len(data), "total_bytes": len(data)}
    if len(data) > MAX_CANDIDATE_BYTES:
        _fail("candidate_too_large")
    try:
        if not data.startswith(b"---\n"):
            _fail()
        header, body = data.decode("utf-8")[4:].split("\n---\n", 1)
        metadata = json.loads(header, object_pairs_hook=_unique, parse_constant=_constant)
        candidate = json.loads(body, object_pairs_hook=_unique, parse_constant=_constant)
        if (not isinstance(metadata, dict) or set(metadata) != {"kind", "schema_version", "run_id"}
                or metadata.get("kind") != "candidate" or metadata.get("schema_version") not in {"0.12", "0.15", "0.18", "0.20", "0.21", "0.22", "0.23"}
                or not isinstance(candidate, dict) or not isinstance(candidate.get("run_id"), str)
                or not _RUN.fullmatch(candidate["run_id"]) or metadata["run_id"] != candidate["run_id"]
                or expected_run_id is not None and candidate["run_id"] != expected_run_id
                or candidate.get("quality_contract") != "skill-quality-v1"):
            _fail()
        from .skill_support import validate_skill_bundle
        bundle = validate_skill_bundle(candidate.get("skill_bundle"))
        expected_pair = (("knowledge-extract@16", "knowledge-review@14")
                         if metadata["schema_version"] == "0.23" else
                         ("knowledge-extract@15", "knowledge-review@13")
                         if metadata["schema_version"] == "0.22" else
                         ("knowledge-extract@14", "knowledge-review@12")
                         if metadata["schema_version"] == "0.21" else
                         ("knowledge-extract@13", "knowledge-review@11")
                         if metadata["schema_version"] == "0.20" else
                         ("knowledge-extract@12", "knowledge-review@10")
                         if metadata["schema_version"] == "0.18" else
                         ("knowledge-extract@11", "knowledge-review@9")
                         if metadata["schema_version"] == "0.15" else
                         ("knowledge-extract@10", "knowledge-review@8"))
        if (bundle["skills"]["extract"]["ref"], bundle["skills"]["review"]["ref"]) != expected_pair:
            _fail()
        if any(key in candidate and candidate[key] is not None and not isinstance(candidate[key], dict)
               for key in _LINEAGE):
            _fail()
        sizes = candidate_storage_sizes(candidate)
        if sizes["core_bytes"] > LEGACY_FILE_BYTES or sizes["total_bytes"] > MAX_CANDIDATE_BYTES:
            _fail("candidate_too_large")
        return {**sizes, "total_bytes": len(data)}
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        _fail()


def validate_file_bytes(relative, data):
    """Keep every non-candidate, legacy or unregistered role at one MiB."""
    if not isinstance(data, bytes):
        _fail()
    if len(data) <= LEGACY_FILE_BYTES:
        return
    match = _STAGE.fullmatch(relative) if isinstance(relative, str) else None
    if match is None:
        _fail("candidate_too_large")
    validate_candidate_bytes(data, expected_run_id=match[1])
