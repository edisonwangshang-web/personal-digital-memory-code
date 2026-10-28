"""Keep applicable, already-reviewed statistical obligations on exact reuse.

Callers select the versioned contract. Legacy validation never repairs model
output. The explicit inheritance helper returns a separate materialized copy;
it never infers statistics from prose or opens an authority outside the selection.
"""

from copy import deepcopy
from contextvars import ContextVar
import hashlib
import re

from .contracts import CaptureError, canonical, markdown
from .knowledge_quality import resolve_statistic


_STAGE = re.compile(r"memory/staging/(run-[0-9a-f]{32})\.md\Z")
_REVIEW = re.compile(r"memory/runs/processes/run-[0-9a-f]{32}/review\.md\Z")
_RECORD = re.compile(r"memory/records/[a-z][a-z0-9-]{0,95}\.md\Z")
_VERIFICATION = ContextVar("reuse_quality_verification", default=None)


def _fail(code="invalid_reuse_quality_authority", *, details=None):
    raise CaptureError(code, "复用统计义务未通过固定证明与显式声明核验", details=details)


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


def _collect(git, related, sources, context, reused_objects, cache, *, include_claims=False):
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
    obligations, inherited = {}, {}
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
        for claim_index, claim in enumerate(candidate["extraction_output"]["quality"]["statistic_claims"]):
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
            if include_claims:
                origin = {"stage_path": entry["path"], "stage_commit": entry["commit_sha"],
                          "stage_sha256": entry["sha256"], "claim_index": claim_index}
                value = {"claim": deepcopy(claim), "origin": origin,
                         "claim_sha256": _sha(canonical(claim))}
                # Several verified generations may prove the same unchanged
                # claim. Select one deterministically, never merge their text.
                if key not in inherited or canonical(value) < canonical(inherited[key]):
                    inherited[key] = value
    values = [obligations[key] for key in sorted(obligations)]
    return (values, inherited) if include_claims else values


def _verified_collection(git, related, sources, context, reused_objects, *, include_claims=False):
    cache = _VERIFICATION.get()
    token = None
    if cache is None:
        cache = {"publications": {}, "active": set()}
        token = _VERIFICATION.set(cache)
    try:
        return _collect(git, related, sources, context, reused_objects, cache, include_claims=include_claims)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _fail()
    finally:
        if token is not None:
            _VERIFICATION.reset(token)


def collect_reuse_statistical_obligations(git, related, sources, context, reused_objects):
    """Return still-applicable old obligations, without copying or inventing claims.

    Every related publication and its full proof DAG is verified once within a
    nested call context. Snapshots are then read from that verified DAG, rather
    than recursively re-verifying each candidate a second time. No cache survives
    the outer call, and no files or model outputs are written.
    """
    return _verified_collection(git, related, sources, context, reused_objects)


INHERITANCE_CONTRACT = "exact-statistic-inheritance-v1"


def materialize_reuse_statistic_claims(git, related, sources, context, reused_objects, output, *,
                                     controls_digest=None, at_commit=None):
    """Materialize omitted, proven claims for explicitly reused exact records.

    Only the new opt-in caller contract may use this helper. The caller retains
    the actual response separately and later reconstructs this materialization
    and its provenance at the candidate's fixed base commit during verification.
    Current deletion guards remain in the complete publication verifier; a past
    materialization does not grant permission to use an old fact as current.
    Existing supplied claims
    are never corrected or overwritten; normal complete quality/Review checks
    still validate the resulting output.
    """
    from .controls import object_policy, read_controls, source_policy
    from .pipeline import _committed

    head = git.check()
    controls = read_controls(git.repo, head=head, at_commit=at_commit)
    snapshot_head = at_commit or head
    if controls_digest is not None and controls_digest != controls["digest"]:
        _fail("reuse_quality_controls_changed")
    try:
        claims = output["quality"]["statistic_claims"]
        if not isinstance(claims, list):
            _fail("invalid_reuse_quality_output")
        declared = set()
        for claim in claims:
            if not isinstance(claim, dict) or any(not isinstance(claim.get(key), str)
                                                   for key in ("record_id", "stat_id")):
                _fail("invalid_reuse_quality_output")
            declared.add((claim["record_id"], claim["stat_id"]))
    except (KeyError, TypeError):
        _fail("invalid_reuse_quality_output")

    obligations, originals = _verified_collection(
        git, related, sources, context, reused_objects, include_claims=True)
    by_source = {}
    for source in sources:
        identity = source.get("metadata", {})
        key = tuple(source.get(field, identity.get(field)) for field in ("source_id", "source_version"))
        if key in by_source and by_source[key] != source["path"]:
            _fail("reuse_quality_source_scope")
        by_source[key] = source["path"]
    eligible = set()
    for obj in reused_objects:
        path = obj["path"]
        if _RECORD.fullmatch(path) is None:
            continue
        metadata = obj["metadata"]
        policy = object_policy(controls, path, content_version=metadata["content_version"])
        if policy["excluded"] or policy["control_ids"]:
            continue
        # Initial materialization checks the live formal bytes. Historical proof
        # reconstruction checks the exact original input snapshot instead.
        formal = _committed(git, path) if at_commit is None else git.read(snapshot_head, path)
        if formal != markdown(metadata, obj["body"]):
            continue
        for reference in metadata["sources"]:
            source_path = by_source.get((reference["source_id"], reference["source_version"]))
            if source_path is None:
                _fail("reuse_quality_source_scope")
            policy = source_policy(controls, source_path)
            if policy["excluded"] or policy["control_ids"]:
                break
        else:
            eligible.add((path, metadata["content_version"]))

    materialized = deepcopy(output)
    provenance = []
    for item in obligations:
        pair = (item["record_id"], item["stat_id"])
        if pair in declared or (item["record_path"], item["content_version"]) not in eligible:
            continue
        key = (item["record_path"], item["content_version"], item["stat_id"])
        original = originals[key]
        materialized["quality"]["statistic_claims"].append(deepcopy(original["claim"]))
        provenance.append({**deepcopy(item), "origin": deepcopy(original["origin"]),
                           "claim_sha256": original["claim_sha256"]})
        declared.add(pair)
    git.check(head)
    return materialized, {"contract": INHERITANCE_CONTRACT, "controls_digest": controls["digest"],
                          "claims": provenance}


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
    missing = sorted({(item["record_id"], item["stat_id"]) for item in obligations
                      if (item["record_id"], item["stat_id"]) not in declared})
    if missing:
        _fail("reuse_statistic_claims_missing", details={
            "field_path": "/quality/statistic_claims",
            "missing_claims": [{"record_id": record_id, "stat_id": stat_id}
                               for record_id, stat_id in missing]})
    return obligations
