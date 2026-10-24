"""统一待确认清单：稳定事项、已采集答复及经过发布核验的完成状态。"""

import copy
import hashlib
import json
import re
from datetime import datetime

from .contracts import CaptureError, M1_SCHEMA_VERSION, QUALITY_SCHEMA_VERSION, canonical, markdown, timestamp
from .transaction import PipelineGit

REGISTRY_PATH = "memory/pending.md"
EXPANDED_REGISTRY_SCHEMA = "0.7"
MAX_SOURCE_REFS = 256
MAX_OBJECT_REFS = 256
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_ITEMS = 1000
STATUSES = frozenset({"open", "answered", "resolved", "withdrawn"})
_SOURCE = re.compile(r"memory/sources/(?:conversations|inbox)/(src-[0-9a-f]{64})/(sha256-[0-9a-f]{64})\.md\Z")
_OBJECT = re.compile(r"memory/(?:records|topics)/[a-z][a-z0-9-]{0,95}\.md\Z")
_ID = re.compile(r"[a-z][a-z0-9-]{0,95}\Z")
_VERSION = re.compile(r"sha256-[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_RUN = re.compile(r"run-[0-9a-f]{32}\Z")
_REF_FIELDS = {"source_id", "source_version", "path"}
_REPLY_FIELDS = _REF_FIELDS | {"commit_sha", "associated_head"}
_ITEM_FIELDS = {"item_id", "key", "question", "source_refs", "object_refs", "run_ids", "pending_status",
                "reply_sources", "resolution", "created_at", "updated_at"}


def _error(code="invalid_pending_data"):
    raise CaptureError(code, "待确认事项校验未通过；保留已有资料，核对后重试")


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        _error()
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _error()
        result[key] = value
    return result


def _json(value):
    try:
        return json.loads(value, object_pairs_hook=_unique, parse_constant=lambda _: _error())
    except (ValueError, TypeError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _error()


def _parse(data):
    try:
        if not isinstance(data, bytes) or len(data) > MAX_REGISTRY_BYTES or not data.startswith(b"---\n"):
            _error()
        header, body = data.decode("utf-8")[4:].split("\n---\n", 1)
        metadata, value = _json(header), _json(body)
        if (not isinstance(metadata, dict) or set(metadata) != {"schema_version", "kind"}
                or metadata["schema_version"] not in {M1_SCHEMA_VERSION, EXPANDED_REGISTRY_SCHEMA}
                or metadata["kind"] != "pending_registry"):
            _error()
        if not isinstance(value, dict) or set(value) != {"items"}:
            _error()
        _validate_items(value["items"], schema=metadata["schema_version"])
        return value
    except (UnicodeError, ValueError, TypeError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _error()


def _source_ref(value, *, reply=False):
    fields = _REPLY_FIELDS if reply else _REF_FIELDS
    if not isinstance(value, dict) or set(value) != fields:
        _error()
    match = _SOURCE.fullmatch(value["path"]) if isinstance(value["path"], str) else None
    if not match or (value["source_id"], value["source_version"]) != match.groups():
        _error("pending_source_mismatch")
    if reply and any(not isinstance(value[key], str) or not _COMMIT.fullmatch(value[key])
                     for key in ("commit_sha", "associated_head")):
        _error()
    return {key: value[key] for key in sorted(fields)}


def _refs(values, *, reply=False, allow_empty=False, maximum=MAX_SOURCE_REFS):
    if not isinstance(values, list) or not (0 if allow_empty else 1) <= len(values) <= maximum:
        _error()
    result = [_source_ref(value, reply=reply) for value in values]
    identities = [value["source_id"] if reply else value["path"] for value in result]
    if len(set(identities)) != len(identities):
        _error("pending_source_mismatch")
    return sorted(result, key=lambda ref: ref["path"])


def _objects(values, *, maximum=MAX_OBJECT_REFS):
    if not isinstance(values, list) or len(values) > maximum:
        _error()
    for value in values:
        if (not isinstance(value, dict) or set(value) != {"id", "content_version", "path"}
                or not isinstance(value["id"], str) or not _ID.fullmatch(value["id"])
                or not isinstance(value["path"], str) or not _OBJECT.fullmatch(value["path"])
                or value["path"].rsplit("/", 1)[1] != value["id"] + ".md"
                or not isinstance(value["content_version"], str) or not _VERSION.fullmatch(value["content_version"])):
            _error()
    if len({value["path"] for value in values}) != len(values):
        _error()
    return sorted(copy.deepcopy(values), key=lambda ref: ref["path"])


def _runs(values):
    if (not isinstance(values, list) or len(values) > 1000
            or any(not isinstance(value, str) or not _RUN.fullmatch(value) for value in values)
            or len(set(values)) != len(values)):
        _error()
    return sorted(values)


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        _error()


def _item_id(key):
    return "pending-" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _identity(item):
    return {key: item[key] for key in ("question", "source_refs", "object_refs")}


def _validate_items(items, *, schema=M1_SCHEMA_VERSION):
    if schema not in {M1_SCHEMA_VERSION, EXPANDED_REGISTRY_SCHEMA}:
        _error()
    source_limit = MAX_SOURCE_REFS if schema == EXPANDED_REGISTRY_SCHEMA else 32
    object_limit = MAX_OBJECT_REFS if schema == EXPANDED_REGISTRY_SCHEMA else 64
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        _error()
    ids, keys = set(), set()
    for item in items:
        if not isinstance(item, dict) or set(item) != _ITEM_FIELDS:
            _error()
        key = _text(item["key"], 512)
        _text(item["question"], 16384)
        if any(ord(char) < 32 for char in key) or item["item_id"] != _item_id(key):
            _error()
        if key in keys or item["item_id"] in ids:
            _error()
        ids.add(item["item_id"])
        keys.add(key)
        if (_refs(item["source_refs"], maximum=source_limit) != item["source_refs"]
                or _objects(item["object_refs"], maximum=object_limit) != item["object_refs"]
                or _runs(item["run_ids"]) != item["run_ids"]
                or _refs(item["reply_sources"], reply=True, allow_empty=True, maximum=source_limit) != item["reply_sources"]
                or not isinstance(item["pending_status"], str) or item["pending_status"] not in STATUSES
                or _time(item["updated_at"]) < _time(item["created_at"])):
            _error()
        state, resolution = item["pending_status"], item["resolution"]
        if state == "open" and item["reply_sources"] or state in {"answered", "resolved"} and not item["reply_sources"]:
            _error()
        if state == "resolved":
            if (not isinstance(resolution, dict) or set(resolution) != {"run_id", "commit_sha", "manifest_path"}
                    or not isinstance(resolution["run_id"], str) or not _RUN.fullmatch(resolution["run_id"])
                    or resolution["run_id"] in item["run_ids"]
                    or not isinstance(resolution["commit_sha"], str) or not _COMMIT.fullmatch(resolution["commit_sha"])
                    or resolution["manifest_path"] != f"memory/runs/processes/{resolution['run_id']}/manifest.md"):
                _error()
        elif resolution is not None:
            _error()


def _read_registry(git, head):
    git.path(REGISTRY_PATH)
    blob = git._tree_blob(head, REGISTRY_PATH)
    current = git._existing(REGISTRY_PATH)
    if blob is None:
        if current is not None:
            _error("saved_bytes_changed")
        if git.text("log", "-1", "--format=%H", "--", REGISTRY_PATH):
            _error("pending_registry_deleted")
        return {"items": []}, None
    data = git.read(head, REGISTRY_PATH)
    if current != data:
        _error("saved_bytes_changed")
    return _parse(data), data


def read_registry(repo, *, head=None):
    """只核验权威清单结构和已提交字节，不读取其来源或正式对象正文。"""
    git = PipelineGit(repo)
    snapshot = head or git.head()
    if not isinstance(snapshot, str) or not _COMMIT.fullmatch(snapshot):
        _error("invalid_commit")
    value, _ = _read_registry(git, snapshot)
    return copy.deepcopy(value)


def list_pending(repo, status=None):
    """返回权威事项数组；不存在清单时返回 []，任何未知结构均拒绝。"""
    if status is not None and (not isinstance(status, str) or status not in STATUSES):
        _error("invalid_pending_status")
    git = PipelineGit(repo)
    head = git.check()
    value, _ = _read_registry(git, head)
    from .controls import read_controls
    controls = read_controls(repo, head=head)
    git.check(head)
    return copy.deepcopy([item for item in value["items"]
                          if (status is None or item["pending_status"] == status) and not _deleted(controls, item)])


def _deleted(controls, item):
    from .controls import object_policy, source_policy
    for ref in [*item["source_refs"], *item["reply_sources"]]:
        if source_policy(controls, ref["path"], purpose="history")["reason"] == "delete":
            return True
    return any(object_policy(controls, ref["path"], purpose="history")["reason"] == "delete"
               for ref in item["object_refs"])


def _check_item_controls(repo, head, item, *, replacement_path=None):
    from .controls import read_controls, source_policy
    controls = read_controls(repo, head=head)
    if _deleted(controls, item):
        _error("pending_scope_deleted")
    replaced = []
    for ref in item["reply_sources"]:
        policy = source_policy(controls, ref["path"])
        if policy["excluded"]:
            if replacement_path is None or replacement_path not in policy["replacement_paths"]:
                _error("pending_reply_excluded")
            replaced.append(ref["path"])
    return replaced


def _verify_input_refs(repo, item):
    # Lazy imports avoid the pipeline → pending → pipeline integration cycle.
    from .pipeline import load_source
    git = PipelineGit(repo)
    for ref in item["source_refs"]:
        source = load_source(repo, ref["path"], purpose="history")
        if any(source[key] != ref[key] for key in _REF_FIELDS):
            _error("pending_source_mismatch")
    unmatched = []
    for ref in item["object_refs"]:
        if git._tree_blob(git.head(), ref["path"]) is not None:
            header, _ = _read_document(git, ref["path"])
            if (header.get("schema_version") in {"0.2", M1_SCHEMA_VERSION}
                    and header.get("review_status") == "passed"
                    and all(header.get(key) == ref[key] for key in ("id", "content_version"))):
                continue
        unmatched.append(ref)
    # Review can ask about not-yet-published objects. Bind these references to
    # the explicit origin runs' already committed candidates, never invent a
    # formal object or require publication before asking for confirmation.
    for run in item["run_ids"] if unmatched else []:
        stage = f"memory/staging/{run}.md"
        if git._tree_blob(git.head(), stage) is None:
            continue
        header, body = _read_document(git, stage)
        candidate = _json(body)
        if (header.get("schema_version") not in {"0.2", "0.3", M1_SCHEMA_VERSION, "0.6", "0.7", "0.8", "0.9", QUALITY_SCHEMA_VERSION}
                or header.get("kind") != "candidate" or header.get("run_id") != run
                or not isinstance(candidate, dict) or candidate.get("run_id") != run):
            _error("pending_object_mismatch")
        try:
            selected = {_source_tuple(ref) for ref in item["source_refs"]}
            bound = {_source_tuple(ref) for ref in [candidate["source"], *candidate.get("additional_sources", [])]}
            if selected != bound:
                _error("pending_source_mismatch")
            if header["schema_version"] == QUALITY_SCHEMA_VERSION:
                # New quality references are accepted only with the complete
                # fixed candidate/request/Review proof, after exact source scope
                # matching. Only the root's final non-passing verdict is
                # permitted; rejected dependencies and missing checks fail.
                from .pipeline import _read_reviewed
                candidate, _, _ = _read_reviewed(git, run, source_paths=sorted(ref[2] for ref in selected),
                                                historical=True, allow_nonpassed=True)
            candidates = {(obj["path"], obj["metadata"]["id"], obj["metadata"]["content_version"])
                          for obj in candidate["objects"]}
            unmatched = [ref for ref in unmatched if (ref["path"], ref["id"], ref["content_version"]) not in candidates]
        except (KeyError, TypeError):
            _error("pending_object_mismatch")
        if not unmatched:
            break
    if unmatched:
        _error("pending_object_mismatch")


def _read_document(git, path):
    data = git.read(git.head(), path)
    if git._existing(path) != data:
        _error("saved_bytes_changed")
    try:
        if not data.startswith(b"---\n") or len(data) > MAX_REGISTRY_BYTES:
            _error()
        header, body = data.decode("utf-8")[4:].split("\n---\n", 1)
        metadata = _json(header)
        if not isinstance(metadata, dict):
            _error()
        return metadata, body
    except (UnicodeError, ValueError, TypeError):
        _error()


def _encode(items, *, previous=None):
    # New large items opt into the expanded contract. Existing legacy items
    # retain their identities and fields; merely reading never rewrites them.
    expanded = any(len(item.get("source_refs", [])) > 32 or len(item.get("reply_sources", [])) > 32
                   or len(item.get("object_refs", [])) > 64 for item in items if isinstance(item, dict))
    if previous is not None:
        _parse(previous)
        prior_schema = _json(previous.decode("utf-8")[4:].split("\n---\n", 1)[0])["schema_version"]
        expanded |= prior_schema == EXPANDED_REGISTRY_SCHEMA
    schema = EXPANDED_REGISTRY_SCHEMA if expanded else M1_SCHEMA_VERSION
    _validate_items(items, schema=schema)
    data = markdown({"schema_version": schema, "kind": "pending_registry"},
                    canonical({"items": items}).decode("utf-8") + "\n")
    if len(data) > MAX_REGISTRY_BYTES:
        _error("pending_registry_too_large")
    return data


def pending_update(repo, item, *, head=None):
    """构建登记 CAS 变更；调用者负责持锁并与 Review 等文件一起提交。

    输入仅接受 question/source_refs，以及可选 key/object_refs/run_ids。返回
    (files, expected_files, normalized_item)，幂等重试时前两者为空字典。
    """
    if not isinstance(item, dict) or set(item) - {"key", "question", "source_refs", "object_refs", "run_ids"}:
        _error()
    normalized = {"question": _text(item.get("question"), 16384),
                  "source_refs": _refs(item.get("source_refs")),
                  "object_refs": _objects(item.get("object_refs", [])),
                  "run_ids": _runs(item.get("run_ids", []))}
    key = item.get("key", "sha256-" + hashlib.sha256(canonical(_identity(normalized))).hexdigest())
    _text(key, 512)
    if any(ord(char) < 32 for char in key):
        _error()
    git = PipelineGit(repo)
    snapshot = git.check(head)
    registry, before = _read_registry(git, snapshot)
    _verify_input_refs(repo, normalized)
    items = registry["items"]
    for saved in items:
        if saved["key"] == key:
            if _identity(saved) != _identity(normalized):
                _error("pending_key_conflict")
            runs = sorted(set(saved["run_ids"]) | set(normalized["run_ids"]))
            if runs == saved["run_ids"]:
                git.check(snapshot)
                return {}, {}, copy.deepcopy(saved)
            saved["run_ids"] = runs
            saved["updated_at"] = timestamp()
            result = saved
            break
    else:
        now = timestamp()
        result = {**normalized, "item_id": _item_id(key), "key": key, "pending_status": "open",
                  "reply_sources": [], "resolution": None, "created_at": now, "updated_at": now}
        items.append(result)
    items.sort(key=lambda value: value["item_id"])
    data = _encode(items, previous=before)
    git.check(snapshot)
    return {REGISTRY_PATH: data}, {REGISTRY_PATH: before}, copy.deepcopy(result)


def _result(item, head, changed):
    return {"item_id": item["item_id"], "key": item["key"], "pending_status": item["pending_status"],
            "head": head, "changed": changed}


def register_pending(repo, item, expected_head=None):
    git = PipelineGit(repo)
    with git.lock():
        git.recover()
        head = git.check(expected_head)
        files, expected, saved = pending_update(repo, item, head=head)
        if files:
            head = git.write_changes(files, expected, head, "pending: register stable confirmation item")
        return _result(saved, head, bool(files))


def _find(registry, item_id):
    if not isinstance(item_id, str) or not re.fullmatch(r"pending-[0-9a-f]{64}", item_id):
        _error("invalid_pending_id")
    for item in registry["items"]:
        if item["item_id"] == item_id:
            return item
    _error("pending_not_found")


def answer_pending(repo, item_id, reply_source_path, expected_head=None):
    """关联先采集并已核验的答复来源；只置 answered，不推断知识处理完成。"""
    from .pipeline import load_source
    git = PipelineGit(repo)
    with git.lock():
        git.recover()
        head = git.check(expected_head)
        registry, before = _read_registry(git, head)
        item = _find(registry, item_id)
        if item["pending_status"] not in {"open", "answered"}:
            _error("pending_status_conflict")
        # Validate the requested path before any source read.
        if not isinstance(reply_source_path, str) or not _SOURCE.fullmatch(reply_source_path):
            _error("path_rejected")
        replaced = _check_item_controls(repo, head, item, replacement_path=reply_source_path)
        if any(ref["path"] == reply_source_path for ref in item["source_refs"]):
            _error("pending_reply_not_new")
        reply = load_source(repo, reply_source_path)
        if reply["metadata"]["acquisition_status"] == "link_only":
            _error("pending_reply_missing_text")
        # Only an explicit control can replace an active reply. Its original
        # association remains in Git history; never load the excluded old body.
        item["reply_sources"] = [ref for ref in item["reply_sources"] if ref["path"] not in replaced]
        for saved in item["reply_sources"]:
            if saved["source_id"] == reply["source_id"]:
                if any(saved[key] != reply[key] for key in _REF_FIELDS):
                    _error("pending_reply_conflict")
                if not replaced:
                    git.check(head)
                    return _result(item, head, False)
                item["reply_sources"].remove(saved)
                break
        item["reply_sources"].append({**{key: reply[key] for key in _REF_FIELDS},
                                      "commit_sha": reply["commit_sha"], "associated_head": head})
        item["reply_sources"].sort(key=lambda value: value["path"])
        item["pending_status"] = "answered"
        item["updated_at"] = timestamp()
        updated = _encode(registry["items"], previous=before)
        commit = git.write_changes({REGISTRY_PATH: updated}, {REGISTRY_PATH: before}, head,
                                   "pending: associate captured reply without resolving")
        return _result(item, commit, True)


def _publication_source_refs(git, run_id):
    """Read only committed manifest bindings, before caller scope checks.

    These bindings do not prove a publication. Callers must authorize every
    returned source before allowing ``verify_publication`` to open source
    bodies and verify the ordinary or batch-group publication chain.
    """
    if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
        _error("invalid_run_id")
    path = f"memory/runs/processes/{run_id}/manifest.md"
    data = git.read(git.head(), path)
    if git._existing(path) != data:
        _error("saved_bytes_changed")
    try:
        if not data.startswith(b"---\n") or len(data) > MAX_REGISTRY_BYTES:
            _error()
        header, body = data.decode("utf-8")[4:].split("\n---\n", 1)
        metadata, manifest = _json(header), _json(body)
        if (not isinstance(metadata, dict)
                or (metadata.get("schema_version"), metadata.get("kind")) not in {
                    ("0.2", "manifest"), ("0.3", "manifest"), (M1_SCHEMA_VERSION, "manifest"),
                    ("0.6", "manifest"), ("0.6", "batch_group_manifest"), ("0.7", "manifest"), ("0.8", "manifest"), ("0.9", "manifest"),
                    (QUALITY_SCHEMA_VERSION, "manifest")}
                or metadata.get("run_id") != run_id
                or not isinstance(manifest, dict) or manifest.get("run_id") != run_id):
            _error()
        bindings = [manifest["source"], *manifest.get("additional_sources", [])]
        if metadata["schema_version"] == QUALITY_SCHEMA_VERSION:
            # Share the read-only candidate/mode preflight with current
            # evidence; full publication verification remains AFTER the item's
            # associated source scope has been checked by the caller.
            from .evidence import _peek_publication
            _peek_publication(git, run_id, git.head())
        maximum = 256 if metadata["schema_version"] in {"0.6", "0.7", "0.8", "0.9", QUALITY_SCHEMA_VERSION} else 8
        chosen = [_source_tuple(binding) for binding in bindings]
        if len(chosen) > maximum or len(set(chosen)) != len(chosen):
            _error("pending_publication_mismatch")
        return [dict(zip(("source_id", "source_version", "path"), ref)) for ref in chosen]
    except (UnicodeError, ValueError, KeyError, TypeError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _error("pending_publication_mismatch")


def _publication_scope(git, run_id, item):
    if run_id in item["run_ids"]:
        _error("pending_publication_mismatch")
    chosen = {_source_tuple(ref) for ref in _publication_source_refs(git, run_id)}
    allowed = {_source_tuple(ref) for ref in [*item["source_refs"], *item["reply_sources"]]}
    replies = {_source_tuple(ref) for ref in item["reply_sources"]}
    if not chosen <= allowed or not replies <= chosen:
        _error("pending_publication_mismatch")
    return sorted(ref[2] for ref in chosen)


def _source_tuple(binding):
    if not isinstance(binding, dict):
        _error("pending_publication_mismatch")
    ref = _source_ref({key: binding[key] for key in _REF_FIELDS})
    return tuple(ref[key] for key in ("source_id", "source_version", "path"))


def _reply_association_commit(git, item, ref):
    """核对实际答复关联提交，不能仅相信清单内可被手改的历史锚。"""
    try:
        git.run("merge-base", "--is-ancestor", ref["commit_sha"], ref["associated_head"])
        git.run("merge-base", "--is-ancestor", ref["associated_head"], git.head())
        commits = git.text("log", "--full-history", "--ancestry-path", "--reverse", "--format=%H",
                           "--max-count=1001", ref["associated_head"] + "..HEAD", "--", REGISTRY_PATH).splitlines()
        if len(commits) > 1000:
            _error("pending_history_too_large")
        for commit in commits:
            if git.text("rev-parse", commit + "^") != ref["associated_head"]:
                continue
            saved = _parse(git.read(commit, REGISTRY_PATH))
            previous = next((value for value in saved["items"] if value["item_id"] == item["item_id"]), None)
            if (previous is not None and previous["pending_status"] == "answered"
                    and _identity(previous) == _identity(item) and ref in previous["reply_sources"]):
                return commit
    except CaptureError as error:
        if error.code == "pending_history_too_large":
            raise
    _error("pending_publication_mismatch")


def _check_reply_records(git, publication, item):
    """答复必须实际成为经核验正式记录的依据；仅进入运行上下文不算完成。"""
    required = {(ref["source_id"], ref["source_version"]) for ref in item["reply_sources"]}
    referenced = set()
    for path in publication["files"]:
        if not path.startswith("memory/records/"):
            continue
        # These exact files and their actual source locators/quotes have already
        # passed verify_publication. Read no object outside that verified scope.
        metadata, _ = _read_document(git, path)
        if metadata.get("review_status") != "passed":
            _error("pending_publication_mismatch")
        referenced.update((ref["source_id"], ref["source_version"]) for ref in metadata["sources"])
    if not required <= referenced:
        _error("pending_reply_not_applied")


def resolve_pending(repo, item_id, publication_run, expected_head=None):
    """只有全部答复被新发布的正式记录实际引用后才置 resolved。"""
    from .pipeline import load_source, verify_publication
    git = PipelineGit(repo)
    with git.lock():
        git.recover()
        head = git.check(expected_head)
        registry, before = _read_registry(git, head)
        item = _find(registry, item_id)
        _check_item_controls(repo, head, item)
        if item["pending_status"] not in {"answered", "resolved"}:
            _error("pending_status_conflict")
        if item["pending_status"] == "resolved" and item["resolution"]["run_id"] != publication_run:
            _error("pending_status_conflict")
        # First restrict the manifest to the item's explicitly associated scope;
        # verify_publication also checks this exact scope before loading sources.
        paths = _publication_scope(git, publication_run, item)
        publication = verify_publication(repo, publication_run, source_paths=paths)
        if publication.get("status") != "published_local":
            _error("pending_publication_mismatch")
        _check_reply_records(git, publication, item)
        commit = publication["commit_sha"]
        for ref in item["reply_sources"]:
            source = load_source(repo, ref["path"])
            if any(source[key] != ref[key] for key in _REF_FIELDS | {"commit_sha"}):
                _error("pending_reply_conflict")
            associated = _reply_association_commit(git, item, ref)
            try:
                git.run("merge-base", "--is-ancestor", associated, commit)
            except CaptureError:
                _error("pending_publication_mismatch")
        resolution = {"run_id": publication_run, "commit_sha": commit, "manifest_path": publication["manifest_path"]}
        if item["pending_status"] == "resolved":
            if resolution != item["resolution"]:
                _error("pending_publication_mismatch")
            git.check(head)
            return _result(item, head, False)
        item["pending_status"] = "resolved"
        item["resolution"] = resolution
        item["updated_at"] = timestamp()
        updated = _encode(registry["items"], previous=before)
        commit = git.write_changes({REGISTRY_PATH: updated}, {REGISTRY_PATH: before}, head,
                                   "pending: resolve after verified reply publication")
        return _result(item, commit, True)
