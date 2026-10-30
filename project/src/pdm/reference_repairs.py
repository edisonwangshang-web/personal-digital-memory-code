"""One extract can repair only the bad references in a fixed failed draft.

This is not a general JSON patch interface or a publishable result.  Both raw
patch and deterministically rebuilt complete draft still face every validator
and a new independent Review.  No quote or locator is inferred or corrected.
"""

from copy import deepcopy
import hashlib
import json
import re

from .contracts import CaptureError


CONTRACT = "reference-fields-repair-v1"
MAX_REPAIRS = 32
REFERENCE_FIELDS = {"source_id", "source_version", "locator", "quote"}
REFERENCE_ERRORS = {
    "invalid_quote", "invalid_locator", "source_reference_mismatch",
    "quality_source_reference_mismatch", "invalid_fragment_reference",
    "fragment_reference_outside_view",
}
_INDEX = r"(?:0|[1-9][0-9]{0,8})"
_PATH = re.compile(
    rf"/(?:objects/{_INDEX}/sources/{_INDEX}|wiki/sources/{_INDEX}|"
    rf"wikis/{_INDEX}/sources/{_INDEX}|"
    rf"quality/temporal_claims/{_INDEX}/source_refs/{_INDEX}|"
    rf"quality/statistic_claims/{_INDEX}/sources/{_INDEX})\Z")


def _fail(code="invalid_reference_repair"):
    raise CaptureError(code, "引用修补未匹配当前完整稿及准确错误位置；没有改写其他知识")


def _sha(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("invalid_reference_repair_context")
    return "sha256-" + hashlib.sha256(raw).hexdigest()


def _parent(draft, path):
    if not isinstance(path, str) or _PATH.fullmatch(path) is None:
        _fail("reference_repair_scope_mismatch")
    parts = path[1:].split("/")
    node = draft
    try:
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        if not isinstance(node, list):
            _fail("reference_repair_scope_mismatch")
        index = int(parts[-1])
        original = node[index]
        if not isinstance(original, dict):
            _fail("reference_repair_scope_mismatch")
        return node, index
    except (KeyError, IndexError, TypeError, ValueError):
        _fail("reference_repair_scope_mismatch")


def build_context(feedback):
    """Offer a narrow repair only for a complete list of reference-only faults."""
    if not isinstance(feedback, dict) or feedback.get("draft_contract") != "current-draft-complete-quality-v2":
        return None
    draft = feedback.get("draft")
    error = feedback.get("validation_error")
    details = error.get("details") if isinstance(error, dict) else None
    if (not isinstance(draft, dict) or not isinstance(draft.get("objects"), list)
            or not isinstance(draft.get("quality"), dict)
            or not isinstance(details, dict) or details.get("issues_truncated") is not False):
        return None
    issues = details.get("issues")
    if not isinstance(issues, list) or not 1 <= len(issues) <= MAX_REPAIRS:
        return None
    # The primary failure is also authoritative; never turn an incomplete
    # diagnostic walk into permission to freeze content or a broken schema.
    if error.get("code") not in REFERENCE_ERRORS:
        return None
    disposition = draft["quality"].get("coverage_disposition")
    if (not isinstance(disposition, list)
            or any(isinstance(item, dict) and item.get("status") == "deferred" for item in disposition)):
        # Do not spend a bounded attempt freezing known unfinished content.
        # Genuine missing evidence follows the ordinary Review/pending path.
        return None
    generations = feedback.get("generation_invocations")
    if (not isinstance(generations, list) or not 1 <= len(generations) <= 4
            or any(not isinstance(item, str) or not item or len(item) > 256 for item in generations)
            or generations != sorted(set(generations))):
        return None
    paths = []
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("code") not in REFERENCE_ERRORS:
            return None
        path = issue.get("field_path")
        try:
            _parent(draft, path)
        except CaptureError:
            return None
        if path not in paths:
            paths.append(path)
    return {"contract": CONTRACT, "field_paths": paths,
            "draft_sha256": _sha(draft), "issues_sha256": _sha(issues),
            "generation_invocations": deepcopy(generations)}


def checked_context(feedback):
    expected = build_context(feedback)
    if expected is None or feedback.get("repair_context") != expected:
        _fail("reference_repair_context_changed")
    return expected


def materialize_patch(raw_output, feedback):
    """Replace exactly the listed whole reference fields; freeze everything else."""
    context = checked_context(feedback)
    if not isinstance(raw_output, dict) or set(raw_output) != {"reference_repairs"}:
        _fail()
    repairs = raw_output["reference_repairs"]
    expected = context["field_paths"]
    if not isinstance(repairs, list) or len(repairs) != len(expected):
        _fail("reference_repair_scope_mismatch")
    result = deepcopy(feedback["draft"])
    seen = set()
    for repair in repairs:
        if not isinstance(repair, dict) or set(repair) != {"field_path", "reference"}:
            _fail()
        path, reference = repair["field_path"], repair["reference"]
        if not isinstance(path, str) or path not in expected or path in seen:
            _fail("reference_repair_scope_mismatch")
        if (not isinstance(reference, dict) or set(reference) != REFERENCE_FIELDS
                or any(not isinstance(reference[key], str) or not reference[key].strip()
                       for key in REFERENCE_FIELDS)):
            _fail()
        parent, index = _parent(result, path)
        parent[index] = deepcopy(reference)
        seen.add(path)
    if seen != set(expected):
        _fail("reference_repair_scope_mismatch")
    return result
