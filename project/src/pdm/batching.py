"""Read-only, immutable-source batch planning and global-line projections.

Only explicit source paths are read. Structural units are conservative: complete
conversations, prose and JSON remain atomic; tables can repeat their headers,
and top-level Markdown sections retain their shared preamble. Unresolved
cross-section context is never silently cut to fit a byte budget.
"""

from copy import deepcopy
from contextlib import nullcontext
import csv
import io
import json
import re

from .contracts import CaptureError, canonical
from .controls import source_locator_policy, source_policy
from .pipeline import (SOURCE_RE, _hash, _source_binding, control_guard,
                       execution_versions, load_source)
from .transaction import PipelineGit

SCHEMA = "0.6"
MAX_PLAN_SOURCES = 256
MAX_BATCH_SOURCES = 8
MAX_BYTES = 256_000
_LOCATOR = re.compile(r"L([1-9][0-9]*)-L([1-9][0-9]*)\Z")
_ID = re.compile(r"(?:plan|batch|fragment)-[0-9a-f]{64}\Z")


def _fail(code="invalid_batch_plan"):
    raise CaptureError(code, "分批范围、语义单元或固定证据校验未通过；未截断原文或扩大读取")


def _paths(paths):
    if (not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_PLAN_SOURCES
            or any(not isinstance(path, str) or SOURCE_RE.fullmatch(path) is None for path in paths)
            or len(set(paths)) != len(paths)):
        _fail("invalid_batch_scope")
    return sorted(paths)


def _bytes(value):
    # Match the transport's JSON whitespace and UTF-8 policy, not compact JSON.
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())


def _bounds(locator):
    match = _LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
    if match is None or any(len(value) > 9 for value in match.groups()):
        _fail("invalid_fragment_locator")
    start, end = map(int, match.groups())
    if start > end:
        _fail("invalid_fragment_locator")
    return start, end


def _lines(source, start, end, *, source_lines=None):
    lines = source["body"].splitlines() if source_lines is None else source_lines
    if not 1 <= start <= end <= len(lines):
        _fail("invalid_fragment_locator")
    return [{"line": number, "text": lines[number - 1]} for number in range(start, end + 1)]


def _fragment(source, start, end, kind, parents=(), *, source_lines=None):
    locator = f"L{start}-L{end}"
    material = {"path": source["path"], "source_id": source["source_id"],
                "source_version": source["source_version"], "locator": locator,
                "text_sha256": _hash(_lines(source, start, end, source_lines=source_lines))}
    return {"fragment_id": "fragment-" + _hash(material)[7:], **material,
            "kind": kind, "parent_fragment_ids": list(parents)}


def _units(source):
    """Return fragments and primary units, retaining all prerequisite lines."""
    lines = source["body"].splitlines()
    if not lines:
        return [], []
    def fragment(start, end, kind, parents=()):
        return _fragment(source, start, end, kind, parents, source_lines=lines)
    def atomic(kind):
        item = fragment(1, len(lines), kind)
        return [item], [item["fragment_id"]]
    if source["metadata"]["source_type"] == "conversation":
        return atomic("conversation_atomic")
    if (source["body"].lstrip().startswith(("{", "["))
            or any(re.match(r"^\s*(`{3,}|~{3,})", line) for line in lines)):
        return atomic("structured_atomic")
    if re.search(r"上述|上文|前文|上节|同上|以上|前述|\b(?:above|previous|aforementioned|ditto)\b", source["body"], re.I):
        return atomic("dependent_context_atomic")

    # A CSV row may span physical lines; csv.reader.line_num preserves the
    # global locator. Every data row depends on the original complete header.
    try:
        dialect = csv.Sniffer().sniff(source["body"][:8192], delimiters=",;\t")
        reader = csv.reader(io.StringIO(source["body"]), dialect=dialect, strict=True)
        rows, previous = [], 0
        for row in reader:
            rows.append((previous + 1, reader.line_num, row))
            previous = reader.line_num
        if (len(rows) >= 2 and len(rows[0][2]) >= 2 and all(len(row[2]) == len(rows[0][2]) for row in rows)
                and any(re.search(r"[A-Za-z\u3400-\u9fff]", value) for value in rows[0][2])):
            header_count = 1
            if len(rows) > 2 and any(re.search(r"单位|\b(?:unit|bpm|kg|km|ms|hours?)\b", value, re.I)
                                     for value in rows[1][2]):
                header_count = 2
            header = fragment(1, rows[header_count - 1][1], "table_context")
            fragments = [header] + [fragment(start, end, "table_row", [header["fragment_id"]])
                                    for start, end, values in rows[header_count:]]
            return fragments, [fragment["fragment_id"] for fragment in fragments[1:]]
    except (csv.Error, ValueError):
        pass

    # Markdown table: retain the entire preamble (including units/conditions),
    # header and separator on every row. Mixed trailing prose remains atomic.
    for index in range(len(lines) - 1):
        cells = [cell.strip() for cell in lines[index + 1].strip().strip("|").split("|")]
        if "|" not in lines[index] or len(cells) < 2 or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        rows = list(range(index + 2, len(lines)))
        if rows and all("|" in lines[row] and lines[row].strip() for row in rows):
            context = fragment(1, index + 2, "table_context")
            fragments = [context] + [fragment(row + 1, row + 1, "table_row", [context["fragment_id"]]) for row in rows]
            return fragments, [fragment["fragment_id"] for fragment in fragments[1:]]
        return atomic("table_with_context_atomic")

    headings = [(index, len(match[1])) for index, line in enumerate(lines)
                if (match := re.match(r"^(#{1,6})\s+\S", line))]
    if headings:
        level = min(depth for index, depth in headings)
        starts = [index for index, depth in headings if depth == level]
        # References across sections are unresolved dependencies. Keeping the
        # whole source is safer than guessing which earlier section suffices.
        if len(starts) > 1:
            fragments, parents = [], []
            if starts[0]:
                preamble = fragment(1, starts[0], "section_context")
                fragments.append(preamble)
                parents.append(preamble["fragment_id"])
            primary = []
            for index, start in enumerate(starts):
                end = starts[index + 1] if index + 1 < len(starts) else len(lines)
                item = fragment(start + 1, end, "markdown_section", parents)
                fragments.append(item)
                primary.append(item["fragment_id"])
            return fragments, primary
    return atomic("prose_atomic")


def _ranges(numbers):
    result = []
    for number in sorted(set(numbers)):
        if result and number == result[-1][1] + 1:
            result[-1][1] = number
        else:
            result.append([number, number])
    return [f"L{start}-L{end}" for start, end in result]


def _views(sources, fragments, *, line_cache=None):
    by_path = {source["path"]: source for source in sources}
    result = []
    for path in sorted({fragment["path"] for fragment in fragments}):
        source = by_path[path]
        result.append({"path": path, "metadata": deepcopy(source["metadata"]), "fragments": [
            {"fragment_id": fragment["fragment_id"], "locator": fragment["locator"],
             "lines": _lines(source, *_bounds(fragment["locator"]),
                             source_lines=line_cache[path] if line_cache is not None else None)}
            for fragment in sorted((item for item in fragments if item["path"] == path), key=lambda item: _bounds(item["locator"]))]})
    return result


def _context(plan_id, batch_id, fragments, parent_batch_ids=()):
    return {"plan_id": plan_id, "batch_id": batch_id, "source_ranges": [
        {key: fragment[key] for key in ("path", "source_id", "source_version", "locator", "fragment_id")}
        for fragment in sorted(fragments, key=lambda item: (item["path"], _bounds(item["locator"])))],
        "parent_batch_ids": list(parent_batch_ids)}


def _build(sources, snapshot, max_input_bytes, payload_overhead_bytes, max_sources, purpose):
    fragments, units = [], []
    for source in sources:
        parts, primary = _units(source)
        fragments.extend(parts)
        units.extend(primary)
    indexed = {fragment["fragment_id"]: fragment for fragment in fragments}
    positions = {fragment["fragment_id"]: index for index, fragment in enumerate(fragments)}
    line_cache = {source["path"]: source["body"].splitlines() for source in sources}
    def closure(ids):
        selected = set(ids)
        for identifier in list(selected):
            selected.update(indexed[identifier]["parent_fragment_ids"])
        return [indexed[identifier] for identifier in sorted(selected, key=positions.__getitem__)]
    # Stable IDs include the immutable snapshot and all semantic boundaries;
    # byte counts are derived after IDs and never alter original identities.
    identity = {"snapshot": snapshot, "sources": [_source_binding(source) for source in sources],
                "fragments": fragments, "max_input_bytes": max_input_bytes,
                "payload_overhead_bytes": payload_overhead_bytes, "max_sources": max_sources, "purpose": purpose}
    plan_id = "plan-" + _hash(identity)[7:]
    def batch(ids):
        selected = closure(ids)
        identifier = "batch-" + _hash({"plan_id": plan_id, "fragment_ids": [item["fragment_id"] for item in selected]})[7:]
        context = _context(plan_id, identifier, selected)
        size = _bytes({"sources": _views(sources, selected, line_cache=line_cache), "batch_context": context}) + payload_overhead_bytes
        return {"batch_id": identifier, "fragment_ids": [item["fragment_id"] for item in selected],
                "parent_batch_ids": [], "input_bytes": size}
    batches, blocked, pending = [], [], []
    for source in sources:
        if not source["body"].splitlines():
            blocked.append({"path": source["path"], "locator": None, "reason": "source_has_no_text"})
    for identifier in units:
        combined = batch([*pending, identifier])
        paths = {indexed[item]["path"] for item in combined["fragment_ids"]}
        if pending and (combined["input_bytes"] > max_input_bytes or len(paths) > max_sources):
            batches.append(batch(pending))
            pending = []
        single = batch([identifier])
        if single["input_bytes"] > max_input_bytes:
            fragment = indexed[identifier]
            blocked.append({"path": fragment["path"], "locator": fragment["locator"],
                            "reason": "semantic_unit_exceeds_budget"})
        else:
            pending.append(identifier)
    if pending:
        batches.append(batch(pending))
    covered = {source["path"]: set() for source in sources}
    for item in batches:
        for identifier in item["fragment_ids"]:
            fragment = indexed[identifier]
            start, end = _bounds(fragment["locator"])
            covered[fragment["path"]].update(range(start, end + 1))
    coverage = {"complete": not blocked, "covered_ranges": {path: _ranges(values) for path, values in covered.items()},
                "uncovered_ranges": {source["path"]: _ranges(set(range(1, len(source["body"].splitlines()) + 1)) - covered[source["path"]])
                                     for source in sources},
                "source_count": len(sources), "batch_count": len(batches),
                "unique_evidence_lines": sum(len(values) for values in covered.values())}
    return {"schema_version": SCHEMA, "kind": "batch_plan", "plan_id": plan_id, "snapshot": snapshot,
            "source_paths": [source["path"] for source in sources],
            "sources": [{**_source_binding(source), "line_count": len(source["body"].splitlines())} for source in sources],
            "purpose": purpose, "max_input_bytes": max_input_bytes, "payload_overhead_bytes": payload_overhead_bytes,
            "max_sources": max_sources, "fragments": fragments, "batches": batches, "coverage": coverage,
            "blocked": blocked, "status": "blocked" if blocked else "ready"}


def plan_batches(repo, source_paths, *, max_input_bytes, payload_overhead_bytes=0,
                 max_sources=MAX_BATCH_SOURCES, expected_head=None, purpose="current", rebuild_context=None):
    paths = _paths(source_paths)
    if (type(max_input_bytes) is not int or not 1 <= max_input_bytes <= MAX_BYTES
            or type(payload_overhead_bytes) is not int or not 0 <= payload_overhead_bytes < max_input_bytes
            or type(max_sources) is not int or not 1 <= max_sources <= MAX_BATCH_SOURCES
            or purpose not in {"current", "history"}):
        _fail("invalid_batch_budget")
    git = PipelineGit(repo)
    with git.lock():
        head = git.check(expected_head)
        controls = control_guard(git)
        versions = execution_versions(git)
        sources = [load_source(repo, path, purpose=purpose) for path in paths]
        snapshot = {"head": head, "versions": versions, "controls_digest": controls["digest"]}
        if rebuild_context is not None:
            from .rebuild import validate_rebuild_context
            validate_rebuild_context(git, rebuild_context, paths, at_commit=head)
            snapshot["rebuild_context"] = dict(rebuild_context)
        result = _build(sources, snapshot,
                        max_input_bytes, payload_overhead_bytes, max_sources, purpose)
        git.check(head)
        if control_guard(git)["digest"] != controls["digest"] or execution_versions(git) != versions:
            _fail("batch_snapshot_changed")
        return result


def validate_plan(repo, plan, *, source_paths=None, require_current_versions=True, historical=False, _locked=False):
    try:
        paths = _paths(plan["source_paths"])
        if source_paths is not None and not set(paths) <= set(_paths(source_paths)):
            _fail("batch_scope_mismatch")
        git = PipelineGit(repo)
        with nullcontext() if _locked else git.lock():
            head = git.check()
            git.run("merge-base", "--is-ancestor", plan["snapshot"]["head"], head)
            if "rebuild_context" in plan["snapshot"]:
                from .rebuild import validate_rebuild_context
                validate_rebuild_context(git, plan["snapshot"]["rebuild_context"], paths,
                                         at_commit=plan["snapshot"]["head"])
            controls = control_guard(git, plan["snapshot"]["head"]) if historical else control_guard(git)
            versions = execution_versions(git)
            if (controls["digest"] != plan["snapshot"]["controls_digest"]
                    or require_current_versions and versions != plan["snapshot"]["versions"]):
                _fail("batch_snapshot_changed")
            sources = [load_source(repo, path, purpose="history" if historical else plan["purpose"]) for path in paths]
            expected = _build(sources, plan["snapshot"], plan["max_input_bytes"], plan["payload_overhead_bytes"],
                              plan["max_sources"], plan["purpose"])
            if expected != plan:
                _fail("batch_plan_mismatch")
            git.check(head)
            return sources
    except CaptureError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError):
        _fail()


def validate_source_views(sources, source_views, batch_context):
    """Check a candidate's saved view against full immutable captured sources."""
    if (not isinstance(batch_context, dict) or set(batch_context) != {"plan_id", "batch_id", "source_ranges", "parent_batch_ids"}
            or not isinstance(source_views, list) or not 1 <= len(source_views) <= MAX_PLAN_SOURCES):
        _fail("invalid_batch_context")
    for key, prefix in (("plan_id", "plan-"), ("batch_id", "batch-")):
        if not isinstance(batch_context[key], str) or not batch_context[key].startswith(prefix) or _ID.fullmatch(batch_context[key]) is None:
            _fail("invalid_batch_context")
    parents = batch_context["parent_batch_ids"]
    if (not isinstance(parents, list) or len(set(parents)) != len(parents)
            or any(not isinstance(item, str) or not re.fullmatch(r"batch-[0-9a-f]{64}", item) for item in parents)
            or batch_context["batch_id"] in parents):
        _fail("invalid_batch_context")
    if not parents and len(source_views) > MAX_BATCH_SOURCES:
        _fail("invalid_batch_context")
    by_path = {source["path"]: source for source in sources}
    line_cache = {source["path"]: source["body"].splitlines() for source in sources}
    ranges, seen = [], set()
    for view in source_views:
        if not isinstance(view, dict) or set(view) != {"path", "metadata", "fragments"}:
            _fail("invalid_source_view")
        source = by_path.get(view["path"])
        if source is None or view["path"] in seen or view["metadata"] != source["metadata"]:
            _fail("batch_scope_mismatch")
        seen.add(view["path"])
        if not isinstance(view["fragments"], list) or not view["fragments"]:
            _fail("invalid_source_view")
        for fragment in view["fragments"]:
            if not isinstance(fragment, dict) or set(fragment) != {"fragment_id", "locator", "lines"}:
                _fail("invalid_source_view")
            if (not isinstance(fragment["lines"], list) or any(not isinstance(line, dict)
                    or set(line) != {"line", "text"} or type(line["line"]) is not int
                    or not isinstance(line["text"], str) for line in fragment["lines"])):
                _fail("invalid_source_view")
            start, end = _bounds(fragment["locator"])
            expected = _fragment(source, start, end, "unused", source_lines=line_cache[source["path"]])
            if fragment["fragment_id"] != expected["fragment_id"] or fragment["lines"] != _lines(source, start, end, source_lines=line_cache[source["path"]]):
                _fail("fragment_bytes_mismatch")
            ranges.append({key: expected[key] for key in ("path", "source_id", "source_version", "locator", "fragment_id")})
    ranges.sort(key=lambda item: (item["path"], _bounds(item["locator"])))
    if ranges != batch_context["source_ranges"] or len({item["fragment_id"] for item in ranges}) != len(ranges):
        _fail("invalid_batch_context")
    return True


def assert_fragment_refs(refs, source_views):
    """A valid full-source quote cannot cite lines absent from this batch view."""
    if not isinstance(refs, list):
        _fail("invalid_fragment_reference")
    provided = {}
    for source in source_views:
        key = (source["metadata"]["source_id"], source["metadata"]["source_version"])
        provided[key] = {line["line"] for fragment in source["fragments"] for line in fragment["lines"]}
    for ref in refs:
        try:
            start, end = _bounds(ref["locator"])
            lines = provided.get((ref["source_id"], ref["source_version"]), set())
            if end - start + 1 > len(lines) or not all(line in lines for line in range(start, end + 1)):
                _fail("fragment_reference_outside_view")
        except (KeyError, TypeError):
            _fail("invalid_fragment_reference")
    return True


def _projection_controls(repo, plan, fragments):
    snapshot = control_guard(PipelineGit(repo))
    if snapshot["digest"] != plan["snapshot"]["controls_digest"]:
        _fail("batch_snapshot_changed")
    policies = {fragment["path"]: source_policy(snapshot, fragment["path"], purpose=plan["purpose"])
                for fragment in fragments}
    excluded = [fragment["fragment_id"] for fragment in fragments
                if source_locator_policy(snapshot, fragment["path"], fragment["locator"],
                                         purpose=plan["purpose"])["excluded"]]
    return excluded, policies


def project_batch_sources(repo, plan, batch_id, *, source_paths, max_bytes=None, fragment_ids=None):
    """Read selected batch fragments with original global lines and coverage.

    A narrowed/limited projection is a reading result, never evidence that the
    entire processing batch completed. The coordinator must require complete.
    """
    scope = _paths(source_paths)
    # Scope must be checked before validate_plan opens any source body.
    if not set(plan.get("source_paths", [])) <= set(scope):
        _fail("batch_scope_mismatch")
    sources = validate_plan(repo, plan, source_paths=scope)
    batches = {batch["batch_id"]: batch for batch in plan["batches"]}
    if batch_id not in batches:
        _fail("unknown_batch")
    batch = batches[batch_id]
    selected_ids = batch["fragment_ids"] if fragment_ids is None else fragment_ids
    if (not isinstance(selected_ids, list) or len(set(selected_ids)) != len(selected_ids)
            or not set(selected_ids) <= set(batch["fragment_ids"])):
        _fail("invalid_fragment_selection")
    if max_bytes is not None and (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES):
        _fail("invalid_batch_budget")
    fragments = {fragment["fragment_id"]: fragment for fragment in plan["fragments"]}
    selected = [fragment for fragment in plan["fragments"] if fragment["fragment_id"] in selected_ids]
    excluded, policies = _projection_controls(repo, plan, selected)
    selected = [fragment for fragment in selected if fragment["fragment_id"] not in excluded]
    selected_ids = [fragment["fragment_id"] for fragment in selected]
    dependencies = sorted({parent for fragment in selected for parent in fragment["parent_fragment_ids"] if parent not in selected_ids})
    if dependencies:
        # No source text is returned without its prerequisite/preamble.
        selected = []
    views = _views(sources, selected)
    context = _context(plan["plan_id"], batch_id, selected, batch["parent_batch_ids"])
    size = _bytes({"sources": views, "batch_context": context}) + plan["payload_overhead_bytes"]
    exhausted = max_bytes is not None and size > max_bytes
    if exhausted:
        views, selected = [], []
        context = _context(plan["plan_id"], batch_id, [], batch["parent_batch_ids"])
        size = _bytes({"sources": views, "batch_context": context}) + plan["payload_overhead_bytes"]
    included = {fragment["fragment_id"] for fragment in selected}
    omitted = sorted(set(batch["fragment_ids"]) - included)
    return {"source_views": views, "batch_context": context, "source_policies": policies,
            "input_bytes": size, "coverage": {"complete": not omitted and not dependencies and not exhausted,
                "requested_source_paths": scope, "read_source_paths": list(plan["source_paths"]),
                "included_source_paths": [view["path"] for view in views],
                "omitted_fragment_ids": omitted, "missing_dependency_fragment_ids": dependencies,
                "controlled_fragment_ids": excluded,
                "budget_exhausted": exhausted, "exceeds_legacy_source_limit": len(scope) > MAX_BATCH_SOURCES,
                "global_locators": True, "overlap_is_independent_evidence": False},
            "limitations": ["仅提供明确选择的固定来源片段；编号始终为原文全局行号，不宣称读取全库。",
                            "程序在本地核验了完整plan明示来源及回执；返回投影只提供列出的片段，不声称仅读取了这些原文字节。",
                            "重复表头或前提仍为同一来源与证据，不增加独立佐证。"]
                + (["本次范围或预算未覆盖完整批次及其依赖，不能据此声明整批加工完成。"] if omitted else [])}


def project_batch_with_context(repo, plan, batch_id, *, source_paths, required_source_paths, max_bytes=None):
    """Project one original batch plus complete, explicitly required sources.

    Required sources are immutable members of this same plan. Their original
    fragments and dependency closure are added to every selected batch, rather
    than guessed from a reply title or silently cut to the original eight-source
    limit. The original batch is the only asserted parent: an added reply need
    not cover the other sources in the batch where it was originally packed.
    This remains a read-only input projection, never a publication proof.
    """
    if not isinstance(required_source_paths, (list, tuple)):
        _fail("invalid_batch_scope")
    if not required_source_paths:
        return project_batch_sources(repo, plan, batch_id, source_paths=source_paths, max_bytes=max_bytes)
    scope, required = _paths(source_paths), _paths(required_source_paths)
    # Validate the entire explicit authorization boundary before opening any
    # original body, including bodies needed solely to verify the fixed plan.
    if not isinstance(plan, dict):
        _fail("batch_scope_mismatch")
    plan_paths = _paths(plan.get("source_paths"))
    if not set(plan_paths) <= set(scope):
        _fail("batch_scope_mismatch")
    if not set(required) <= set(plan_paths):
        _fail("batch_required_source_mismatch")
    if max_bytes is not None and (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES):
        _fail("invalid_batch_budget")
    git = PipelineGit(repo)
    with git.lock():
        head = git.check()
        sources = validate_plan(repo, plan, source_paths=scope, _locked=True)
        batches = {batch["batch_id"]: batch for batch in plan["batches"]}
        if batch_id not in batches:
            _fail("unknown_batch")
        indexed = {fragment["fragment_id"]: fragment for fragment in plan["fragments"]}
        if not set(required) <= {fragment["path"] for fragment in plan["fragments"]}:
            _fail("batch_required_source_has_no_text")
        identifiers = set(batches[batch_id]["fragment_ids"])
        identifiers.update(fragment["fragment_id"] for fragment in plan["fragments"]
                           if fragment["path"] in required)
        # Take the transitive prerequisite closure using only the immutable
        # plan; no new line slices or semantic boundaries are invented.
        pending = list(identifiers)
        while pending:
            fragment = indexed[pending.pop()]
            for parent in fragment["parent_fragment_ids"]:
                if parent not in identifiers:
                    identifiers.add(parent)
                    pending.append(parent)
        selected = [fragment for fragment in plan["fragments"] if fragment["fragment_id"] in identifiers]
        excluded, policies = _projection_controls(repo, plan, selected)
        parents = [batch_id]
        identifier = "batch-" + _hash({"plan_id": plan["plan_id"], "parent_batch_ids": parents,
            "required_source_paths": required, "fragments": [{"fragment_id": fragment["fragment_id"],
                "parent_fragment_ids": fragment["parent_fragment_ids"]} for fragment in selected]})[7:]
        context = _context(plan["plan_id"], identifier, selected, parents)
        views = _views(sources, selected)
        size = _bytes({"sources": views, "batch_context": context}) + plan["payload_overhead_bytes"]
        limit = plan["max_input_bytes"] if max_bytes is None else max_bytes
        exhausted = size > limit
        reasons = (["controlled_fragment"] if excluded else []) + (["batch_input_limit"] if exhausted else [])
        if len(views) > MAX_PLAN_SOURCES:
            reasons.append("batch_source_limit")
        included = [] if reasons else views
        git.check(head)
        if (control_guard(git)["digest"] != plan["snapshot"]["controls_digest"]
                or execution_versions(git) != plan["snapshot"]["versions"]):
            _fail("batch_snapshot_changed")
        return {"status": "blocked_capacity" if exhausted else "blocked_control" if reasons else "ready",
            "source_views": included, "batch_context": context, "source_policies": policies,
            "input_bytes": size, "coverage": {"complete": not reasons,
                "requested_source_paths": scope, "required_source_paths": required,
                "read_source_paths": list(plan["source_paths"]),
                "included_source_paths": [view["path"] for view in included],
                "fragment_ids": [fragment["fragment_id"] for fragment in selected],
                "omitted_fragment_ids": sorted(identifiers) if reasons else [],
                "parent_batch_ids": parents, "missing_dependency_fragment_ids": [],
                "controlled_fragment_ids": excluded, "budget_exhausted": exhausted,
                "blocked_reasons": reasons, "exceeds_legacy_source_limit": len(views) > MAX_BATCH_SOURCES,
                "global_locators": True, "overlap_is_independent_evidence": False},
            "limitations": ["只读投影包含原批次与明确要求的固定来源全部片段及依赖；未创建、切片或发布新知识。",
                            "程序在本地核验完整plan明示来源及回执；模型输入只提供列出的原文全局片段。",
                            "附加来源与重复前提仍保留原来源版本和片段身份，不增加独立佐证。"]
                + (["完整必要上下文受控制或超过容量，未返回部分模型输入。"] if reasons else [])}


def merge_batch_sources(repo, plan, batch_ids, *, source_paths, max_bytes):
    """Project a union for an additional real merge extract/Review invocation.

    Parent IDs bind the already planned batches. No new semantic fragments are
    invented. Over-budget or over-eight-source unions return blocked, never a
    shortened input that could be mistaken for a complete merged candidate.
    """
    if (not isinstance(batch_ids, list) or not batch_ids or len(set(batch_ids)) != len(batch_ids)
            or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BYTES):
        _fail("invalid_batch_selection")
    scope = _paths(source_paths)
    if not set(plan.get("source_paths", [])) <= set(scope):
        _fail("batch_scope_mismatch")
    sources = validate_plan(repo, plan, source_paths=scope)
    indexed = {batch["batch_id"]: batch for batch in plan["batches"]}
    if not set(batch_ids) <= set(indexed):
        _fail("unknown_batch")
    parents = sorted(batch_ids)
    identifiers = {identifier for parent in parents for identifier in indexed[parent]["fragment_ids"]}
    fragments = [fragment for fragment in plan["fragments"] if fragment["fragment_id"] in identifiers]
    excluded, policies = _projection_controls(repo, plan, fragments)
    identifier = "batch-" + _hash({"plan_id": plan["plan_id"], "parent_batch_ids": parents,
                                  "fragment_ids": [fragment["fragment_id"] for fragment in fragments]})[7:]
    context = _context(plan["plan_id"], identifier, fragments, parents)
    views = _views(sources, fragments)
    size = _bytes({"sources": views, "batch_context": context}) + plan["payload_overhead_bytes"]
    reasons = []
    if excluded:
        reasons.append("controlled_fragment")
    if len(views) > MAX_PLAN_SOURCES:
        reasons.append("merge_source_limit")
    if size > max_bytes:
        reasons.append("merge_input_limit")
    return {"status": "blocked" if reasons else "ready", "source_views": [] if reasons else views,
            "source_policies": policies,
            "batch_context": context, "input_bytes": size,
            "coverage": {"complete": not reasons, "parent_batch_ids": parents,
                         "read_source_paths": list(plan["source_paths"]),
                         "included_source_paths": [] if reasons else [view["path"] for view in views],
                         "fragment_ids": [fragment["fragment_id"] for fragment in fragments],
                         "blocked_reasons": reasons, "global_locators": True,
                         "controlled_fragment_ids": excluded,
                         "overlap_is_independent_evidence": False},
            "limitations": ["合并输入仅为同一固定plan的明示片段并集，须另行提取与独立Review，不能沿用子批Review宣称新综合已受审。"]}
