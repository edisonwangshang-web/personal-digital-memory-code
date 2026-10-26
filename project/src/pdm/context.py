"""Authorized context discovery and resumable answered-confirmation work.

Discovery reads manifest bindings and formal frontmatter, never source or formal
bodies. It proposes context, not semantic identity or approval: the ordinary
pipeline still verifies the publication, original evidence and independent Review.
"""

import copy
import hashlib
import re

from .contracts import CaptureError, canonical
from .controls import _metadata_only, object_policy, read_controls, source_policy
from .pending import read_registry
from .transaction import PipelineGit

_SOURCE = re.compile(r"memory/sources/(?:conversations|inbox)/src-[0-9a-f]{64}/sha256-[0-9a-f]{64}\.md\Z")
_RUN = re.compile(r"run-[0-9a-f]{32}\Z")
_ID = re.compile(r"[a-z][a-z0-9-]{0,95}\Z")
_VERSION = re.compile(r"sha256-[0-9a-f]{64}\Z")


def _fail(code="invalid_context_selection"):
    raise CaptureError(code, "关联上下文或答复续接校验失败；未扩大读取范围")


def _selection(values, pattern, *, empty=True):
    if (not isinstance(values, (list, tuple)) or len(values) > 1000
            or (not empty and not values)
            or any(not isinstance(value, str) or not pattern.fullmatch(value) for value in values)
            or len(set(values)) != len(values)):
        _fail()
    return sorted(values)


def _capacity(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        _fail()


def _scope(source_paths, allowed_source_paths, allowed_run_ids):
    requested = _selection(source_paths, _SOURCE, empty=False)
    allowed = _selection(allowed_source_paths, _SOURCE)
    runs = _selection(allowed_run_ids, _RUN)
    if not set(requested) <= set(allowed):
        _fail("context_source_not_authorized")
    return requested, allowed, runs


def discover_context(repo, source_paths, *, allowed_source_paths, allowed_run_ids,
                     object_ids=(), topic_ids=(), max_sources=8, max_runs=8):
    """Discover current, explicitly authorized publication context with no writes.

    With no identity hints, all compatible current runs in the declared scope
    are proposed to the semantic model. A common title/domain is never treated
    as proof that two statements concern the same fact. Explicit hints select
    exact IDs only. The result is metadata-only and requires pipeline verification.
    """
    from .evidence import _peek_publication

    _capacity(max_sources, 1000)
    _capacity(max_runs, 1000)
    paths, allowed, runs = _scope(source_paths, allowed_source_paths, allowed_run_ids)
    wanted_records = set(_selection(object_ids, _ID))
    wanted_topics = set(_selection(topic_ids, _ID))
    wanted = wanted_records | wanted_topics
    git = PipelineGit(repo)
    head = git.check()
    controls = read_controls(repo, head=head)
    gaps, skipped, candidates = [], [], []
    for path in paths:
        if source_policy(controls, path, purpose="history")["excluded"]:
            gaps.append({"reason": "deleted_input_source", "source_path": path})
    for run in runs:
        manifest = f"memory/runs/processes/{run}/manifest.md"
        if git._tree_blob(head, manifest) is None:
            skipped.append({"run_id": run, "reason": "not_published"})
            continue
        bound, status = _peek_publication(git, run, head)
        # Inspect source identities before any formal metadata or source body.
        missing = sorted(set(bound) - set(allowed))
        if missing:
            gaps.append({"run_id": run, "reason": "unauthorized_context_sources",
                         "missing_source_paths": missing})
            continue
        if not status["current_files_match"]:
            skipped.append({"run_id": run, "reason": "historical_publication"})
            continue
        if any(source_policy(controls, path, purpose="history")["excluded"] for path in bound):
            skipped.append({"run_id": run, "reason": "deleted_context_source"})
            continue
        # The manifest is already scope-checked. Read only its own object list.
        from .pipeline import _committed, _load, _parse_saved
        manifest_bytes = _committed(git, manifest)
        header, _ = _parse_saved(manifest_bytes)
        kind = header.get("kind")
        if kind not in {"manifest", "batch_group_manifest"}:
            _fail("context_publication_mismatch")
        publication = _load(manifest_bytes, kind, header["schema_version"])
        objects = []
        deleted = False
        for path in sorted(publication["files"]):
            policy = object_policy(controls, path, purpose="history")
            if policy["excluded"]:
                deleted = True
                break
            metadata = _metadata_only(git, path)
            identifier, version = metadata.get("id"), metadata.get("content_version")
            if (not isinstance(identifier, str) or not _ID.fullmatch(identifier)
                    or path.rsplit("/", 1)[1] != identifier + ".md"
                    or not isinstance(version, str) or not _VERSION.fullmatch(version)
                    or metadata.get("review_status") != "passed"):
                _fail("context_object_mismatch")
            objects.append({"path": path, "id": identifier, "content_version": version,
                            "kind": "topic" if path.startswith("memory/topics/") else "record",
                            "current_policy": object_policy(controls, path, content_version=version)})
        if deleted:
            skipped.append({"run_id": run, "reason": "deleted_context_object"})
            continue
        if wanted and not any(obj["id"] in (wanted_topics if obj["kind"] == "topic" else wanted_records)
                                  for obj in objects):
            continue
        candidates.append({"run_id": run, "source_paths": bound, "objects": objects})
    # Reused objects may be proved by several current manifests. Choose one
    # non-overlapping cover; do not feed duplicate object paths to the pipeline.
    selected, covered, offered = [], set(), set()
    for candidate in sorted(candidates, key=lambda value: (-len(value["objects"]), value["run_id"])):
        current = {obj["path"] for obj in candidate["objects"]}
        offered.update(obj["id"] for obj in candidate["objects"])
        if current <= covered:
            continue
        if current & covered:
            gaps.append({"run_id": candidate["run_id"], "reason": "overlapping_context_requires_selection",
                         "object_paths": sorted(current - covered)})
            continue
        selected.append(candidate)
        covered.update(current)
    for identifier in sorted(wanted - offered):
        gaps.append({"reason": "requested_object_not_available", "object_id": identifier})
    expanded = sorted(set(paths) | {path for item in selected for path in item["source_paths"]})
    if len(expanded) > max_sources:
        gaps.append({"reason": "context_source_capacity", "required": len(expanded), "capacity": max_sources})
    if len(selected) > max_runs:
        gaps.append({"reason": "context_run_capacity", "required": len(selected), "capacity": max_runs})
    git.check(head)
    return {"head": head, "source_paths": expanded,
            "related_runs": sorted(item["run_id"] for item in selected),
            "candidate_objects": [copy.deepcopy(obj) for item in selected for obj in item["objects"]],
            "controls_digest": controls["digest"], "skipped_runs": skipped,
            "coverage": {"complete": not gaps, "gaps": gaps}, "model_calls": 0,
            "selection_basis": "authorized_current_metadata_candidates",
            "publication_verification_required": True, "semantic_identity_verified": False}


def _pending_origin(git, item, run):
    """Bind the registry question to an actual fixed needs-confirmation Review."""
    try:
        return _pending_origin_data(git, item, run)
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, CaptureError):
            raise
        _fail("continuation_origin_mismatch")


def _pending_origin_data(git, item, run):
    from .assembly_final import extraction_invocations as _extraction_invocations
    from .pipeline import (_call_metadata, _candidate_binding, _candidate_bindings,
                           _committed, _hash, _last_commit, _load, _review_output, _read_reviewed)

    stage_path = f"memory/staging/{run}.md"
    review_path = f"memory/runs/processes/{run}/review.md"
    stage, review_bytes = _committed(git, stage_path), _committed(git, review_path)
    candidate, review = _load(stage, "candidate"), _load(review_bytes, "review")
    bindings = _candidate_bindings(candidate)
    refs = sorted(({key: binding[key] for key in ("source_id", "source_version", "path")}
                   for binding in bindings), key=lambda ref: ref["path"])
    objects = sorted(({"path": obj["path"], "id": obj["metadata"]["id"],
                       "content_version": obj["metadata"]["content_version"]}
                      for obj in candidate["objects"]), key=lambda ref: ref["path"])
    quality = candidate.get("quality_contract") == "skill-quality-v1"
    _review_output(review["output"], quality=quality)
    call = _call_metadata({**review["call"], "output": review["output"]})
    stage_commit = _last_commit(git, git.head(), stage_path)
    review_commit = _last_commit(git, git.head(), review_path)
    extended_review = (candidate.get("review_strategy") in {"auto", "decomposed-v1"}
                       or "revision_context" in candidate or quality)
    if extended_review:
        # The new proof chain legitimately writes independently checked units
        # between stage and aggregate Review. Its complete verifier must run
        # before accepting the non-passing original question and references.
        verified_candidate, verified_review, verified_commit = _read_reviewed(
            git, run, source_paths=sorted(ref["path"] for ref in refs), historical=True, allow_nonpassed=True)
        if verified_candidate != candidate or verified_review != review or verified_commit != review_commit:
            _fail("continuation_origin_mismatch")
    if (candidate.get("run_id") != run or candidate.get("binding") != _candidate_binding(candidate)
            or review.get("run_id") != run or review.get("binding") != candidate["binding"]
            or review.get("stage_path") != stage_path or review.get("stage_commit") != stage_commit
            or review.get("stage_sha256") != _hash(stage)
            or git.text("rev-parse", stage_commit + "^") != candidate.get("base_head")
            or not extended_review and git.text("rev-parse", review_commit + "^") != stage_commit
            or call != review["call"] or call["invocation_id"] in _extraction_invocations(candidate)
            or review["output"]["verdict"] != "needs_confirmation"
            or item["question"] != "\n".join(review["output"]["reasons"])
            or refs != item["source_refs"] or objects != item["object_refs"]):
        _fail("continuation_origin_mismatch")
    return candidate.get("related_runs", [])


def plan_answered_pending(repo, *, allowed_source_paths, allowed_run_ids, max_sources=8, max_runs=8):
    """Plan stable jobs for actual answers; open/resolved items never become jobs."""
    from .evidence import _capture_metadata
    from .pending import _check_item_controls, _deleted

    allowed = _selection(allowed_source_paths, _SOURCE)
    runs = _selection(allowed_run_ids, _RUN)
    _capacity(max_sources, 1000)
    _capacity(max_runs, 1000)
    git = PipelineGit(repo)
    head = git.check()
    registry = read_registry(repo, head=head)
    controls = read_controls(repo, head=head)
    jobs, gaps = [], []
    for item in registry["items"]:
        if item["pending_status"] != "answered" or _deleted(controls, item):
            continue
        required = sorted({ref["path"] for ref in [*item["source_refs"], *item["reply_sources"]]})
        # Unrelated private items disclose no question, identity or metadata.
        if not set(required) & set(allowed):
            continue
        missing_sources, missing_runs = sorted(set(required) - set(allowed)), sorted(set(item["run_ids"]) - set(runs))
        if missing_sources or missing_runs:
            gaps.append({"item_id": item["item_id"], "reason": "incomplete_continuation_scope",
                         "missing_source_paths": missing_sources, "missing_run_ids": missing_runs})
            continue
        _check_item_controls(repo, head, item)
        related = set()
        for run in item["run_ids"]:
            related.update(_pending_origin(git, item, run))
        missing_context = sorted(related - set(runs))
        if missing_context:
            gaps.append({"item_id": item["item_id"], "reason": "unauthorized_parent_context",
                         "missing_run_ids": missing_context})
            continue
        for ref in item["reply_sources"]:
            receipt = _capture_metadata(git, ref["path"], head)
            if receipt["commit_sha"] != ref["commit_sha"]:
                _fail("continuation_reply_mismatch")
        context = discover_context(repo, required, allowed_source_paths=required,
                                   allowed_run_ids=sorted(related), max_sources=max_sources, max_runs=max_runs)
        if not context["coverage"]["complete"]:
            gaps.append({"item_id": item["item_id"], "reason": "incomplete_continuation_context",
                         "context_gaps": context["coverage"]["gaps"]})
            continue
        identity = {"parent_item_id": item["item_id"], "question": item["question"],
                    "parent_run_ids": item["run_ids"], "source_refs": item["source_refs"],
                    "object_refs": item["object_refs"], "reply_sources": item["reply_sources"]}
        jobs.append({**copy.deepcopy(identity), "job_id": "continuation-" + hashlib.sha256(canonical(identity)).hexdigest(),
                     "head": head, "source_paths": context["source_paths"], "related_runs": context["related_runs"],
                     "controls_digest": controls["digest"], "status": "ready",
                     "authorization": {"allowed_source_paths": allowed, "allowed_run_ids": runs},
                     "capacity": {"max_sources": max_sources, "max_runs": max_runs}})
    git.check(head)
    return {"head": head, "jobs": sorted(jobs, key=lambda job: job["job_id"]),
            "coverage": {"complete": not gaps, "gaps": gaps}, "model_calls": 0}


def execute_continuation(repo, job, *, process_call, publish_call=None, resolve_call=None):
    """Run a planned answer through independent Review, publication and resolve.

    ``process_call(repo, job)`` supplies the normal budget/model adapter. A
    returned status or callback is never accepted as evidence of publication.
    The ordinary verifier and pending resolver inspect actual committed bytes.
    """
    from .pending import resolve_pending
    from .pipeline import publish, verify_publication

    if not isinstance(job, dict):
        _fail("invalid_continuation_job")
    try:
        plan = plan_answered_pending(repo, **job["authorization"], **job["capacity"])
        actual = next((value for value in plan["jobs"] if value["job_id"] == job["job_id"]), None)
        if actual != job:
            _fail("continuation_job_changed")
    except (KeyError, TypeError):
        _fail("invalid_continuation_job")
    processed = process_call(repo, copy.deepcopy(job))
    if not isinstance(processed, dict):
        _fail("invalid_continuation_result")
    if processed.get("status") not in {"passed", "already_processed"}:
        return {"job_id": job["job_id"], "parent_item_id": job["parent_item_id"],
                "status": processed.get("status", "failed"), "processing": processed, "resolved": False}
    run = processed.get("run_id")
    if not isinstance(run, str) or not _RUN.fullmatch(run) or run in job["parent_run_ids"]:
        _fail("continuation_publication_mismatch")
    if processed["status"] == "passed":
        (publish_call or publish)(repo, run, expected_head=processed.get("head"))
    publication = verify_publication(repo, run, source_paths=job["source_paths"])
    resolution = (resolve_call or resolve_pending)(repo, job["parent_item_id"], run)
    saved = next((item for item in read_registry(repo)["items"] if item["item_id"] == job["parent_item_id"]), None)
    expected = {"run_id": run, "commit_sha": publication["commit_sha"], "manifest_path": publication["manifest_path"]}
    if saved is None or saved["pending_status"] != "resolved" or saved["resolution"] != expected:
        _fail("continuation_not_resolved")
    return {"job_id": job["job_id"], "parent_item_id": job["parent_item_id"], "status": "resolved",
            "publication_run_id": run, "publication": publication, "resolution": resolution, "resolved": True}
