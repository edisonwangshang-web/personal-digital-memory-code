"""An explicit, immutable request to clear formal knowledge and rebuild it.

The receipt grants a narrowly scoped exception to create-only history checks.
It never deletes inputs/proofs, resets attempts, retries a model, or authorizes a
source/object excluded by controls. Ordinary processing has no such exception.
"""

import hashlib
import json
import re

from .contracts import CaptureError, canonical, markdown, parse_markdown, timestamp
from .transaction import PipelineGit


CONTRACT = "explicit-knowledge-rebuild-v1"
SCHEMA = "0.4"
CONTRACT_V2 = "explicit-knowledge-rebuild-v2"
SCHEMA_V2 = "0.17"
KIND = "knowledge_rebuild"
ROOT = "memory/runs/processes/rebuilds/"
_FORMAL = re.compile(r"memory/(records|topics)/([a-z][a-z0-9-]{0,95})\.md\Z")
_RECEIPT = re.compile(re.escape(ROOT) + r"([0-9a-f]{64})\.md\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST = re.compile(r"sha256-[0-9a-f]{64}\Z")


def _fail(code="rebuild_context_changed"):
    raise CaptureError(code, "明确重建的清理证明、来源或目标已变化；未删除资料或重试旧调用")


def _hash(raw):
    return "sha256-" + hashlib.sha256(raw).hexdigest()


def _intent_path(intent_id):
    if (not isinstance(intent_id, str) or not intent_id.strip() or len(intent_id) > 512
            or any(ord(char) < 32 for char in intent_id)):
        _fail("invalid_rebuild_intent")
    return ROOT + hashlib.sha256(intent_id.encode()).hexdigest() + ".md"


def _targets(paths):
    if (not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 128
            or any(not isinstance(path, str) or _FORMAL.fullmatch(path) is None for path in paths)
            or len(set(paths)) != len(paths)):
        _fail("invalid_rebuild_scope")
    return sorted(paths)


def _source_material(git, paths, *, snapshot=None):
    from .pipeline import load_source, normalize_source_paths, _source_binding
    paths = normalize_source_paths(paths, allow_versions=True, max_sources=256)
    sources = [load_source(git.repo, path, commit=snapshot,
                           purpose="history" if snapshot is not None else "current") for path in paths]
    return [_source_binding(source) for source in sources]


def _within_scope(ref, source_keys):
    return (isinstance(ref, dict) and isinstance(ref.get("source_id"), str)
            and isinstance(ref.get("source_version"), str)
            and (ref["source_id"], ref["source_version"]) in source_keys)


def _formal(raw, path, source_keys):
    metadata, _ = parse_markdown(raw)
    match = _FORMAL.fullmatch(path)
    references = metadata.get("sources")
    if (match is None or metadata.get("schema_version") != "0.2"
            or metadata.get("id") != match[2] or metadata.get("review_status") != "passed"
            or not isinstance(metadata.get("content_version"), str)
            or _DIGEST.fullmatch(metadata["content_version"]) is None
            or not isinstance(references, list) or not references
            or any(not _within_scope(ref, source_keys) for ref in references)
            or (metadata.get("type") == "wiki") != (match[1] == "topics")):
        _fail("invalid_rebuild_target")
    return {"path": path, "sha256": _hash(raw), "id": metadata["id"],
            "content_version": metadata["content_version"]}


def _dependency_scope(git, entries, snapshot=None):
    from .controls import _metadata_only, MAX_FORMAL_OBJECTS
    removed = {(item["id"], item["content_version"]) for item in entries if item["path"].startswith("memory/records/")}
    selected = {item["path"] for item in entries}
    topics = git.text("ls-tree", "-r", "--name-only", snapshot or git.head(), "--", "memory/topics").splitlines()
    topics = [path for path in topics if _FORMAL.fullmatch(path)]
    if len(topics) > MAX_FORMAL_OBJECTS:
        _fail("rebuild_scope_limit")
    for path in topics:
        if path in selected:
            continue
        metadata = (_metadata_only(git, path) if snapshot is None
                    else parse_markdown(git.read(snapshot, path))[0])
        references = metadata.get("records", [])
        if (not isinstance(references, list)
                or any(not isinstance(ref, dict) or not isinstance(ref.get("id"), str)
                       or not isinstance(ref.get("content_version"), str) for ref in references)):
            _fail("invalid_rebuild_target")
        if any((ref.get("id"), ref.get("content_version")) in removed for ref in references):
            _fail("rebuild_dependency_scope")


def _decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail()
            value[key] = item
        return value
    try:
        metadata, body = parse_markdown(raw)
        if metadata not in ({"schema_version": SCHEMA, "kind": KIND},
                            {"schema_version": SCHEMA_V2, "kind": KIND}):
            _fail()
        value = json.loads(body, object_pairs_hook=unique)
        expected = CONTRACT_V2 if metadata["schema_version"] == SCHEMA_V2 else CONTRACT
        if not isinstance(value, dict) or value.get("contract") != expected:
            _fail()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _original_context(git, path):
    additions = git.text("log", "--full-history", "--diff-filter=A", "--format=%H", git.head(), "--", path).splitlines()
    if len(additions) != 1:
        _fail()
    return {"path": path, "commit_sha": additions[0], "sha256": _hash(git.read(additions[0], path))}


def _reference(context):
    if (not isinstance(context, dict) or set(context) != {"path", "commit_sha", "sha256"}
            or not isinstance(context["path"], str) or _RECEIPT.fullmatch(context["path"]) is None
            or not isinstance(context["commit_sha"], str) or _COMMIT.fullmatch(context["commit_sha"]) is None
            or not isinstance(context["sha256"], str) or _DIGEST.fullmatch(context["sha256"]) is None):
        _fail()


def _previous_cleanup(git, context, paths, snapshot):
    # Inspect the fixed receipt role before recursion. Reprocessing receipts
    # cannot be chained to manufacture an unbounded authority graph.
    _reference(context)
    raw = git._existing(context["path"])
    if raw is None or _decode(raw).get("contract") != CONTRACT:
        _fail("rebuild_previous_must_cleanup")
    return validate_rebuild_context(git, context, paths, at_commit=snapshot)


def _unwritten_targets(git, targets, cleanup_commit, snapshot):
    for path in targets:
        if (git.run("ls-tree", "-z", snapshot, "--", path)
                or git.text("log", "--full-history", "--format=%H", cleanup_commit + ".." + snapshot, "--", path)):
            _fail("rebuild_target_changed")


def validate_rebuild_context(git, context, source_paths=None, *, at_commit=None):
    """Validate the original cleanup, not whether targets are still unwritten.

    Historical candidates use their own base head. Current source and deletion
    guards continue to apply; a historical receipt cannot resurrect an input.
    """
    from .pipeline import control_guard, normalize_source_paths
    from .controls import object_policy
    _reference(context)
    head, commit, path = git.head(), context["commit_sha"], context["path"]
    snapshot = head if at_commit is None else at_commit
    if not isinstance(snapshot, str) or _COMMIT.fullmatch(snapshot) is None:
        _fail()
    git.run("merge-base", "--is-ancestor", commit, snapshot)
    git.run("merge-base", "--is-ancestor", snapshot, head)
    raw = git._existing(path)
    if raw is None or _hash(raw) != context["sha256"] or git.read(commit, path) != raw:
        _fail()
    value = _decode(raw)
    reprocessing = value.get("contract") == CONTRACT_V2
    fields = {"contract", "intent_id", "base_head", "branch", "sources", "controls_digest", "targets", "created_at"}
    if reprocessing:
        fields.add("previous_rebuild_context")
    if (not isinstance(value, dict) or set(value) != fields
            or _intent_path(value["intent_id"]) != path or not isinstance(value["base_head"], str)
            or _COMMIT.fullmatch(value["base_head"]) is None
            or not isinstance(value["branch"], str) or not value["branch"]
            or value["branch"] != git.text("symbolic-ref", "--quiet", "--short", "HEAD")
            or not isinstance(value["created_at"], str) or not value["created_at"]
            or not isinstance(value["sources"], list) or not value["sources"]
            or not isinstance(value["controls_digest"], str) or _DIGEST.fullmatch(value["controls_digest"]) is None
            or not isinstance(value["targets"], list)):
        _fail()
    parents = git.text("rev-list", "--parents", "-n", "1", commit).split()[1:]
    if parents != [value["base_head"]]:
        _fail()
    original_tree = git.run("ls-tree", "-z", commit, "--", path)
    for change in git.text("log", "--full-history", "--format=%H", head, "--", path).splitlines():
        if change != commit:
            merge_parents = git.text("rev-list", "--parents", "-n", "1", change).split()[1:]
            if len(merge_parents) < 2 or git.run("ls-tree", "-z", change, "--", path) != original_tree:
                _fail()
    paths = [source.get("path") if isinstance(source, dict) else None for source in value["sources"]]
    if paths != normalize_source_paths(paths, allow_versions=True, max_sources=256):
        _fail()
    if source_paths is not None and normalize_source_paths(source_paths, allow_versions=True, max_sources=256) != paths:
        _fail("rebuild_source_scope_changed")
    if _source_material(git, paths, snapshot=snapshot) != value["sources"]:
        _fail("rebuild_source_scope_changed")
    if (control_guard(git, value["base_head"])["digest"] != value["controls_digest"]
            or control_guard(git, snapshot)["digest"] != value["controls_digest"]):
        _fail("rebuild_controls_changed")
    if reprocessing:
        if value["targets"] != []:
            _fail("invalid_rebuild_scope")
        changes = git.text("diff-tree", "--no-commit-id", "--name-status", "-r", commit).splitlines()
        if changes != ["A\t" + path]:
            _fail("rebuild_cleanup_not_atomic")
        previous_ref = value["previous_rebuild_context"]
        previous = _previous_cleanup(git, previous_ref, paths, snapshot)
        git.run("merge-base", "--is-ancestor", previous_ref["commit_sha"], value["base_head"])
        if previous["sources"] != value["sources"]:
            _fail("rebuild_source_scope_changed")
        if previous["controls_digest"] != value["controls_digest"]:
            _fail("rebuild_controls_changed")
        # This establishes the new intent's original empty state. A replay
        # after its legitimate publication must not demand emptiness again.
        _unwritten_targets(git, [item["path"] for item in previous["targets"]],
                           previous_ref["commit_sha"], value["base_head"])
        return value
    targets = _targets([item.get("path") if isinstance(item, dict) else None for item in value["targets"]])
    if targets != [item["path"] for item in value["targets"]]:
        _fail()
    changes = git.text("diff-tree", "--no-commit-id", "--name-status", "-r", commit).splitlines()
    if set(changes) != {"A\t" + path, *("D\t" + target for target in targets)}:
        _fail("rebuild_cleanup_not_atomic")
    keys = {(source["source_id"], source["source_version"]) for source in value["sources"]}
    controls = control_guard(git)
    for item in value["targets"]:
        if (_formal(git.read(value["base_head"], item["path"]), item["path"], keys) != item
                or git.run("ls-tree", "-z", commit, "--", item["path"])):
            _fail()
        if object_policy(controls, item["path"], purpose="history" if at_commit is not None else "current",
                         content_version=item["content_version"])["excluded"]:
            _fail("rebuild_target_excluded")
    _dependency_scope(git, value["targets"], value["base_head"])
    return value


def allow_rebuild_targets(git, objects, context, *, at_commit=None):
    """Return only authorized old paths; the caller keeps normal create checks.

    Use candidate.base_head for a saved candidate's historical validation, and
    current HEAD for a pending publication. No write since cleanup is permitted.
    """
    value = validate_rebuild_context(git, context, at_commit=at_commit)
    if value["contract"] == CONTRACT_V2:
        # The new receipt grants a counted round, not a new deletion exception.
        # The original V1 cleanup must still authorize every inherited path.
        return allow_rebuild_targets(git, objects, value["previous_rebuild_context"], at_commit=at_commit)
    snapshot = git.head() if at_commit is None else at_commit
    paths = {item["path"] for item in value["targets"]}
    keys = {(source["source_id"], source["source_version"]) for source in value["sources"]}
    if not isinstance(objects, list):
        _fail("invalid_rebuild_target")
    for obj in objects:
        metadata = obj.get("metadata", {}) if isinstance(obj, dict) else {}
        references = metadata.get("sources")
        if (not isinstance(obj, dict) or not isinstance(obj.get("path"), str)
                or _FORMAL.fullmatch(obj["path"]) is None
                or not isinstance(references, list) or not references
                or any(not _within_scope(ref, keys) for ref in references)):
            _fail("rebuild_source_scope_changed")
    _unwritten_targets(git, paths, context["commit_sha"], snapshot)
    return paths


def authority_refs(git, context, *, at_commit=None):
    """Return both fixed receipts needed to verify a reprocessing authority."""
    value = validate_rebuild_context(git, context, at_commit=at_commit)
    refs = [dict(context)]
    if value["contract"] == CONTRACT_V2:
        refs.append(dict(value["previous_rebuild_context"]))
    return refs


def prepare_rebuild(repo, intent_id, source_paths, target_paths, expected_head=None, *, previous_rebuild_context=None):
    """Clear only selected formal files once, atomically recording their hashes.

    A caller creates this intent explicitly for an authorized cleanup. Queues do
    not create/renew it. Replaying an existing intent never deletes new knowledge.
    """
    from .pipeline import control_guard
    from .controls import object_policy
    git = PipelineGit(repo)
    reprocessing = previous_rebuild_context is not None
    if reprocessing:
        if target_paths not in ([], ()):
            _fail("invalid_rebuild_scope")
        _reference(previous_rebuild_context)
        targets = []
    else:
        targets = _targets(target_paths)
    path = _intent_path(intent_id)
    with git.lock():
        head = git.check(expected_head)
        sources = _source_material(git, source_paths)
        existing = git._existing(path)
        if existing is not None:
            context = _original_context(git, path)
            value = validate_rebuild_context(git, context, source_paths)
            if (targets != [item["path"] for item in value["targets"]]
                    or reprocessing != (value["contract"] == CONTRACT_V2)
                    or reprocessing and value["previous_rebuild_context"] != previous_rebuild_context):
                _fail("rebuild_intent_scope_changed")
            return {"status": "already_prepared", "head": head, "rebuild_context": context}
        if git.text("log", "--full-history", "--format=%H", head, "--", path):
            _fail()
        controls = control_guard(git)
        if reprocessing:
            previous = _previous_cleanup(git, previous_rebuild_context, source_paths, head)
            if previous["sources"] != sources:
                _fail("rebuild_source_scope_changed")
            if previous["controls_digest"] != controls["digest"]:
                _fail("rebuild_controls_changed")
            _unwritten_targets(git, [item["path"] for item in previous["targets"]],
                               previous_rebuild_context["commit_sha"], head)
            value = {"contract": CONTRACT_V2, "intent_id": intent_id, "base_head": head,
                     "branch": git.text("symbolic-ref", "--quiet", "--short", "HEAD"), "sources": sources,
                     "controls_digest": controls["digest"], "targets": [], "created_at": timestamp(),
                     "previous_rebuild_context": dict(previous_rebuild_context)}
            raw = markdown({"schema_version": SCHEMA_V2, "kind": KIND}, canonical(value).decode() + "\n")
            final = git.write_changes({path: raw}, {path: None}, head,
                                      "rebuild: record explicit reprocessing of cleared knowledge")
            return {"status": "prepared", "head": final,
                    "rebuild_context": {"path": path, "commit_sha": final, "sha256": _hash(raw)}}
        keys = {(source["source_id"], source["source_version"]) for source in sources}
        before, entries = {}, []
        for target in targets:
            raw = git._existing(target)
            if raw is None or git.read(head, target) != raw:
                _fail("rebuild_target_missing")
            entry = _formal(raw, target, keys)
            if object_policy(controls, target, content_version=entry["content_version"])["excluded"]:
                _fail("rebuild_target_excluded")
            before[target] = raw
            entries.append(entry)
        _dependency_scope(git, entries)
        value = {"contract": CONTRACT, "intent_id": intent_id, "base_head": head,
                 "branch": git.text("symbolic-ref", "--quiet", "--short", "HEAD"), "sources": sources,
                 "controls_digest": controls["digest"], "targets": entries, "created_at": timestamp()}
        raw = markdown({"schema_version": SCHEMA, "kind": KIND}, canonical(value).decode() + "\n")
        final = git.write_changes({**{target: None for target in targets}, path: raw},
                                  {**before, path: None}, head, "rebuild: clear explicitly selected formal knowledge")
        return {"status": "prepared", "head": final,
                "rebuild_context": {"path": path, "commit_sha": final, "sha256": _hash(raw)}}
