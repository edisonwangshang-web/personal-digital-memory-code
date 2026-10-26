"""Atomic publication of exact, independently reviewed final batch objects.

A group has its own manifest. It never invents ordinary per-run publication
manifests for extraction/Review runs that did not publish independently.
"""

from contextvars import ContextVar
import re

from .contracts import CaptureError, canonical, markdown, timestamp
from .transaction import PipelineGit

SCHEMA = "0.6"
_RUN = re.compile(r"run-[0-9a-f]{32}\Z")
_VERIFY_STACK = ContextVar("pdm_batch_group_verification_stack", default=())


def _fail(code="invalid_batch_group"):
    raise CaptureError(code, "依赖发布组未通过完整证据、审核或条件提交检查；未部分发布")


def _paths(source_paths):
    from .pipeline import normalize_source_paths
    return normalize_source_paths(source_paths, allow_versions=True, max_sources=256)


def _scope(plan, source_paths):
    try:
        paths = _paths(source_paths)
        expected = _paths(plan["source_paths"])
        if not set(expected) <= set(paths):
            _fail("batch_group_scope_mismatch")
        return paths
    except (KeyError, TypeError):
        _fail("batch_group_scope_mismatch")


def _marker_path(plan):
    if not isinstance(plan.get("plan_id"), str) or not re.fullmatch(r"plan-[0-9a-f]{64}", plan["plan_id"]):
        _fail()
    return f"memory/runs/processes/grouped/{plan['plan_id']}.md"


def _coverage(plan, candidates):
    """Names are hints; original fragment descriptors establish real coverage."""
    fragments = {item["fragment_id"]: {key: item[key] for key in
        ("path", "source_id", "source_version", "locator", "fragment_id")} for item in plan["fragments"]}
    batches = {item["batch_id"]: set(item["fragment_ids"]) for item in plan["batches"]}
    covered = set()
    for candidate in candidates:
        context = candidate.get("batch_context")
        if not isinstance(context, dict) or context.get("plan_id") != plan["plan_id"]:
            _fail("batch_group_plan_binding_mismatch")
        provided = set()
        for fragment in context.get("source_ranges", []):
            identifier = fragment.get("fragment_id") if isinstance(fragment, dict) else None
            if identifier not in fragments or fragment != fragments[identifier] or identifier in provided:
                _fail("batch_group_fragment_mismatch")
            provided.add(identifier)
        parents = context.get("parent_batch_ids", [])
        if not isinstance(parents, list) or not set(parents) <= set(batches):
            _fail("batch_group_plan_binding_mismatch")
        if any(not batches[parent] <= provided for parent in parents):
            _fail("batch_group_incomplete_parent_coverage")
        for batch, required in batches.items():
            if required <= provided:
                covered.add(batch)
    if covered != set(batches):
        _fail("batch_group_incomplete_coverage")


def _proofs(git, plan, reviewed_runs, allowed_paths, *, historical):
    from .pipeline import (_candidate_bindings, _committed, _hash, _last_commit,
                           _load, _publication_files, _read_reviewed)

    if (not isinstance(reviewed_runs, (list, tuple)) or not 1 <= len(reviewed_runs) <= 256
            or any(not isinstance(run, str) or not _RUN.fullmatch(run) for run in reviewed_runs)
            or len(set(reviewed_runs)) != len(reviewed_runs)):
        _fail("invalid_batch_group_proofs")
    plan_sources = [{key: value for key, value in source.items() if key != "line_count"} for source in plan["sources"]]
    expected_sources = {source["path"]: source for source in plan_sources}
    entries, candidates, files, objects, owners, operations, before, origins = [], [], {}, {}, {}, {}, {}, {}
    for run in sorted(reviewed_runs):
        # A named candidate may contain quotations, but no original source is
        # opened until every binding has passed the explicit scope check.
        stage_path = f"memory/staging/{run}.md"
        saved = _load(_committed(git, stage_path), "candidate")
        bindings = _candidate_bindings(saved)
        bound = sorted(binding["path"] for binding in bindings)
        if not set(bound) <= set(allowed_paths) or any(expected_sources.get(binding["path"]) != binding for binding in bindings):
            _fail("batch_group_scope_mismatch")
        candidate, review, review_commit = _read_reviewed(git, run, source_paths=bound, historical=historical)
        if candidate.get("batch_material", {}).get("draft_only"):
            _fail("batch_requires_final_review")
        if (candidate.get("versions") != plan["snapshot"]["versions"]
                or candidate.get("controls_digest") != plan["snapshot"]["controls_digest"]):
            _fail("batch_group_snapshot_mismatch")
        rebuild_context = candidate.get("rebuild_context")
        if rebuild_context != plan["snapshot"].get("rebuild_context"):
            _fail("batch_group_rebuild_context_mismatch")
        if candidates and rebuild_context != candidates[0].get("rebuild_context"):
            _fail("batch_group_rebuild_context_mismatch")
        if rebuild_context is not None:
            from .rebuild import validate_rebuild_context
            validate_rebuild_context(git, rebuild_context, plan["source_paths"],
                                     at_commit=candidate["base_head"])
        candidates.append(candidate)
        review_path = f"memory/runs/processes/{run}/review.md"
        entries.append({"run_id": run, "source_paths": bound, "binding": candidate["binding"],
            "stage_path": stage_path, "stage_commit": review["stage_commit"],
            "stage_sha256": _hash(_committed(git, stage_path)), "review_path": review_path,
            "review_commit": review_commit, "review_sha256": _hash(_committed(git, review_path))})
        formal = _publication_files(candidate, review)
        previous = {document["path"]: document for related in candidate.get("related", []) for document in related["documents"]}
        for obj in candidate["objects"]:
            path = obj["path"]
            operation = candidate.get("operations", {}).get(path)
            if operation is None:
                _fail("batch_group_missing_operation")
            old = previous.get(path)
            before_bytes = None if old is None else markdown(old["metadata"], old["body"])
            if path in objects:
                if (canonical(objects[path]) != canonical(obj) or files[path] != formal[path]
                        or operations[path] != operation or before[path] != before_bytes):
                    _fail("batch_group_object_conflict")
                continue
            objects[path], files[path], operations[path], before[path] = obj, formal[path], operation, before_bytes
            owners[path] = {"run_id": run, "review_path": review_path,
                            "content_version": obj["metadata"]["content_version"]}
            if operation["action"] == "reuse":
                metadata = old["metadata"]
                origins[path] = {"run_id": metadata["generated_by"]["run_id"], "review_path": metadata["review_ref"]}
            else:
                origins[path] = {"run_id": (obj["metadata"]["generated_by"]["run_id"]
                                             if "assembly" in candidate else run), "review_path": review_path}
    _coverage(plan, candidates)
    # Each Wiki's exact record dependencies must have been read together by at
    # least its own independent Review, not merely unioned after local Reviews.
    final_records = {(obj["metadata"]["id"], obj["metadata"]["content_version"]): path
                     for path, obj in objects.items() if path.startswith("memory/records/")}
    by_run = {candidate["run_id"]: candidate for candidate in candidates}
    for path, obj in objects.items():
        if not path.startswith("memory/topics/"):
            continue
        candidate = by_run[owners[path]["run_id"]]
        local = {(value["metadata"]["id"], value["metadata"]["content_version"])
                 for value in candidate["objects"] if value["path"].startswith("memory/records/")}
        for ref in obj["metadata"].get("records", []):
            key = (ref.get("id"), ref.get("content_version"))
            if key not in final_records or key not in local:
                _fail("batch_group_unreviewed_dependency")
    return {"proofs": entries, "candidates": candidates, "objects": objects, "files": files,
            "object_proofs": owners, "operations": operations, "before": before,
            "object_origins": origins, "sources": plan_sources}


def _manifest(git, group_id, plan, assembled, parent_head, published_at):
    from .pipeline import _hash
    sources = assembled["sources"]
    result = {"run_id": group_id, "mode": "batch_group", "plan_id": plan["plan_id"],
        "plan_sha256": _hash(plan), "plan": plan, "versions": plan["snapshot"]["versions"],
        "controls_digest": plan["snapshot"]["controls_digest"], "source": sources[0],
        "files": {path: _hash(data) for path, data in assembled["files"].items()},
        "proofs": assembled["proofs"], "object_proofs": assembled["object_proofs"],
        "object_origins": assembled["object_origins"], "operations": assembled["operations"],
        "review_path": assembled["proofs"][0]["review_path"],
        "review_commit": assembled["proofs"][0]["review_commit"],
        "parent_head": parent_head, "published_at": published_at}
    if len(sources) > 1:
        result["additional_sources"] = sources[1:]
    result["binding"] = _hash({key: value for key, value in result.items() if key != "published_at"})
    return result


def _authority(git, assembled, group_id, commit, manifest_path, marker_path, receipt_path):
    from .pipeline import _hash, _last_commit, verify_publication, _computation_snapshots
    entries = []

    def add(revision, path):
        entries.append({"commit_sha": revision, "path": path, "sha256": _hash(git.read(revision, path))})

    for source in assembled["sources"]:
        add(source["commit_sha"], source["path"])
        add(_last_commit(git, commit, source["receipt_path"]), source["receipt_path"])
    for proof, candidate in zip(assembled["proofs"], assembled["candidates"]):
        entries.extend(_computation_snapshots(git, candidate))
        if candidate.get("rebuild_context") is not None:
            from .rebuild import validate_rebuild_context
            context = candidate["rebuild_context"]
            validate_rebuild_context(git, context, [source["path"] for source in assembled["sources"]],
                                     at_commit=candidate["base_head"])
            add(context["commit_sha"], context["path"])
        add(proof["stage_commit"], proof["stage_path"])
        add(proof["review_commit"], proof["review_path"])
        add(_last_commit(git, candidate["base_head"], candidate["attempt_path"]), candidate["attempt_path"])
        for related in candidate.get("related", []):
            if related["run_id"] == group_id:
                _fail("publication_dependency_cycle")
            previous = verify_publication(git.repo, related["run_id"], historical=True)
            entries.extend(previous.get("authority_snapshots", []))
    for path in assembled["files"]:
        add(commit, path)
    add(commit, manifest_path)
    add(commit, marker_path)
    if git.path(receipt_path).exists():
        add(_last_commit(git, git.head(), receipt_path), receipt_path)
    return list({(entry["commit_sha"], entry["path"]): entry for entry in entries}.values())


def verify_group_publication(repo, group_id, *, source_paths=None, historical=False):
    """Bound recursive verification even through ordinary/group dispatch calls."""
    stack = _VERIFY_STACK.get()
    if group_id in stack or len(stack) >= 16:
        _fail("publication_dependency_cycle")
    token = _VERIFY_STACK.set((*stack, group_id))
    try:
        return _verify_group_publication(repo, group_id, source_paths=source_paths, historical=historical)
    finally:
        _VERIFY_STACK.reset(token)


def _verify_group_publication(repo, group_id, *, source_paths=None, historical=False):
    """Verify the stored plan and Review proofs without relabeling old versions."""
    from .batching import validate_plan
    from .pipeline import (_committed, _deleted_after_publication, _document, _hash,
                           _last_commit, _load, _run_path)

    if not isinstance(group_id, str) or not _RUN.fullmatch(group_id) or type(historical) is not bool:
        _fail()
    git = PipelineGit(repo)
    head = git.check()
    manifest_path, receipt_path = _run_path(group_id, "manifest"), _run_path(group_id, "receipt")
    manifest_bytes = _committed(git, manifest_path)
    manifest = _load(manifest_bytes, "batch_group_manifest", SCHEMA)
    try:
        plan = manifest["plan"]
        bound = [manifest["source"]["path"], *[source["path"] for source in manifest.get("additional_sources", [])]]
        selected = _paths(bound if source_paths is None else source_paths)
        if sorted(bound) != selected or manifest.get("run_id") != group_id:
            _fail("batch_group_scope_mismatch")
        _scope(plan, selected)
        validate_plan(repo, plan, source_paths=selected, require_current_versions=False, historical=True, _locked=True)
        assembled = _proofs(git, plan, [proof["run_id"] for proof in manifest["proofs"]], selected, historical=True)
        expected = _manifest(git, group_id, plan, assembled, manifest["parent_head"], manifest["published_at"])
        if manifest != expected:
            _fail("batch_group_manifest_mismatch")
        commit = _last_commit(git, head, manifest_path)
        if git.text("rev-parse", commit + "^") != manifest["parent_head"] or git.read(commit, manifest_path) != manifest_bytes:
            _fail("batch_group_manifest_mismatch")
        marker_path = _marker_path(plan)
        marker = {"run_id": group_id, "plan_id": plan["plan_id"], "plan_sha256": _hash(plan), "manifest_path": manifest_path}
        if (git.read(commit, marker_path) != _document("batch_group_inputs", marker, SCHEMA)
                or _committed(git, marker_path) != git.read(commit, marker_path)
                or _last_commit(git, head, marker_path) != commit):
            _fail("batch_group_marker_mismatch")
        changes = {manifest_path: "A", marker_path: "A"}
        for path, data in assembled["files"].items():
            operation, before = assembled["operations"][path], assembled["before"][path]
            if git.read(commit, path) != data:
                _fail("batch_group_file_mismatch")
            if operation["action"] != "reuse":
                changes[path] = "A" if operation["action"] == "create" else "M"
            if (git._tree_blob(manifest["parent_head"], path) is None) != (before is None):
                _fail("batch_group_before_mismatch")
            if before is not None and git.read(manifest["parent_head"], path) != before:
                _fail("batch_group_before_mismatch")
            if not historical and _committed(git, path) != data:
                _fail("batch_group_file_mismatch")
            origin = _last_commit(git, commit, path) if operation["action"] == "reuse" else commit
            if _deleted_after_publication(git, path, origin, head):
                _fail("batch_group_file_deleted")
        diff = git.run("diff-tree", "--no-commit-id", "--no-renames", "--name-status", "-r", "-z", manifest["parent_head"], commit).decode().split("\0")
        if diff != [entry for path in sorted(changes) for entry in (changes[path], path)] + [""]:
            _fail("batch_group_change_mismatch")
        receipt_saved = git.path(receipt_path).exists()
        if receipt_saved:
            receipt = _load(_committed(git, receipt_path), "batch_group_receipt", SCHEMA)
            if receipt != {"run_id": group_id, "commit_sha": commit, "manifest_path": manifest_path,
                           "manifest_sha256": _hash(manifest_bytes), "local_status": "confirmed", "remote_status": "not_requested"}:
                _fail("batch_group_receipt_mismatch")
        snapshots = _authority(git, assembled, group_id, commit, manifest_path, marker_path, receipt_path)
        git.check(head)
        return {"status": "published_local", "run_id": group_id, "mode": "batch_group",
            "manifest_path": manifest_path, "commit_sha": commit, "receipt_path": receipt_path,
            "receipt_saved": receipt_saved, "files": manifest["files"], "source": manifest["source"],
            **({"additional_sources": manifest["additional_sources"]} if "additional_sources" in manifest else {}),
            "review_path": manifest["review_path"], "review_commit": manifest["review_commit"],
            "versions": manifest["versions"], "plan_id": plan["plan_id"], "plan_sha256": manifest["plan_sha256"],
            "object_origins": manifest["object_origins"], "operations": manifest["operations"],
            "controls_digest": manifest["controls_digest"], "authority_snapshots": snapshots,
            "authority_files": {entry["path"]: entry["sha256"] for entry in snapshots
                                if not historical or not entry["path"].startswith(("memory/records/", "memory/topics/"))},
            "remote_status": "not_requested", **({"historical": True} if historical else {})}
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _fail()


def publish_final_group(repo, plan, final_objects, reviewed_runs, *, source_paths, expected_head=None, group_id=None):
    """Commit all formal changes and a distinct group proof in one conditional tree."""
    from .batching import validate_plan
    from .pipeline import (_committed, _document, _execution_guard, _hash, _load,
                           _run_path, _targets_clear, _updated_objects)

    scope = _scope(plan, source_paths)
    if (not isinstance(final_objects, list) or not final_objects
            or any(not isinstance(obj, dict) or not isinstance(obj.get("path"), str) for obj in final_objects)
            or len({obj["path"] for obj in final_objects}) != len(final_objects)):
        _fail("batch_group_final_objects_mismatch")
    if group_id is not None and (not isinstance(group_id, str) or not _RUN.fullmatch(group_id)):
        _fail()
    git = PipelineGit(repo)
    with git.lock():
        git.recover()
        head = git.check(expected_head)
        marker_path = _marker_path(plan)
        if git.path(marker_path).exists():
            marker = _load(_committed(git, marker_path), "batch_group_inputs", SCHEMA)
            if marker.get("plan_id") != plan["plan_id"] or marker.get("plan_sha256") != _hash(plan):
                _fail("batch_group_marker_mismatch")
            if group_id is not None and marker.get("run_id") != group_id:
                _fail("batch_group_plan_already_published")
            group_id = marker["run_id"]
            result = verify_group_publication(repo, group_id, source_paths=plan["source_paths"])
            requested = _proofs(git, plan, reviewed_runs, scope, historical=True)
            if ({obj["path"]: _hash(obj) for obj in final_objects}
                    != {path: _hash(obj) for path, obj in requested["objects"].items()}
                    or {path: _hash(data) for path, data in requested["files"].items()} != result["files"]):
                _fail("batch_group_final_objects_mismatch")
        else:
            if git.text("log", "-1", "--format=%H", "--", marker_path):
                _fail("batch_group_marker_deleted")
            validate_plan(repo, plan, source_paths=scope, _locked=True)
            if plan.get("status") != "ready" or not plan.get("coverage", {}).get("complete") or not plan.get("batches"):
                _fail("batch_group_incomplete_plan")
            assembled = _proofs(git, plan, reviewed_runs, scope, historical=False)
            if (not isinstance(final_objects, list) or len(final_objects) != len(assembled["objects"])
                    or len({obj["path"] for obj in final_objects}) != len(final_objects)
                    or {obj["path"]: canonical(obj) for obj in final_objects}
                       != {path: canonical(obj) for path, obj in assembled["objects"].items()}):
                _fail("batch_group_final_objects_mismatch")
            for candidate in assembled["candidates"]:
                _execution_guard(repo, candidate.get("execution_context"), run_id=candidate["run_id"])
                _updated_objects(git, candidate["objects"], candidate["related"], check_current=True,
                                 rebuild_context=candidate.get("rebuild_context"))
            group_id = group_id or "run-" + _hash({"plan_id": plan["plan_id"],
                "files": {path: _hash(data) for path, data in assembled["files"].items()}})[7:39]
            manifest_path = _run_path(group_id, "manifest")
            if git.path(manifest_path).exists() or git.text("log", "-1", "--format=%H", "--", manifest_path):
                _fail("batch_group_id_exists")
            manifest = _manifest(git, group_id, plan, assembled, head, timestamp())
            files, expected = {}, {}
            for path, data in assembled["files"].items():
                if assembled["operations"][path]["action"] == "reuse":
                    if _committed(git, path) != data:
                        _fail("batch_group_before_mismatch")
                    continue
                if assembled["operations"][path]["action"] == "create":
                    _targets_clear(git, [assembled["objects"][path]],
                                   rebuild_context=assembled["candidates"][0].get("rebuild_context"))
                files[path], expected[path] = data, assembled["before"][path]
            files[manifest_path] = _document("batch_group_manifest", manifest, SCHEMA)
            files[marker_path] = _document("batch_group_inputs", {"run_id": group_id, "plan_id": plan["plan_id"],
                "plan_sha256": _hash(plan), "manifest_path": manifest_path}, SCHEMA)
            expected.update({manifest_path: None, marker_path: None})
            git.write_changes(files, expected, head, "publish: atomically persist final reviewed dependency group")
            result = verify_group_publication(repo, group_id, source_paths=plan["source_paths"])
        if not result["receipt_saved"]:
            receipt_path = result["receipt_path"]
            if git.text("log", "-1", "--format=%H", "--", receipt_path):
                _fail("batch_group_receipt_deleted")
            receipt = {"run_id": group_id, "commit_sha": result["commit_sha"], "manifest_path": result["manifest_path"],
                "manifest_sha256": _hash(_committed(git, result["manifest_path"])),
                "local_status": "confirmed", "remote_status": "not_requested"}
            git.write_files({receipt_path: _document("batch_group_receipt", receipt, SCHEMA)}, git.head(),
                            "publish: confirm existing final dependency group")
        return {**verify_group_publication(repo, group_id, source_paths=plan["source_paths"]), "head": git.head()}
