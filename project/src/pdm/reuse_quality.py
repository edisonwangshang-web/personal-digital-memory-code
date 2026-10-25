"""Keep applicable, already-reviewed statistical obligations on exact reuse.

Callers select the versioned contract. This module never repairs model output,
infers statistics from prose, or opens an authority outside the selected sources.
"""

from contextvars import ContextVar
import hashlib
import re

from .contracts import CaptureError, canonical, markdown
from .knowledge_quality import resolve_statistic


_STAGE = re.compile(r"memory/staging/(run-[0-9a-f]{32})\.md\Z")
_REVIEW = re.compile(r"memory/runs/processes/run-[0-9a-f]{32}/review\.md\Z")
_RECORD = re.compile(r"memory/records/[a-z][a-z0-9-]{0,95}\.md\Z")
_VERIFICATION = ContextVar("reuse_quality_verification", default=None)


def _fail(code="invalid_reuse_quality_authority"):
    raise CaptureError(code, "复用统计义务未通过固定证明与显式声明核验")


def _sha(raw):
    return "sha256-" + hashlib.sha256(raw).hexdigest()


def _scope(bindings, selected):
    from .pipeline import normalize_source_paths
    if not isinstance(bindings, list) or not bindings:
        _fail()
    paths = normalize_source_paths([item["path"] for item in bindings], allow_versions=True, max_sources=256)
    for binding in bindings:
        source = selected.get(binding["path"])
        if source is None:
            _fail("reuse_quality_source_scope")
        if any(binding.get(key) != source[key] for key in ("source_id", "source_version")):
            _fail("reuse_quality_source_scope")
    return paths


def _snapshot(git, entry):
    from .pipeline import _committed
    raw = git.read(entry["commit_sha"], entry["path"])
    if _sha(raw) != entry["sha256"] or _committed(git, entry["path"]) != raw:
        _fail()
    return raw


def _publication(git, related, selected, cache):
    from .pipeline import _committed, _load, _parse_saved, _run_path, verify_publication
    path = _run_path(related["run_id"], "manifest")
    if related["manifest_path"] != path:
        _fail()
    raw = _committed(git, path)
    header, _ = _parse_saved(raw)
    kind = "batch_group_manifest" if header.get("kind") == "batch_group_manifest" else "manifest"
    manifest = _load(raw, kind, header.get("schema_version"))
    paths = _scope([manifest["source"], *manifest.get("additional_sources", [])], selected)
    key = (str(git.repo), git.head(), related["run_id"], tuple(paths))
    if key in cache["active"] or len(cache["active"]) >= 16:
        _fail("publication_dependency_cycle")
    if key not in cache["publications"]:
        cache["active"].add(key)
        try:
            # This verifies the complete publication/Review dependency DAG.
            cache["publications"][key] = verify_publication(
                git.repo, related["run_id"], source_paths=paths, historical=True)
        finally:
            cache["active"].remove(key)
    publication = cache["publications"][key]
    if publication["commit_sha"] != related["commit_sha"]:
        _fail()
    documents = related["documents"]
    if (not isinstance(documents, list) or len({item["path"] for item in documents}) != len(documents)
            or {item["path"] for item in documents} != set(publication["files"])):
        _fail()
    for document in documents:
        raw = git.read(publication["commit_sha"], document["path"])
        if (_sha(raw) != document["sha256"] or _sha(raw) != publication["files"][document["path"]]
                or raw != markdown(document["metadata"], document["body"])
                or document["content_version"] != document["metadata"].get("content_version")):
            _fail()
    snapshots = list(publication.get("authority_snapshots", []))
    if kind == "manifest":
        review_path = _run_path(related["run_id"], "review")
        review_raw = _committed(git, review_path)
        review = _load(review_raw, "review")
        snapshots.extend([
            {"path": review["stage_path"], "commit_sha": review["stage_commit"], "sha256": review["stage_sha256"]},
            {"path": review_path, "commit_sha": manifest["review_commit"], "sha256": _sha(review_raw)},
        ])
    return snapshots


def _collect(git, related, sources, context, reused_objects, cache):
    from .pipeline import _candidate_binding, _candidate_bindings, _load, _run_path
    selected = {}
    for source in sources:
        identity = source.get("metadata", {})
        selected[source["path"]] = {key: source.get(key, identity.get(key)) for key in ("source_id", "source_version")}
    known, snapshots, pending = {}, {}, []

    def include(entries):
        for entry in entries:
            key = entry["path"]
            if _STAGE.fullmatch(key) is None and _REVIEW.fullmatch(key) is None:
                continue
            if key in snapshots and snapshots[key] != entry:
                if _snapshot(git, snapshots[key]) != _snapshot(git, entry):
                    _fail()
            if key not in snapshots and _STAGE.fullmatch(key):
                pending.append(key)
            snapshots[key] = entry

    for item in related:
        include(_publication(git, item, selected, cache))
        for document in item["documents"]:
            if document["path"] in known:
                _fail()
            known[document["path"]] = document
    reused = {}
    for obj in reused_objects:
        old = known.get(obj["path"])
        if (old is None or obj["metadata"] != old["metadata"] or obj["body"] != old["body"]
                or obj["metadata"].get("content_version") != old["content_version"] or obj["path"] in reused):
            _fail()
        if _RECORD.fullmatch(obj["path"]):
            reused[obj["path"]] = obj
    current = {stat["stat_id"]: stat for stat in context["statistics"]}
    resolved = {}
    obligations = {}
    # A group DAG can omit snapshots for an older ordinary nonincremental
    # publication. Follow related authority only for these exact record versions.
    for path in pending:
        entry, match = snapshots[path], _STAGE.fullmatch(path)
        raw = _snapshot(git, entry)
        candidate = _load(raw, "candidate")
        _scope(_candidate_bindings(candidate), selected)
        if candidate.get("binding") != _candidate_binding(candidate) or candidate.get("run_id") != match.group(1):
            _fail()
        review_entry = snapshots.get(_run_path(candidate["run_id"], "review"))
        if review_entry is None:
            _fail()
        review = _load(_snapshot(git, review_entry), "review")
        if (review.get("binding") != candidate["binding"] or review.get("stage_path") != path
                or review.get("stage_commit") != entry["commit_sha"] or review.get("stage_sha256") != _sha(raw)):
            _fail()
        # Rejected feedback can appear in a verified revision's authority DAG.
        if review.get("output", {}).get("verdict") != "passed":
            continue
        old_records = {obj["metadata"]["id"]: obj for obj in candidate["objects"]
                       if obj["path"] in reused and obj["metadata"]["content_version"] == reused[obj["path"]]["metadata"]["content_version"]
                       and obj["body"] == reused[obj["path"]]["body"]}
        if not old_records:
            continue
        for ancestor in candidate.get("related", []):
            if any(doc["path"] in reused and doc["content_version"] == reused[doc["path"]]["metadata"]["content_version"]
                   for doc in ancestor["documents"]):
                include(_publication(git, ancestor, selected, cache))
        if candidate.get("quality_contract") != "skill-quality-v1":
            continue
        previous = {stat["stat_id"]: stat for stat in candidate["quality_context"]["statistics"]}
        for claim in candidate["extraction_output"]["quality"]["statistic_claims"]:
            old = old_records.get(claim["record_id"])
            sid = claim["stat_id"]
            if old is None or sid not in current:
                continue
            if sid not in resolved:
                resolved[sid] = canonical(resolve_statistic(context, current[sid]))
            if sid not in previous or canonical(resolve_statistic(candidate["quality_context"], previous[sid])) != resolved[sid]:
                continue
            key = (old["path"], old["metadata"]["content_version"], sid)
            obligations[key] = {"record_path": old["path"], "record_id": claim["record_id"],
                                "content_version": old["metadata"]["content_version"], "stat_id": sid}
    return [obligations[key] for key in sorted(obligations)]


def collect_reuse_statistical_obligations(git, related, sources, context, reused_objects):
    """Return still-applicable old obligations, without copying or inventing claims.

    Every related publication and its full proof DAG is verified once within a
    nested call context. Snapshots are then read from that verified DAG, rather
    than recursively re-verifying each candidate a second time. No cache survives
    the outer call, and no files or model outputs are written.
    """
    cache = _VERIFICATION.get()
    token = None
    if cache is None:
        cache = {"publications": {}, "active": set()}
        token = _VERIFICATION.set(cache)
    try:
        return _collect(git, related, sources, context, reused_objects, cache)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _fail()
    finally:
        if token is not None:
            _VERIFICATION.reset(token)


def model_reuse_requirements(obligations):
    """Expose compact required IDs; complete bodies and statistics stay in input."""
    grouped = {}
    for obligation in obligations:
        key = (obligation["record_id"], obligation["content_version"])
        grouped.setdefault(key, set()).add(obligation["stat_id"])
    return [{"record_id": record, "content_version": version, "stat_ids": sorted(grouped[(record, version)])}
            for record, version in sorted(grouped)]


def validate_reuse_quality(git, related, sources, context, reused_objects, output):
    """Require explicit claims for all applicable old obligations; never fill []."""
    obligations = collect_reuse_statistical_obligations(git, related, sources, context, reused_objects)
    try:
        claims = output["quality"]["statistic_claims"]
        if not isinstance(claims, list):
            _fail("invalid_reuse_quality_output")
        declared = {(claim["record_id"], claim["stat_id"]) for claim in claims}
    except (KeyError, TypeError, ValueError):
        _fail("invalid_reuse_quality_output")
    if any((item["record_id"], item["stat_id"]) not in declared for item in obligations):
        _fail("reuse_statistic_claims_missing")
    return obligations
