"""Read-only confirmation context inside an existing evidence authorization.

Registry references never grant permission to open another source or run. The
registry state belongs to the evidence HEAD; even a verified resolution proves
a historical processing event, rather than the current truth of its contents.
"""

import copy
import hashlib

from .contracts import CaptureError, parse_markdown
from .controls import object_policy, read_controls, source_policy
from .pending import (REGISTRY_PATH, _deleted, _json, _publication_scope,
                      _read_document, _read_registry, _reply_association_commit)


def _resolution(git, item, paths, runs, sources, controls, publications):
    from .pipeline import _last_commit, verify_publication

    saved = item["resolution"]
    if saved is None:
        return {"status": "not_applicable"}
    run = saved["run_id"]
    if run not in runs:
        return {"status": "not_selected", "missing_run_ids": [run]}
    # This reads only the manifest, and constrains both the verified candidate
    # and its source bodies before verify_publication is allowed to open them.
    bound = _publication_scope(git, run, item)
    if not set(bound) <= paths:
        raise CaptureError("pending_publication_mismatch", "解决依据超出本次来源范围")
    _, body = _read_document(git, saved["manifest_path"])
    manifest = _json(body)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files:
        raise CaptureError("pending_publication_mismatch", "解决依据的发布范围无效")
    if any(object_policy(controls, path, purpose="history")["reason"] == "delete" for path in files):
        return {"status": "excluded_by_control"}
    if _last_commit(git, git.head(), saved["manifest_path"]) != saved["commit_sha"]:
        raise CaptureError("pending_publication_mismatch", "解决依据不属于登记的固定发布")
    publication = publications.get(run)
    if publication is None:
        publication = verify_publication(git.repo, run, source_paths=bound, historical=True)
    if (saved != {"run_id": publication["run_id"], "commit_sha": publication["commit_sha"],
                  "manifest_path": publication["manifest_path"]}):
        raise CaptureError("pending_publication_mismatch", "解决依据与核验发布不符")
    referenced = set()
    for path in publication["files"]:
        if path.startswith("memory/records/"):
            metadata, _ = parse_markdown(git.read(publication["commit_sha"], path))
            if metadata.get("review_status") != "passed":
                raise CaptureError("pending_publication_mismatch", "解决依据未经审核")
            referenced.update((ref["source_id"], ref["source_version"]) for ref in metadata["sources"])
    if not {(ref["source_id"], ref["source_version"]) for ref in item["reply_sources"]} <= referenced:
        raise CaptureError("pending_reply_not_applied", "答复未成为正式记录的实际依据")
    for ref in item["reply_sources"]:
        source = sources[ref["path"]]
        if any(source.get(key) != ref[key] for key in ("source_id", "source_version", "path", "commit_sha")):
            raise CaptureError("pending_reply_conflict", "答复引用与本次已核验来源不符")
        associated = _reply_association_commit(git, item, ref)
        try:
            git.run("merge-base", "--is-ancestor", associated, publication["commit_sha"])
        except CaptureError as error:
            raise CaptureError("pending_publication_mismatch", "发布早于真实答复关联") from error
    return {"status": "verified_history", **saved,
            "reply_record_references_verified": True, "reply_association_verified": True}


def build_pending_context(git, head, source_paths, sources, run_ids=(), *, history=False, publications=()):
    """Return scoped registry context, or None when no item concerns this scope.

    ``sources`` are already read, capture-verified and budgeted by the caller.
    This helper never loads a source, and only verifies explicitly selected runs.
    Partial, excluded or budget-omitted items expose an opaque ID and a coverage
    gap instead of their question, key, object references or answer contents.
    """
    registry, data = _read_registry(git, head)
    if not registry["items"]:
        return None
    paths = set(source_paths)
    provided = {source["path"]: source for source in sources}
    controls = read_controls(git.repo, head=head)
    purpose = "history" if history else "current"
    result = {"registry_path": REGISTRY_PATH, "registry_head": head,
              "registry_sha256": "sha256-" + hashlib.sha256(data).hexdigest(),
              "state_basis": "registry_at_evidence_head", "items": [],
              "coverage": {"complete": True, "gaps": [], "excluded_item_count": 0,
                           "unverified_resolution_item_ids": [], "missing_run_ids": []},
              "limitations": ["待确认状态属于本次固定HEAD的统一清单，不代表历史发布当时的状态或问题前提已成为事实。",
                              "open尚未答复；answered仅收到已采集答复，未完成正式知识处理；withdrawn表示撤回。",
                              "resolved的解决依据核验只证明登记的历史发布与答复关联链通过检查，不独立证明问题语义完全解决或结论当前仍适用。"]}
    coverage = result["coverage"]
    verified = {publication["run_id"]: publication for publication in publications}
    for item in registry["items"]:
        refs = [*item["source_refs"], *item["reply_sources"]]
        item_paths = {ref["path"] for ref in refs}
        if not item_paths & paths:
            continue
        if _deleted(controls, item):
            coverage["excluded_item_count"] += 1
            continue
        missing, unread = sorted(item_paths - paths), sorted((item_paths & paths) - provided.keys())
        controlled = [ref["path"] for ref in refs if source_policy(controls, ref["path"], purpose=purpose)["excluded"]]
        controlled_objects = [ref["path"] for ref in item["object_refs"]
                              if object_policy(controls, ref["path"], purpose=purpose,
                                               content_version=ref["content_version"])["excluded"]]
        if missing or unread or controlled or controlled_objects:
            gap = {"item_id": item["item_id"], "pending_status": item["pending_status"],
                   "reason": "incomplete_or_excluded_scope", "missing_source_paths": missing,
                   "unread_source_paths": unread, "controlled_source_paths": sorted(controlled),
                   "controlled_object_count": len(controlled_objects)}
            coverage["gaps"].append(gap)
            continue
        for ref in refs:
            if any(provided[ref["path"]].get(key) != ref[key] for key in ("source_id", "source_version", "path")):
                raise CaptureError("pending_source_mismatch", "待确认引用与本次来源版本不符")
        for ref in item["reply_sources"]:
            source = provided[ref["path"]]
            if source.get("commit_sha") != ref["commit_sha"]:
                raise CaptureError("pending_reply_conflict", "答复引用与实际采集提交不符")
            if source.get("metadata", {}).get("acquisition_status") == "link_only":
                raise CaptureError("pending_reply_missing_text", "仅链接不能作为实质答复")
        selected = copy.deepcopy(item)
        selected.pop("key")  # The stable key is not additional question evidence.
        selected["resolution_verification"] = _resolution(git, item, paths, set(run_ids), provided, controls, verified)
        if item["pending_status"] == "resolved" and selected["resolution_verification"]["status"] != "verified_history":
            coverage["unverified_resolution_item_ids"].append(item["item_id"])
            coverage["missing_run_ids"].extend(selected["resolution_verification"].get("missing_run_ids", []))
        result["items"].append(selected)
    coverage["missing_run_ids"] = sorted(set(coverage["missing_run_ids"]))
    coverage["complete"] = not (coverage["gaps"] or coverage["unverified_resolution_item_ids"])
    if not result["items"] and not coverage["gaps"] and not coverage["excluded_item_count"]:
        return None
    if not coverage["complete"]:
        result["limitations"].append("部分待确认事项或解决依据不在本次已提供范围内；未自动读取其他来源或运行，不能据此声称无待确认或已核验解决。")
    return result
