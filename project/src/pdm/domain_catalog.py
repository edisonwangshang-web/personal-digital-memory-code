"""Project a bounded current evidence snapshot into a read-only domain catalog.

Only documents already verified by build_current_evidence are used. Domain
labels and topic record references never authorize extra reads or model calls.
"""

from copy import deepcopy
import html
import json
import re

from .contracts import CaptureError
from .domains import DOMAIN_LABELS, domain_ids
from .evidence import MAX_EVIDENCE_BYTES, build_current_evidence

_PATH = re.compile(r"memory/(records|topics)/([a-z][a-z0-9-]{0,95})\.md\Z")
_HASH = re.compile(r"sha256-[0-9a-f]{64}\Z")


def _fail(code="invalid_catalog_document"):
    raise CaptureError(code, "领域目录校验失败；未补读资料或写入记忆")


def _budget(max_bytes):
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EVIDENCE_BYTES:
        _fail("invalid_evidence_budget")


def _entry(document, runs):
    metadata = document["metadata"]
    return {"id": metadata["id"], "title": metadata["title"], "path": document["path"],
            "content_version": metadata["content_version"], "sha256": document["sha256"],
            "publication_runs": sorted(runs)}


def _documents(evidence):
    indexed = {}
    for publication in evidence["publications"]:
        for document in publication["documents"]:
            path, metadata = document.get("path"), document.get("metadata")
            match = _PATH.fullmatch(path) if isinstance(path, str) else None
            if (match is None or not isinstance(metadata, dict)
                    or metadata.get("id") != match[2]
                    or not isinstance(metadata.get("title"), str) or not metadata["title"].strip()
                    or not isinstance(metadata.get("content_version"), str) or not _HASH.fullmatch(metadata["content_version"])
                    or not isinstance(document.get("sha256"), str) or not _HASH.fullmatch(document["sha256"])):
                _fail()
            if path in indexed:
                # Current evidence cannot contain competing versions of one
                # path. Compare all already-read bytes/metadata, not just ID.
                if indexed[path]["document"] != document:
                    _fail("catalog_document_conflict")
                indexed[path]["runs"].add(publication["run_id"])
            else:
                indexed[path] = {"document": document, "runs": {publication["run_id"]}, "kind": match[1]}
    return indexed


def build_domain_catalog(repo, source_paths, run_ids=None, *, domain=None, max_bytes=MAX_EVIDENCE_BYTES,
                         source_views=None, batch_context=None):
    """List selected current knowledge by domain without discovery or persistence.

    ``domain`` filters presentation only. Evidence coverage and classification
    gaps continue to describe the entire explicitly authorized source/run set.
    """
    _budget(max_bytes)
    if domain is not None and (not isinstance(domain, str) or domain not in DOMAIN_LABELS):
        _fail("invalid_domain_filter")
    projection = ({"source_views": source_views, "batch_context": batch_context}
                  if source_views is not None or batch_context is not None else {})
    selected = build_current_evidence(repo, source_paths, run_ids, max_bytes=max_bytes, **projection)
    indexed = _documents(selected)
    groups = {key: {"label": label, "records": [], "topics": []} for key, label in DOMAIN_LABELS.items()}
    unclassified = {"records": [], "topics": []}
    gaps = []
    unmapped = []
    records = {}
    ids = {}
    counts = {"records": 0, "topics": 0, "classified_records": 0, "classified_topics": 0}
    for path, item in sorted(indexed.items()):
        if item["kind"] != "records":
            continue
        document, metadata = item["document"], item["document"]["metadata"]
        labels = metadata.get("domains", [])
        assigned = domain_ids(labels, allow_legacy=True)
        unknown = sorted({label for label in labels if not domain_ids([label], allow_legacy=True)})
        key = (metadata["id"], metadata["content_version"])
        if key in records:
            _fail("catalog_document_conflict")
        records[key] = {"domains": assigned, "unmapped_labels": unknown, "path": path}
        ids.setdefault(metadata["id"], set()).add(metadata["content_version"])
        entry = _entry(document, item["runs"])
        counts["records"] += 1
        if assigned:
            counts["classified_records"] += 1
            for key in assigned:
                groups[key]["records"].append(entry)
        else:
            unclassified["records"].append(entry)
            gaps.append({"path": path, "reason": "unclassified_record"})
        if unknown:
            # Legacy records may retain fine-grained tags alongside a known
            # major domain. Those extra tags do not make that domain unknown.
            unmapped.append({"path": path, "labels": unknown})
    for path, item in sorted(indexed.items()):
        if item["kind"] != "topics":
            continue
        document, metadata = item["document"], item["document"]["metadata"]
        refs = metadata.get("records")
        assigned = set()
        seen = set()
        if not isinstance(refs, list) or not refs:
            gaps.append({"path": path, "reason": "missing_topic_record_references"})
            refs = []
        for index, ref in enumerate(refs):
            if (not isinstance(ref, dict) or set(ref) != {"id", "content_version"}
                    or not isinstance(ref.get("id"), str) or not isinstance(ref.get("content_version"), str)):
                gaps.append({"path": path, "reason": "invalid_topic_record_reference", "reference_index": index})
                continue
            key = (ref["id"], ref["content_version"])
            if key in seen:
                gaps.append({"path": path, "reason": "duplicate_topic_record_reference", "record_ref": deepcopy(ref)})
                continue
            seen.add(key)
            record = records.get(key)
            if record is None:
                reason = "record_version_mismatch" if ref["id"] in ids else "missing_record"
                gaps.append({"path": path, "reason": reason, "record_ref": deepcopy(ref)})
                continue
            assigned.update(record["domains"])
            if not record["domains"]:
                gaps.append({"path": path, "reason": "incomplete_record_classification", "record_ref": deepcopy(ref)})
        entry = _entry(document, item["runs"])
        counts["topics"] += 1
        if assigned:
            counts["classified_topics"] += 1
            for key in DOMAIN_LABELS:
                if key in assigned:
                    groups[key]["topics"].append(entry)
        else:
            unclassified["topics"].append(entry)
    controls = selected["controls"]
    result = {"head": selected["head"], "scope": "explicit_current_domain_catalog",
              "evidence_scope": selected["scope"], "domain_filter": domain,
              "knowledge_status": selected["knowledge_status"], "coverage": deepcopy(selected["coverage"]),
              "controls": {"digest": controls["digest"], "event_count": len(controls["events"]),
                           "control_ids": sorted(event["control_id"] for event in controls["events"])},
              "domains": groups if domain is None else {domain: groups[domain]},
              "unclassified": unclassified, "classification": {"complete": not gaps, "counts": counts,
                  "gaps": gaps, "unmapped_labels": unmapped},
              "catalog_complete_within_selected_scope": bool(selected["coverage"]["current_complete"]) and not gaps,
              "model_calls": 0,
              "limitations": list(dict.fromkeys([*selected["limitations"],
                  "目录仅由本次明确来源与运行的已验证当前正式对象生成；空列表不代表该领域全库没有知识。",
                  "领域仅使用受审标签及明确旧标签别名；主题按所选record的精确ID与内容版本聚合，不根据标题、正文或数值猜测。",
                  "领域筛选只改变目录显示，不扩大读取范围，也不移除其他分类或证据覆盖缺口；未自动写入README或Git。"]))}
    if len((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()) > max_bytes:
        _fail("evidence_limit")
    return result


def _label(value):
    text = " ".join(value.split())
    return re.sub(r"([\\`*_{}\[\]()#+.!|])", r"\\\1", html.escape(text, quote=False))


def render_domain_catalog(catalog, *, max_bytes=MAX_EVIDENCE_BYTES):
    """Render the same bounded directory for reading; never write a README."""
    _budget(max_bytes)
    lines = ["# 所选范围的当前知识目录", "", f"资料版本：`{catalog['head']}`", "",
             "仅列出明确选择范围；空目录不代表全库没有该领域知识。", ""]
    def entries(kind, values):
        lines.extend([f"### {kind}", ""])
        lines.extend(f"- [{_label(item['title'])}]({item['path']}) · `{item['content_version']}`" for item in values)
        if not values:
            lines.append("本次范围没有可列出的对象。")
        lines.append("")
    for group in catalog["domains"].values():
        lines.extend([f"## {group['label']}", ""])
        entries("记录", group["records"])
        entries("主题", group["topics"])
    lines.extend(["## 未归类对象", ""])
    entries("记录", catalog["unclassified"]["records"])
    entries("主题", catalog["unclassified"]["topics"])
    lines.extend(["## 覆盖范围与缺口", "",
                  "```json", json.dumps({"coverage": catalog["coverage"], "classification": catalog["classification"],
                    "catalog_complete_within_selected_scope": catalog["catalog_complete_within_selected_scope"]}, ensure_ascii=False, indent=2),
                  "```", "", "## 限制", ""])
    lines.extend("- " + _label(limit) for limit in catalog["limitations"])
    result = "\n".join(lines) + "\n"
    if len(result.encode()) > max_bytes:
        _fail("evidence_limit")
    return result
