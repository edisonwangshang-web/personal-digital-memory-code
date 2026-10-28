"""Reuse one completed, fixed extraction whose Review never started.

The witness records local Git/audit observations, not a server output signature.
Large original payload/response values are read from the original fixed stage.
"""

from copy import deepcopy
import hashlib
import re

from .contracts import CaptureError, canonical


CONTRACT = "capacity-blocked-stage-extraction-v1"
_RUN = re.compile(r"run-[0-9a-f]{32}\Z")
_POOL = ("schema_version", "mode", "pool_id", "authorization_sha256", "limit", "initial_used")
_PROTECTED = ("rules_version", "domain_policy_version", "domain_policy_sha256")


def _fail(code="stage_reuse_changed"):
    raise CaptureError(code, "原已完成提取、未启动审核或固定资料范围不一致；未新增提取调用")


def _sha(value):
    return "sha256-" + hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def _snapshot(git, path):
    from .pipeline import _committed, _last_commit
    commit = _last_commit(git, git.head(), path)
    if not commit:
        _fail()
    return {"path": path, "commit_sha": commit, "sha256": _sha(_committed(git, path, commit))}


def _fixed(git, ref, path, base, *, mutable=False):
    if not isinstance(ref, dict) or set(ref) != {"path", "commit_sha", "sha256"} or ref["path"] != path:
        _fail()
    git.run("merge-base", "--is-ancestor", ref["commit_sha"], base)
    raw = git.read(ref["commit_sha"], path)
    if _sha(raw) != ref["sha256"] or (not mutable and git._existing(path) != raw):
        _fail()
    return raw


def _scope(payload):
    value = deepcopy(payload)
    value.pop("extraction_feedback", None)  # Original attempt feedback is not a new fact or permission.
    if isinstance(value.get("controls"), dict):
        value["controls"].pop("head", None)
    if isinstance(value.get("batch_context"), dict):
        # Both identities are checked against a real, fixed single-batch plan.
        value["batch_context"].pop("plan_id", None)
        value["batch_context"].pop("batch_id", None)
    return _sha(value)


def _plan_scope(git, state, payload, versions, *, historical):
    from .batching import _context, _views, validate_plan
    from .pipeline import _load_sources
    plan, request = state["plan"], state["request"]
    forbidden = ("rebuild_context", "continuation_context")
    if (len(plan["batches"]) != 1 or not plan["coverage"]["complete"]
            or any(request.get(key) for key in forbidden)
            or plan["snapshot"]["versions"] != versions
            or request.get("skill_bundle") != payload.get("skill_bundle")
            or plan["source_paths"] != sorted(view["path"] for view in payload["sources"])
            or state["related_runs"] != [item["run_id"] for item in payload.get("related", [])]):
        _fail("stage_reuse_scope_changed")
    validate_plan(git.repo, plan, source_paths=plan["source_paths"],
                  require_current_versions=not historical, historical=historical, _locked=True)
    sources = _load_sources(git.repo, plan["source_paths"], allow_versions=True, purpose="history", max_sources=256)
    batch = plan["batches"][0]
    if (payload.get("batch_context") != _context(plan["plan_id"], batch["batch_id"], plan["fragments"], batch["parent_batch_ids"])
            or payload["sources"] != _views(sources, plan["fragments"])
            or payload.get("prior_candidates") != [] or payload.get("output_contract") != "batch-v1"):
        _fail("stage_reuse_scope_changed")


def _find_queue(git, payload, versions, *, source_run=None):
    from .pipeline import _committed, _load
    paths = git.text("ls-tree", "-r", "--name-only", "HEAD", "--", "memory/runs/processes/queues").splitlines()
    if len(paths) > 1024:
        _fail()
    for path in paths:
        if not path.endswith("/state.md"):
            continue
        state = _load(_committed(git, path), "production_queue")
        if (state["plan"]["plan_id"] != payload["batch_context"]["plan_id"]
                or source_run is not None and state.get("active_run_id") != source_run):
            continue
        _plan_scope(git, state, payload, versions, historical=source_run is not None)
        return _snapshot(git, path), state
    _fail("stage_reuse_plan_missing")


def _unstarted(git, plan, base, adapter=None, *, target_run=None):
    from .decomposed_review import COUNTERS, _hash, _node_identity
    from .pipeline import _run_path
    paths = [_run_path(plan["run_id"], name) for name in
             ("review", "manifest", "receipt", "pending", "pending-registration", "reused")]
    paths.append(f"memory/runs/processes/{plan['run_id']}/decomposed/")
    keys = [_hash(_node_identity(plan, unit))[7:] for unit in plan["units"]]
    if target_run is None:
        paths.extend(COUNTERS + key + ".md" for key in keys)
    if git.text("log", "--full-history", "--format=%H", base, "--", *paths):
        _fail("stage_reuse_review_started")
    if adapter is not None:
        prefixes = tuple("decomposed:" + key + ":" for key in keys)
        observed = {operation for operation in adapter.ledger.snapshot()["operations"] if operation.startswith(prefixes)}
        allowed = set()
        if target_run is not None:
            from .pipeline import _committed, _load
            for key in keys:
                path = COUNTERS + key + ".md"
                if git._existing(path) is None:
                    continue
                counter = _load(_committed(git, path), "decomposed_review_attempts")
                for attempt in counter["attempts"]:
                    prefix = f"memory/runs/processes/{target_run}/decomposed/"
                    if not all(attempt.get(field, "").startswith(prefix) for field in ("reservation_path", "result_path")):
                        _fail("stage_reuse_review_started")
                    allowed.add(attempt["operation_id"])
        if observed - allowed:
            _fail("stage_reuse_review_started")


def _claimed_stage(git, witness):
    from .pipeline import _candidate_binding, _committed, _load, _run_path, _stage_path
    run = witness["target"]["run_id"]
    if git._existing(_stage_path(run)) is None:
        return None
    candidate = _load(_committed(git, _stage_path(run)), "candidate")
    path = _run_path(run, "stage-extraction-reuse")
    raw = _committed(git, path)
    wrapper = _load(raw, "stage_extraction_reuse", "0.19")
    if (candidate.get("binding") != _candidate_binding(candidate)
            or candidate.get("run_id") != run
            or candidate.get("stage_reuse_ref") != {"path": path, "sha256": _sha(raw)}
            or wrapper != {"run_id": run, "witness": witness}):
        _fail()
    return run


def _material(git, refs, source_run, base, *, source_paths):
    from .decomposed_review import _plan, _read
    from .engine_provenance import read_reservation
    from .pipeline import (_call_metadata, _candidate_payload, _load, _read_fixed_candidate,
                           _run_path, _stage_path)
    expected = {"stage": _stage_path(source_run), "started": _run_path(source_run, "started"),
                "engine": _run_path(source_run, "engine"), "plan": _run_path(source_run, "decomposed-plan")}
    if not expected.keys() <= refs.keys():
        _fail()
    for key, path in expected.items():
        _fixed(git, refs[key], path, base)
    peek = _load(git.read(refs["stage"]["commit_sha"], refs["stage"]["path"]), "candidate")
    if any(key in peek for key in ("stage_reuse_ref", "stage_extraction_reuse")):
        _fail("stage_reuse_scope_unsupported")
    candidate, sources, raw, stage_commit = _read_fixed_candidate(
        git, source_run, source_paths=source_paths, historical=True)
    if (stage_commit != refs["stage"]["commit_sha"] or raw != git.read(stage_commit, refs["stage"]["path"])
            or candidate.get("stage_reuse_ref") is not None
            or candidate.get("stage_extraction_reuse") is not None
            or candidate.get("mode") != "incremental" or candidate.get("review_strategy") not in {"auto", "decomposed-v1"}
            or any(candidate.get(key) is not None for key in
                   ("rebuild_context", "continuation_context", "revision_context", "revision_resume", "revision_revalidation", "assembly"))
            or candidate.get("merge_inputs") != [] or candidate.get("batch_material", {}).get("draft_only") is not False):
        _fail("stage_reuse_scope_unsupported")
    payload = _candidate_payload(git, candidate, sources)
    output = candidate.get("raw_extraction_output", candidate["extraction_output"])
    response = {"output": deepcopy(output), **{key: deepcopy(candidate["extraction"][key])
                 for key in ("model", "invocation_id", "elapsed_seconds", "usage")}}
    if _call_metadata(response) != candidate["extraction"]:
        _fail()
    engine = read_reservation(git, source_run)
    if (engine is None or engine["versions"] != candidate["versions"]
            or refs["engine"]["commit_sha"] != refs["started"]["commit_sha"]):
        _fail()
    plan, _ = _read(git, refs["plan"]["path"], "decomposed_review_plan", commit=refs["plan"]["commit_sha"])
    if _plan(git, candidate, stage_commit, payload, prompt=plan["prompt"], request_contract=plan.get("request_contract")) != plan:
        _fail()
    return candidate, response, payload, plan


def prepare(git, source_run, *, target_payload, target_versions, work_id, target_attempt, model_call):
    """Observe one completed original and reserve no calls or data writes."""
    from .pipeline import _budget_adapter, _committed, _load, _run_path, _stage_path
    from .production import ATTEMPTS, _hash
    if (not isinstance(source_run, str) or _RUN.fullmatch(source_run) is None
            or not isinstance(work_id, str) or re.fullmatch(r"[0-9a-f]{64}", work_id) is None):
        _fail()
    base = git.head()
    refs = {name: _snapshot(git, path) for name, path in
            (("stage", _stage_path(source_run)), ("started", _run_path(source_run, "started")),
             ("engine", _run_path(source_run, "engine")), ("plan", _run_path(source_run, "decomposed-plan")))}
    candidate, response, payload, plan = _material(
        git, refs, source_run, base, source_paths=[view["path"] for view in target_payload["sources"]])
    refs["source_queue"], source_queue = _find_queue(git, payload, candidate["versions"], source_run=source_run)
    refs["target_queue"], _ = _find_queue(git, target_payload, target_versions)
    refs["counter"] = _snapshot(git, ATTEMPTS + work_id + ".md")
    counter = _load(_committed(git, refs["counter"]["path"]), "production_attempts")
    rows = counter["attempts"]
    if (not rows or rows[-1].get("run_id") != source_run or rows[-1].get("status") != "blocked_capacity"
            or rows[-1].get("error") != "decomposed_input_limit"
            or _hash(counter["identity"]) != work_id or type(target_attempt) is not int
            or target_attempt != len(rows) + 1 or target_attempt > 4):
        _fail("stage_reuse_attempt_mismatch")
    adapter = _budget_adapter(model_call)
    if adapter is None:
        _fail("stage_reuse_audit_mismatch")
    audit = adapter.ledger.snapshot()
    pool = {key: audit.get(key) for key in _POOL}
    if source_queue.get("audit_pool") != {key: pool[key] for key in ("pool_id", "authorization_sha256", "schema_version", "mode")}:
        _fail("stage_reuse_audit_mismatch")
    operation = adapter.ledger.operation(rows[-1]["operation_id"])
    call = operation.get("calls", {}).get("extract") if operation is not None else None
    witness = {"contract": CONTRACT, "authority": "local_git_and_audit_observation_not_server_signature",
               "base_head": base, "source_run_id": source_run, "snapshots": refs,
               "source_operation_id": rows[-1]["operation_id"], "audit_pool": pool,
               "completed_extract": deepcopy(call), "original_payload_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
               "original_request_binding": deepcopy(candidate["extraction_request_binding"]),
               "response_sha256": _sha(response),
               "target": {"work_id": work_id, "attempt": target_attempt,
                   "run_id": "run-" + _hash({"work": work_id, "attempt": target_attempt})[:32],
                   "operation_id": "batch:" + work_id + ":" + str(target_attempt),
                   "versions": deepcopy(target_versions), "scope_sha256": _scope(target_payload)}}
    read(git, witness, target_payload=target_payload, target_versions=target_versions, model_call=model_call)
    return witness


def read(git, witness, *, target_payload, target_versions, model_call=None, historical=False, require_claim=False):
    """Verify frozen evidence; historical reads never claim live ledger access."""
    from .model_calls import request_binding
    from .pipeline import _budget_adapter, _committed, _load
    from .production import ATTEMPTS, _hash
    fields = {"contract", "authority", "base_head", "source_run_id", "snapshots", "source_operation_id", "audit_pool",
              "completed_extract", "original_payload_sha256", "original_request_binding", "response_sha256", "target"}
    if (not isinstance(witness, dict) or set(witness) != fields or witness["contract"] != CONTRACT
            or witness["authority"] != "local_git_and_audit_observation_not_server_signature"):
        _fail()
    base, refs, target = witness["base_head"], witness["snapshots"], witness["target"]
    if (not isinstance(refs, dict) or set(refs) != {"stage", "started", "engine", "plan", "source_queue", "target_queue", "counter"}
            or not isinstance(target, dict) or set(target) != {"work_id", "attempt", "run_id", "operation_id", "versions", "scope_sha256"}
            or type(target["attempt"]) is not int
            or not isinstance(witness["audit_pool"], dict) or set(witness["audit_pool"]) != set(_POOL)):
        _fail()
    git.run("merge-base", "--is-ancestor", base, git.head())
    candidate, response, payload, plan = _material(
        git, refs, witness["source_run_id"], base,
        source_paths=[view["path"] for view in target_payload["sources"]])
    counter_path = ATTEMPTS + target["work_id"] + ".md"
    counter = _load(_fixed(git, refs["counter"], counter_path, base, mutable=True), "production_attempts")
    prefix = counter["attempts"]
    source_queue = _load(_fixed(git, refs["source_queue"], refs["source_queue"]["path"], base, mutable=True), "production_queue")
    target_queue = _load(_fixed(git, refs["target_queue"], refs["target_queue"]["path"], base, mutable=True), "production_queue")
    _plan_scope(git, source_queue, payload, candidate["versions"], historical=True)
    _plan_scope(git, target_queue, target_payload, target_versions, historical=historical)
    identity = {"fragments": sorted(item["fragment_id"] for item in payload["batch_context"]["source_ranges"]),
                "phase": "final", "related_runs": candidate["related_runs"]}
    call = witness["completed_extract"]
    if (counter["identity"] != identity or _hash(identity) != target["work_id"]
            or not prefix or prefix[-1].get("run_id") != candidate["run_id"]
            or prefix[-1].get("status") != "blocked_capacity" or prefix[-1].get("error") != "decomposed_input_limit"
            or prefix[-1].get("operation_id") != witness["source_operation_id"]
            or target["attempt"] != len(prefix) + 1 or not 1 < target["attempt"] <= 4
            or target["run_id"] != "run-" + _hash({"work": target["work_id"], "attempt": target["attempt"]})[:32]
            or target["operation_id"] != "batch:" + target["work_id"] + ":" + str(target["attempt"])
            or target["versions"] != target_versions
            or any(candidate["versions"].get(key) != target_versions.get(key) for key in _PROTECTED)
            or _scope(payload) != _scope(target_payload) or target["scope_sha256"] != _scope(target_payload)
            or witness["original_payload_sha256"] != hashlib.sha256(canonical(payload)).hexdigest()
            or witness["original_request_binding"] != candidate["extraction_request_binding"]
            or request_binding("extract", payload) != witness["original_request_binding"]
            or witness["response_sha256"] != _sha(response)
            or not isinstance(call, dict) or call.get("status") != "completed"
            or call.get("invocation_id") != response["invocation_id"]
            or call.get("payload_sha256") != witness["original_payload_sha256"]
            or source_queue.get("active_run_id") != candidate["run_id"]
            or source_queue.get("status") != "blocked_capacity"
            or source_queue.get("audit_pool") != {key: witness["audit_pool"].get(key) for key in
                                                 ("pool_id", "authorization_sha256", "schema_version", "mode")}):
        _fail()
    for number, row in enumerate(prefix, 1):
        if (row.get("attempt") != number
                or row.get("run_id") != "run-" + _hash({"work": target["work_id"], "attempt": number})[:32]
                or row.get("operation_id") != "batch:" + target["work_id"] + ":" + str(number)):
            _fail("stage_reuse_attempt_mismatch")
    current = _load(_committed(git, counter_path), "production_attempts")["attempts"]
    if current[:len(prefix)] != prefix or len(current) < len(prefix):
        _fail("stage_reuse_attempt_mismatch")
    if len(current) >= target["attempt"]:
        row = current[target["attempt"] - 1]
        if (any(row.get(key) != target[key] for key in ("attempt", "run_id", "operation_id"))
                or row.get("stage_reuse_sha256") != _sha(witness)):
            _fail("stage_reuse_attempt_mismatch")
    elif require_claim:
        _fail("stage_reuse_claim_missing")
    _unstarted(git, plan, base)
    if not historical:
        adapter = _budget_adapter(model_call)
        if (adapter is None or adapter.operation_id != target["operation_id"]
                or {key: adapter.ledger.snapshot().get(key) for key in _POOL} != witness["audit_pool"]):
            _fail("stage_reuse_audit_mismatch")
        operation = adapter.ledger.operation(witness["source_operation_id"])
        if (operation is None or operation["reserved_roles"] != ["extract", "review"]
                or operation["calls"] != {"extract": call}):
            _fail("stage_reuse_audit_mismatch")
        _unstarted(git, plan, git.head(), adapter, target_run=_claimed_stage(git, witness))
    return {"candidate": candidate, "response": response, "original_payload": payload,
            "snapshots": [deepcopy(ref) for ref in refs.values()]}


verify = read
