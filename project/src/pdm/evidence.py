"""Explicit source and reviewed-object evidence for bounded, read-only answers.

Only explicitly selected sources and the exact files of one verified publication
may be sent. Original source lines remain the sole citation coordinates;
summaries never become independent evidence. No answer is persisted as knowledge.
"""

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

from .contracts import CaptureError, M1_SCHEMA_VERSION, PIPELINE_SCHEMA_VERSION, QUALITY_SCHEMA_VERSION, parse_markdown
from .controls import _json, _metadata_only, _temporal, object_policy, read_controls, source_locator_policy, source_policy
from .model_calls import OUTPUT_SCHEMAS, USAGE_FIELDS, _valid, call_model
from .pending_evidence import build_pending_context
from .pipeline import control_guard, load_source, normalize_source_paths, verify_publication
from .transaction import PipelineGit

# Includes every source representation and metadata in the serialized evidence.
# The transport has a separate 256,000-byte bound including question and rules.
MAX_EVIDENCE_BYTES = 192_000
MAX_PUBLICATION_DOCUMENTS = 21
MAX_BATCH_PUBLICATION_DOCUMENTS = 40
MAX_GROUP_PUBLICATION_DOCUMENTS = 256
MAX_CURRENT_SOURCES = 256
MAX_CURRENT_RUNS = 256
_OBJECT_PATH = re.compile(r"memory/(records|topics)/[a-z][a-z0-9-]{0,95}\.md\Z")
_HASH = re.compile(r"sha256-[0-9a-f]{64}\Z")


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _bounded(value):
    size = len(_encoded(value))
    if size > MAX_EVIDENCE_BYTES:
        raise CaptureError("evidence_limit", "选定证据超过问答字节预算；未截断或发送资料")
    return size


def _missing_line_ranges(count, numbers):
    missing, cursor = [], 1
    for number in sorted(set(numbers)):
        if cursor < number:
            missing.append(f"L{cursor}-L{number - 1}")
        cursor = number + 1
    if cursor <= count:
        missing.append(f"L{cursor}-L{count}")
    return missing


def _snapshot_guard(git, head):
    if git.head() != head:
        raise CaptureError("head_conflict", "读取或问答期间资料版本发生变化")
    control_guard(git, head)
    if git.run("status", "--porcelain", "--untracked-files=all"):
        raise CaptureError("dirty_worktree", "问答要求固定且干净的资料工作区")


def _partial_legacy_guard(git, paths, head):
    snapshot = control_guard(git, head)
    if snapshot is None or not snapshot.get("events"):
        return
    if any(source_policy(snapshot, path).get("object_restrictions") for path in paths):
        raise CaptureError("current_evidence_required", "对象级纠正须使用提供局部控制与行范围限制的current证据入口")


def _publication_documents(git, publication, source, head, run_id, *, sources=None):
    files = publication.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_PUBLICATION_DOCUMENTS:
        raise CaptureError("publication_mismatch", "发布证据对象范围无效")
    documents = []
    total = 0
    # No directory traversal or search: open only code-verified manifest entries.
    for path, expected in sorted(files.items()):
        if (not isinstance(path, str) or not _OBJECT_PATH.fullmatch(path)
                or not isinstance(expected, str) or not _HASH.fullmatch(expected)):
            raise CaptureError("path_rejected", "发布对象不在受限正式记录或主题路径内")
        data = git._existing(path)  # bounded O_NOFOLLOW, ordinary files only
        if data is None or "sha256-" + hashlib.sha256(data).hexdigest() != expected:
            raise CaptureError("publication_mismatch", "正式对象字节与受审发布版本不一致")
        if git.read(head, path) != data or git.read(publication["commit_sha"], path) != data:
            raise CaptureError("publication_mismatch", "正式对象不属于固定发布提交")
        total += len(data)
        if total > MAX_EVIDENCE_BYTES:
            raise CaptureError("evidence_limit", "正式对象超过问答字节预算；未截断或发送资料")
        metadata, body = parse_markdown(data)
        refs = metadata.get("sources")
        origin = publication.get("object_origins", {}).get(path, {"run_id": run_id, "review_path": publication["review_path"]})
        if (metadata.get("schema_version") != PIPELINE_SCHEMA_VERSION
                or metadata.get("review_status") != "passed"
                or metadata.get("review_ref") != origin["review_path"]
                or not isinstance(metadata.get("generated_by"), dict)
                or metadata["generated_by"].get("run_id") != origin["run_id"]
                or not isinstance(refs, list) or not refs
                or any(not _formal_reference_matches(ref, source, sources) for ref in refs)):
            raise CaptureError("evidence_source_mismatch", "正式对象不属于本次受审来源与运行")
        documents.append({"path": path, "sha256": expected, "metadata": metadata, "body": body})
    return documents


def _formal_reference_matches(ref, source, sources, provided_lines=None):
    if not isinstance(ref, dict):
        return False
    if sources is None:
        # Preserve the original M0 evidence contract and serialized payload.
        return (ref.get("source_id") == source["source_id"]
                and ref.get("source_version") == source["source_version"])
    matching = next((item for item in sources
                     if (ref.get("source_id"), ref.get("source_version"))
                     == (item["source_id"], item["source_version"])), None)
    if matching is None or matching.get("metadata", {}).get("acquisition_status") == "link_only":
        return False
    locator, quote = ref.get("locator"), ref.get("quote")
    match = re.fullmatch(r"L([1-9][0-9]*)-L([1-9][0-9]*)", locator if isinstance(locator, str) else "")
    if provided_lines is not None:
        lines = provided_lines.get((ref.get("source_id"), ref.get("source_version")), {})
        if match is None or any(len(number) > 9 for number in match.groups()):
            return False
        start, end = map(int, match.groups())
        return (1 <= start <= end and end - start + 1 <= len(lines)
                and all(number in lines for number in range(start, end + 1))
                and isinstance(quote, str) and bool(quote.strip())
                and quote in "".join(matching["body"].splitlines(keepends=True)[start - 1:end]))
    lines = matching["body"].splitlines(keepends=True)
    return (match is not None and 1 <= int(match[1]) <= int(match[2]) <= len(lines)
            and isinstance(quote, str) and bool(quote.strip())
            and quote in "".join(lines[int(match[1]) - 1:int(match[2])]))


def evidence(repo: Path, source_path: str, run_id: str | None = None) -> dict:
    git = PipelineGit(repo)
    with git.lock():
        head = git.head()
        _snapshot_guard(git, head)
        _partial_legacy_guard(git, [source_path], head)
        # First validate all publication bindings and reviewed bytes; only then
        # select formal object bodies for this request.
        publication = verify_publication(repo, run_id, source_paths=[source_path]) if run_id is not None else None
        source = load_source(repo, source_path)
        if publication is not None:
            if publication.get("additional_sources"):
                raise CaptureError("evidence_source_mismatch", "多来源发布必须明确选择完整来源集合")
            bound = publication["source"]
            if any(bound.get(key) != source.get(key) for key in ("source_id", "source_version", "path")):
                raise CaptureError("evidence_source_mismatch", "发布证据不属于选定来源")
            documents = _publication_documents(git, publication, source, head, run_id)
            selected_publication = {key: publication[key] for key in
                                    ("run_id", "manifest_path", "commit_sha", "review_path", "files")}
            if "object_origins" in publication:
                selected_publication["object_origins"] = publication["object_origins"]
            selected_publication["documents"] = documents
        else:
            selected_publication = None
        result = {
            "head": head,
            "scope": ("one_explicit_source_and_verified_publication" if publication is not None
                      else "one_explicit_source"),
            "knowledge_status": "published" if publication is not None else "unprocessed_or_unverified",
            "source": source,
            "lines": [{"line": index, "text": line}
                      for index, line in enumerate(source["body"].splitlines(), 1)],
            "publication": selected_publication,
            "limitations": ["仅读取明确指定的一份原文及所选发布运行的正式对象，未检查全库或判断全局最新状态。",
                            "Review 检查整理忠实性，不等于独立事实核验。",
                            "正式对象是该原文的整理结果，引用位置始终对应原文实际行号。"],
        }
        pending = build_pending_context(git, head, [source_path], [source],
                                        [run_id] if run_id is not None else [],
                                        publications=[selected_publication] if selected_publication is not None else [])
        if pending is not None:
            result["pending"] = pending
            result["limitations"].extend(pending["limitations"])
        _bounded(result)
        _snapshot_guard(git, head)
        return result


_SOURCE_BINDING_FIELDS = ("source_id", "source_version", "path", "commit_sha",
                          "source_bytes_sha256", "receipt_path", "receipt_sha256")
_PUBLICATION_FIELDS = ("run_id", "manifest_path", "commit_sha", "review_path", "files")


def _check_publication_sources(publication, sources):
    additional = publication.get("additional_sources", [])
    if not isinstance(additional, list) or not isinstance(publication.get("source"), dict):
        raise CaptureError("evidence_source_mismatch", "发布来源集合无效")
    bindings = [publication["source"], *additional]
    if (len(bindings) != len(sources) or any(not isinstance(item, dict) for item in bindings)):
        raise CaptureError("evidence_source_mismatch", "发布来源集合与选定范围不一致")
    expected = {source["path"]: source for source in sources}
    if (len({binding.get("path") for binding in bindings}) != len(bindings)
            or any(binding.get("path") not in expected or
                   any(binding.get(key) != expected[binding["path"]].get(key)
                       for key in _SOURCE_BINDING_FIELDS) for binding in bindings)):
        raise CaptureError("evidence_source_mismatch", "发布来源集合与选定版本不一致")


def build_evidence_sources(repo: Path, source_paths: list[str], run_id: str | None = None) -> dict:
    """Read 1–8 exact source versions without discovery or implicit latest reads.

    A single selection delegates to the original M0 API. A multi-source run is
    usable only with its complete source set; unrelated publications are never
    searched, opened, or included.
    """
    paths = normalize_source_paths(source_paths)
    if len(paths) == 1:
        return evidence(repo, paths[0], run_id)
    git = PipelineGit(repo)
    with git.lock():
        head = git.head()
        _snapshot_guard(git, head)
        _partial_legacy_guard(git, paths, head)
        publication = verify_publication(repo, run_id, source_paths=paths) if run_id is not None else None
        sources = [load_source(repo, path) for path in paths]
        if publication is not None:
            _check_publication_sources(publication, sources)
            selected_publication = {key: publication[key] for key in _PUBLICATION_FIELDS}
            if "object_origins" in publication:
                selected_publication["object_origins"] = publication["object_origins"]
            selected_publication["documents"] = _publication_documents(
                git, publication, sources[0], head, run_id, sources=sources)
        else:
            selected_publication = None
        status = "published" if publication is not None else "unprocessed_or_unverified"
        published_sources = ({(ref["source_id"], ref["source_version"])
                              for document in selected_publication["documents"]
                              for ref in document["metadata"]["sources"]}
                             if selected_publication is not None else set())
        result = {
            "head": head,
            "scope": ("explicit_sources_and_verified_publication" if publication is not None
                      else "explicit_sources"),
            "knowledge_status": status,
            "sources": [{"source": source,
                         "lines": [{"line": index, "text": line}
                                   for index, line in enumerate(source["body"].splitlines(), 1)],
                         "knowledge_status": ("published" if (source["source_id"], source["source_version"])
                                              in published_sources else "unprocessed_or_unverified")}
                        for source in sources],
            "publication": selected_publication,
            "limitations": ["仅读取明确指定的来源版本及所选发布运行，未检查全库或判断全局最新状态。",
                            "未指定发布运行的原文保持未加工或未核验状态，不能当作已审核知识。",
                            "Review 检查整理忠实性，不等于独立事实核验。",
                            "正式记录和主题页是选定原文的派生整理，不是额外的独立事实；引用始终对应各自原文行号。"],
        }
        if any(source.get("metadata", {}).get("acquisition_status") == "link_only" for source in sources):
            result["limitations"].append("仅链接资料未取得正文，只能说明链接与实际获取状态，不能生成正文摘要或引用；附件本体未备份。")
        pending = build_pending_context(git, head, paths, sources, [run_id] if run_id is not None else [],
                                        publications=[selected_publication] if selected_publication is not None else [])
        if pending is not None:
            result["pending"] = pending
            result["limitations"].extend(pending["limitations"])
        _bounded(result)
        _snapshot_guard(git, head)
        return result


def _recheck_sources(repo, selected, paths, run_id):
    git = PipelineGit(repo)
    with git.lock():
        _snapshot_guard(git, selected["head"])
        sources = [entry["source"] for entry in selected["sources"]]
        if [load_source(repo, path) for path in paths] != sources:
            raise CaptureError("evidence_source_mismatch", "问答期间选定来源发生变化")
        if run_id is not None:
            publication = verify_publication(repo, run_id, source_paths=paths)
            _check_publication_sources(publication, sources)
            documents = _publication_documents(git, publication, sources[0],
                                               selected["head"], run_id, sources=sources)
            expected = selected["publication"]
            if (any(publication.get(key) != expected[key] for key in _PUBLICATION_FIELDS)
                    or publication.get("object_origins", {}) != expected.get("object_origins", {})
                    or documents != expected["documents"]):
                raise CaptureError("publication_mismatch", "问答期间已发送正式证据发生变化")
        pending = build_pending_context(git, selected["head"], paths, sources,
                                        [run_id] if run_id is not None else [],
                                        publications=[selected["publication"]] if selected["publication"] is not None else [])
        if pending != selected.get("pending"):
            raise CaptureError("evidence_changed", "问答期间待确认上下文发生变化")
        _snapshot_guard(git, selected["head"])


def _metrics(result):
    elapsed = result.get("elapsed_seconds")
    if elapsed is not None and (type(elapsed) not in (int, float)
            or not math.isfinite(elapsed) or not 0 <= elapsed <= 86400):
        raise CaptureError("invalid_model_invocation", "问答调用耗时无效")
    usage = result.get("usage")
    if usage is not None and (not isinstance(usage, dict) or set(usage) - USAGE_FIELDS
            or any(type(value) is not int or value < 0 for value in usage.values())):
        raise CaptureError("invalid_model_invocation", "问答调用用量无效")
    model = result.get("model")
    if model is None:
        model = "unknown"
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model):
        raise CaptureError("invalid_model_invocation", "问答实际模型标识无效")
    return {"elapsed_seconds": elapsed, "usage": usage, "model": model}


def _answer_citation_text(text):
    """Remove supported, closed Markdown literals before checking numeric markers.

    This is a deliberately small Markdown subset, not a renderer. Unclosed
    code/link syntax stays visible so it cannot hide invalid citations.
    """
    # Mask complete fenced blocks only. A missing closing fence masks nothing.
    fence = re.compile(r"(?m)^ {0,3}(`{3,}|~{3,})[^\n]*(?:\n|$)")
    characters = list(text)
    cursor = 0
    while opening := fence.search(text, cursor):
        delimiter = opening[1]
        if delimiter[0] == "`" and "`" in opening[0].lstrip(" ")[len(delimiter):]:
            cursor = opening.end()
            continue
        closing = re.search(r"(?m)^ {0,3}" + re.escape(delimiter[0]) + "{" + str(len(delimiter))
                            + r",}[ \t]*(?:\n|$)", text[opening.end():])
        if closing is None:
            cursor = opening.end()
            continue
        end = opening.end() + closing.end()
        characters[opening.start():end] = " " * (end - opening.start())
        cursor = end
    text = "".join(characters)
    # Escapes are paired so two backslashes do not escape the following '['.
    text = re.sub(r"\\[\\`*{}\[\]()#+.!_>\-]", "  ", text)
    # Inline backtick spans on one line, with exactly matching delimiters.
    text = re.sub(r"(?<!`)(`+)(?!`)[^\n]*?(?<!`)\1(?!`)", lambda match: " " * len(match[0]), text)
    # Complete inline Markdown links/images, allowing balanced destination
    # parentheses one level deep. Reference-style labels are not exemptions.
    text = re.sub(r"!?\[[^\[\]\n]*\]\((?:[^()\n]|\([^()\n]*\))*\)",
                  lambda match: " " * len(match[0]), text)
    # Normalize only after masking: fullwidth punctuation must not manufacture
    # a Markdown code/link exemption absent from the actual answer.
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(str.maketrans({"【": "[", "】": "]", "、": ",", "，": ",",
                                       "；": ";", "–": "-", "—": "-", "−": "-"}))
    text = "".join(str(unicodedata.decimal(char)) if char.isdecimal() else char for char in text)
    return text


def _validate_answer(output):
    """Validate shape and index binding, without claiming semantic support."""
    if not _valid(output, OUTPUT_SCHEMAS["answer"]):
        raise CaptureError("invalid_answer", "回答结构不符合约定")
    count = len(output["citations"])
    number = r"[1-9][0-9]*"
    group = number + r"(?:\s*-\s*" + number + r")?"
    grammar = re.compile(group + r"(?:\s*[,;]\s*" + group + r")*")

    def reject():
        raise CaptureError("invalid_inline_citation", "正文引用编号无效或超出 citations 数组")

    def index(token):
        # Bound conversion before int(), including Python's long-integer limit.
        if len(token) > len(str(count)):
            reject()
        value = int(token)
        if not 1 <= value <= count:
            reject()
        return value

    for marker in re.finditer(r"\[([^\[\]\r\n]*)(\]?)", _answer_citation_text(output["answer"])):
        content = marker[1].strip()
        if not re.match(r"(?:[+\-^.]\s*)*[0-9]", content):
            continue
        if not marker[2] or grammar.fullmatch(content) is None:
            reject()
        for item in re.split(r"\s*[,;]\s*", content):
            endpoints = [index(token.strip()) for token in item.split("-")]
            if len(endpoints) == 2 and endpoints[0] > endpoints[1]:
                reject()


def answer(repo: Path, source_path: str, question: str, run_id: str | None = None,
           *, model_call=call_model) -> dict:
    if not isinstance(question, str) or not question.strip() or len(question.encode("utf-8")) > 16384:
        raise CaptureError("invalid_question", "问题必须为非空、有界文本")
    selected = evidence(repo, source_path, run_id)
    payload = {"question": question, "evidence": selected}
    _bounded(payload)
    try:
        result = model_call("answer", deepcopy(payload))
    finally:
        # Recheck registry physical bytes as well as HEAD even when transport
        # fails; git status alone can hide a skip-worktree change.
        guard = PipelineGit(repo)
        with guard.lock():
            _snapshot_guard(guard, selected["head"])
        if evidence(repo, source_path, run_id) != selected:
            raise CaptureError("evidence_changed", "问答期间已发送证据发生变化")
    output = result.get("output") if isinstance(result, dict) else None
    _validate_answer(output)
    metrics = _metrics(result)
    source = selected["source"]
    for citation in output["citations"]:
        if (citation["source_id"] != source["source_id"] or
                citation["source_version"] != source["source_version"]):
            raise CaptureError("invalid_citation", "回答引用超出本次来源")
        match = re.fullmatch(r"L([1-9][0-9]*)-L([1-9][0-9]*)", citation["locator"])
        if match is None or not 1 <= int(match[1]) <= int(match[2]) <= len(selected["lines"]):
            raise CaptureError("invalid_citation", "回答引用不在原文行范围内")
    if not output["citations"] and not output["limitations"]:
        raise CaptureError("unsupported_answer", "没有来源依据时必须说明资料缺口")
    git = PipelineGit(repo)
    with git.lock():
        _snapshot_guard(git, selected["head"])
        if load_source(repo, source_path) != source:
            raise CaptureError("evidence_source_mismatch", "问答期间来源发生变化")
        if run_id is not None:
            # Revalidate candidate/Review as well as exactly the formal bytes
            # sent to the model, including uncommitted physical changes.
            publication = verify_publication(repo, run_id, source_paths=[source_path])
            documents = _publication_documents(git, publication, source, selected["head"], run_id)
            expected = selected["publication"]
            if (any(publication.get(key) != expected[key] for key in
                    ("run_id", "manifest_path", "commit_sha", "review_path", "files"))
                    or documents != expected["documents"]):
                raise CaptureError("publication_mismatch", "问答期间已发送正式证据发生变化")
        _snapshot_guard(git, selected["head"])
    formal = selected["publication"]
    publication_summary = ({key: formal[key] for key in
                            ("run_id", "manifest_path", "commit_sha", "review_path", "files")}
                           if formal is not None else None)
    if formal is not None:
        if "object_origins" in formal:
            publication_summary["object_origins"] = formal["object_origins"]
        publication_summary["object_versions"] = {doc["path"]: {
            "schema_version": doc["metadata"]["schema_version"],
            "content_version": doc["metadata"].get("content_version")}
            for doc in formal["documents"]}
    return {
        **output,
        "limitations": list(dict.fromkeys(output["limitations"] + selected["limitations"])),
        "knowledge_status": selected["knowledge_status"],
        "evidence_head": selected["head"],
        "evidence_scope": selected["scope"],
        "evidence_source": {key: source.get(key) for key in
                            ("source_id", "source_version", "path", "commit_sha", "source_bytes_sha256")},
        "evidence_publication": publication_summary,
        "evidence_bytes": len(_encoded(selected)),
        "evidence_sha256": "sha256-" + hashlib.sha256(_encoded(selected)).hexdigest(),
        "input_payload_bytes": len(_encoded(payload)),
        "input_payload_sha256": "sha256-" + hashlib.sha256(_encoded(payload)).hexdigest(),
        "invocation_id": result.get("invocation_id"),
        **metrics,
    }


def ask_sources(repo: Path, source_paths: list[str], question: str, run_id: str | None = None,
                *, model_call=call_model) -> dict:
    """Answer from an explicit bounded source set, then recheck all sent bytes."""
    paths = normalize_source_paths(source_paths)
    if len(paths) == 1:
        return answer(repo, paths[0], question, run_id, model_call=model_call)
    if not isinstance(question, str) or not question.strip() or len(question.encode("utf-8")) > 16384:
        raise CaptureError("invalid_question", "问题必须为非空、有界文本")
    selected = build_evidence_sources(repo, paths, run_id)
    payload = {"question": question, "evidence": selected}
    _bounded(payload)
    try:
        result = model_call("answer", deepcopy(payload))
    finally:
        # Run even when a transport or malformed answer fails; no failed call
        # can turn a concurrent change into an apparently fixed evidence read.
        _recheck_sources(repo, selected, paths, run_id)
    output = result.get("output") if isinstance(result, dict) else None
    _validate_answer(output)
    metrics = _metrics(result)
    entries = {(entry["source"]["source_id"], entry["source"]["source_version"]): entry
               for entry in selected["sources"]}
    for citation in output["citations"]:
        entry = entries.get((citation["source_id"], citation["source_version"]))
        if entry is None:
            raise CaptureError("invalid_citation", "回答引用超出本次来源或版本")
        match = re.fullmatch(r"L([1-9][0-9]*)-L([1-9][0-9]*)", citation["locator"])
        if (entry["source"].get("metadata", {}).get("acquisition_status") == "link_only"
                or match is None or not 1 <= int(match[1]) <= int(match[2]) <= len(entry["lines"])):
            raise CaptureError("invalid_citation", "回答引用不在已取得的原文行范围内")
    if not output["citations"] and not output["limitations"]:
        raise CaptureError("unsupported_answer", "没有来源依据时必须说明资料缺口")
    formal = selected["publication"]
    publication_summary = ({key: formal[key] for key in _PUBLICATION_FIELDS}
                           if formal is not None else None)
    if formal is not None:
        if "object_origins" in formal:
            publication_summary["object_origins"] = formal["object_origins"]
        publication_summary["object_versions"] = {doc["path"]: {
            "schema_version": doc["metadata"]["schema_version"],
            "content_version": doc["metadata"].get("content_version")}
            for doc in formal["documents"]}
    return {
        **output,
        "limitations": list(dict.fromkeys(output["limitations"] + selected["limitations"])),
        "knowledge_status": selected["knowledge_status"],
        "evidence_head": selected["head"],
        "evidence_scope": selected["scope"],
        "evidence_sources": [{**{key: entry["source"].get(key) for key in
                                  ("source_id", "source_version", "path", "commit_sha", "source_bytes_sha256")},
                              "knowledge_status": entry["knowledge_status"],
                              "acquisition_status": entry["source"].get("metadata", {}).get("acquisition_status")}
                             for entry in selected["sources"]],
        "evidence_publication": publication_summary,
        "evidence_bytes": len(_encoded(selected)),
        "evidence_sha256": "sha256-" + hashlib.sha256(_encoded(selected)).hexdigest(),
        "input_payload_bytes": len(_encoded(payload)),
        "input_payload_sha256": "sha256-" + hashlib.sha256(_encoded(payload)).hexdigest(),
        "invocation_id": result.get("invocation_id"),
        **metrics,
    }


def _current_runs(run_ids):
    if run_ids is None:
        return []
    if (not isinstance(run_ids, (list, tuple)) or len(run_ids) > MAX_CURRENT_RUNS
            or any(not isinstance(run, str) or not re.fullmatch(r"run-[0-9a-f]{32}", run) for run in run_ids)
            or len(set(run_ids)) != len(run_ids)):
        raise CaptureError("invalid_run_selection", "发布运行必须是最多256个不重复的明确运行ID")
    return sorted(run_ids)


def _receipt_metadata(data):
    try:
        if not data.startswith(b"---\n"):
            raise ValueError
        header, body = data[4:].split(b"\n---\n", 1)
    except ValueError:
        raise CaptureError("receipt_mismatch", "来源回执格式无效")
    metadata = _json(header)
    if not isinstance(metadata, dict) or body:
        raise CaptureError("receipt_mismatch", "来源回执不能包含正文")
    return metadata


def _capture_metadata(git, path, head):
    """Validate a capture receipt and Git identity without opening source text."""
    match = re.fullmatch(r"memory/sources/(?:conversations|inbox)/(src-[0-9a-f]{64})/(sha256-[0-9a-f]{64})\.md", path)
    if match is None:
        raise CaptureError("path_rejected", "来源版本路径无效")
    receipt_path = f"memory/runs/captures/{match[1]}/{match[2]}.md"
    data = git._existing(receipt_path)
    if data is None or len(data) > 65536 or git.read(head, receipt_path) != data:
        raise CaptureError("receipt_mismatch", "来源回执缺失、超限或未提交")
    metadata = _receipt_metadata(data)
    commit = metadata.get("commit_sha")
    expected = {"schema_version": "0.1", "source_id": match[1], "source_version": match[2],
                "path": path, "save_result": "local_saved"}
    if any(metadata.get(key) != value for key, value in expected.items()) or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise CaptureError("receipt_mismatch", "来源回执身份或版本无效")
    git.run("merge-base", "--is-ancestor", commit, head)
    if (git.text("log", "-1", "--format=%H", head, "--", path) != commit
            or git._tree_blob(head, path) is None or git._tree_blob(commit, path) != git._tree_blob(head, path)):
        raise CaptureError("source_mismatch", "来源捕获后曾被修改或移除；未读取其正文")
    return metadata


def _unselected_updates(git, paths, decisions, head):
    """Find same-ID version gaps using receipts and Git ancestry only."""
    groups = {}
    for path in paths:
        if decisions[path]["reason"] == "delete":
            continue
        match = re.fullmatch(r"memory/sources/(conversations|inbox)/(src-[0-9a-f]{64})/(sha256-[0-9a-f]{64})\.md", path)
        groups.setdefault(match[2], []).append(path)
    missing = set()
    for source_id, selected_paths in groups.items():
        selected_commits = [_capture_metadata(git, path, head)["commit_sha"] for path in selected_paths]
        receipts = git.run("ls-tree", "-r", "--name-only", head, "--", f"memory/runs/captures/{source_id}").decode().splitlines()
        if len(receipts) > 128:
            raise CaptureError("source_version_limit", "同来源版本元数据超限，不能确认当前范围")
        for receipt in receipts:
            if Path(receipt).name in {"README.md", ".gitkeep"}:
                continue
            data = git._existing(receipt)
            if data is None or len(data) > 65536 or git.read(head, receipt) != data:
                raise CaptureError("receipt_mismatch", "同来源回执缺失、超限或未提交")
            metadata = _receipt_metadata(data)
            candidate = metadata.get("path")
            if not isinstance(candidate, str) or not re.fullmatch(r"memory/sources/(conversations|inbox)/" + re.escape(source_id) + r"/sha256-[0-9a-f]{64}\.md", candidate):
                raise CaptureError("receipt_mismatch", "同来源回执路径不匹配")
            if receipt != f"memory/runs/captures/{source_id}/{metadata.get('source_version')}.md":
                raise CaptureError("receipt_mismatch", "同来源回执文件名不匹配")
            if candidate in selected_paths:
                continue
            candidate_metadata = _capture_metadata(git, candidate, head)
            older = False
            for selected_commit in selected_commits:
                try:
                    git.run("merge-base", "--is-ancestor", candidate_metadata["commit_sha"], selected_commit)
                    older = True
                    break
                except CaptureError:
                    pass
            if not older:
                missing.add(candidate)
    return sorted(missing)


def _peek_publication(git, run_id, head):
    """Inspect bindings before the verifier may open any selected source body."""
    path = f"memory/runs/processes/{run_id}/manifest.md"
    data = git._existing(path)
    if data is None or git.read(head, path) != data or len(data) > MAX_EVIDENCE_BYTES:
        raise CaptureError("publication_mismatch", "所选发布清单缺失、未提交或超限")
    metadata, body = parse_markdown(data)
    value = _json(body)
    batch_group = metadata.get("schema_version") == "0.6" and metadata.get("kind") == "batch_group_manifest"
    ordinary = metadata.get("kind") == "manifest" and metadata.get("schema_version") in {"0.2", "0.3", M1_SCHEMA_VERSION, "0.6", "0.7", "0.8", "0.9", QUALITY_SCHEMA_VERSION}
    if ((not ordinary and not batch_group) or metadata.get("run_id") != run_id
            or not isinstance(value, dict) or value.get("run_id") != run_id):
        raise CaptureError("publication_mismatch", "所选发布清单绑定无效")
    additional = value.get("additional_sources", [])
    if not isinstance(additional, list):
        raise CaptureError("publication_mismatch", "所选发布来源绑定无效")
    bindings = [value.get("source"), *additional]
    if any(not isinstance(binding, dict) for binding in bindings):
        raise CaptureError("publication_mismatch", "所选发布来源绑定无效")
    batch = ordinary and metadata["schema_version"] in {"0.6", "0.7", "0.8"}
    revision = ordinary and metadata["schema_version"] == "0.9"
    quality = ordinary and metadata["schema_version"] == QUALITY_SCHEMA_VERSION
    incremental = metadata.get("schema_version") in {M1_SCHEMA_VERSION, "0.6", "0.7", "0.8", "0.9"}
    if batch or revision or quality:
        # A version label alone cannot expand legacy limits. Inspect only the
        # committed candidate's declared bindings here; full Review/source
        # verification remains after the caller checks this returned scope.
        from .pipeline import (_candidate_binding, _candidate_bindings, _committed, _load,
                               _pipeline_schema, _validated_skill_bundle)
        candidate = _load(_committed(git, f"memory/staging/{run_id}.md"), "candidate", metadata["schema_version"])
        if revision or quality:
            batch = "batch_context" in candidate
        if (candidate.get("run_id") != run_id or batch and not isinstance(candidate.get("batch_context"), dict)
                or _pipeline_schema(candidate) != metadata["schema_version"]
                or candidate.get("binding") != _candidate_binding(candidate)
                or value.get("binding") != candidate["binding"]
                or _candidate_bindings(candidate) != bindings
                or metadata["schema_version"] == "0.6" and "continuation_context" in candidate
                or metadata["schema_version"] == "0.7" and not isinstance(candidate.get("continuation_context"), dict)
                or metadata["schema_version"] == "0.9" and not isinstance(candidate.get("revision_context"), dict)
                or metadata["schema_version"] == "0.8" and "revision_context" in candidate
                or metadata["schema_version"] == "0.8" and (
                    candidate.get("review_strategy") not in {"auto", "decomposed-v1"}
                    or "continuation_context" in candidate and not isinstance(candidate["continuation_context"], dict))):
            raise CaptureError("publication_mismatch", "批次发布须绑定真实候选与对应续接上下文；未补读来源正文")
        if quality:
            # Quality is orthogonal to ordinary/batch/revision processing.
            # A schema label must not grant batch limits or historical-source
            # access. Read only frozen declarations here, never source bodies.
            _validated_skill_bundle(candidate["skill_bundle"])
            incremental = candidate.get("mode") == "incremental"
            revision = "revision_context" in candidate
            continuation = "continuation_context" in candidate
            strategy = candidate.get("review_strategy", "legacy-v1")
            if (not isinstance(candidate.get("quality_context"), dict)
                    or not isinstance(candidate.get("extraction_request_binding"), dict)
                    or not isinstance(strategy, str)
                    or strategy not in {"legacy-v1", "auto", "decomposed-v1"}
                    or strategy != "legacy-v1" and not batch
                    or (batch or revision) and not incremental
                    or revision and (not isinstance(candidate["revision_context"], dict) or continuation)
                    or continuation and (not batch or not isinstance(candidate["continuation_context"], dict))
                    or batch and (not isinstance(candidate.get("source_views"), list)
                                  or not candidate["source_views"]
                                  or not isinstance(candidate.get("batch_material"), dict)
                                  or set(candidate["batch_context"]) != {
                                      "plan_id", "batch_id", "source_ranges", "parent_batch_ids"}
                                  or not isinstance(candidate["batch_context"]["source_ranges"], list)
                                  or not candidate["batch_context"]["source_ranges"])):
                raise CaptureError("publication_mismatch", "质量发布须绑定真实加工模式与上下文；未补读来源正文")
    metadata["incremental_sources"] = incremental
    files = value.get("files")
    maximum = (MAX_GROUP_PUBLICATION_DOCUMENTS if batch_group else
               MAX_BATCH_PUBLICATION_DOCUMENTS if batch else MAX_PUBLICATION_DOCUMENTS)
    if (not isinstance(files, dict) or not 1 <= len(files) <= maximum
            or any(not isinstance(path, str) or not _OBJECT_PATH.fullmatch(path) for path in files)):
        raise CaptureError("publication_mismatch", "所选发布正式对象范围无效")
    commit = git.text("log", "-1", "--format=%H", head, "--", path)
    metadata["current_files_match"] = all(git._tree_blob(head, target) == git._tree_blob(commit, target) for target in files)
    return normalize_source_paths([binding.get("path") for binding in bindings], allow_versions=True,
                                  max_sources=MAX_CURRENT_SOURCES if batch or batch_group else 8), metadata


def _view_document(git, publication, path, expected, sources, controls, head, run_id, history, as_of=None,
                   provided_lines=None):
    if not isinstance(path, str) or not _OBJECT_PATH.fullmatch(path) or not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise CaptureError("path_rejected", "正式证据路径或摘要无效")
    purpose = "history" if history else "current"
    if object_policy(controls, path, purpose=purpose, as_of=as_of)["reason"] == "delete":
        return None
    if history:
        data = git.read(publication["commit_sha"], path)
    else:
        metadata = _metadata_only(git, path)
        if object_policy(controls, path, content_version=metadata.get("content_version"), as_of=as_of)["excluded"]:
            return None
        selected = {(source["source_id"], source["source_version"]): source for source in sources}
        refs = metadata.get("sources")
        if not isinstance(refs, list) or not refs:
            raise CaptureError("evidence_source_mismatch", "正式证据缺少来源")
        if any(not isinstance(ref, dict) or (ref.get("source_id"), ref.get("source_version")) not in selected for ref in refs):
            return None
        data = git._existing(path)
        if data is None or git.read(head, path) != data:
            raise CaptureError("publication_mismatch", "正式证据不属于固定当前版本")
    if "sha256-" + hashlib.sha256(data).hexdigest() != expected or git.read(publication["commit_sha"], path) != data:
        raise CaptureError("publication_mismatch", "正式证据不属于受审发布版本")
    metadata, body = parse_markdown(data)
    origin = publication.get("object_origins", {}).get(path, {"run_id": run_id, "review_path": publication["review_path"]})
    refs = metadata.get("sources")
    if provided_lines is not None and isinstance(refs, list) and any(
            not _formal_reference_matches(ref, None, sources, provided_lines) for ref in refs):
        return None
    if not history and isinstance(refs, list):
        by_identity = {(source["source_id"], source["source_version"]): source for source in sources}
        if any(isinstance(ref, dict) and (ref.get("source_id"), ref.get("source_version")) in by_identity
               and source_locator_policy(controls, by_identity[(ref["source_id"], ref["source_version"])]["path"],
                                         ref.get("locator"))["excluded"] for ref in refs):
            return None
    if (metadata.get("schema_version") != PIPELINE_SCHEMA_VERSION or metadata.get("review_status") != "passed"
            or metadata.get("review_ref") != origin["review_path"]
            or not isinstance(metadata.get("generated_by"), dict)
            or metadata["generated_by"].get("run_id") != origin["run_id"]
            or not isinstance(refs, list) or not refs
            or any(not _formal_reference_matches(ref, None, sources, provided_lines) for ref in refs)):
        raise CaptureError("evidence_source_mismatch", "正式证据不属于所选受审来源")
    return {"path": path, "sha256": expected, "metadata": metadata, "body": body}


def _object_selection_paths(value):
    if value is None:
        return None
    if (not isinstance(value, list) or not 1 <= len(value) <= MAX_GROUP_PUBLICATION_DOCUMENTS
            or any(not isinstance(path, str) or not _OBJECT_PATH.fullmatch(path) for path in value)
            or len(set(value)) != len(value)):
        raise CaptureError("invalid_object_selection", "选择须为不重复的正式记录或主题路径")
    return sorted(value)


def _selected_object_publications(git, paths, runs, requested, decisions, controls, head,
                                  history, full_sources, provided_lines):
    """Verify publication authority before projecting an explicit object closure.

    Complete publication/source proof stays on the program side. Only selected
    objects and the exact record versions referenced by selected topics are
    returned; an excluded or unavailable dependency cannot become a partial
    successful selection.
    """
    publications, candidates, skipped = {}, {}, []
    for run in runs:
        bound, metadata = _peek_publication(git, run, head)
        if not set(bound) <= set(paths):
            raise CaptureError("evidence_source_mismatch", "发布运行引用了选择范围外的来源；未读取其正文")
        reason = None
        if any(decisions[path]["reason"] == "delete" for path in bound):
            reason = "deleted_source"
        elif not history and not metadata["current_files_match"]:
            reason = "historical_publication"
        elif any(decisions[path]["excluded"] for path in bound) and not metadata["incremental_sources"]:
            reason = "corrected_or_superseded_source"
        if reason:
            skipped.append({"run_id": run, "reason": reason})
            continue
        publication = verify_publication(git.repo, run, source_paths=bound, historical=history)
        publications[run] = publication
        for path in bound:
            if not decisions[path]["excluded"] and path not in full_sources:
                full_sources[path] = load_source(git.repo, path, purpose="history" if history else "current")
        for path, expected in publication["files"].items():
            candidates.setdefault(path, []).append((run, expected))

    documents, origins, required_versions = {}, {}, {}
    queue = list(requested)
    while queue:
        path = queue.pop(0)
        if path in documents:
            continue
        matches = []
        for run, expected in candidates.get(path, []):
            document = _view_document(git, publications[run], path, expected,
                                      list(full_sources.values()), controls, head, run, history,
                                      provided_lines=provided_lines)
            if document is not None:
                matches.append((run, document))
        if not matches:
            raise CaptureError("object_selection_unavailable", "所选对象或主题依赖不存在、已被排除，或缺少必要来源行")
        if len({document["sha256"] for _, document in matches}) != 1:
            raise CaptureError("object_selection_conflict", "所选路径对应多个历史版本，须收窄发布运行范围")
        run, document = matches[0]
        documents[path], origins[path] = document, run
        if path.startswith("memory/topics/"):
            refs = document["metadata"].get("records")
            if not isinstance(refs, list) or not refs:
                raise CaptureError("object_dependency_mismatch", "所选主题缺少精确记录依赖")
            for ref in refs:
                if (not isinstance(ref, dict) or set(ref) != {"id", "content_version"}
                        or not isinstance(ref["id"], str)
                        or not _OBJECT_PATH.fullmatch("memory/records/" + ref["id"] + ".md")
                        or not isinstance(ref["content_version"], str) or not _HASH.fullmatch(ref["content_version"])):
                    raise CaptureError("object_dependency_mismatch", "所选主题记录依赖无效")
                dependency = "memory/records/" + ref["id"] + ".md"
                if dependency in required_versions and required_versions[dependency] != ref["content_version"]:
                    raise CaptureError("object_dependency_mismatch", "所选主题引用了冲突的记录版本")
                required_versions[dependency] = ref["content_version"]
                queue.append(dependency)
    for path, version in required_versions.items():
        metadata = documents[path]["metadata"]
        if metadata.get("content_version") != version or path != "memory/records/" + metadata.get("id", "") + ".md":
            raise CaptureError("object_dependency_mismatch", "所选主题依赖的精确记录版本未提供")

    selected, needed_lines = [], {}
    by_identity = {(source["source_id"], source["source_version"]): source for source in full_sources.values()}
    for run, publication in publications.items():
        included = [documents[path] for path in sorted(documents) if origins[path] == run]
        if not included:
            continue
        item = {key: publication[key] for key in _PUBLICATION_FIELDS}
        item["files"] = {document["path"]: document["sha256"] for document in included}
        if "object_origins" in publication:
            item["object_origins"] = {path: origin for path, origin in publication["object_origins"].items()
                                      if path in item["files"]}
        item["documents"] = included
        selected.append(item)
        for document in included:
            for ref in document["metadata"]["sources"]:
                source = by_identity[(ref["source_id"], ref["source_version"])]
                match = re.fullmatch(r"L([1-9][0-9]*)-L([1-9][0-9]*)", ref["locator"])
                start, end = map(int, match.groups())  # already verified against the complete source
                needed_lines.setdefault(source["path"], set()).update(range(start, end + 1))
    return selected, needed_lines, sorted(set(documents) - set(requested)), skipped, publications


def _object_reference_fragments(source, numbers):
    from .batching import _fragment
    ranges, start, end = [], None, None
    for number in sorted(numbers):
        if end is not None and number != end + 1:
            ranges.append((start, end))
            start = None
        if start is None:
            start = number
        end = number
    if start is not None:
        ranges.append((start, end))
    lines = source["body"].splitlines()
    return [_fragment(source, start, end, "object_reference", source_lines=lines) for start, end in ranges]


def build_current_evidence(repo: Path, source_paths: list[str], run_ids=None, *,
                           history=False, max_bytes=MAX_EVIDENCE_BYTES,
                           source_views=None, batch_context=None, object_paths=None,
                           compact_sources=False) -> dict:
    """Build a controlled current view inside an explicit source/run boundary.

    Exclusions and whole-source budget omissions are reported as coverage gaps.
    Replacement links never authorize reading an unselected source. Historical
    mode permits corrected evidence but never permits deleted evidence.
    Explicit object selection includes exact topic record dependencies and only
    their referenced original lines. Compact mode removes the duplicate source
    body while retaining original line coordinates. Both options are opt-in;
    None/False preserves the legacy payload and budget behavior.
    """
    if type(history) is not bool or type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EVIDENCE_BYTES:
        raise CaptureError("invalid_evidence_budget", "证据预算或历史模式无效")
    requested_objects = _object_selection_paths(object_paths)
    if type(compact_sources) is not bool:
        raise CaptureError("invalid_object_selection", "原文压缩标记必须为布尔值")
    paths = normalize_source_paths(source_paths, allow_versions=True, max_sources=MAX_CURRENT_SOURCES)
    fragmented = source_views is not None or batch_context is not None
    if fragmented and (source_views is None or batch_context is None):
        raise CaptureError("invalid_batch_context", "片段视图与范围上下文必须同时提供")
    runs = _current_runs(run_ids)
    if requested_objects is not None and not runs:
        raise CaptureError("invalid_object_selection", "选择正式对象须明确指定其发布运行")
    git = PipelineGit(repo)
    with git.lock():
        head = git.head()
        _snapshot_guard(git, head)
        controls = read_controls(repo, head=head)
        purpose = "history" if history else "current"
        decisions = {path: source_policy(controls, path, purpose=purpose) for path in paths}
        views, full_sources = {}, {}
        if fragmented:
            from .batching import validate_source_views
            if (not isinstance(source_views, list) or not source_views
                    or any(not isinstance(view, dict) or not isinstance(view.get("path"), str)
                           or view["path"] not in paths for view in source_views)):
                raise CaptureError("batch_scope_mismatch", "片段读取不得扩大明确来源范围")
            for view in source_views:
                if decisions[view["path"]]["excluded"]:
                    raise CaptureError("controlled_source_view", "片段包含已被控制排除的原文")
                if not history and isinstance(view.get("fragments"), list) and any(
                        isinstance(fragment, dict) and source_locator_policy(controls, view["path"], fragment.get("locator"))["excluded"]
                        for fragment in view["fragments"]):
                    raise CaptureError("controlled_source_view", "片段包含被对象级控制排除的旧行")
            full_sources = {view["path"]: load_source(repo, view["path"], purpose=purpose) for view in source_views}
            validate_source_views(list(full_sources.values()), source_views, batch_context)
            views = {view["path"]: deepcopy(view) for view in source_views}
        relevant = [event for event in controls["events"]
                    if any(event["control_id"] in decision["control_ids"] for decision in decisions.values())]
        excluded = [{"path": path, **decision} for path, decision in decisions.items() if decision["excluded"]]
        missing = sorted({replacement for decision in decisions.values() for replacement in decision["replacement_paths"]
                          if replacement not in paths})
        if not history:
            # A future replacement is context, not evidence of today's state.
            # Its still-current predecessor remains required metadata coverage
            # even when only the replacement was explicitly selected.
            missing = sorted(set(missing) | {
                predecessor for event in relevant
                if event["action"] == "supersedes" and _temporal(event)["not_yet_effective"]
                and set(event["replacement_source_paths"]) & set(paths)
                for predecessor in event["source_paths"] if predecessor not in paths})
        missing = sorted(set(missing) | set(_unselected_updates(git, paths, decisions, head)))
        coverage = {"requested_source_paths": paths, "read_source_paths": [], "unread_source_paths": [],
                    "examined_not_included_source_paths": [], "excluded_sources": excluded,
                    "missing_update_paths": missing, "requested_runs": runs, "read_runs": [],
                    "unread_runs": [], "excluded_documents": [], "proof_history_source_paths": [],
                    "temporal_undetermined_source_paths": [path for path, decision in decisions.items() if decision["temporal_undetermined"]],
                    "not_yet_effective_source_paths": [path for path, decision in decisions.items() if decision["not_yet_effective"]],
                    "budget_exhausted": False, "current_complete": False}
        result = {"head": head, "scope": "explicit_history_view" if history else "explicit_current_view",
                  "knowledge_status": "unprocessed_or_unverified", "sources": [], "publications": [],
                  "controls": {"digest": controls["digest"], "events": relevant}, "coverage": coverage,
                  "limitations": ["仅检查明确选择的来源与发布运行及现存控制，不宣称查遍全库或确定全局最新状态。",
                                  "未加工或未审核原文可参与回答；模型Review不等于独立事实核验。",
                                  "正式记录与主题页是原文派生知识，不是新增独立事实；回答不保存为个人记忆。"]}
        selected_publications, object_lines, verified_publications = None, {}, {}
        if requested_objects is not None:
            supplied = ({(source["source_id"], source["source_version"]):
                         {line["line"]: line["text"] for fragment in views[path]["fragments"] for line in fragment["lines"]}
                         for path, source in full_sources.items()} if fragmented else None)
            selected_publications, object_lines, dependencies, skipped, verified_publications = _selected_object_publications(
                git, paths, runs, requested_objects, decisions, controls, head, history, full_sources, supplied)
            read_objects = sorted(set(requested_objects) | set(dependencies))
            coverage.update(requested_object_paths=requested_objects, dependency_object_paths=dependencies,
                            read_object_paths=read_objects,
                            not_selected_object_paths=sorted({path for publication in verified_publications.values()
                                                              for path in publication["files"]} - set(read_objects)),
                            object_selection_complete=True,
                            not_needed_source_paths=[path for path in paths if path not in object_lines and not decisions[path]["excluded"]],
                            not_needed_runs=[run for run in verified_publications
                                             if run not in {publication["run_id"] for publication in selected_publications}],
                            fragment_incomplete_source_paths=[], fragment_missing_ranges={}, provided_source_ranges=[])
            coverage["unread_runs"].extend(skipped)
            result["limitations"].append("正式知识仅提供明确所选对象及主题引用的精确记录依赖；原文仅提供这些对象实际引用的完整行范围，不表示查遍所选来源或全库。")
        partial = [path for path, decision in decisions.items() if decision.get("object_restrictions")]
        if fragmented and requested_objects is None:
            coverage.update(fragment_incomplete_source_paths=[], fragment_missing_ranges={},
                            provided_source_ranges=deepcopy(batch_context["source_ranges"]))
            result["batch_context"] = deepcopy(batch_context)
            result["limitations"].append("本次仅提供明确片段及全文实际行号；未读范围不表示资料中不存在，不能据此宣称检查完整原文。")
        if partial:
            coverage["object_controlled_source_paths"] = partial
            result["limitations"].append("对象级控制仅约束列明对象旧版本及原文locator；同源其他事实仍可用。受控旧行只作纠正历史上下文，禁止无条件用作当前事实依据，当前回答须引用未受限依据或补充来源。")
        for path in paths:
            if decisions[path]["excluded"]:
                continue
            if requested_objects is not None and path not in object_lines:
                continue
            if fragmented and path not in views:
                coverage["unread_source_paths"].append(path)
                continue
            # Avoid opening a large source when even its physical bytes exceed
            # the remaining budget. A source is never sliced or truncated.
            remaining = max_bytes - len(_encoded(result))
            if not fragmented and not compact_sources and requested_objects is None and git.path(path).stat().st_size > remaining:
                coverage["unread_source_paths"].append(path)
                coverage["budget_exhausted"] = True
                continue
            original = (full_sources[path] if fragmented or requested_objects is not None
                        else load_source(repo, path, purpose=purpose))
            if compact_sources:
                full_sources[path] = original
            source = deepcopy(original)
            # Model-facing evidence uses the explicit relevant control view
            # below, not the loader's internal object-validation context.
            source.pop("object_controls", None)
            lines = [{"line": index, "text": line} for index, line in enumerate(source["body"].splitlines(), 1)]
            view_marker = None
            if requested_objects is not None:
                source_line_count = len(lines)
                numbers = object_lines[path]
                missing_ranges = _missing_line_ranges(source_line_count, numbers)
                if missing_ranges:
                    coverage["fragment_incomplete_source_paths"].append(path)
                    coverage["fragment_missing_ranges"][path] = missing_ranges
                lines = [line for line in lines if line["line"] in numbers]
                source["body"] = ""
                source["content_scope"] = "fragment"
                fragments = _object_reference_fragments(original, numbers)
                view_marker = {"kind": "fragment", "source_line_count": source_line_count,
                               "fragments": [{key: fragment[key] for key in ("fragment_id", "locator")}
                                             for fragment in fragments]}
                coverage["provided_source_ranges"].extend(
                    {key: fragment[key] for key in ("path", "source_id", "source_version", "locator", "fragment_id")}
                    for fragment in fragments)
            elif fragmented:
                selected_lines = {line["line"]: line for fragment in views[path]["fragments"] for line in fragment["lines"]}
                missing_ranges = _missing_line_ranges(len(lines), selected_lines)
                if missing_ranges:
                    coverage["fragment_incomplete_source_paths"].append(path)
                    coverage["fragment_missing_ranges"][path] = missing_ranges
                source_line_count = len(lines)
                lines = [selected_lines[number] for number in sorted(selected_lines)]
                source["body"] = ""
                source["content_scope"] = "fragment"
                view_marker = {"kind": "fragment", "source_line_count": source_line_count,
                               "fragments": [{key: fragment[key] for key in ("fragment_id", "locator")}
                                for fragment in views[path]["fragments"]]}
            elif compact_sources:
                source["body"] = ""
                source["content_scope"] = "provided_lines"
            entry = {"source": source, "lines": lines,
                     "knowledge_status": "unprocessed_or_unverified", "source_policy": decisions[path]}
            if view_marker is not None:
                entry["source_view"] = view_marker
            result["sources"].append(entry)
            if len(_encoded(result)) > max_bytes:
                if requested_objects is not None:
                    raise CaptureError("selected_evidence_limit", "所选对象所需完整证据超过预算；未调用模型")
                result["sources"].pop()
                coverage["examined_not_included_source_paths"].append(path)
                coverage["unread_source_paths"].append(path)
                coverage["budget_exhausted"] = True
            else:
                coverage["read_source_paths"].append(path)
        included = {entry["source"]["path"] for entry in result["sources"]}
        for run_id in runs if selected_publications is None else []:
            bound_paths, metadata = _peek_publication(git, run_id, head)
            if not set(bound_paths) <= set(paths):
                raise CaptureError("evidence_source_mismatch", "发布运行引用了选择范围外的来源；未读取其正文")
            if any(decisions[path]["reason"] == "delete" for path in bound_paths):
                coverage["unread_runs"].append({"run_id": run_id, "reason": "deleted_source"})
                continue
            if not history and not metadata["current_files_match"]:
                coverage["unread_runs"].append({"run_id": run_id, "reason": "historical_publication"})
                continue
            suppressed = [path for path in bound_paths if decisions[path]["excluded"]]
            if suppressed and not metadata["incremental_sources"]:
                coverage["unread_runs"].append({"run_id": run_id, "reason": "corrected_or_superseded_source"})
                continue
            if any(path not in included and path not in suppressed for path in bound_paths):
                coverage["unread_runs"].append({"run_id": run_id, "reason": "source_budget"})
                continue
            publication = verify_publication(repo, run_id, source_paths=bound_paths, historical=history)
            selected = {key: publication[key] for key in _PUBLICATION_FIELDS}
            if "object_origins" in publication:
                selected["object_origins"] = publication["object_origins"]
            selected["documents"] = []
            sources = [entry["source"] for entry in result["sources"]]
            reference_sources = [full_sources[source["path"]] for source in sources] if fragmented or compact_sources else sources
            provided_lines = ({(entry["source"]["source_id"], entry["source"]["source_version"]):
                              {line["line"]: line["text"] for line in entry["lines"]} for entry in result["sources"]}
                              if fragmented else None)
            for path, expected in sorted(publication["files"].items()):
                document = _view_document(git, publication, path, expected, reference_sources, controls, head, run_id, history,
                                          provided_lines=provided_lines)
                if document is None:
                    coverage["excluded_documents"].append(path)
                    continue
                selected["documents"].append(document)
            result["publications"].append(selected)
            if len(_encoded(result)) > max_bytes:
                result["publications"].pop()
                coverage["unread_runs"].append({"run_id": run_id, "reason": "publication_budget"})
                coverage["budget_exhausted"] = True
            else:
                coverage["read_runs"].append(run_id)
                coverage["proof_history_source_paths"].extend(path for path in suppressed
                                                              if path not in coverage["proof_history_source_paths"])
        if selected_publications is not None:
            result["publications"] = selected_publications
            coverage["read_runs"] = [publication["run_id"] for publication in selected_publications]
            for run in coverage["read_runs"]:
                publication = verified_publications[run]
                coverage["proof_history_source_paths"].extend(
                    ref["path"] for ref in [publication["source"], *publication.get("additional_sources", [])]
                    if decisions[ref["path"]]["excluded"] and ref["path"] not in coverage["proof_history_source_paths"])
            if len(_encoded(result)) > max_bytes:
                raise CaptureError("selected_evidence_limit", "所选正式对象及必要依据超过预算；未调用模型")
        published = {(ref["source_id"], ref["source_version"])
                     for publication in result["publications"] for document in publication["documents"]
                     for ref in document["metadata"]["sources"]}
        for entry in result["sources"]:
            if (entry["source"]["source_id"], entry["source"]["source_version"]) in published:
                entry["knowledge_status"] = "published"
        if published:
            result["knowledge_status"] = "published"
        coverage["current_complete"] = not (history or missing or coverage["unread_source_paths"] or coverage["unread_runs"]
                                               or coverage["excluded_documents"] or coverage["temporal_undetermined_source_paths"]
                                               or coverage.get("fragment_incomplete_source_paths") or coverage.get("not_needed_source_paths")
                                               or coverage.get("not_selected_object_paths"))
        if excluded:
            result["limitations"].append("当前视图已排除被纠正、替代或删除的来源；禁止用其原文恢复旧错误或被删除内容。")
        if missing:
            result["limitations"].append("存在未获本次读取范围覆盖的相关更新或当前依据，不能确认这些问题的当前答案。")
        if coverage["budget_exhausted"]:
            result["limitations"].append("完整证据超出本次预算，已列出未提供的整份资料或运行，未截断正文。")
        if history:
            result["limitations"].append("本次为显式历史视图；旧错误和历史状态不能当作当前事实，删除控制仍然生效。")
        if coverage["not_yet_effective_source_paths"]:
            result["limitations"].append("存在尚未到已知生效年月或日期的替代关系，旧资料仍可用；替换资料只作未来上下文，不能当作已经发生。")
        if coverage["temporal_undetermined_source_paths"]:
            result["limitations"].append("替代关系的生效时间未知，不能仅按采集时间或消息顺序裁定当前状态。")
        # Partial original text cannot justify exposing an entire pending
        # question/reply context. Omit it and let the existing helper report a
        # budget/scope gap rather than leak an unread fragment through pending.
        pending_sources = [full_sources.get(entry["source"]["path"], entry["source"]) for entry in result["sources"]
                           if entry["source"]["path"] not in coverage.get("fragment_incomplete_source_paths", [])]
        pending = build_pending_context(git, head, paths, pending_sources,
                                        runs, history=history, publications=(list(verified_publications.values())
                                                                           if requested_objects is not None else result["publications"]))
        if pending is not None:
            result["pending"] = pending
            result["limitations"].extend(pending["limitations"])
            coverage["pending_complete"] = pending["coverage"]["complete"]
            coverage["current_complete"] &= coverage["pending_complete"]
        if len(_encoded(result)) > max_bytes:
            if requested_objects is not None:
                raise CaptureError("selected_evidence_limit", "所选证据与必要范围说明超过预算；未调用模型")
            raise CaptureError("evidence_limit", "控制与范围说明已超证据预算；未调用模型")
        _snapshot_guard(git, head)
        if read_controls(repo)["digest"] != controls["digest"]:
            raise CaptureError("controls_changed", "读取期间控制发生变化")
        return result


def ask_current(repo: Path, source_paths: list[str], question: str, run_ids=None, *, history=False,
                max_bytes=MAX_EVIDENCE_BYTES, model_call=call_model, source_views=None, batch_context=None,
                object_paths=None, compact_sources=False) -> dict:
    if not isinstance(question, str) or not question.strip() or len(question.encode()) > 16384:
        raise CaptureError("invalid_question", "问题必须为非空、有界文本")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EVIDENCE_BYTES:
        raise CaptureError("invalid_evidence_budget", "证据预算无效")
    evidence_budget = max_bytes - len(_encoded({"question": question, "evidence": {}}))
    if evidence_budget < 1:
        raise CaptureError("evidence_limit", "问题已超过证据预算")
    projection = ({"object_paths": object_paths, "compact_sources": compact_sources}
                  if object_paths is not None or compact_sources is not False else {})
    selected = build_current_evidence(repo, source_paths, run_ids, history=history, max_bytes=evidence_budget,
                                      source_views=source_views, batch_context=batch_context, **projection)
    payload = {"question": question, "evidence": selected}
    if len(_encoded(payload)) > max_bytes:
        raise CaptureError("evidence_limit", "完整问答输入超过预算")
    try:
        result = model_call("answer", deepcopy(payload))
    finally:
        checked = build_current_evidence(repo, source_paths, run_ids, history=history, max_bytes=evidence_budget,
                                         source_views=source_views, batch_context=batch_context, **projection)
        if checked != selected:
            raise CaptureError("evidence_changed", "问答期间证据或控制发生变化")
    output = result.get("output") if isinstance(result, dict) else None
    _validate_answer(output)
    entries = {(entry["source"]["source_id"], entry["source"]["source_version"]): entry for entry in selected["sources"]}
    for citation in output["citations"]:
        entry = entries.get((citation["source_id"], citation["source_version"]))
        match = re.fullmatch(r"L([1-9][0-9]*)-L([1-9][0-9]*)", citation["locator"])
        if (entry is None or entry["source"]["metadata"].get("acquisition_status") == "link_only"
                or match is None or any(len(number) > 9 for number in match.groups())
                or not 1 <= int(match[1]) <= int(match[2])
                or int(match[2]) - int(match[1]) + 1 > len(entry["lines"])
                or not all(number in {line["line"] for line in entry["lines"]}
                           for number in range(int(match[1]), int(match[2]) + 1))):
            raise CaptureError("invalid_citation", "回答引用了未提供、已排除或无效行范围的来源")
        if not history and source_locator_policy({"events": selected["controls"]["events"]},
                                                entry["source"]["path"], citation["locator"])["excluded"]:
            raise CaptureError("invalid_citation", "受对象级纠正控制的旧原文行不能作为当前事实依据；请引用补充或未受限来源")
    if not output["citations"] and not output["limitations"]:
        raise CaptureError("unsupported_answer", "没有来源依据时必须说明资料缺口")
    return {**output, "limitations": list(dict.fromkeys(output["limitations"] + selected["limitations"])),
            "knowledge_status": selected["knowledge_status"], "evidence_head": selected["head"],
            "evidence_scope": selected["scope"], "coverage": selected["coverage"],
            "controls_digest": selected["controls"]["digest"],
            "evidence_sources": [{key: entry["source"].get(key) for key in
                                  ("source_id", "source_version", "path", "commit_sha", "source_bytes_sha256")}
                                 for entry in selected["sources"]],
            "evidence_publications": [{**{key: publication[key] for key in _PUBLICATION_FIELDS},
                                       **({"object_origins": publication["object_origins"]} if "object_origins" in publication else {})}
                                      for publication in selected["publications"]],
            "evidence_bytes": len(_encoded(selected)), "evidence_sha256": "sha256-" + hashlib.sha256(_encoded(selected)).hexdigest(),
            "input_payload_bytes": len(_encoded(payload)), "input_payload_sha256": "sha256-" + hashlib.sha256(_encoded(payload)).hexdigest(),
            "invocation_id": result.get("invocation_id"), **_metrics(result)}
