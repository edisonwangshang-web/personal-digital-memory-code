"""A bounded current-task view, derived without reading facts or changing scope."""

import re


CONTRACT = "completion-goals-v1"
INSTRUCTIONS = (
    "completion_goals.contract=completion-goals-v1仅是本次工作清单，不是事实来源、审核结论或额外权限。"
    "all_target_ids限定当前实际quality_context目标；unfinished_target_ids是当前完整草稿尚未自报整理的目标。"
    "先前summarized仍是未通过稿的自报，须保留有效内容并重新核对，不能当成受审事实。"
    "本次须同时处理全部未完成目标、实际validation_error及实际Review意见；审核列出的局部问题不是全部工作范围。"
    "required_related_paths与required_current_draft_paths是须逐一处理的文档身份，不要求复制错误内容或维持原文件数量；"
    "合法纠正、合并及撤回仍按实际来源、依赖和审核规则执行。"
    "对可整理资料形成实质正文，真实必要证据缺口如实deferred并交既有Review/pending；不得编造summarized。"
    "完整原文、当前稿、错误与审核仍在原字段中，不从清单补造事实、读新来源或增加阶段。"
)
_PATH = re.compile(r"memory/(?:records|topics)/[a-z][a-z0-9-]{0,95}\.md\Z")
_ID = re.compile(r"[a-z][a-z0-9-]{0,95}\Z")
_RUN = re.compile(r"run-[0-9a-f]{32}\Z")


def _paths(draft):
    """Keep valid declared identities; malformed draft fields remain in feedback."""
    paths = set()
    for key, folder in (("objects", "records"), ("wikis", "topics")):
        entries = draft.get(key, [])
        for obj in entries if isinstance(entries, list) else []:
            if not isinstance(obj, dict):
                continue
            path = obj.get("path")
            if isinstance(path, str) and _PATH.fullmatch(path):
                paths.add(path)
            elif isinstance(obj.get("id"), str) and _ID.fullmatch(obj["id"]):
                paths.add(f"memory/{folder}/{obj['id']}.md")
    wiki = draft.get("wiki")
    if isinstance(wiki, dict) and isinstance(wiki.get("id"), str) and _ID.fullmatch(wiki["id"]):
        paths.add(f"memory/topics/{wiki['id']}.md")
    refs = draft.get("reuse_refs", [])
    for ref in refs if isinstance(refs, list) else []:
        path = ref.get("path") if isinstance(ref, dict) else None
        if isinstance(path, str) and _PATH.fullmatch(path):
            paths.add(path)
    return sorted(paths)


def current_completion_goals(payload):
    """Use only the current inventory and one latest draft, never full history."""
    if "completion_goals" in payload:
        raise ValueError("invalid_completion_goals")
    context = payload.get("quality_context")
    targets = context.get("targets") if isinstance(context, dict) else None
    if not isinstance(targets, list):
        raise ValueError("invalid_quality_context")
    ids = [item.get("target_id") if isinstance(item, dict) else None for item in targets]
    if any(not isinstance(item, str) or not item.strip() for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("invalid_quality_context")
    all_ids = set(ids)
    draft, quality, run, kind = {}, {}, None, "initial_extraction"
    feedback = payload.get("extraction_feedback")
    if isinstance(feedback, dict) and isinstance(feedback.get("draft"), dict):
        draft = feedback["draft"]
        quality = draft.get("quality", {})
        run, kind = feedback.get("source_run_id"), "completed_extraction_failed_validation"
    else:
        revision = payload.get("revision_context")
        if isinstance(revision, dict) and isinstance(revision.get("candidate"), dict):
            draft = revision["candidate"]
            provenance = draft.get("quality_provenance")
            quality = provenance.get("quality", {}) if isinstance(provenance, dict) else {}
            run, kind = revision.get("source_run_id") or draft.get("run_id"), "rejected_candidate"
    rows = quality.get("coverage_disposition", []) if isinstance(quality, dict) else []
    declarations = {}
    for row in rows if isinstance(rows, list) else []:
        if (isinstance(row, dict) and isinstance(row.get("target_id"), str)
                and row["target_id"] in all_ids):
            declarations.setdefault(row["target_id"], []).append(row.get("status"))
    # Duplicate or malformed declarations do not remove a target from work.
    summarized = {key for key, statuses in declarations.items() if statuses == ["summarized"]}
    related = set()
    for group in payload.get("related", []) if isinstance(payload.get("related", []), list) else []:
        documents = group.get("documents", []) if isinstance(group, dict) else []
        for document in documents if isinstance(documents, list) else []:
            path = document.get("path") if isinstance(document, dict) else None
            if not isinstance(path, str) or not _PATH.fullmatch(path):
                raise ValueError("invalid_completion_goals")
            related.add(path)
    return {
        "contract": CONTRACT, "scope": "current_authorized_quality_targets_only",
        "all_target_ids": sorted(all_ids),
        "unfinished_target_ids": sorted(all_ids - summarized),
        "required_related_paths": sorted(related),
        "required_current_draft_paths": _paths(draft),
        "current_draft_provenance": {
            "kind": kind, "source_run_id": run if isinstance(run, str) and _RUN.fullmatch(run) else None,
            "coverage_status_is_self_reported_not_reviewed_fact": True,
        },
    }
