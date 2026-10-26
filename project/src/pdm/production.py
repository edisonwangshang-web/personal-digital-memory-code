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


class _GuardedModelCall(BudgetedModelCall):
    """Keep the real audit adapter while checking the gate before every start."""

    def __init__(self, delegate, repo, queue_id, run_id, guard):
        self._delegate = delegate
        self._repo, self._queue_id, self._run_id, self._guard = repo, queue_id, run_id, guard

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def for_operation(self, operation_id):
        return type(self)(self._delegate.for_operation(operation_id), self._repo,
                          self._queue_id, self._run_id, self._guard)

    def __call__(self, role, payload, timeout=120):
        self._guard(self._repo, "before_call", {"queue_id": self._queue_id,
                    "run_id": self._run_id, "operation_id": self.operation_id, "role": role})
        return self._delegate(role, payload, timeout=timeout)


def _guarded(model_call, guard, repo, queue_id, run_id):
    if guard is None:
        return model_call
    if isinstance(model_call, BudgetedModelCall):
        return _GuardedModelCall(model_call, repo, queue_id, run_id, guard)
    def invoke(role, payload, timeout=120):
        guard(repo, "before_call", {"queue_id": queue_id, "run_id": run_id,
                                    "operation_id": None, "role": role})
        return model_call(role, payload, timeout=timeout)
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


def prepare_production(repo, queue_id, source_paths, *, allowed_source_paths=None,
                       allowed_run_ids=(), max_input_bytes=128000, payload_overhead_bytes=16000,
                       required_context_sources=(), continuation_context=None, review_strategy=None,
                       skill_bundle=None, use_default_skills=False, rebuild_context=None):
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
            and all(isinstance(skills.get(role), dict) and skills[role].get("ref") == ref
                    for role, ref in (("extract", "knowledge-extract@10"),
                                      ("review", "knowledge-review@8"))))


def _revision_feedback(git, previous, state, projection, processing_paths, *, draft, merge_runs):
    """Bind a verified rejection to the same immutable source work, not old rules.

    New plan IDs may name a fresh current program snapshot. The complete old
    candidate/Review remains immutable feedback, while the new candidate must
    bind the new plan and independently pass its own publication proof.
    """
    from .pipeline import _candidate_bindings, _committed, _load, _stage_path
    from .revision_context import build_revision_context

    rejected = next((attempt for attempt in reversed(previous)
                     if attempt["status"] == "needs_revision"), None)
    if rejected is None:
        return None
    if draft or state["request"].get("continuation_context") is not None:
        _fail("queue_revision_phase_unsupported")
    candidate = _load(_committed(git, _stage_path(rejected["run_id"])), "candidate")
    expected_sources = [{key: value for key, value in source.items() if key != "line_count"}
                        for source in state["plan"]["sources"]]
    old_context = candidate.get("batch_context")
    material = candidate.get("batch_material", {})
    if (sorted(item["path"] for item in _candidate_bindings(candidate)) != processing_paths
            or _candidate_bindings(candidate) != expected_sources
            or candidate.get("related_runs") != state["related_runs"]
            or candidate.get("rebuild_context") != state["request"].get("rebuild_context")
            or candidate.get("controls_digest") != state["plan"]["snapshot"]["controls_digest"]
            or candidate.get("execution_context") is not None
            or candidate.get("continuation_context") is not None
            or not isinstance(old_context, dict)
            or old_context.get("source_ranges") != projection["batch_context"]["source_ranges"]
            or candidate.get("source_views") != projection["source_views"]
            or material.get("draft_only") is not False
            or sorted(item["run_id"] for item in candidate.get("merge_inputs", [])) != sorted(merge_runs)
            or candidate.get("review_strategy", "legacy-v1") != state["request"].get("review_strategy", "legacy-v1")):
        _fail("queue_revision_context_mismatch")
    # This checks the full actual independent verdict, candidate computation,
    # source/related scope, current controls and every frozen ancestor binding.
    context = build_revision_context(git, rejected["run_id"], source_paths=processing_paths,
                                     related_runs=state["related_runs"])
    return {"run_id": rejected["run_id"], "context_sha256": _hash(context),
            "candidate_binding": candidate["binding"],
            "source_plan_id": old_context["plan_id"],
            "target_plan_id": projection["batch_context"]["plan_id"]}


def _attempt(repo, state, projection, work, model_call, *, draft, merge_runs=(), execution_guard=None):
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
    strategy = state["request"].get("review_strategy", "legacy-v1")
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
    admission_policy = feedback_policy or rebuild_context is not None
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
    registrations, resumptions = {}, {}
    for old in previous.get("attempts", []) if previous else []:
        if old["status"] not in {"reserved", "unknown", "partial", "needs_confirmation", "registration_required"}:
            continue
        marker = _run_path(old["run_id"], "pending-registration")
        if git.path(marker).is_file():
            from .pipeline import resume_pending_registration
            registrations[old["run_id"]] = resume_pending_registration(
                repo, old["run_id"], allowed_source_paths=state["request"]["allowed_source_paths"])
        elif old["status"] in {"reserved", "unknown", "partial"} and strategy in {"auto", "decomposed-v1"}:
            from .pipeline import _stage_path, resume_decomposed_processing
            stage = _stage_path(old["run_id"])
            if git.path(stage).is_file() and not git.path(_run_path(old["run_id"], "review")).is_file():
                candidate = _load(_committed(git, stage), "candidate")
                if reusable(candidate):
                    delegate = (model_call.for_operation(old["operation_id"])
                                if isinstance(model_call, BudgetedModelCall) else model_call)
                    delegate = _guarded(delegate, execution_guard, repo, state["queue_id"], old["run_id"])
                    resumptions[old["run_id"]] = resume_decomposed_processing(
                        repo, old["run_id"], allowed_source_paths=state["request"]["allowed_source_paths"],
                        model_call=delegate)
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
        for old in attempts["attempts"]:
            if old["run_id"] in registrations:
                restored = registrations[old["run_id"]]
                old.update(status=restored["status"], error=restored.get("error"))
                head = _save(git, path, attempts, "production_attempts", head)
                return {**deepcopy(old), **restored}
            if old["run_id"] in resumptions:
                restored = resumptions[old["run_id"]]
                old.update(status=restored["status"], error=restored.get("error"))
                head = _save(git, path, attempts, "production_attempts", head)
                return {**deepcopy(old), **restored}
            if old["status"] == "passed":
                candidate, _, _ = _read_reviewed(git, old["run_id"], historical=True)
                if reusable(candidate):
                    return {"status": "passed", "run_id": old["run_id"], "reused": True}
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
            if old["status"] in {"needs_confirmation", "registration_required", "blocked_capacity", "blocked_context"}:
                return deepcopy(old)
        if len(attempts["attempts"]) >= 4:
            return {"status": "attempt_limit", "attempts": 4, "next_action": "operator_repair"}
        number = len(attempts["attempts"]) + 1
        run_id = "run-" + _hash({"work": work_key, "attempt": number})[:32]
        operation = "batch:" + work_key + ":" + str(number)
        delegate = model_call.for_operation(operation) if isinstance(model_call, BudgetedModelCall) else model_call
        reuse_extraction = None
        if isinstance(model_call, BudgetedModelCall) and rebuild_context is None:
            for old in reversed(attempts["attempts"]):
                if old["status"] != "failed" or old.get("error") not in SAFE_REVIEW_PRELAUNCH_ERRORS:
                    continue
                previous = model_call.ledger.operation(old["operation_id"])
                if (previous is not None and "review" not in previous["calls"]
                        and git.path("memory/staging/" + old["run_id"] + ".md").is_file()):
                    candidate = _load(_committed(git, "memory/staging/" + old["run_id"] + ".md"), "candidate")
                    if reusable(candidate):
                        reuse_extraction = old["run_id"]
                        break
        feedback = None
        if feedback_policy:
            try:
                feedback = _revision_feedback(git, attempts["attempts"], state, projection,
                                              processing_paths, draft=draft, merge_runs=merge_runs)
            except CaptureError as error:
                if error.code not in {"queue_revision_phase_unsupported", "queue_revision_context_mismatch"}:
                    raise
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
        if not feedback_policy and projection["input_bytes"] + overhead > MAX_INPUT_BYTES:
            return {"status": "blocked_capacity", "next_action": "replan"}
        current = {"run_id": run_id, "operation_id": operation, "attempt": number, "status": "reserved"}
        process_options = {"related_runs": [] if draft else state["related_runs"], "run_id": run_id,
                           "batch_context": projection["batch_context"], "source_views": projection["source_views"],
                           "draft_only": draft, "merge_runs": merge_runs, "reuse_extraction_run": reuse_extraction,
                           "continuation_context": continuation,
                           **({"skill_bundle": skill_bundle} if skill_bundle is not None else {}),
                           **({"review_strategy": strategy} if strategy != "legacy-v1" else {})}
        if rebuild_context is not None:
            process_options["rebuild_context"] = deepcopy(rebuild_context)
        if feedback is not None:
            process_options.update(revision_from_run=feedback["run_id"], reuse_extraction_run=None)
            current.update(revision_from_run=feedback["run_id"],
                           revision_context_sha256=feedback["context_sha256"],
                           revision_candidate_binding=feedback["candidate_binding"],
                           revision_source_plan_id=feedback["source_plan_id"],
                           revision_target_plan_id=feedback["target_plan_id"])
        if not admission_policy:
            attempts["attempts"].append(current)
            _save(git, path, attempts, "production_attempts", head)
    if admission_policy:
        # Render the complete request before claiming an attempt, audit roles or
        # processing reservation. Avoid a reentrant Git lock around preparation.
        from .pipeline import preflight_processing
        checked = preflight_processing(repo, processing_paths, **process_options)
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
    return {**result, "status": status, "attempt": number}


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
            if not projection["coverage"]["complete"]:
                state["status"] = "blocked_capacity"
                break
            result = _attempt(repo, state, projection, "final", model_call, draft=False,
                              merge_runs=sorted(set(state["completed_batches"].values())), execution_guard=execution_guard)
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
