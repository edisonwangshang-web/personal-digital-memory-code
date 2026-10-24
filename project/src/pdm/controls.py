"""Bounded source controls, with explicit deletion scope and no copied content."""

from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from .contracts import CaptureError, CONTINUOUS_SCHEMA_VERSION, M1_SCHEMA_VERSION, canonical, markdown, timestamp
from .transaction import PipelineGit

_SOURCE = re.compile(r"memory/sources/(?:conversations|inbox)/(src-[0-9a-f]{64})/(sha256-[0-9a-f]{64})\.md\Z")
_OBJECT = re.compile(r"memory/(?:records|topics)/([a-z][a-z0-9-]{0,95})\.md\Z")
_CONTROL = re.compile(r"memory/runs/controls/(ctl-[0-9a-f]{64})\.md\Z")
_ROOTS = ("memory/controls", "memory/runs/controls", "memory/runs/deletions", "memory/runs/corrections")
_FIELDS = {"request_key", "action", "source_paths", "object_paths", "replacement_source_paths",
           "basis_source_path", "effective_from"}
_OBJECT_FIELDS = {"scope", "object_versions"}
_LOCATOR = re.compile(r"L([1-9][0-9]{0,8})-L([1-9][0-9]{0,8})\Z")
MAX_CONTROLS = 1000
MAX_CONTROL_BYTES = 65536
MAX_FORMAL_OBJECTS = 1000


def _error(code="invalid_controls", message="控制资料无效；未读取受控正文或改写资料"):
    raise CaptureError(code, message)


def _hash(value):
    return "sha256-" + hashlib.sha256(canonical(value)).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _error()
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique,
                          parse_constant=lambda value: _error())
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _error()


def _paths(value, pattern):
    if (not isinstance(value, list) or len(value) > 128
            or any(not isinstance(path, str) or not pattern.fullmatch(path) for path in value)
            or len(set(value)) != len(value)):
        _error("invalid_control_scope")
    return sorted(value)


def _payload(value):
    if not isinstance(value, dict) or set(value) - (_FIELDS | _OBJECT_FIELDS):
        _error()
    key, action = value.get("request_key"), value.get("action")
    if not isinstance(key, str) or not key.strip() or len(key) > 512 or any(ord(char) < 32 for char in key):
        _error("invalid_control_key")
    if action not in {"corrects", "supersedes", "delete"}:
        _error("invalid_control_action")
    sources = _paths(value.get("source_paths", []), _SOURCE)
    objects = _paths(value.get("object_paths", []), _OBJECT)
    replacements = _paths(value.get("replacement_source_paths", []), _SOURCE)
    basis = value.get("basis_source_path")
    if basis is not None and (not isinstance(basis, str) or not _SOURCE.fullmatch(basis)):
        _error("invalid_control_scope")
    if not sources and not objects:
        _error("invalid_control_scope")
    if action == "delete":
        if replacements:
            _error("invalid_control_scope")
    elif not sources or not replacements or set(sources) & set(replacements):
        _error("invalid_control_scope")
    object_scope = "scope" in value or "object_versions" in value
    if object_scope:
        versions = value.get("object_versions")
        if (value.get("scope") != "objects" or action == "delete" or not objects
                or basis not in replacements or not isinstance(versions, dict)
                or set(versions) != set(objects)
                or any(not isinstance(version, str) or not re.fullmatch(r"sha256-[0-9a-f]{64}", version)
                       for version in versions.values())):
            _error("invalid_control_scope")
    effective = value.get("effective_from")
    if effective is not None:
        try:
            if not isinstance(effective, str):
                raise ValueError
            if re.fullmatch(r"\d{4}-\d{2}", effective):
                date.fromisoformat(effective + "-01")
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", effective):
                date.fromisoformat(effective)
            else:
                parsed = datetime.fromisoformat(effective)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError
        except ValueError:
            _error("invalid_control_time")
    result = {"request_key": key, "action": action, "source_paths": sources,
            "object_paths": objects, "replacement_source_paths": replacements,
            "basis_source_path": basis, "effective_from": effective}
    if object_scope:
        result.update(scope="objects", object_versions={path: versions[path] for path in objects})
    return result


def _object_scope(event):
    return event.get("scope") == "objects"


def _event_payload(event):
    fields = _FIELDS | _OBJECT_FIELDS if _object_scope(event) else _FIELDS
    return {key: event[key] for key in fields}


def _validate_locators(event):
    locators = event.get("source_locators")
    if not isinstance(locators, dict) or set(locators) != set(event["source_paths"]):
        _error()
    for path, items in locators.items():
        if not isinstance(items, list) or not 1 <= len(items) <= 1024:
            _error()
        seen = set()
        for item in items:
            if (not isinstance(item, dict) or set(item) != {"object_path", "object_version", "locator"}
                    or item.get("object_path") not in event["object_versions"]
                    or item.get("object_version") != event["object_versions"][item["object_path"]]
                    or not isinstance(item.get("locator"), str)):
                _error()
            match = _LOCATOR.fullmatch(item["locator"])
            identity = (item["object_path"], item["locator"])
            if match is None or int(match[1]) > int(match[2]) or identity in seen:
                _error()
            seen.add(identity)


def _identity(key):
    return "ctl-" + hashlib.sha256(key.encode()).hexdigest()


def _committed_bytes(git, path, head):
    data = git._existing(path)
    if data is None or git.read(head, path) != data:
        _error("saved_bytes_changed")
    return data


def read_controls(repo, *, head=None, at_commit=None):
    """Read a fixed control snapshot; read guards always use the current one.

    at_commit is only for reconstructing an explicitly selected historical
    Review binding. It does not authorize reading a currently deleted source.
    """
    git = PipelineGit(repo)
    actual_head = git.head()
    if head is not None and head != actual_head:
        _error("head_conflict")
    if at_commit is not None:
        if not isinstance(at_commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", at_commit):
            _error("invalid_commit")
        git.run("merge-base", "--is-ancestor", at_commit, actual_head)
    current = at_commit or actual_head
    paths = git.run("ls-tree", "-r", "--name-only", current, "--", *_ROOTS).decode().splitlines()
    removed = git.run("log", "--format=", "--name-only", "--diff-filter=D", actual_head, "--", *_ROOTS).decode().splitlines()
    if any(path and Path(path).name not in {"README.md", ".gitkeep"} for path in removed):
        _error("control_removed")
    actual = set()
    for root in _ROOTS:
        target = git.repo / root
        if target.is_symlink():
            _error("path_rejected")
        if target.is_file():
            _error("unsupported_controls")
        if target.is_dir():
            for item in target.rglob("*"):
                if item.is_symlink():
                    _error("path_rejected")
                if item.is_file() and item.name not in {"README.md", ".gitkeep"}:
                    actual.add(str(item.relative_to(git.repo)))
                    if len(actual) > MAX_CONTROLS:
                        _error("control_limit")
    paths = [path for path in paths if Path(path).name not in {"README.md", ".gitkeep"}]
    if len(paths) > MAX_CONTROLS:
        _error("control_limit")
    current_paths = (git.run("ls-tree", "-r", "--name-only", actual_head, "--", *_ROOTS).decode().splitlines()
                     if at_commit is not None else paths)
    current_paths = [path for path in current_paths if Path(path).name not in {"README.md", ".gitkeep"}]
    if any(not _CONTROL.fullmatch(path) for path in set(current_paths) | actual):
        _error("unsupported_controls")
    if set(current_paths) != actual:
        _error("uncommitted_controls")
    events = []
    for path in sorted(paths):
        match = _CONTROL.fullmatch(path)
        if match is None:
            _error("unsupported_controls")
        if git.run("log", "--format=%H", "--diff-filter=M", actual_head, "--", path):
            _error("control_modified")
        data = _committed_bytes(git, path, current)
        if len(data) > MAX_CONTROL_BYTES or not data.startswith(b"---\n"):
            _error()
        try:
            header, body = data[4:].split(b"\n---\n", 1)
        except ValueError:
            _error()
        metadata, event = _json(header), _json(body)
        if not isinstance(event, dict):
            _error()
        object_scope = _object_scope(event)
        fields = _FIELDS | {"control_id", "created_at", "object_versions"}
        if object_scope:
            fields |= {"scope", "source_locators"}
        expected_header = {"schema_version": CONTINUOUS_SCHEMA_VERSION if object_scope else M1_SCHEMA_VERSION,
                           "kind": "object_control" if object_scope else "source_control", "control_id": match[1]}
        if metadata != expected_header or set(event) != fields:
            _error()
        normalized = _payload(_event_payload(event))
        if any(normalized[key] != event[key] for key in normalized) or event["control_id"] != _identity(event["request_key"]) or event["control_id"] != match[1]:
            _error()
        if (not isinstance(event["object_versions"], dict) or set(event["object_versions"]) != set(event["object_paths"])
                or any(not isinstance(version, str) or not re.fullmatch(r"sha256-[0-9a-f]{64}", version)
                       for version in event["object_versions"].values())):
            _error()
        if object_scope:
            _validate_locators(event)
        try:
            created = datetime.fromisoformat(event["created_at"])
            if created.tzinfo is None or created.utcoffset() is None:
                raise ValueError
        except (ValueError, TypeError):
            _error()
        events.append(event)
    # Reject cycles rather than guessing which replacement is authoritative.
    edges = {}
    for event in events:
        if event["action"] != "delete":
            for path in event["source_paths"]:
                edges.setdefault(path, set()).update(event["replacement_source_paths"])
    checked = set()
    def visit(path, ancestors):
        if path in ancestors:
            _error("control_cycle")
        if path in checked:
            return
        if len(ancestors) > 128:
            _error("control_limit")
        for following in edges.get(path, ()):
            visit(following, ancestors | {path})
        checked.add(path)
    for path in edges:
        visit(path, set())
    if git.head() != actual_head:
        _error("head_conflict")
    return {"head": current, "digest": _hash(events), "events": events}


def source_id_policy(snapshot, source_id):
    """All-version deletion guard for capture before it reads an existing body."""
    matching = [event for event in snapshot["events"] if event["action"] == "delete"
                and any(_SOURCE.fullmatch(path)[1] == source_id for path in event["source_paths"])]
    return {"excluded": bool(matching), "reason": "delete" if matching else None,
            "replacement_paths": [], "history_allowed": not bool(matching),
            "control_ids": [event["control_id"] for event in matching],
            "not_yet_effective": False, "temporal_undetermined": False}


def _temporal(event, as_of=None):
    if event["action"] != "supersedes":
        return {"active": True, "not_yet_effective": False, "temporal_undetermined": False}
    effective = event["effective_from"]
    if effective is None:
        return {"active": False, "not_yet_effective": False, "temporal_undetermined": True}
    current = as_of or datetime.now(ZoneInfo("Asia/Shanghai"))
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        _error("invalid_control_time")
    local = current.astimezone(ZoneInfo("Asia/Shanghai"))
    if len(effective) == 7:
        future = effective > local.strftime("%Y-%m")
    elif len(effective) == 10:
        future = effective > local.date().isoformat()
    else:
        future = datetime.fromisoformat(effective) > current
    return {"active": not future, "not_yet_effective": future, "temporal_undetermined": False}


def source_policy(snapshot, source_path, *, purpose="current", as_of=None):
    if purpose not in {"current", "history"} or not isinstance(source_path, str) or not _SOURCE.fullmatch(source_path):
        _error("invalid_control_scope")
    deleted = source_id_policy(snapshot, _SOURCE.fullmatch(source_path)[1])
    if deleted["excluded"]:
        return deleted
    matching = [event for event in snapshot["events"] if source_path in event["source_paths"]]
    source_matching = [event for event in matching if not _object_scope(event)]
    incoming = [event for event in snapshot["events"] if source_path in event["replacement_source_paths"]]
    temporal = [_temporal(event, as_of) for event in matching + incoming]
    result = {"excluded": any(_temporal(event, as_of)["active"] for event in source_matching) and purpose != "history",
            "reason": "corrects" if any(event["action"] == "corrects" for event in matching)
                      else "supersedes" if matching else None,
            "replacement_paths": sorted({path for event in matching for path in event["replacement_source_paths"]}),
            "history_allowed": True, "control_ids": sorted({event["control_id"] for event in matching + incoming}),
            "not_yet_effective": any(item["not_yet_effective"] for item in temporal),
            "temporal_undetermined": any(item["temporal_undetermined"] for item in temporal)}
    partial = [{"control_id": event["control_id"], "action": event["action"], **item,
                **_temporal(event, as_of)}
               for event in matching if _object_scope(event)
               for item in event["source_locators"][source_path]]
    if partial:
        result["object_restrictions"] = partial
    return result


def source_locator_policy(snapshot, source_path, locator, *, purpose="current", as_of=None):
    """A raw locator cannot silently restore an object-scoped corrected fact."""
    match = _LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
    if match is None or int(match[1]) > int(match[2]):
        _error("invalid_control_scope")
    policy = source_policy(snapshot, source_path, purpose=purpose, as_of=as_of)
    restrictions = []
    for item in policy.get("object_restrictions", []):
        affected = _LOCATOR.fullmatch(item["locator"])
        if int(match[1]) <= int(affected[2]) and int(affected[1]) <= int(match[2]):
            restrictions.append(item)
    return {"excluded": policy["excluded"] or purpose != "history" and any(item["active"] for item in restrictions),
            "restrictions": restrictions, "control_ids": sorted({item["control_id"] for item in restrictions})}


def object_policy(snapshot, object_path, *, purpose="current", content_version=None, as_of=None):
    if purpose not in {"current", "history"} or not isinstance(object_path, str) or not _OBJECT.fullmatch(object_path):
        _error("invalid_control_scope")
    matching = [event for event in snapshot["events"] if object_path in event["object_paths"]
                and (event["action"] == "delete" or content_version is None
                     or event["object_versions"].get(object_path) == content_version)]
    deleted = any(event["action"] == "delete" for event in matching)
    temporal = [_temporal(event, as_of) for event in matching]
    return {"excluded": deleted or any(item["active"] for item in temporal) and purpose != "history",
            "reason": "delete" if deleted else matching[-1]["action"] if matching else None,
            "replacement_paths": sorted({path for event in matching for path in event["replacement_source_paths"]}),
            "history_allowed": not deleted, "control_ids": [event["control_id"] for event in matching],
            "not_yet_effective": any(item["not_yet_effective"] for item in temporal),
            "temporal_undetermined": any(item["temporal_undetermined"] for item in temporal)}


def _metadata_only(git, path):
    """Unbuffered header reads never prefetch a record's private Markdown body."""
    try:
        with git._parent(path) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb", buffering=0) as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or stream.readline(4) != b"---\n":
                    _error()
                header = bytearray()
                while len(header) <= MAX_CONTROL_BYTES:
                    line = stream.readline(max(4, MAX_CONTROL_BYTES - len(header) + 1))
                    if line == b"---\n":
                        value = _json(header)
                        if not isinstance(value, dict):
                            _error()
                        return value
                    if not line:
                        _error()
                    header.extend(line)
    except OSError as error:
        raise CaptureError("path_rejected", "正式对象元数据不能安全读取") from error
    _error("control_limit")


def _partial_plan(git, value, snapshot):
    """Bind the named old versions; dependency discovery reads headers only."""
    identities = {tuple(_SOURCE.fullmatch(path).groups()): path for path in value["source_paths"]}
    metadata = {}
    for path in value["object_paths"]:
        if not git.run("ls-tree", snapshot["head"], "--", path):
            _error("control_target_missing")
        meta = _metadata_only(git, path)
        if meta.get("content_version") != value["object_versions"][path]:
            _error("control_version_mismatch")
        if object_policy(snapshot, path, content_version=meta.get("content_version"))["excluded"]:
            _error("object_excluded")
        refs = meta.get("sources")
        if (not isinstance(refs, list) or not refs or any(not isinstance(ref, dict) for ref in refs)
                or not any((ref.get("source_id"), ref.get("source_version")) in identities for ref in refs)):
            _error("control_scope_mismatch")
        metadata[path] = meta
    # A topic containing a controlled record must not keep asserting the old
    # state. Report dependencies for explicit authorization, never add writes.
    required = set(value["object_paths"])
    topic_paths = git.run("ls-tree", "-r", "--name-only", snapshot["head"], "--", "memory/topics").decode().splitlines()
    topic_paths = [path for path in topic_paths if Path(path).name not in {"README.md", ".gitkeep"}]
    if len(topic_paths) > MAX_FORMAL_OBJECTS:
        _error("control_limit")
    for path in topic_paths:
        if not _OBJECT.fullmatch(path):
            _error("path_rejected")
        if path not in metadata:
            metadata[path] = _metadata_only(git, path)
    while True:
        identities_by_id = {_OBJECT.fullmatch(path)[1] for path in required}
        before = len(required)
        for path in topic_paths:
            refs = metadata[path].get("records", [])
            if not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs):
                _error()
            if any(ref.get("id") in identities_by_id for ref in refs):
                required.add(path)
        if len(required) == before:
            break
    # Records define the fact-level raw restriction. A dependent topic often
    # cites additional unaffected records, and must not broaden that restriction.
    roots = [path for path in value["object_paths"] if path.startswith("memory/records/")]
    if not roots:
        roots = value["object_paths"]
    locators = {path: [] for path in value["source_paths"]}
    for path in roots:
        for ref in metadata[path]["sources"]:
            source_path = identities.get((ref.get("source_id"), ref.get("source_version")))
            if source_path is None:
                continue
            locator = ref.get("locator")
            match = _LOCATOR.fullmatch(locator) if isinstance(locator, str) else None
            if match is None or int(match[1]) > int(match[2]):
                _error("control_scope_mismatch")
            item = {"object_path": path, "object_version": value["object_versions"][path], "locator": locator}
            if item not in locators[source_path]:
                locators[source_path].append(item)
    if any(not items for items in locators.values()):
        _error("control_scope_mismatch")
    return required, locators


def _plan(git, payload, snapshot):
    value = _payload(payload)
    required_objects = set(value["object_paths"])
    required_sources = set(value["source_paths"])
    locators = None
    if _object_scope(value):
        required_objects, locators = _partial_plan(git, value, snapshot)
    elif value["action"] != "delete":
        identities = {tuple(_SOURCE.fullmatch(path).groups()) for path in value["source_paths"]}
        for path in value["object_paths"]:
            metadata = _metadata_only(git, path)
            refs = metadata.get("sources")
            if (not isinstance(refs, list) or not any(isinstance(ref, dict) and
                    (ref.get("source_id"), ref.get("source_version")) in identities for ref in refs)):
                _error("control_scope_mismatch")
    if value["action"] == "delete":
        source_ids = {_SOURCE.fullmatch(path)[1] for path in value["source_paths"]}
        paths = git.run("ls-tree", "-r", "--name-only", snapshot["head"], "--", "memory/records", "memory/topics").decode().splitlines()
        paths = [path for path in paths if Path(path).name not in {"README.md", ".gitkeep"}]
        if len(paths) > MAX_FORMAL_OBJECTS:
            _error("control_limit")
        metadata = {}
        for path in paths:
            if not _OBJECT.fullmatch(path):
                _error("path_rejected")
            metadata[path] = _metadata_only(git, path)
        for path in value["object_paths"]:
            if path not in metadata:
                _error("control_target_missing")
            for ref in metadata[path].get("sources", []):
                if not isinstance(ref, dict) or not re.fullmatch(r"src-[0-9a-f]{64}", str(ref.get("source_id", ""))) or not re.fullmatch(r"sha256-[0-9a-f]{64}", str(ref.get("source_version", ""))):
                    _error()
                if ref["source_id"] not in source_ids:
                    candidates = [f"memory/sources/{folder}/{ref['source_id']}/{ref['source_version']}.md" for folder in ("conversations", "inbox")]
                    found = [candidate for candidate in candidates if git.run("ls-tree", snapshot["head"], "--", candidate)]
                    if len(found) != 1:
                        _error("control_target_missing")
                    required_sources.add(found[0])
        for path, meta in metadata.items():
            if any(isinstance(ref, dict) and ref.get("source_id") in source_ids for ref in meta.get("sources", [])):
                required_objects.add(path)
        while True:
            ids = {_OBJECT.fullmatch(path)[1] for path in required_objects}
            before = len(required_objects)
            for path, meta in metadata.items():
                if any(isinstance(ref, dict) and ref.get("id") in ids for ref in meta.get("records", [])):
                    required_objects.add(path)
            if len(required_objects) == before:
                break
    result = {"head": snapshot["head"], "action": value["action"], "payload": value,
            "required_object_paths": sorted(required_objects), "required_source_paths": sorted(required_sources),
            "missing_object_paths": sorted(required_objects - set(value["object_paths"])),
            "missing_source_paths": sorted(required_sources - set(value["source_paths"])),
            "source_suppression": ("all_versions" if value["action"] == "delete" else
                                   "object_locators" if _object_scope(value) else "exact_versions")}
    if locators is not None:
        result["source_locators"] = locators
    return result


def plan_control(repo, payload, expected_head=None):
    git = PipelineGit(repo)
    head = git.check(expected_head)
    return _plan(git, payload, read_controls(repo, head=head))


def record_control(repo, payload, expected_head=None):
    """Record an explicit control; deletions atomically remove all declared views."""
    git = PipelineGit(repo)
    with git.lock():
        git.recover()
        head = git.check(expected_head)
        snapshot = read_controls(repo, head=head)
        value = _payload(payload)
        identifier = _identity(value["request_key"])
        for event in snapshot["events"]:
            if event["control_id"] == identifier:
                if _event_payload(event) != value:
                    _error("control_key_conflict")
                return {"control_id": identifier, "path": f"memory/runs/controls/{identifier}.md", "head": head, "reused": True}
        plan = _plan(git, value, snapshot)
        if plan["missing_object_paths"] or plan["missing_source_paths"]:
            error = CaptureError("control_dependencies", "控制范围未覆盖全部来源或直接依赖；请查看控制计划")
            error.details = plan
            raise error
        # Local imports avoid a cycle with pipeline's read-before-body guard.
        from .pipeline import load_source
        loaded = {}
        for path in sorted(set(value["source_paths"] + value["replacement_source_paths"] + ([value["basis_source_path"]] if value["basis_source_path"] else []))):
            loaded[path] = load_source(repo, path, purpose="history")
        if _object_scope(value):
            for path in value["replacement_source_paths"]:
                source = loaded[path]
                if (source["metadata"].get("acquisition_status") == "link_only" or not source["body"].strip()
                        or source_policy(snapshot, path)["excluded"]):
                    _error("control_basis_missing")
            for source_path, items in plan["source_locators"].items():
                lines = loaded[source_path]["body"].splitlines(keepends=True)
                for item in items:
                    refs = _metadata_only(git, item["object_path"])["sources"]
                    identity = _SOURCE.fullmatch(source_path).groups()
                    matching = [ref for ref in refs if (ref.get("source_id"), ref.get("source_version")) == identity
                                and ref.get("locator") == item["locator"]]
                    match = _LOCATOR.fullmatch(item["locator"])
                    if (not 1 <= int(match[1]) <= int(match[2]) <= len(lines)
                            or any(not isinstance(ref.get("quote"), str) or not ref["quote"]
                                   or ref["quote"] not in "".join(lines[int(match[1]) - 1:int(match[2])])
                                   for ref in matching)):
                        _error("control_scope_mismatch")
        # Reject a replacement cycle before writing a persistent event.
        versions = {path: _metadata_only(git, path).get("content_version") for path in value["object_paths"]}
        if any(not isinstance(version, str) or not re.fullmatch(r"sha256-[0-9a-f]{64}", version) for version in versions.values()):
            _error()
        event = {**value, "control_id": identifier, "created_at": timestamp(), "object_versions": versions}
        if _object_scope(value):
            if versions != value["object_versions"]:
                _error("control_version_mismatch")
            event["source_locators"] = plan["source_locators"]
            _validate_locators(event)
        prospective = {**snapshot, "events": snapshot["events"] + [event]}
        for replacement in value["replacement_source_paths"]:
            todo, seen = [replacement], set()
            while todo:
                following = todo.pop()
                if following in value["source_paths"]:
                    _error("control_cycle")
                if following not in seen:
                    seen.add(following)
                    todo.extend(source_policy(prospective, following)["replacement_paths"])
        path = f"memory/runs/controls/{identifier}.md"
        data = markdown({"schema_version": CONTINUOUS_SCHEMA_VERSION if _object_scope(value) else M1_SCHEMA_VERSION,
                         "kind": "object_control" if _object_scope(value) else "source_control", "control_id": identifier}, canonical(event).decode() + "\n")
        if len(data) > MAX_CONTROL_BYTES:
            _error("control_limit")
        files = {path: data}
        expected = {path: None}
        if value["action"] == "delete":
            for target in value["object_paths"]:
                expected[target] = _committed_bytes(git, target, head)
                files[target] = None
        final = git.write_changes(files, expected, head, "control: persist explicit scope and current-view removal")
        return {"control_id": identifier, "path": path, "head": final, "reused": False,
                "plan": plan, "controls_digest": read_controls(repo)["digest"]}
