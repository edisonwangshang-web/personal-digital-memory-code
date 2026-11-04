"""Recoverable batch production over immutable, explicitly authorized inputs.

Discovery/planning, model attempts and publication are separate durable units.
This worker does not install a timer. Unknown requests never get replayed.
"""

from copy import deepcopy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .contracts import (CaptureError, QUALITY_SCHEMA_VERSIONS, REUSE_SCHEMA_VERSION,
                        canonical, markdown, parse_markdown, quality_schema_version)
from .budget import BudgetedModelCall
from .transaction import PipelineGit

SCHEMA = "0.8"
QUALITY_SCHEMA = "0.10"
LEGACY_SCHEMA = "0.6"
ROOT = "memory/runs/processes/queues/"
ATTEMPTS = "memory/runs/processes/queue-attempts/"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_CONTEXT_ERRORS = frozenset({"invalid_continuation_context", "continuation_source_scope",
    "continuation_context_changed", "continuation_related_scope", "continuation_reply_mismatch",
    "continuation_job_changed"})


def _fail(code):
    raise CaptureError(code, "批次生产暂停；保留原文、候选和调用记录，未自动重发未知请求")


def _attempt_result(result, number):
    # Keep the actual fourth verdict in the durable attempt row. The queue is
    # terminal immediately, even when this worker slice ends at that verdict.
    if type(number) is int and number == 4 and result["status"] in {"failed", "needs_revision"}:
        return {**result, "status": "attempt_limit", "attempts": 4,
                "last_attempt_status": result["status"], "next_action": "operator_repair"}
    return result


class _GuardedModelCall(BudgetedModelCall):
    """Keep the real audit adapter while checking the gate before every start."""

    def __init__(self, delegate, repo, queue_id, run_id, guard):
        self._delegate = delegate
        self._repo, self._queue_id, self._run_id, self._guard = repo, queue_id, run_id, guard

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def for_operation(self, operation_id, *, default_timeout=None):
        return type(self)(self._delegate.for_operation(operation_id, default_timeout=default_timeout), self._repo,
                          self._queue_id, self._run_id, self._guard)

    def __call__(self, role, payload, timeout=None):
        self._guard(self._repo, "before_call", {"queue_id": self._queue_id,
                    "run_id": self._run_id, "operation_id": self.operation_id, "role": role})
        return (self._delegate(role, payload) if timeout is None
                else self._delegate(role, payload, timeout=timeout))


def _guarded(model_call, guard, repo, queue_id, run_id):
    if guard is None:
        return model_call
    if isinstance(model_call, BudgetedModelCall):
        return _GuardedModelCall(model_call, repo, queue_id, run_id, guard)
    def invoke(role, payload, timeout=None):
        guard(repo, "before_call", {"queue_id": queue_id, "run_id": run_id,
                                    "operation_id": None, "role": role})
        return (model_call(role, payload) if timeout is None
                else model_call(role, payload, timeout=timeout))
    return invoke


def _checkpoint(guard, repo, state):
    if guard is not None:
        guard(repo, "after_checkpoint", {"queue_id": state["queue_id"],
              "run_id": state.get("active_run_id") or state.get("final_run"),
              "operation_id": None, "status": state["status"]})


@contextmanager
def _worker_lock(repo):
    """One worker per checkout, released by the OS even after a process crash.

    This lock is distinct from each short Git transaction lock. It covers the
    audit identity and model-call interval, so stale queue snapshots cannot
    overwrite another worker's progress or claimed audit pool.
    """
    git = PipelineGit(repo)
    path = Path(git.text("rev-parse", "--absolute-git-dir")) / "pdm-production-worker.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            _fail("unsafe_worker_lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("production_locked")
        yield
    finally:
        os.close(descriptor)


def _hash(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _path(queue_id):
    if not isinstance(queue_id, str) or not _ID.fullmatch(queue_id):
        _fail("invalid_queue_id")
    return ROOT + queue_id + "/state.md"


def _quality_schema(value):
    bundle = value.get("request", {}).get("skill_bundle")
    if bundle is not None:
        return quality_schema_version(bundle)
    marker = value.get("schema_version")
    if marker is not None and marker not in QUALITY_SCHEMA_VERSIONS:
        _fail("invalid_queue_quality_binding")
    return marker if marker is not None else QUALITY_SCHEMA


def _read(git, path, kind):
    data = git._existing(path)
    if data is None:
        if git.text("log", "-1", "--format=%H", "--", path):
            _fail("queue_state_deleted")
        return None
    if git.read(git.head(), path) != data:
        _fail("queue_state_changed")
    meta, body = parse_markdown(data)
    if meta not in tuple({"schema_version": version, "kind": kind}
                         for version in (*QUALITY_SCHEMA_VERSIONS, SCHEMA, "0.7", LEGACY_SCHEMA)):
        _fail("invalid_queue_state")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail("invalid_queue_state")
            result[key] = value
        return result
    try:
        value = json.loads(body, object_pairs_hook=unique,
                           parse_constant=lambda _: _fail("invalid_queue_state"))
    except (ValueError, TypeError, RecursionError):
        _fail("invalid_queue_state")
    if not isinstance(value, dict):
        _fail("invalid_queue_state")
    bundle = value.get("request", {}).get("skill_bundle") if kind == "production_queue" else None
    quality = bundle is not None or value.get("quality_contract") == "skill-quality-v1"
    if (quality != (meta["schema_version"] in QUALITY_SCHEMA_VERSIONS)
            or "schema_version" in value and (kind != "production_attempts" or not quality)
            or quality and meta["schema_version"] != _quality_schema(value)):
        _fail("invalid_queue_quality_binding")
    if bundle is not None:
        from .skill_support import validate_skill_bundle
        validate_skill_bundle(bundle)
    return value


def _save(git, path, value, kind, head):
    before = git._existing(path)
    quality = ("skill_bundle" in value.get("request", {})
               or value.get("quality_contract") == "skill-quality-v1")
    data = markdown({"schema_version": _quality_schema(value) if quality else SCHEMA, "kind": kind},
                    canonical(value).decode() + "\n")
    if data == before:
        return head
    return git.write_changes({path: data}, {path: before}, head, "queue: persist " + kind)


PROGRAM_REFRESH = "same-skill-program-refresh-v1"
PROGRAM_REFRESH_V2 = "same-skill-program-refresh-v2"
DOMAIN_POLICY_REFRESH = "same-sources-domain-policy-refresh-v1"
# This one registered upgrade keeps the existing 16/14 prompts and Skill tree.
# Historical proof must not start depending on future, unrelated Skill files.
_DOMAIN_POLICY_REFRESH_BINDINGS = {
    "three-domains-v1": {
        "domain_policy_sha256": "sha256-630587eb95be41d7effcdbe5d6e90b47b5921bf9341c8a17c91f1991dd1892eb",
        "rules_version": "sha256-79f0e45de05f2af7b17758607aadced30c9cd480ad49a08204f1b1aa29334b6a"},
    "three-domains-v2": {
        "domain_policy_sha256": "sha256-543a6f0a19d0058013c46e3d391e9a163c2092aa0ad8d7b1a7d69718f4798caf",
        "rules_version": "sha256-398f896124e21e3cc402cfef3f193937a4ca319f4667e5dded96e6afa62cb50a"},
}


def _program_work_identity(state):
    plan = state["plan"]
    if (len(plan["batches"]) != 1 or not plan["coverage"]["complete"]
            or set(plan["batches"][0]["fragment_ids"]) != {item["fragment_id"] for item in plan["fragments"]}):
        _fail("program_refresh_scope_changed")
    value = {"fragments": sorted(plan["batches"][0]["fragment_ids"]),
             "phase": "final", "related_runs": state["related_runs"]}
    for key in ("continuation_context", "rebuild_context"):
        if state["request"].get(key) is not None:
            value[key] = deepcopy(state["request"][key])
    return value


def _program_plan_inputs(plan):
    value = deepcopy(plan)
    value.pop("plan_id")
    value["snapshot"].pop("head")
    value["snapshot"].pop("versions")
    for batch in value["batches"]:
        batch.pop("batch_id")
    return value


def _program_versions(old, new):
    return (isinstance(old, dict) and isinstance(new, dict) and set(old) == set(new)
            and old != new and all(old[key] == new[key] for key in old
                                   if key not in {"program_commit", "program_sha256"}))


def _domain_policy_versions(old, new, *, admission=False):
    """The single registered v1-to-v2 policy upgrade, never an arbitrary rule change."""
    if not isinstance(old, dict) or not isinstance(new, dict) or set(old) != set(new):
        return False
    from .domains import domain_policy
    from .pipeline import _hash as policy_hash, _rules_version_for_domain_policy
    for value, version in ((old, "three-domains-v1"), (new, "three-domains-v2")):
        fixed = _DOMAIN_POLICY_REFRESH_BINDINGS[version]
        if (value.get("domain_policy_version") != version
                or any(value.get(key) != digest for key, digest in fixed.items())
                or policy_hash(domain_policy(version)) != fixed["domain_policy_sha256"]
                or admission and _rules_version_for_domain_policy(version) != fixed["rules_version"]):
            return False
    changed = {"program_commit", "program_sha256", "domain_policy_version", "domain_policy_sha256", "rules_version"}
    return all(old[key] == new[key] for key in old if key not in changed)


def _refresh_versions(contract, old, new):
    if contract in {PROGRAM_REFRESH, PROGRAM_REFRESH_V2}:
        return _program_versions(old, new)
    if contract == DOMAIN_POLICY_REFRESH:
        return _domain_policy_versions(old, new)
    return False


def _refresh_snapshot(git, ref):
    from .pipeline import _load
    if (not isinstance(ref, dict) or set(ref) != {"path", "commit_sha", "sha256"}
            or not isinstance(ref["path"], str)
            or re.fullmatch(re.escape(ROOT) + r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}/state\.md", ref["path"]) is None
            or not isinstance(ref["commit_sha"], str)
            or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", ref["commit_sha"]) is None):
        _fail("program_refresh_proof_changed")
    git.run("merge-base", "--is-ancestor", ref["commit_sha"], git.head())
    raw = git._storage_blob(ref["commit_sha"], ref["path"])
    if ref["sha256"] != "sha256-" + hashlib.sha256(raw).hexdigest():
        _fail("program_refresh_proof_changed")
    return _load(raw, "production_queue")


def _refresh_counter_snapshot(git, witness, revision):
    from .pipeline import _load
    path = witness["counter_path"]
    raw = (git._storage_blob(revision, path) if git.run("ls-tree", "-z", revision, "--", path) else None)
    digest = None if raw is None else "sha256-" + hashlib.sha256(raw).hexdigest()
    if digest != witness["counter_sha256"]:
        _fail("program_refresh_proof_changed")
    return None if raw is None else _load(raw, "production_attempts")


def _check_program_refresh_birth(git, state, revision):
    """Progress may retain a witness; a later edit cannot replace its birth."""
    from .pipeline import _load
    path, witness = _path(state["queue_id"]), state["program_refresh"]
    birth = None
    for commit in git.text("log", "--first-parent", "--format=%H", revision, "--", path).splitlines():
        if not git.run("ls-tree", "-z", commit, "--", path):
            break
        saved = _load(git._storage_blob(commit, path), "production_queue")
        if saved.get("program_refresh") != witness:
            break
        birth = commit
    if birth is None:
        _fail("program_refresh_proof_changed")
    parents = git.text("rev-list", "--parents", "-n", "1", birth).split()[1:]
    if (len(parents) != 1 or not git.run("ls-tree", "-z", parents[0], "--", path)
            or git._storage_blob(parents[0], path) != git._storage_blob(
                witness["previous_queue"]["commit_sha"], path)):
        _fail("program_refresh_proof_changed")


def _check_program_refresh_counter(git, state):
    """Validate every fixed hop and only append to original counter prefixes."""
    from .skill_support import resolve_skill_refs
    fields = {"contract", "previous_queue", "work_id", "counter_path", "counter_sha256"}
    seen = set()
    revision = git.head()
    try:
        for _ in range(64):
            witness = state.get("program_refresh")
            if witness is None:
                return
            if (not isinstance(witness, dict)
                    or set(witness) != fields | ({"feedback_projection"}
                        if witness.get("contract") == PROGRAM_REFRESH_V2 else set())):
                _fail("program_refresh_proof_changed")
            key = canonical(witness["previous_queue"])
            if key in seen:
                _fail("program_refresh_proof_changed")
            seen.add(key)
            old = _refresh_snapshot(git, witness["previous_queue"])
            _check_program_refresh_birth(git, state, revision)
            identity = _program_work_identity(old)
            work_id = _hash(identity)
            if (not _refresh_versions(witness.get("contract"), old["plan"]["snapshot"]["versions"],
                                     state["plan"]["snapshot"]["versions"])
                    or witness["work_id"] != work_id
                    or witness["counter_path"] != ATTEMPTS + work_id + ".md"
                    or state["queue_id"] != old["queue_id"]
                    or state["request"] != old["request"]
                    or state["related_runs"] != old["related_runs"]
                    or state["request"].get("skill_bundle") != resolve_skill_refs("knowledge-extract@16", "knowledge-review@14")
                    or _program_plan_inputs(state["plan"]) != _program_plan_inputs(old["plan"])
                    or _program_work_identity(state) != identity):
                _fail("program_refresh_proof_changed")
            fixed = _refresh_counter_snapshot(git, witness, witness["previous_queue"]["commit_sha"])
            if witness["contract"] == PROGRAM_REFRESH_V2:
                from .extraction_feedback import check_program_projection
                check_program_projection(witness["feedback_projection"], fixed)
            current = _read(git, witness["counter_path"], "production_attempts")
            prefix = [] if fixed is None else fixed.get("attempts")
            rows = [] if current is None else current.get("attempts")
            if (not isinstance(prefix, list) or not isinstance(rows, list)
                    or fixed is not None and fixed.get("identity") != identity
                    or current is not None and current.get("identity") != identity
                    or len(rows) < len(prefix) or rows[:len(prefix)] != prefix):
                _fail("program_refresh_proof_changed")
            # Old queue progress can legitimately follow its own refresh. Check
            # that hop's fixed transition and prefix, not a guessed old status.
            revision = witness["previous_queue"]["commit_sha"]
            state = old
        _fail("program_refresh_proof_changed")
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail("program_refresh_proof_changed")


def program_refresh_material(git, ref):
    """Read the fixed refresh revision; later queue progress never replaces it."""
    from .pipeline import _load
    from .skill_support import resolve_skill_refs
    try:
        state = _refresh_snapshot(git, ref)
        witness = state.get("program_refresh")
        fields = {"contract", "previous_queue", "work_id", "counter_path", "counter_sha256"}
        if (not isinstance(witness, dict)
                or set(witness) != fields | ({"feedback_projection"}
                    if witness.get("contract") == PROGRAM_REFRESH_V2 else set())
                or witness["contract"] not in {PROGRAM_REFRESH, PROGRAM_REFRESH_V2, DOMAIN_POLICY_REFRESH}
                or witness["previous_queue"].get("path") != ref["path"]):
            _fail("program_refresh_proof_changed")
        old = _refresh_snapshot(git, witness["previous_queue"])
        git.run("merge-base", "--is-ancestor", witness["previous_queue"]["commit_sha"], ref["commit_sha"])
        parents = git.text("rev-list", "--parents", "-n", "1", ref["commit_sha"]).split()[1:]
        if (len(parents) != 1 or not git.run("ls-tree", "-z", parents[0], "--", ref["path"])
                or git._storage_blob(parents[0], ref["path"]) != git._storage_blob(
                    witness["previous_queue"]["commit_sha"], ref["path"])):
            # A new v2 witness cannot discard its projection later by merely
            # declaring the old format. Genuine v1 births remain readable.
            _fail("program_refresh_proof_changed")
        expected = deepcopy(old)
        expected.update(plan=state["plan"], revision=old["revision"] + 1, program_refresh=witness)
        if state != expected:
            _fail("program_refresh_proof_changed")
        identity = _program_work_identity(old)
        work_id = _hash(identity)
        if (state["queue_id"] != old["queue_id"] or state["request"] != old["request"]
                or state["related_runs"] != old["related_runs"]
                or state["request"].get("skill_bundle") != resolve_skill_refs("knowledge-extract@16", "knowledge-review@14")
                or not _refresh_versions(witness["contract"], old["plan"]["snapshot"]["versions"],
                                         state["plan"]["snapshot"]["versions"])
                or _program_plan_inputs(old["plan"]) != _program_plan_inputs(state["plan"])
                or _program_work_identity(state) != identity
                or witness["work_id"] != work_id or witness["counter_path"] != ATTEMPTS + work_id + ".md"):
            _fail("program_refresh_proof_changed")
        raw = (git._storage_blob(ref["commit_sha"], witness["counter_path"])
               if git.run("ls-tree", "-z", ref["commit_sha"], "--", witness["counter_path"]) else None)
        digest = None if raw is None else "sha256-" + hashlib.sha256(raw).hexdigest()
        if digest != witness["counter_sha256"]:
            _fail("program_refresh_proof_changed")
        attempts = None if raw is None else _load(raw, "production_attempts")
        if attempts is not None and attempts.get("identity") != identity:
            _fail("program_refresh_proof_changed")
        if witness["contract"] == PROGRAM_REFRESH_V2:
            from .extraction_feedback import check_program_projection
            check_program_projection(witness["feedback_projection"], attempts)
        return old, state, attempts
    except (KeyError, TypeError, ValueError, AttributeError):
        _fail("program_refresh_proof_changed")


def _refresh_program(repo, git, path, state, model_call):
    """Explicit bounded program/policy refresh; never write a counter or call a model."""
    from .batching import plan_batches, validate_plan
    from .context import discover_context
    from .extraction_feedback import prepare_feedback
    from .pipeline import (_committed, _last_commit, _run_path, _stage_path,
                           _validate_continuation_context, execution_versions)
    from .skill_support import resolve_skill_refs
    if (not isinstance(model_call, BudgetedModelCall)
            or model_call.ledger.snapshot().get("mode") != "audit"):
        _fail("program_refresh_audit_required")
    if state["request"].get("skill_bundle") != resolve_skill_refs("knowledge-extract@16", "knowledge-review@14"):
        _fail("program_refresh_skill_changed")
    _check_program_refresh_counter(git, state)
    versions = execution_versions(git)
    if versions == state["plan"]["snapshot"]["versions"]:
        return {"queue_id": state["queue_id"], "status": state["status"], "state_path": path,
                "plan_id": state["plan"]["plan_id"], "head": git.head(), "reused": True, "program_refreshed": False}
    old_versions = state["plan"]["snapshot"]["versions"]
    contract = (PROGRAM_REFRESH_V2 if _program_versions(old_versions, versions)
                else DOMAIN_POLICY_REFRESH if _domain_policy_versions(old_versions, versions, admission=True) else None)
    if contract is None:
        _fail("program_refresh_rules_changed")
    with _worker_lock(repo):
        head = git.check()
        if _read(git, path, "production_queue") != state:
            _fail("queue_state_changed")
        if (state["status"] not in {"ready", "failed"} or state["completed_batches"]
                or state["final_run"] is not None or state["publication"] is not None):
            _fail("program_refresh_state_blocked")
        identity = _program_work_identity(state)
        work_id = _hash(identity)
        counter_path = ATTEMPTS + work_id + ".md"
        counter = _read(git, counter_path, "production_attempts")
        rows = counter.get("attempts", []) if counter is not None else []
        if counter is not None and counter.get("identity") != identity:
            _fail("queue_attempt_identity_conflict")
        if len(rows) >= 4:
            _fail("program_refresh_attempt_limit")
        pool = model_call.ledger.snapshot()
        binding = {key: pool[key] for key in ("pool_id", "authorization_sha256", "schema_version", "mode")}
        if state["audit_pool"] is not None and state["audit_pool"] != binding:
            _fail("program_refresh_audit_changed")
        if (rows and (state["status"] != "failed" or state["active_run_id"] != rows[-1]["run_id"])
                or not rows and (state["status"] != "ready" or state["active_run_id"] is not None
                                 or state["last_result"] is not None)):
            _fail("program_refresh_state_blocked")
        for number, row in enumerate(rows, 1):
            if (row.get("attempt") != number or row.get("status") != "failed"
                    or row.get("operation_id") != "batch:" + work_id + ":" + str(number)
                    or row.get("run_id") != "run-" + _hash({"work": work_id, "attempt": number})[:32]):
                _fail("program_refresh_state_blocked")
            if prepare_feedback(git, row, model_call.for_operation(row["operation_id"])) is None:
                _fail("program_refresh_state_blocked")
            forbidden = [_stage_path(row["run_id"]), *[_run_path(row["run_id"], name) for name in
                         ("review", "manifest", "receipt", "pending", "pending-registration", "reused")]]
            if any(git.path(item).is_file() or _last_commit(git, head, item) for item in forbidden):
                _fail("program_refresh_state_blocked")
        validate_plan(repo, state["plan"], source_paths=state["request"]["allowed_source_paths"],
                      require_current_versions=False)
        if state["request"].get("continuation_context") is not None:
            _validate_continuation_context(git, state["request"]["continuation_context"],
                                           state["plan"]["source_paths"], state["related_runs"])
        context = discover_context(repo, state["request"]["source_paths"],
                    allowed_source_paths=state["request"]["allowed_source_paths"],
                    allowed_run_ids=state["request"]["allowed_run_ids"], max_sources=256, max_runs=256)
        if not context["coverage"]["complete"] or context["related_runs"] != state["related_runs"]:
            _fail("program_refresh_scope_changed")
        options = {"rebuild_context": state["request"]["rebuild_context"]} if "rebuild_context" in state["request"] else {}
        plan = plan_batches(repo, state["plan"]["source_paths"], max_input_bytes=state["plan"]["max_input_bytes"],
                            payload_overhead_bytes=state["plan"]["payload_overhead_bytes"],
                            max_sources=state["plan"]["max_sources"], expected_head=head,
                            purpose=state["plan"]["purpose"], **options)
        if _program_plan_inputs(plan) != _program_plan_inputs(state["plan"]):
            _fail("program_refresh_scope_changed")
        previous = {"path": path, "commit_sha": _last_commit(git, head, path),
                    "sha256": "sha256-" + hashlib.sha256(_committed(git, path)).hexdigest()}
        raw = git._existing(counter_path)
        updated = deepcopy(state)
        updated.update(plan=plan, revision=state["revision"] + 1,
                       program_refresh={"contract": contract, "previous_queue": previous,
                           "work_id": work_id, "counter_path": counter_path,
                           "counter_sha256": None if raw is None else "sha256-" + hashlib.sha256(raw).hexdigest()})
        if contract == PROGRAM_REFRESH_V2:
            from .extraction_feedback import program_projection
            updated["program_refresh"]["feedback_projection"] = program_projection(
                git, updated, rows[-1] if rows else None, model_call)
        _refresh_counter_snapshot(git, updated["program_refresh"], previous["commit_sha"])
        with git.lock():
            git.check(head)
            if _read(git, path, "production_queue") != state:
                _fail("queue_state_changed")
            final = _save(git, path, updated, "production_queue", head)
        return {"queue_id": state["queue_id"], "status": state["status"], "state_path": path,
                "plan_id": plan["plan_id"], "previous_plan_id": state["plan"]["plan_id"],
                "work_id": work_id, "counter_path": counter_path, "attempts_used": len(rows),
                "head": final, "reused": False, "program_refreshed": True}


def prepare_production(repo, queue_id, source_paths, *, allowed_source_paths=None,
                       allowed_run_ids=(), max_input_bytes=128000, payload_overhead_bytes=16000,
                       required_context_sources=(), continuation_context=None, review_strategy=None,
                       skill_bundle=None, use_default_skills=False, rebuild_context=None,
                       refresh_program=False, model_call=None):
    """Persist one immutable plan; the stable caller key cannot be repurposed."""
    from .batching import plan_batches
    from .context import discover_context
    path = _path(queue_id)
    selected = sorted(source_paths)
    allowed = sorted(allowed_source_paths if allowed_source_paths is not None else selected)
    request = {"source_paths": selected, "allowed_source_paths": allowed,
               "allowed_run_ids": sorted(allowed_run_ids), "max_input_bytes": max_input_bytes,
               "payload_overhead_bytes": payload_overhead_bytes}
    required = []
    if required_context_sources:
        from .pipeline import normalize_source_paths
        required = normalize_source_paths(required_context_sources, allow_versions=True, max_sources=256)
        if not set(required) <= set(selected):
            _fail("queue_required_context_scope_mismatch")
        request["required_context_sources"] = required
    if continuation_context is not None:
        if (not isinstance(continuation_context, dict)
                or not isinstance(continuation_context.get("reply_sources"), list)
                or not continuation_context["reply_sources"]
                or any(not isinstance(ref, dict) or not isinstance(ref.get("path"), str)
                       for ref in continuation_context["reply_sources"])):
            _fail("queue_continuation_context_mismatch")
        if not {ref["path"] for ref in continuation_context["reply_sources"]} <= set(required):
            _fail("queue_required_context_scope_mismatch")
        request["continuation_context"] = deepcopy(continuation_context)
    git = PipelineGit(repo)
    head = git.check()
    if rebuild_context is not None:
        from .rebuild import validate_rebuild_context
        if request["allowed_run_ids"] or required or continuation_context is not None:
            _fail("invalid_queue_rebuild_context")
        validate_rebuild_context(git, rebuild_context, source_paths=selected)
        request["rebuild_context"] = deepcopy(rebuild_context)
    existing = _read(git, path, "production_queue")
    if type(refresh_program) is not bool or refresh_program and existing is None:
        _fail("program_refresh_existing_queue_required")
    from .unknown_retry import POLICY
    if existing is None or "timeout_retry_policy" in existing["request"]:
        request["timeout_retry_policy"] = POLICY if existing is None else existing["request"]["timeout_retry_policy"]
    if type(use_default_skills) is not bool:
        _fail("invalid_skill_default_policy")
    if skill_bundle is None and existing is not None:
        skill_bundle = existing["request"].get("skill_bundle")
    elif skill_bundle is None and use_default_skills:
        from .skill_support import resolve_skill_refs
        skill_bundle = resolve_skill_refs()
    if skill_bundle is not None:
        from .skill_support import validate_skill_bundle
        validate_skill_bundle(skill_bundle)
        request["skill_bundle"] = deepcopy(skill_bundle)
    if review_strategy not in (None, "legacy-v1", "auto", "decomposed-v1"):
        _fail("invalid_review_strategy")
    if review_strategy is None:
        if existing is None:
            request["review_strategy"] = "auto"
        elif "review_strategy" in existing["request"]:
            request["review_strategy"] = existing["request"]["review_strategy"]
    elif not (existing is not None and "review_strategy" not in existing["request"]
              and review_strategy == "legacy-v1"):
        request["review_strategy"] = review_strategy
    if existing is not None:
        if existing.get("request") != request:
            _fail("queue_identity_conflict")
        if refresh_program:
            return _refresh_program(repo, git, path, existing, model_call)
        return {"queue_id": queue_id, "status": existing["status"], "state_path": path,
                "plan_id": existing["plan"]["plan_id"], "head": head, "reused": True}
    context = discover_context(repo, selected, allowed_source_paths=allowed,
                               allowed_run_ids=list(allowed_run_ids), max_sources=256, max_runs=256)
    if not context["coverage"]["complete"]:
        return {"queue_id": queue_id, "status": "blocked_context", "context": context, "head": head}
    plan = plan_batches(repo, context["source_paths"], max_input_bytes=max_input_bytes,
                        payload_overhead_bytes=payload_overhead_bytes, purpose="history",
                        **({"rebuild_context": rebuild_context} if rebuild_context is not None else {}))
    if rebuild_context is not None and len(plan["batches"]) != 1:
        _fail("queue_rebuild_requires_single_batch")
    state = {"queue_id": queue_id, "request": request, "plan": plan, "related_runs": context["related_runs"],
             "status": "ready" if plan["coverage"]["complete"] else "blocked_capacity",
             "completed_batches": {}, "final_run": None, "publication": None,
             "audit_pool": None, "revision": 0, "last_result": None, "active_run_id": None}
    with git.lock():
        head = git.check(head)
        final = _save(git, path, state, "production_queue", head)
    return {"queue_id": queue_id, "status": state["status"], "state_path": path,
            "plan_id": plan["plan_id"], "coverage": plan["coverage"], "head": final, "reused": False}


def production_status(repo, queue_id):
    git = PipelineGit(repo)
    head = git.check()
    state = _read(git, _path(queue_id), "production_queue")
    if state is None:
        _fail("unknown_production_queue")
    return {"queue_id": queue_id, "status": state["status"], "plan_id": state["plan"]["plan_id"],
            "completed_batches": deepcopy(state["completed_batches"]), "final_run": state["final_run"],
            "publication": state["publication"], "coverage": state["plan"]["coverage"], "head": head,
            "last_result": deepcopy(state.get("last_result")), "active_run_id": state.get("active_run_id"),
            "next_action": ("done" if state["status"] == "published" else "reconcile" if state["status"] == "unknown"
                            else "repair_registration" if state["status"] == "registration_required"
                            else "await_reply" if state["status"] == "needs_confirmation"
                            else "operator_repair" if state["status"] in {"attempt_limit", "blocked_capacity", "blocked_context"} else "continue")}


def _feedback_enabled(bundle):
    """Only the new frozen contract changes a queue's retry semantics."""
    skills = bundle.get("skills", {}) if isinstance(bundle, dict) else {}
    return (isinstance(skills, dict)
            and any(all(isinstance(skills.get(role), dict) and skills[role].get("ref") == ref
                        for role, ref in pair) for pair in (
                    (("extract", "knowledge-extract@10"), ("review", "knowledge-review@8")),
                    (("extract", "knowledge-extract@11"), ("review", "knowledge-review@9")),
                    (("extract", "knowledge-extract@12"), ("review", "knowledge-review@10")),
            (("extract", "knowledge-extract@13"), ("review", "knowledge-review@11")),
            (("extract", "knowledge-extract@14"), ("review", "knowledge-review@12")),
            (("extract", "knowledge-extract@15"), ("review", "knowledge-review@13")),
            (("extract", "knowledge-extract@16"), ("review", "knowledge-review@14")))))


def _revision_feedback(git, previous, state, projection, processing_paths, *, draft, merge_runs):
    """Bind a verified rejection to the same immutable source work, not old rules.

    New plan IDs may name a fresh current program snapshot. The complete old
    candidate/Review remains immutable feedback, while the new candidate must
    bind the new plan and independently pass its own publication proof.
    """
    from .pipeline import _candidate_bindings, _committed, _load, _run_path, _stage_path
    from .quality_completed_extraction import CONTRACT_V2
    from .revision_context import build_revision_context

    rejected = None
    for attempt in reversed(previous):
        if attempt["status"] == "needs_revision":
            rejected = attempt
            break
        # V2 keeps the original program failure and counter row immutable.
        # Its later genuine Review is feedback without relabeling that row.
        if attempt["status"] != "failed" or attempt.get("error") != "quality_temporal_mismatch":
            continue
        stage_path = _stage_path(attempt["run_id"])
        if not git.path(stage_path).is_file():
            continue
        restored = _load(_committed(git, stage_path), "candidate")
        if restored.get("revision_revalidation", {}).get("contract") != CONTRACT_V2:
            continue
        review_path = _run_path(attempt["run_id"], "review")
        if not git.path(review_path).is_file():
            _fail("queue_revision_context_mismatch")
        review = _load(_committed(git, review_path), "review")
        if review.get("output", {}).get("verdict") != "needs_revision":
            _fail("queue_revision_context_mismatch")
        rejected = attempt
        break
    if rejected is None:
        return None
    candidate = _load(_committed(git, _stage_path(rejected["run_id"])), "candidate")
    expected_sources = [{key: value for key, value in source.items() if key != "line_count"}
                        for source in state["plan"]["sources"] if source["path"] in processing_paths]
    old_context = candidate.get("batch_context")
    material = candidate.get("batch_material", {})
    if (sorted(item["path"] for item in _candidate_bindings(candidate)) != processing_paths
            or _candidate_bindings(candidate) != expected_sources
            or candidate.get("related_runs") != ([] if draft else state["related_runs"])
            or candidate.get("rebuild_context") != state["request"].get("rebuild_context")
            or candidate.get("controls_digest") != state["plan"]["snapshot"]["controls_digest"]
            or candidate.get("execution_context") is not None
            or candidate.get("continuation_context") != state["request"].get("continuation_context")
            or not isinstance(old_context, dict)
            or old_context.get("source_ranges") != projection["batch_context"]["source_ranges"]
            or candidate.get("source_views") != projection["source_views"]
            or material.get("draft_only") is not draft
            or sorted(item["run_id"] for item in candidate.get("merge_inputs", [])) != sorted(merge_runs)
            or candidate.get("review_strategy", "legacy-v1") != state["request"].get("review_strategy", "legacy-v1")):
        _fail("queue_revision_context_mismatch")
    # This checks the full actual independent verdict, candidate computation,
    # source/related scope, current controls and every frozen ancestor binding.
    context = build_revision_context(git, rejected["run_id"], source_paths=processing_paths,
                                     related_runs=[] if draft else state["related_runs"])
    return {"run_id": rejected["run_id"], "context_sha256": _hash(context),
            "candidate_binding": candidate["binding"],
            "source_plan_id": old_context["plan_id"],
            "target_plan_id": projection["batch_context"]["plan_id"]}


def _timeout_feedback(git, ref, attempts, model_call):
    """Keep a timed-out extraction's fixed draft, with its original live audit."""
    from .unknown_retry import validate_ref
    inherited = validate_ref(git, ref, model_call=model_call)["original"].get("extraction_feedback_ref")
    if inherited is None:
        return None
    rows = [row for row in attempts if row.get("run_id") == inherited.get("source_run_id")]
    if len(rows) != 1:
        _fail("extraction_feedback_changed")
    from .extraction_feedback import prepare_feedback
    actual = prepare_feedback(git, rows[0], model_call)
    if actual != inherited:
        _fail("extraction_feedback_changed")
    return actual


def _stage_reuse_successor(git, source, successor, work_id):
    """Recognize a bound immediate adoption before considering the old source.

    The successor's actual candidate and Review still pass the normal recovery
    or revision verifier below; this linkage alone grants no model call.
    """
    witness = successor.get("stage_reuse")
    if witness is None:
        return False
    from .pipeline import _committed, _stage_path
    from .stage_extraction_reuse import CONTRACT
    target = witness.get("target") if isinstance(witness, dict) else None
    snapshots = witness.get("snapshots") if isinstance(witness, dict) else None
    ref = snapshots.get("stage") if isinstance(snapshots, dict) else None
    number = source.get("attempt")
    path = _stage_path(source["run_id"])
    if (not isinstance(witness, dict) or not isinstance(target, dict) or not isinstance(ref, dict)
            or witness.get("contract") != CONTRACT
            or witness.get("source_run_id") != source["run_id"]
            or successor.get("stage_reuse_sha256") != "sha256-" + _hash(witness)
            or type(number) is not int or not 1 <= number < 4
            or successor.get("attempt") != number + 1 or target.get("attempt") != number + 1
            or target.get("work_id") != work_id
            or any(successor.get(key) != target.get(key) for key in ("run_id", "operation_id"))
            or target.get("run_id") != "run-" + _hash({"work": work_id, "attempt": number + 1})[:32]
            or target.get("operation_id") != "batch:" + work_id + ":" + str(number + 1)
            or set(ref) != {"path", "commit_sha", "sha256"} or ref["path"] != path
            or ref["sha256"] != "sha256-" + hashlib.sha256(_committed(git, path)).hexdigest()
            or ref["commit_sha"] != git.text("log", "-1", "--format=%H", "HEAD", "--", path)):
        _fail("stage_reuse_attempt_mismatch")
    return True


def _attempt(repo, state, projection, work, model_call, *, draft, merge_runs=(), execution_guard=None, assembly=False):
    from .budget import BudgetedModelCall
    from .model_calls import MAX_INPUT_BYTES, PROMPTS
    from .pipeline import (_committed, _load, _read_reviewed, _run_path,
                           process_sources, SAFE_REVIEW_PRELAUNCH_ERRORS, _validate_continuation_context)
    plan = state["plan"]
    if state["request"].get("rebuild_context") != plan["snapshot"].get("rebuild_context"):
        _fail("queue_rebuild_plan_mismatch")
    # Identity deliberately excludes queue name, rule version and calendar day.
    # Renaming a queue/trigger cannot erase attempts for the same source ranges.
    identity = {"fragments": sorted(item["fragment_id"] for item in projection["batch_context"]["source_ranges"]),
                "phase": "draft" if draft else "final", "related_runs": state["related_runs"] if not draft else []}
    continuation = state["request"].get("continuation_context")
    if assembly and (state["related_runs"] or continuation is not None
                     or state["request"].get("rebuild_context") is not None):
        return {"status": "blocked_context", "error": "assembly_context_requires_merge",
                "attempted_calls": 0, "model_calls": 0}
    strategy = "decomposed-v1" if assembly else state["request"].get("review_strategy", "legacy-v1")
    skill_bundle = state["request"].get("skill_bundle")
    feedback_policy = _feedback_enabled(skill_bundle)
    processing_paths = plan["source_paths"]
    if skill_bundle is not None:
        # A quality draft profiles only the sources actually supplied to that
        # batch. The queue retains its complete authorized source inventory.
        views = projection.get("source_views")
        if not isinstance(views, list) or not views or any(
                not isinstance(view, dict) or not isinstance(view.get("path"), str) for view in views):
            _fail("queue_projection_scope_mismatch")
        processing_paths = sorted(view["path"] for view in views)
        scope = set(processing_paths)
        if (len(scope) != len(processing_paths) or not scope <= set(plan["source_paths"])
                or not scope <= set(state["request"]["allowed_source_paths"])
                or not draft and scope != set(plan["source_paths"])):
            _fail("queue_projection_scope_mismatch")
    if continuation is not None:
        identity["continuation_context"] = continuation
        try:
            _validate_continuation_context(PipelineGit(repo), continuation, processing_paths,
                                          [] if draft else state["related_runs"], draft_only=draft)
        except CaptureError as error:
            if error.code not in _CONTEXT_ERRORS:
                raise
            return {"status": "blocked_context", "error": error.code,
                    "attempted_calls": 0, "model_calls": 0}
    work_key = _hash(identity)
    path = ATTEMPTS + work_key + ".md"
    git = PipelineGit(repo)
    rebuild_context = state["request"].get("rebuild_context")
    if rebuild_context is not None:
        from .rebuild import validate_rebuild_context
        if (state["related_runs"] or state["request"]["allowed_run_ids"]
                or state["request"].get("required_context_sources") or continuation is not None):
            _fail("invalid_queue_rebuild_context")
        if draft or merge_runs or len(plan["batches"]) != 1:
            _fail("queue_rebuild_requires_single_batch")
        validate_rebuild_context(git, rebuild_context, source_paths=plan["source_paths"])
        identity["rebuild_context"] = deepcopy(rebuild_context)
        work_key = _hash(identity)
        path = ATTEMPTS + work_key + ".md"
    admission_policy = feedback_policy or rebuild_context is not None or assembly
    def reusable(candidate):
        return (candidate.get("versions") == plan["snapshot"]["versions"]
                and candidate.get("controls_digest") == plan["snapshot"]["controls_digest"]
                and candidate.get("batch_context") == projection["batch_context"]
                and candidate.get("batch_material", {}).get("draft_only") == draft
                and sorted(item["run_id"] for item in candidate.get("merge_inputs", [])) == sorted(merge_runs)
                and candidate.get("related_runs") == ([] if draft else state["related_runs"])
                and candidate.get("rebuild_context") == rebuild_context
                and candidate.get("continuation_context") == continuation
                and candidate.get("skill_bundle") == skill_bundle
                and candidate.get("review_strategy", "legacy-v1") == strategy)
    # Registration takes its own short CAS lock. Recover outside the attempt
    # transaction, without starting another extraction or Review invocation.
    previous = _read(git, path, "production_attempts")
    if previous is not None and previous.get("identity") != identity:
        _fail("queue_attempt_identity_conflict")
    for old in previous.get("attempts", []) if previous else []:
        if old["status"] == "attempt_limit":
            return deepcopy(old)
    from .unknown_retry import timeout_context
    retired, next_timeout_ref = (timeout_context(repo, state, work_key, previous, model_call)
                                 if previous is not None and execution_guard is None else (set(), None))
    registrations, resumptions = {}, {}
    for old in previous.get("attempts", []) if previous else []:
        if old["run_id"] in retired:
            continue
        if (old["status"] not in {"reserved", "unknown", "partial", "needs_confirmation", "registration_required"}
                and not (old["status"] == "failed" and old.get("stage_reuse") is not None)):
            continue
        marker = _run_path(old["run_id"], "pending-registration")
        if git.path(marker).is_file():
            from .pipeline import resume_pending_registration
            registrations[old["run_id"]] = resume_pending_registration(
                repo, old["run_id"], allowed_source_paths=state["request"]["allowed_source_paths"])
        elif (old["status"] in {"reserved", "unknown", "partial"}
              or old["status"] == "failed" and old.get("stage_reuse") is not None) and (strategy in {"auto", "decomposed-v1"} or old.get("timeout_retry_ref") is not None):
            from .pipeline import _stage_path, resume_decomposed_processing
            stage = _stage_path(old["run_id"])
            if (strategy in {"auto", "decomposed-v1"} and git.path(stage).is_file()
                    and not git.path(_run_path(old["run_id"], "review")).is_file()):
                candidate = _load(_committed(git, stage), "candidate")
                if reusable(candidate):
                    delegate = (model_call.for_operation(old["operation_id"])
                                if isinstance(model_call, BudgetedModelCall) else model_call)
                    delegate = _guarded(delegate, execution_guard, repo, state["queue_id"], old["run_id"])
                    resumptions[old["run_id"]] = resume_decomposed_processing(
                        repo, old["run_id"], allowed_source_paths=state["request"]["allowed_source_paths"],
                        model_call=delegate)
                    break
            elif old.get("timeout_retry_ref") is not None and not git.path(stage).is_file():
                from .unknown_retry import unstarted_resume
                delegate = model_call.for_operation(old["operation_id"]) if isinstance(model_call, BudgetedModelCall) else model_call
                delegate = _guarded(delegate, execution_guard, repo, state["queue_id"], old["run_id"])
                if isinstance(delegate, BudgetedModelCall) and unstarted_resume(git, old["timeout_retry_ref"], old["run_id"], delegate) is not None:
                    options = {"related_runs": [], "run_id": old["run_id"], "batch_context": projection["batch_context"],
                               "source_views": projection["source_views"], "review_strategy": strategy,
                               "skill_bundle": skill_bundle, "rebuild_context": rebuild_context,
                               "timeout_retry_ref": old["timeout_retry_ref"]}
                    inherited = _timeout_feedback(git, old["timeout_retry_ref"], previous["attempts"], delegate)
                    if inherited is not None:
                        options["extraction_feedback_ref"] = inherited
                    if old.get("revision_from_run") is not None:
                        options["revision_from_run"] = old["revision_from_run"]
                    resumptions[old["run_id"]] = process_sources(repo, processing_paths, model_call=delegate, **options)
                    break
            elif old.get("stage_reuse") is not None and not git.path(stage).is_file():
                if (draft or assembly or merge_runs or continuation is not None or rebuild_context is not None
                        or old.get("stage_reuse_sha256") != "sha256-" + _hash(old["stage_reuse"])):
                    _fail("stage_reuse_attempt_mismatch")
                delegate = (model_call.for_operation(old["operation_id"])
                            if isinstance(model_call, BudgetedModelCall) else model_call)
                delegate = _guarded(delegate, execution_guard, repo, state["queue_id"], old["run_id"])
                resumptions[old["run_id"]] = process_sources(repo, processing_paths, model_call=delegate,
                    related_runs=state["related_runs"], run_id=old["run_id"],
                    batch_context=projection["batch_context"], source_views=projection["source_views"],
                    review_strategy=strategy, skill_bundle=skill_bundle, stage_reuse=deepcopy(old["stage_reuse"]))
                break
            elif assembly and not git.path(stage).is_file():
                from .assembly_final import can_resume_unstaged_assembly, finalize_drafts
                number = old.get("attempt")
                if (type(number) is not int or not 1 <= number <= 4
                        or old.get("operation_id") != "batch:" + work_key + ":" + str(number)
                        or old.get("run_id") != "run-" + _hash({"work": work_key, "attempt": number})[:32]):
                    _fail("queue_attempt_identity_conflict")
                delegate = (model_call.for_operation(old["operation_id"])
                            if isinstance(model_call, BudgetedModelCall) else model_call)
                delegate = _guarded(delegate, execution_guard, repo, state["queue_id"], old["run_id"])
                if can_resume_unstaged_assembly(repo, plan, merge_runs, old["run_id"],
                                               model_call=delegate, skill_bundle=skill_bundle):
                    resumptions[old["run_id"]] = finalize_drafts(
                        repo, plan, merge_runs, old["run_id"], model_call=delegate)
                    break
    with git.lock():
        head = git.check()
        attempts = _read(git, path, "production_attempts") or {"identity": identity, "attempts": []}
        if attempts.get("identity") != identity:
            _fail("queue_attempt_identity_conflict")
        if skill_bundle is not None:
            attempts["quality_contract"] = "skill-quality-v1"
            selected_schema = quality_schema_version(skill_bundle)
            if selected_schema != QUALITY_SCHEMA:
                attempts["schema_version"] = selected_schema
        stage_source_run = None
        for index, old in enumerate(attempts["attempts"]):
            if old["run_id"] in retired:
                continue
            if old["run_id"] in registrations:
                restored = registrations[old["run_id"]]
                old.update(status=restored["status"], error=restored.get("error"))
                head = _save(git, path, attempts, "production_attempts", head)
                return {**deepcopy(old), **restored}
            if old["run_id"] in resumptions:
                restored = resumptions[old["run_id"]]
                old.update(status=restored["status"], error=restored.get("error"))
                head = _save(git, path, attempts, "production_attempts", head)
                result = _attempt_result({**deepcopy(old), **restored}, old["attempt"])
                # Receipt creation takes its own short lock, below this block.
                return result
            if old["status"] == "passed":
                candidate, _, _ = _read_reviewed(git, old["run_id"], historical=True)
                if reusable(candidate):
                    return {"status": "passed", "run_id": old["run_id"], "reused": True}
            if assembly and old["status"] == "needs_revision":
                # Re-reviewing the same verbatim union cannot repair a real
                # global rejection. Preserve feedback, without another call.
                return {"status": "blocked_context", "error": "assembly_revision_required",
                        "next_action": "operator_repair", "run_id": old["run_id"],
                        "attempted_calls": 0, "model_calls": 0}
                continue
            if old["status"] in {"reserved", "unknown", "partial"}:
                review_path = _run_path(old["run_id"], "review")
                if git.path(review_path).is_file():
                    candidate = _load(_committed(git, _run_path(old["run_id"], "review")), "review")
                    if candidate["output"]["verdict"] == "passed":
                        checked, _, _ = _read_reviewed(git, old["run_id"], historical=True)
                        old["status"] = "passed"
                        head = _save(git, path, attempts, "production_attempts", head)
                        if reusable(checked):
                            return {"status": "passed", "run_id": old["run_id"], "reused": True}
                        continue
                    _, checked_review, _ = _read_reviewed(
                        git, old["run_id"], historical=True, allow_nonpassed=True)
                    old["status"] = checked_review["output"]["verdict"]
                    head = _save(git, path, attempts, "production_attempts", head)
                    if old["status"] == "needs_confirmation":
                        return deepcopy(old)
                    continue
                # A timeout, missing result or uncertain launch is never a free retry.
                return {"status": "unknown", "run_id": old["run_id"], "next_action": "reconcile"}
            if (old["status"] == "blocked_capacity" and old.get("error") == "decomposed_input_limit"
                    and not draft and not assembly and not merge_runs and continuation is None
                    and rebuild_context is None and execution_guard is None
                    and isinstance(model_call, BudgetedModelCall)):
                from .skill_support import incremental_repair_enabled
                if incremental_repair_enabled(skill_bundle):
                    previous_stage = "memory/staging/" + old["run_id"] + ".md"
                    if git.path(previous_stage).is_file():
                        previous_candidate = _load(_committed(git, previous_stage), "candidate")
                        if (previous_candidate.get("batch_context", {}).get("plan_id") != plan["plan_id"]
                                and previous_candidate.get("stage_reuse_ref") is None):
                            if (index + 1 < len(attempts["attempts"])
                                    and _stage_reuse_successor(git, old, attempts["attempts"][index + 1], work_key)):
                                continue
                            stage_source_run = old["run_id"]
                            continue
            if old["status"] in {"needs_confirmation", "registration_required", "blocked_capacity", "blocked_context", "attempt_limit"}:
                return deepcopy(old)
        if len(attempts["attempts"]) >= 4:
            return {"status": "attempt_limit", "attempts": 4, "next_action": "operator_repair"}
        number = len(attempts["attempts"]) + 1
        run_id = "run-" + _hash({"work": work_key, "attempt": number})[:32]
        operation = "batch:" + work_key + ":" + str(number)
        delegate = model_call.for_operation(operation) if isinstance(model_call, BudgetedModelCall) else model_call
        reuse_extraction = None
        reused_feedback = None
        if isinstance(model_call, BudgetedModelCall) and rebuild_context is None and stage_source_run is None:
            for old in reversed(attempts["attempts"]):
                if old["status"] != "failed" or old.get("error") not in SAFE_REVIEW_PRELAUNCH_ERRORS:
                    continue
                previous = model_call.ledger.operation(old["operation_id"])
                if (previous is not None and "review" not in previous["calls"]
                        and git.path("memory/staging/" + old["run_id"] + ".md").is_file()):
                    candidate = _load(_committed(git, "memory/staging/" + old["run_id"] + ".md"), "candidate")
                    if reusable(candidate) and candidate.get("stage_reuse_ref") is None:
                        reuse_extraction = old["run_id"]
                        reused_feedback = deepcopy(candidate.get("extraction_feedback_ref"))
                        break
        feedback = None
        validation_feedback = reused_feedback
        if next_timeout_ref is not None:
            try:
                validation_feedback = _timeout_feedback(git, next_timeout_ref, attempts["attempts"], delegate)
            except CaptureError as error:
                return {"status": "blocked_context", "error": error.code,
                        "attempted_calls": 0, "model_calls": 0}
        if feedback_policy and not assembly and stage_source_run is None:
            try:
                feedback = _revision_feedback(git, attempts["attempts"], state, projection,
                                              processing_paths, draft=draft, merge_runs=merge_runs)
            except CaptureError as error:
                if error.code not in {"queue_revision_phase_unsupported", "queue_revision_context_mismatch"}:
                    raise
                return {"status": "blocked_context", "error": error.code,
                        "attempted_calls": 0, "model_calls": 0}
        if not assembly and reuse_extraction is None and stage_source_run is None and attempts["attempts"]:
            from .extraction_feedback import enabled, prepare_feedback
            previous_failed = attempts["attempts"][-1]
            if enabled(skill_bundle) and previous_failed["status"] == "failed":
                number_before = previous_failed.get("attempt")
                if (type(number_before) is not int or not 1 <= number_before < 4
                        or previous_failed.get("operation_id") != "batch:" + work_key + ":" + str(number_before)
                        or previous_failed.get("run_id") != "run-" + _hash({"work": work_key, "attempt": number_before})[:32]):
                    _fail("queue_attempt_identity_conflict")
                try:
                    validation_feedback = prepare_feedback(git, previous_failed, delegate)
                    if validation_feedback is not None and state.get("program_refresh") is not None:
                        from .extraction_feedback import _checked_failure
                        _, _, previous_context = _checked_failure(git, validation_feedback)
                        if previous_context["versions"] != plan["snapshot"]["versions"]:
                            from .pipeline import _last_commit
                            queue_path = _path(state["queue_id"])
                            validation_feedback["program_refresh"] = {
                                "path": queue_path, "commit_sha": _last_commit(git, git.head(), queue_path),
                                "sha256": "sha256-" + hashlib.sha256(_committed(git, queue_path)).hexdigest()}
                except CaptureError as error:
                    return {"status": "blocked_context", "error": error.code,
                            "attempted_calls": 0, "model_calls": 0}
        # Account for prior candidates, control/related payload and actual rules at
        # the real transport boundary, before reserving a durable processing run.
        # The transport rechecks the exact full serialized payload for each role.
        overhead = sum((PROMPTS / (role + ".md")).stat().st_size for role in ("extract", "review"))
        if skill_bundle is not None:
            from .model_calls import render_model_request
            overhead = max(len(render_model_request(role, {
                "quality_contract": "skill-quality-v1", "skill_bundle": skill_bundle}))
                for role in ("extract", "review"))
        if not assembly and not feedback_policy and projection["input_bytes"] + overhead > MAX_INPUT_BYTES:
            return {"status": "blocked_capacity", "next_action": "replan"}
        current = {"run_id": run_id, "operation_id": operation, "attempt": number, "status": "reserved"}
        process_options = {"related_runs": [] if draft else state["related_runs"], "run_id": run_id,
                           "batch_context": projection["batch_context"], "source_views": projection["source_views"],
                           "draft_only": draft, "merge_runs": merge_runs, "reuse_extraction_run": reuse_extraction,
                           "continuation_context": continuation,
                           **({"skill_bundle": skill_bundle} if skill_bundle is not None else {}),
                           **({"review_strategy": strategy} if strategy != "legacy-v1" else {})}
        if stage_source_run is not None:
            process_options["stage_reuse"] = {"source_run_id": stage_source_run,
                "work_id": work_key, "target_attempt": number}
        if next_timeout_ref is not None:
            current["timeout_retry_ref"] = deepcopy(next_timeout_ref)
            process_options["timeout_retry_ref"] = deepcopy(next_timeout_ref)
        if rebuild_context is not None:
            process_options["rebuild_context"] = deepcopy(rebuild_context)
        if feedback is not None:
            process_options.update(revision_from_run=feedback["run_id"], reuse_extraction_run=None)
            current.update(revision_from_run=feedback["run_id"],
                           revision_context_sha256=feedback["context_sha256"],
                           revision_candidate_binding=feedback["candidate_binding"],
                           revision_source_plan_id=feedback["source_plan_id"],
                           revision_target_plan_id=feedback["target_plan_id"])
        if validation_feedback is not None:
            process_options["extraction_feedback_ref"] = validation_feedback
            current["extraction_feedback_ref"] = deepcopy(validation_feedback)
        if not admission_policy:
            attempts["attempts"].append(current)
            _save(git, path, attempts, "production_attempts", head)
    if admission_policy:
        # Render the complete request before claiming an attempt, audit roles or
        # processing reservation. Avoid a reentrant Git lock around preparation.
        if assembly:
            from .assembly_final import preflight_assembly
            try:
                checked = preflight_assembly(repo, plan, merge_runs, run_id)
            except CaptureError as error:
                return {"status": "blocked_capacity" if error.code == "candidate_too_large" else "blocked_context",
                        "error": error.code, "attempted_calls": 0, "model_calls": 0}
        else:
            from .pipeline import preflight_processing
            try:
                checked = preflight_processing(repo, processing_paths, **process_options,
                    **({"stage_reuse_audit": delegate} if stage_source_run is not None else {}))
            except CaptureError as error:
                if stage_source_run is None:
                    raise
                return {"status": "blocked_context", "error": error.code,
                        "attempted_calls": 0, "model_calls": 0}
        if checked["status"] != "ready":
            if checked["status"] in {"unknown", "blocked_capacity"}:
                return checked
            if checked["status"] == "passed":
                candidate, _, _ = _read_reviewed(git, checked["run_id"], historical=True)
                if reusable(candidate):
                    return checked
            return {**checked, "status": "blocked_context",
                    "error": checked.get("error", "queue_revision_existing_attempt"),
                    "attempted_calls": 0, "model_calls": 0}
        if stage_source_run is not None:
            witness = checked.get("stage_reuse")
            if not isinstance(witness, dict):
                _fail("stage_reuse_changed")
            process_options["stage_reuse"] = deepcopy(witness)
            current.update(stage_reuse=deepcopy(witness), stage_reuse_sha256="sha256-" + _hash(witness))
        with git.lock():
            head = git.check(head)
            stored = _read(git, path, "production_attempts")
            # ``attempts`` may carry the new quality schema marker, but its old
            # entries must still match exactly. No competing claim may be lost.
            expected = deepcopy(stored) if stored is not None else {"identity": identity, "attempts": []}
            if skill_bundle is not None:
                expected["quality_contract"] = "skill-quality-v1"
                selected_schema = quality_schema_version(skill_bundle)
                if selected_schema != QUALITY_SCHEMA:
                    expected["schema_version"] = selected_schema
            if expected != attempts:
                _fail("queue_attempt_changed")
            attempts["attempts"].append(current)
            head = _save(git, path, attempts, "production_attempts", head)
            process_options["expected_head"] = head
    delegate = _guarded(delegate, execution_guard, repo, state["queue_id"], run_id)
    try:
        if assembly:
            from .assembly_final import finalize_drafts
            result = finalize_drafts(repo, plan, merge_runs, run_id, model_call=delegate)
        else:
            result = process_sources(repo, processing_paths, model_call=delegate, **process_options)
    except CaptureError as error:
        observed = delegate.ledger.operation(operation) if isinstance(delegate, BudgetedModelCall) else None
        if (admission_policy and error.code in {"input_limit", "head_conflict"}
                and (observed is None or not observed["started_calls"])):
            result = {"status": "blocked_capacity" if error.code == "input_limit" else "blocked_context",
                      "run_id": run_id, "error": error.code,
                      "attempted_calls": 0, "model_calls": 0}
        else:
            if error.code not in _CONTEXT_ERRORS or observed is not None and observed["started_calls"]:
                raise
            result = {"status": "blocked_context", "run_id": run_id, "error": error.code,
                      "attempted_calls": 0, "model_calls": 0}
    except BaseException:
        # Leave the durable reservation for reconciliation, including interrupts.
        raise
    if admission_policy and result.get("run_id") != run_id:
        # An explicitly started operator revision can share the feedback pointer.
        # Never mark this reservation passed when another run was returned.
        observed = delegate.ledger.operation(operation) if isinstance(delegate, BudgetedModelCall) else None
        calls = observed["started_calls"] if observed is not None else result.get("attempted_calls", 0)
        result = {"status": "unknown" if calls else "blocked_context", "run_id": run_id,
                  "error": "queue_revision_run_mismatch", "observed_run_id": result.get("run_id"),
                  "attempted_calls": calls, "model_calls": calls}
    status = result["status"]
    if status == "failed":
        status = "failed"
        if result.get("error") in {"input_limit", "candidate_too_large"}:
            status = "blocked_capacity"
        elif result.get("error") in _CONTEXT_ERRORS:
            status = "blocked_context"
        elif isinstance(delegate, BudgetedModelCall):
            op = delegate.ledger.operation(operation)
            if op and any(call["status"] in {"started", "unknown"} for call in op["calls"].values()):
                status = "unknown"
    with git.lock():
        head = git.check()
        stored = _read(git, path, "production_attempts")
        if stored != attempts:
            _fail("queue_attempt_changed")
        stored["attempts"][-1].update(status=status, error=result.get("error"))
        _save(git, path, stored, "production_attempts", head)
    return _attempt_result({**result, "status": status, "attempt": number}, number)


def run_production(repo, queue_id, *, model_call, max_steps=8, require_binding=False, execution_guard=None):
    if execution_guard is not None and not callable(execution_guard):
        _fail("invalid_execution_guard")
    with _worker_lock(repo):
        return _run_production(repo, queue_id, model_call=model_call,
                               max_steps=max_steps, require_binding=require_binding, execution_guard=execution_guard)


def _verify_queue_publication(git, state, publication):
    from .pipeline import _committed, _load, _read_reviewed
    manifest = _load(_committed(git, publication["manifest_path"]), "batch_group_manifest", "0.6")
    if manifest["plan"] != state["plan"]:
        _fail("queue_publication_plan_mismatch")
    for proof in manifest["proofs"]:
        candidate, _, _ = _read_reviewed(git, proof["run_id"], historical=True)
        if candidate.get("skill_bundle") != state["request"].get("skill_bundle"):
            _fail("queue_publication_skill_mismatch")
        if candidate.get("rebuild_context") != state["request"].get("rebuild_context"):
            _fail("queue_publication_rebuild_mismatch")
    if (_feedback_enabled(state["request"].get("skill_bundle")) and state["final_run"] is not None
            and {proof["run_id"] for proof in manifest["proofs"]} != {state["final_run"]}):
        _fail("queue_publication_run_mismatch")
    return manifest


def _run_production(repo, queue_id, *, model_call, max_steps=8, require_binding=False, execution_guard=None):
    """Advance bounded execution slices; counts do not impose a daily quota."""
    from .batching import project_batch_sources, merge_batch_sources, validate_plan
    from .batch_groups import publish_final_group
    from .budget import BudgetedModelCall
    from .pipeline import _read_reviewed
    from .runtime import require_environment
    if type(max_steps) is not int or not 1 <= max_steps <= 256:
        _fail("invalid_worker_slice")
    environment = require_environment(repo, require_binding=require_binding)
    if environment is not None and isinstance(model_call, BudgetedModelCall):
        if not model_call.ledger.path.is_relative_to(Path(environment["state_root"])):
            _fail("environment_audit_path_mismatch")
    if isinstance(model_call, BudgetedModelCall) and model_call.ledger.snapshot().get("mode") != "audit":
        _fail("audit_ledger_required")
    git = PipelineGit(repo)
    path = _path(queue_id)
    state = _read(git, path, "production_queue")
    if state is None:
        _fail("unknown_production_queue")
    _check_program_refresh_counter(git, state)
    if state["request"].get("rebuild_context") != state["plan"]["snapshot"].get("rebuild_context"):
        _fail("queue_rebuild_plan_mismatch")
    rebuild_context = state["request"].get("rebuild_context")
    if rebuild_context is not None:
        from .rebuild import validate_rebuild_context
        if (state["related_runs"] or state["request"]["allowed_run_ids"]
                or state["request"].get("required_context_sources")
                or state["request"].get("continuation_context") is not None):
            _fail("invalid_queue_rebuild_context")
        if len(state["plan"]["batches"]) != 1:
            _fail("queue_rebuild_requires_single_batch")
        validate_rebuild_context(git, rebuild_context, source_paths=state["plan"]["source_paths"])
    if state["audit_pool"] is not None:
        if not isinstance(model_call, BudgetedModelCall):
            _fail("queue_audit_identity_changed")
        snapshot = model_call.ledger.snapshot()
        actual_pool = {key: snapshot[key] for key in ("pool_id", "authorization_sha256", "schema_version", "mode")}
        if actual_pool != state["audit_pool"]:
            _fail("queue_audit_identity_changed")
    if state["status"] == "published":
        from .batch_groups import verify_group_publication
        publication = verify_group_publication(repo, state["publication"]["run_id"])
        _verify_queue_publication(git, state, publication)
        _checkpoint(execution_guard, repo, state)
        return production_status(repo, queue_id)
    validate_plan(repo, state["plan"], source_paths=state["request"]["allowed_source_paths"])
    # An explicitly reviewed operator revision can publish this complete plan
    # without rewriting the failed queue's attempt history. Discovery consumes
    # that real group before it considers a new extraction operation.
    from .batch_groups import _marker_path, verify_group_publication
    from .pipeline import _committed, _load, _hash as pipeline_hash
    marker_path = _marker_path(state["plan"])
    if git.path(marker_path).is_file():
        marker_head = git.check()
        marker = _load(_committed(git, marker_path), "batch_group_inputs", "0.6")
        if marker.get("plan_id") != state["plan"]["plan_id"] or marker.get("plan_sha256") != pipeline_hash(state["plan"]):
            _fail("batch_group_marker_mismatch")
        publication = verify_group_publication(repo, marker["run_id"], source_paths=state["plan"]["source_paths"])
        # A plan's prior publication is reusable only under the same frozen
        # quality rules. A legacy passed Review is not a quality-mode Review.
        manifest = _verify_queue_publication(git, state, publication)
        if _feedback_enabled(state["request"].get("skill_bundle")):
            # The marker proves an independently completed operator group for
            # this exact plan/rules. Persist the queue's adoption, never rewrite
            # failed attempts or infer a pass from a marker alone.
            reviewed = sorted(proof["run_id"] for proof in manifest["proofs"])
            adopted = deepcopy(state)
            adopted.update(status="published", publication=publication,
                           revision=state["revision"] + 1,
                           last_result={"status": "published", "adopted": True,
                                        "publication_run_id": publication["run_id"],
                                        "reviewed_run_ids": reviewed,
                                        "attempted_calls": 0, "model_calls": 0})
            if len(reviewed) == 1:
                adopted.update(final_run=reviewed[0], active_run_id=reviewed[0])
            with git.lock():
                head = git.check(marker_head)
                if _read(git, path, "production_queue") != state:
                    _fail("queue_state_changed")
                _save(git, path, adopted, "production_queue", head)
            _checkpoint(execution_guard, repo, adopted)
            return {**production_status(repo, queue_id), "queue_status": "published",
                    "reused": True, "adopted": True, "steps": 0,
                    "external_wakeup": "not_provisioned"}
        _checkpoint(execution_guard, repo, state)
        return {**production_status(repo, queue_id), "queue_status": state["status"],
                "status": "published", "publication": publication, "reused": True, "steps": 0,
                "external_wakeup": "not_provisioned"}
    if not state["plan"]["coverage"]["complete"]:
        return production_status(repo, queue_id)
    if isinstance(model_call, BudgetedModelCall):
        snapshot = model_call.ledger.snapshot()
        binding = {key: snapshot[key] for key in ("pool_id", "authorization_sha256", "schema_version", "mode")}
        if state["audit_pool"] is not None and state["audit_pool"] != binding:
            _fail("queue_audit_identity_changed")
        state["audit_pool"] = binding
        with git.lock():
            head = git.check()
            latest = _read(git, path, "production_queue")
            if latest["audit_pool"] is not None and latest["audit_pool"] != binding:
                _fail("queue_audit_identity_changed")
            _save(git, path, state, "production_queue", head)
        _checkpoint(execution_guard, repo, state)
    steps = 0
    while steps < max_steps:
        batches = state["plan"]["batches"]
        pending = next((batch for batch in batches if batch["batch_id"] not in state["completed_batches"]), None)
        if pending is not None:
            required = state["request"].get("required_context_sources", [])
            if required:
                from .batching import project_batch_with_context
                projection = project_batch_with_context(repo, state["plan"], pending["batch_id"],
                    source_paths=state["request"]["allowed_source_paths"], required_source_paths=required)
            else:
                projection = project_batch_sources(repo, state["plan"], pending["batch_id"],
                                                    source_paths=state["request"]["allowed_source_paths"])
            if not projection["coverage"]["complete"]:
                state["status"] = "blocked_capacity"
                break
            single = len(batches) == 1
            result = _attempt(repo, state, projection, pending["batch_id"], model_call, draft=not single,
                              execution_guard=execution_guard)
            from .unknown_retry import auto_timeout_result
            if execution_guard is None:
                result = auto_timeout_result(repo, state, None, result, model_call)
            state.update(last_result=deepcopy(result), active_run_id=result.get("run_id"))
            steps += 1
            if result["status"] == "passed":
                state["completed_batches"][pending["batch_id"]] = result["run_id"]
                if single:
                    state["final_run"] = result["run_id"]
                state["status"] = "ready"
            else:
                state["status"] = result["status"]
        elif state["final_run"] is None:
            projection = merge_batch_sources(repo, state["plan"], [batch["batch_id"] for batch in batches],
                                              source_paths=state["request"]["allowed_source_paths"],
                                              max_bytes=state["plan"]["max_input_bytes"])
            assembly = not projection["coverage"]["complete"]
            if assembly:
                from .assembly_final import review_projection
                if set(projection["coverage"]["blocked_reasons"]) != {"merge_input_limit"}:
                    state["status"] = "blocked_capacity"
                    break
                projection = review_projection(repo, state["plan"])
            result = _attempt(repo, state, projection, "final", model_call, draft=False,
                              merge_runs=sorted(set(state["completed_batches"].values())), execution_guard=execution_guard,
                              assembly=assembly)
            from .unknown_retry import auto_timeout_result
            if execution_guard is None:
                result = auto_timeout_result(repo, state, None, result, model_call)
            state.update(last_result=deepcopy(result), active_run_id=result.get("run_id"))
            steps += 1
            if result["status"] == "passed":
                state["final_run"] = result["run_id"]
                state["status"] = "ready"
            else:
                state["status"] = result["status"]
        else:
            candidate, _, _ = _read_reviewed(git, state["final_run"])
            if execution_guard is not None:
                execution_guard(repo, "before_publish", {"queue_id": state["queue_id"],
                                "run_id": state["final_run"], "operation_id": None})
            publication = publish_final_group(repo, state["plan"], candidate["objects"], [state["final_run"]],
                                              source_paths=state["request"]["allowed_source_paths"])
            state.update(status="published", publication=publication)
        with git.lock():
            head = git.check()
            old = _read(git, path, "production_queue")
            state["revision"] = old["revision"] + 1
            _save(git, path, state, "production_queue", head)
        _checkpoint(execution_guard, repo, state)
        if state["status"] not in {"ready", "failed", "needs_revision"} or state["status"] == "published":
            break
    # Persist capacity blocks and the selected audit identity even without calls.
    with git.lock():
        head = git.check()
        stored = _read(git, path, "production_queue")
        if stored != state:
            state["revision"] = stored["revision"] + 1
            _save(git, path, state, "production_queue", head)
    _checkpoint(execution_guard, repo, state)
    return {**production_status(repo, queue_id), "steps": steps, "external_wakeup": "not_provisioned"}
