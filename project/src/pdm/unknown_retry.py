"""Bounded retries of a locally finished extraction timeout.

Receipts retire an attempt's right to publish; they never relabel its model
call, cancel a remote request, refund usage, or create a new work allowance.
"""

from copy import deepcopy
import json
import re

from .contracts import CaptureError, canonical, markdown, parse_markdown, timestamp
from .transaction import PipelineGit

POLICY = "bounded-timeout-v1"
CONTRACT = "automatic-extraction-timeout-retry-v1"
SCHEMA = "0.16"
ROOT = "memory/runs/processes/timeout-retries/"
_REF = re.compile(re.escape(ROOT) + r"([0-9a-f]{64})/(run-[0-9a-f]{32})\.md\Z")


def _fail(code="timeout_retry_changed"):
    raise CaptureError(code, "超时重试的原调用、固定范围或接续记录不一致；未重发未知调用")


def _sha(value):
    import hashlib
    return "sha256-" + hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def _path(work, run):
    value = ROOT + work + "/" + run + ".md"
    if _REF.fullmatch(value) is None:
        _fail()
    return value


def _receipt_paths(git):
    current = set(git.text("ls-tree", "-r", "--name-only", "HEAD", "--", ROOT).splitlines())
    history = set(filter(None, git.text("log", "--full-history", "--format=", "--name-only",
                                       git.head(), "--", ROOT).splitlines()))
    if len(history) > 1024 or current != history or any(_REF.fullmatch(path) is None for path in current):
        _fail()
    return sorted(current)


def _json(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail()
            result[key] = value
        return result
    meta, body = parse_markdown(raw)
    if meta != {"schema_version": SCHEMA, "kind": "timeout_retry"}:
        _fail()
    try:
        value = json.loads(body, object_pairs_hook=unique)
    except (ValueError, TypeError, RecursionError):
        _fail()
    return value


def _snapshot(git, path, commit=None):
    from .pipeline import _committed, _last_commit
    revision = commit or _last_commit(git, git.head(), path)
    if not revision:
        _fail()
    raw = _committed(git, path, revision)
    return {"path": path, "commit_sha": revision, "sha256": _sha(raw)}


def _read_snapshot(git, ref):
    if not isinstance(ref, dict) or set(ref) != {"path", "commit_sha", "sha256"}:
        _fail()
    git.run("merge-base", "--is-ancestor", ref["commit_sha"], git.head())
    raw = git.read(ref["commit_sha"], ref["path"])
    if _sha(raw) != ref["sha256"]:
        _fail()
    return raw


def _queue_shape(state):
    plan, request = state["plan"], state["request"]
    if (len(plan["batches"]) != 1 or not plan["coverage"]["complete"]
            or state["related_runs"] or request.get("allowed_run_ids")
            or request.get("continuation_context") is not None
            or request.get("required_context_sources")
            or request["source_paths"] != request["allowed_source_paths"]
            or request["source_paths"] != plan["source_paths"]):
        _fail("timeout_retry_scope_unsupported")
    identity = {"fragments": sorted(f["fragment_id"] for f in plan["fragments"]),
                "phase": "final", "related_runs": []}
    if request.get("rebuild_context") is not None:
        if request["rebuild_context"] != plan["snapshot"].get("rebuild_context"):
            _fail()
        identity["rebuild_context"] = deepcopy(request["rebuild_context"])
    return identity


def _pool(ledger):
    value = ledger.snapshot()
    if value.get("mode") != "audit":
        _fail("audit_ledger_required")
    return {k: value[k] for k in ("schema_version", "mode", "pool_id", "authorization_sha256", "limit", "initial_used")}


def _bound_key(git, old, started, engine, row):
    from .batching import _context
    from .pipeline import _processing_key, _source_fields, load_source
    plan = old["plan"]
    batch = plan["batches"][0]
    context = _context(plan["plan_id"], batch["batch_id"], plan["fragments"], batch["parent_batch_ids"])
    strategy = old["request"].get("review_strategy", "legacy-v1")
    material = {"draft_only": False, "merge_bindings": []}
    if strategy != "legacy-v1":
        material["review_strategy"] = strategy
    sources = [load_source(git.repo, p, purpose="history") for p in plan["source_paths"]]
    revision = None
    if row.get("revision_from_run") is not None:
        from .revision_context import build_revision_context
        revision = build_revision_context(git, row["revision_from_run"], source_paths=plan["source_paths"],
                                          related_runs=[], at_commit=engine["data_base_head"])
        if (row.get("revision_context_sha256") != _sha(revision).removeprefix("sha256-")
                or row.get("revision_candidate_binding") != revision["candidate"]["binding"]
                or row.get("revision_source_plan_id") != revision["candidate"]["batch_context"]["plan_id"]
                or row.get("revision_target_plan_id") != context["plan_id"]):
            _fail("timeout_retry_revision_changed")
    elif any(key.startswith("revision_") for key in row):
        _fail("timeout_retry_revision_changed")
    return _processing_key(_source_fields(sources), engine["versions"], plan["snapshot"]["controls_digest"],
        context, material, review_strategy=strategy, revision_context=revision,
        skill_bundle=old["request"].get("skill_bundle"), rebuild_context=old["request"].get("rebuild_context"),
        timeout_retry_ref=started.get("timeout_retry_ref"))


def _old_plan(git, target, started, engine, row):
    from .production import ROOT as QUEUES, _read
    from .batching import validate_plan
    names = git.text("ls-tree", "-r", "--name-only", "HEAD", "--", QUEUES).splitlines()
    if len(names) > 1024:
        _fail()
    paths = [QUEUES + target["queue_id"] + "/state.md", *names]
    for path in dict.fromkeys(paths):
        if not path.endswith("/state.md"):
            continue
        old = _read(git, path, "production_queue")
        if old is None:
            continue
        try:
            if (_queue_shape(old) != _queue_shape(target)
                    or old["plan"]["sources"] != target["plan"]["sources"]
                    or old["plan"]["snapshot"]["controls_digest"] != target["plan"]["snapshot"]["controls_digest"]
                    or old["plan"]["snapshot"]["versions"] != engine["versions"]
                    or old["request"].get("skill_bundle") != target["request"].get("skill_bundle")):
                continue
            validate_plan(git.repo, old["plan"], source_paths=old["plan"]["source_paths"],
                          require_current_versions=False, historical=True, _locked=True)
            if _bound_key(git, old, started, engine, row) == started["processing_key"]:
                return _snapshot(git, path), _sha(old["plan"])
        except CaptureError:
            continue
    _fail("timeout_retry_original_plan_missing")


def _eligible(git, state, work, attempts, old, model_call):
    from .budget import BudgetedModelCall
    from .pipeline import _committed, _load, _run_path, _stage_path, control_guard
    from .engine_provenance import read_reservation
    from .production import _hash
    if not isinstance(model_call, BudgetedModelCall) or state["request"].get("timeout_retry_policy") != POLICY:
        return None
    number = old.get("attempt")
    if (old.get("status") != "unknown" or old.get("error") != "timeout"
            or type(number) is not int or not 1 <= number <= 4
            or old.get("operation_id") != f"batch:{work}:{number}"
            or old.get("run_id") != "run-" + _hash({"work": work, "attempt": number})[:32]):
        return None
    try:
        identity = _queue_shape(state)
    except CaptureError:
        return None
    if attempts.get("identity") != identity or _hash(identity) != work:
        _fail()
    op = model_call.ledger.operation(old["operation_id"])
    if op is None or set(op["calls"]) != {"extract"}:
        return None
    call = op["calls"]["extract"]
    if call["status"] != "unknown" or call["error_code"] != "timeout" or call["finished_at"] is None:
        return None
    run = old["run_id"]
    for path in [_stage_path(run), *[_run_path(run, n) for n in
                 ("review", "manifest", "receipt", "reused", "decomposed-plan")]]:
        if git.path(path).exists() or git.text("log", "--full-history", "--format=%H", "HEAD", "--", path):
            return None
    started = _load(_committed(git, _run_path(run, "started")), "processing_attempt")
    failure = _load(_committed(git, _run_path(run, "failure")), "failure")
    if (started.get("execution_context") is not None or started.get("related_runs") != []
            or started.get("source_paths") != state["plan"]["source_paths"]
            or failure.get("phase") != "extract" or failure.get("error") != "timeout"
            or failure.get("call", {}).get("invocation_id") != call["invocation_id"]):
        return None
    engine = read_reservation(git, run)
    if engine is None:
        return None
    if control_guard(git)["digest"] != state["plan"]["snapshot"]["controls_digest"]:
        _fail("timeout_retry_scope_changed")
    from .batching import validate_plan
    validate_plan(git.repo, state["plan"], source_paths=state["plan"]["source_paths"], require_current_versions=True, _locked=True)
    return call, started, engine


def ensure_timeout_receipt(repo, state, work, attempts, old, model_call):
    """Record the finite policy decision without altering the original attempt."""
    from .production import ATTEMPTS, _hash, _read, _path as queue_path
    git = PipelineGit(repo)
    path = _path(work, old["run_id"])
    with git.lock():
        head = git.check()
        if git.path(path).exists():
            ref = _snapshot(git, path)
            validate_ref(git, ref, model_call=model_call)
            return ref
        if git.text("log", "--full-history", "--format=%H", "HEAD", "--", path):
            _fail()
        checked = _eligible(git, state, work, attempts, old, model_call)
        if checked is None:
            return None
        if attempts["attempts"][-1] != old:
            _fail("timeout_retry_attempt_changed")
        if _read(git, ATTEMPTS + work + ".md", "production_attempts") != attempts:
            _fail("timeout_retry_attempt_changed")
        call, started, engine = checked
        old_plan_ref, old_plan_sha = _old_plan(git, state, started, engine, old)
        number = old["attempt"] + 1
        successor = (None if number > 4 else {"attempt": number,
            "run_id": "run-" + _hash({"work": work, "attempt": number})[:32], "operation_id": f"batch:{work}:{number}"})
        value = {"contract": CONTRACT, "policy": POLICY, "work_id": work,
                 "created_at": timestamp(), "base_head": head,
                 "original": {**deepcopy(old), "call": deepcopy(call),
                     "artifacts": [_snapshot(git, f"memory/runs/processes/{old['run_id']}/{n}.md")
                                   for n in ("started", "engine", "failure")]},
                 "original_plan": {"ref": old_plan_ref, "sha256": old_plan_sha},
                 "target": {"queue_id": state["queue_id"], "plan_sha256": _sha(state["plan"]),
                     "queue_ref": _snapshot(git, queue_path(state["queue_id"])), "successor": successor},
                 "attempt_prefix": deepcopy(attempts["attempts"]), "identity": deepcopy(attempts["identity"]),
                 "counter_ref": _snapshot(git, ATTEMPTS + work + ".md"), "audit_pool": _pool(model_call.ledger),
                 "model_outcome": "unknown", "remote_cancellation_confirmed": False,
                 "local_condition": "finished_timeout_under_exclusive_worker", "original_usage_retained": True}
        raw = markdown({"kind": "timeout_retry", "schema_version": SCHEMA}, canonical(value).decode() + "\n")
        commit = git.write_files({path: raw}, head, "process: record bounded timeout retry without relabeling original call")
        return {"path": path, "commit_sha": commit, "sha256": _sha(raw)}


def _validate_ref(git, ref, *, model_call=None):
    from .production import ATTEMPTS, _read
    from .pipeline import _load
    if (not isinstance(ref, dict) or set(ref) != {"path", "commit_sha", "sha256"}
            or not isinstance(ref.get("path"), str) or _REF.fullmatch(ref["path"]) is None):
        _fail()
    raw = _read_snapshot(git, ref)
    if git._existing(ref["path"]) != raw:
        _fail()
    history = git.text("log", "--full-history", "--format=%H", "HEAD", "--", ref["path"]).splitlines()
    for commit in history:
        if commit == ref["commit_sha"]:
            continue
        if len(git.text("rev-list", "--parents", "-n", "1", commit).split()) < 3 or git.read(commit, ref["path"]) != raw:
            _fail()
    value = _json(raw)
    if (value.get("contract") != CONTRACT or value.get("policy") != POLICY
            or value.get("model_outcome") != "unknown" or value.get("remote_cancellation_confirmed") is not False
            or value.get("original_usage_retained") is not True
            or _path(value["work_id"], value["original"]["run_id"]) != ref["path"]
            or git.text("rev-parse", ref["commit_sha"] + "^") != value["base_head"]):
        _fail()
    from .production import ROOT as QUEUES, _path as queue_path
    run = value["original"]["run_id"]
    old_queue = value["original_plan"]["ref"]["path"]
    if (value["counter_ref"]["path"] != ATTEMPTS + value["work_id"] + ".md"
            or value["target"]["queue_ref"]["path"] != queue_path(value["target"]["queue_id"])
            or not old_queue.startswith(QUEUES) or not old_queue.endswith("/state.md")
            or queue_path(old_queue[len(QUEUES):-len("/state.md")]) != old_queue
            or [entry["path"] for entry in value["original"]["artifacts"]]
                != [f"memory/runs/processes/{run}/{name}.md" for name in ("started", "engine", "failure")]):
        _fail()
    counter = _load(_read_snapshot(git, value["counter_ref"]), "production_attempts")
    if counter.get("identity") != value["identity"] or counter.get("attempts") != value["attempt_prefix"]:
        _fail()
    original = {k: v for k, v in value["original"].items() if k not in {"call", "artifacts"}}
    if not value["attempt_prefix"] or value["attempt_prefix"][-1] != original:
        _fail()
    current = _read(git, ATTEMPTS + value["work_id"] + ".md", "production_attempts")
    if current is None or current["attempts"][:len(value["attempt_prefix"])] != value["attempt_prefix"]:
        _fail("timeout_retry_attempt_changed")
    for artifact in value["original"]["artifacts"]:
        if git._existing(artifact["path"]) != _read_snapshot(git, artifact):
            _fail()
    for item in (value["original_plan"], {"ref": value["target"]["queue_ref"], "sha256": value["target"]["plan_sha256"]}):
        queue = _load(_read_snapshot(git, item["ref"]), "production_queue")
        if _sha(queue["plan"]) != item["sha256"]:
            _fail()
    _validate_semantics(git, value)
    if model_call is not None:
        if _pool(model_call.ledger) != value["audit_pool"]:
            _fail("timeout_retry_audit_changed")
        operation = model_call.ledger.operation(value["original"]["operation_id"])
        if operation is None or operation["calls"] != {"extract": value["original"]["call"]}:
            _fail("timeout_retry_call_changed")
    return value


def _validate_semantics(git, value):
    """Recheck the receipt's original snapshot, never late predecessor output."""
    from .production import ATTEMPTS, _hash, _path as queue_path
    from .pipeline import _load, _run_path, _stage_path, control_guard
    from .batching import validate_plan
    base, work, row = value["base_head"], value["work_id"], value["original"]
    prefix, identity = value["attempt_prefix"], value["identity"]
    if (set(value) != {"contract", "policy", "work_id", "created_at", "base_head", "original", "original_plan",
            "target", "attempt_prefix", "identity", "counter_ref", "audit_pool", "model_outcome",
            "remote_cancellation_confirmed", "local_condition", "original_usage_retained"}
            or value["local_condition"] != "finished_timeout_under_exclusive_worker"
            or not isinstance(prefix, list) or not 1 <= len(prefix) <= 4 or _hash(identity) != work
            or value["counter_ref"]["path"] != ATTEMPTS + work + ".md"):
        _fail()
    refs = [value["counter_ref"], value["original_plan"]["ref"], value["target"]["queue_ref"], *row["artifacts"]]
    for ref in refs:
        git.run("merge-base", "--is-ancestor", ref["commit_sha"], base)
        if git.read(base, ref["path"]) != _read_snapshot(git, ref):
            _fail()
    for number, attempt in enumerate(prefix, 1):
        if (attempt["attempt"] != number or attempt["operation_id"] != f"batch:{work}:{number}"
                or attempt["run_id"] != "run-" + _hash({"work": work, "attempt": number})[:32]):
            _fail()
    number, run, call = row["attempt"], row["run_id"], row["call"]
    if (number != len(prefix) or row["status"] != "unknown" or row["error"] != "timeout"
            or set(call) != {"payload_sha256", "status", "started_at", "finished_at", "invocation_id", "error_code"}
            or call["status"] != "unknown" or call["error_code"] != "timeout" or call["finished_at"] is None):
        _fail()
    from .pipeline import _time
    _time(call["started_at"]); _time(call["finished_at"]); _time(value["created_at"])
    if not isinstance(call["payload_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", call["payload_sha256"]) is None:
        _fail()
    for path in [_stage_path(run), *[_run_path(run, n) for n in ("review", "manifest", "receipt", "reused", "decomposed-plan")]]:
        if git.text("log", "--full-history", "--format=%H", base, "--", path):
            _fail()
    expected_paths = [_run_path(run, n) for n in ("started", "engine", "failure")]
    if [r["path"] for r in row["artifacts"]] != expected_paths:
        _fail()
    started, engine, failure = [_load(_read_snapshot(git, ref), kind) for ref, kind in
                              zip(row["artifacts"], ("processing_attempt", "engine_provenance", "failure"))]
    for ref in row["artifacts"]:
        for change in git.text("log", "--full-history", "--format=%H", "HEAD", "--", ref["path"]).splitlines():
            if change == ref["commit_sha"]:
                continue
            if len(git.text("rev-list", "--parents", "-n", "1", change).split()) < 3 or git.read(change, ref["path"]) != _read_snapshot(git, ref):
                _fail()
    if (started.get("execution_context") is not None or started.get("related_runs") != []
            or started.get("run_id") != run or engine.get("run_id") != run
            or engine.get("processing_key") != started.get("processing_key")
            or engine.get("started_sha256") != row["artifacts"][0]["sha256"]
            or row["artifacts"][0]["commit_sha"] != row["artifacts"][1]["commit_sha"]
            or git.text("rev-parse", row["artifacts"][0]["commit_sha"] + "^") != engine.get("data_base_head")
            or failure.get("phase") != "extract" or failure.get("error") != "timeout"
            or failure.get("call", {}).get("invocation_id") != call["invocation_id"]):
        _fail()
    from .engine_provenance import read_reservation
    if read_reservation(git, run) != engine:
        _fail()
    old = _load(_read_snapshot(git, value["original_plan"]["ref"]), "production_queue")
    target = _load(_read_snapshot(git, value["target"]["queue_ref"]), "production_queue")
    if (value["target"]["queue_ref"]["path"] != queue_path(value["target"]["queue_id"])
            or target["queue_id"] != value["target"]["queue_id"]
            or target["request"].get("timeout_retry_policy") != POLICY
            or _queue_shape(old) != identity or _queue_shape(target) != identity
            or old["plan"]["sources"] != target["plan"]["sources"]
            or old["request"].get("skill_bundle") != target["request"].get("skill_bundle")
            or old["plan"]["snapshot"]["versions"] != engine["versions"]
            or old["plan"]["snapshot"]["controls_digest"] != target["plan"]["snapshot"]["controls_digest"]
            or control_guard(git, base)["digest"] != target["plan"]["snapshot"]["controls_digest"]
            or started.get("source_paths") != target["plan"]["source_paths"]
            or _bound_key(git, old, started, engine, row) != started["processing_key"]):
        _fail()
    for queue in (old, target):
        validate_plan(git.repo, queue["plan"], source_paths=queue["plan"]["source_paths"],
                      require_current_versions=False, historical=True, _locked=True)
    expected = None if number == 4 else {"attempt": number + 1, "operation_id": f"batch:{work}:{number + 1}",
        "run_id": "run-" + _hash({"work": work, "attempt": number + 1})[:32]}
    if value["target"]["successor"] != expected:
        _fail()
    pool = value["audit_pool"]
    if (set(pool) != {"schema_version", "mode", "pool_id", "authorization_sha256", "limit", "initial_used"}
            or pool["mode"] != "audit"):
        _fail()


def validate_ref(git, ref, *, model_call=None):
    try:
        return _validate_ref(git, ref, model_call=model_call)
    except CaptureError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, RecursionError):
        _fail()


def timeout_context(repo, state, work, attempts, model_call):
    """Resolve retired rows and the one next slot for this frozen target plan."""
    if state["request"].get("timeout_retry_policy") != POLICY:
        return set(), None
    retired, next_ref = set(), None
    for old in attempts["attempts"]:
        if old["status"] != "unknown":
            continue
        ref = ensure_timeout_receipt(repo, state, work, attempts, old, model_call)
        if ref is None:
            continue
        value = validate_ref(PipelineGit(repo), ref, model_call=model_call)
        successor = value["target"]["successor"]
        if successor is not None:
            if successor["attempt"] <= len(attempts["attempts"]):
                row = attempts["attempts"][successor["attempt"] - 1]
                if any(row.get(k) != successor[k] for k in successor) or row.get("timeout_retry_ref") != ref:
                    _fail("timeout_retry_successor_changed")
            elif value["target"]["plan_sha256"] == _sha(state["plan"]):
                next_ref = ref
            else:
                _fail("timeout_retry_target_changed")
        retired.add(old["run_id"])
    return retired, next_ref


def auto_timeout_result(repo, state, work, result, model_call):
    from .production import ATTEMPTS, _read
    if result.get("status") != "unknown" or result.get("error") != "timeout":
        return result
    if state["request"].get("timeout_retry_policy") != POLICY:
        return result
    if work is None:
        from .production import _hash
        try:
            work = _hash(_queue_shape(state))
        except CaptureError:
            return result
    git = PipelineGit(repo)
    attempts = _read(git, ATTEMPTS + work + ".md", "production_attempts")
    old = next((r for r in attempts["attempts"] if r["run_id"] == result.get("run_id")), None)
    if old is None:
        return result
    ref = ensure_timeout_receipt(repo, state, work, attempts, old, model_call)
    if ref is None:
        return result
    record = validate_ref(PipelineGit(repo), ref, model_call=model_call)
    return {**result, "status": "attempt_limit" if record["target"]["successor"] is None else "failed",
            "call_outcome": "unknown", "retryable": record["target"]["successor"] is not None,
            "timeout_retry_ref": ref, "head": PipelineGit(repo).head()}


def validate_execution(git, ref, run_id, *, source_paths, plan_id, model_call=None, require_claim=True, execution_options=None):
    """Bind one successor to its original, counted work slot."""
    from .production import ATTEMPTS, _read
    # Establish the declared source scope from metadata before validating any
    # original source evidence carried by this receipt.
    from .pipeline import _load
    from .production import _path as queue_path
    try:
        if (not isinstance(ref, dict) or set(ref) != {"path", "commit_sha", "sha256"}
                or not isinstance(ref["path"], str) or _REF.fullmatch(ref["path"]) is None):
            _fail()
        preliminary = _json(_read_snapshot(git, ref))
        if preliminary["target"]["queue_ref"]["path"] != queue_path(preliminary["target"]["queue_id"]):
            _fail()
        target = _load(_read_snapshot(git, preliminary["target"]["queue_ref"]), "production_queue")
        if target["plan"]["source_paths"] != source_paths or target["plan"]["plan_id"] != plan_id:
            _fail("timeout_retry_successor_changed")
    except CaptureError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, RecursionError):
        _fail()
    value = validate_ref(git, ref, model_call=model_call)
    successor = value["target"]["successor"]
    target = _load(
        _read_snapshot(git, value["target"]["queue_ref"]), "production_queue")
    if (successor is None or successor["run_id"] != run_id
            or target["plan"]["plan_id"] != plan_id
            or target["plan"]["source_paths"] != source_paths):
        _fail("timeout_retry_successor_changed")
    expected_revision = value["original"].get("revision_from_run")
    if execution_options is not None:
        from .batching import _context, _views
        from .pipeline import load_source
        plan = target["plan"]
        batch = plan["batches"][0]
        sources = [load_source(git.repo, p, purpose="history") for p in source_paths]
        expected_options = {
            "batch_context": _context(plan["plan_id"], batch["batch_id"], plan["fragments"], batch["parent_batch_ids"]),
            "source_views": _views(sources, plan["fragments"]),
            "skill_bundle": target["request"].get("skill_bundle"),
            "review_strategy": target["request"].get("review_strategy", "legacy-v1"),
            "rebuild_context": target["request"].get("rebuild_context"), "revision_from_run": expected_revision,
            "versions": plan["snapshot"]["versions"]}
        if execution_options != expected_options:
            _fail("timeout_retry_scope_changed")
    current = _read(git, "memory/runs/processes/queues/" + value["target"]["queue_id"] + "/state.md", "production_queue")
    if current is None or current["request"] != target["request"] or current["plan"] != target["plan"]:
        _fail("timeout_retry_target_changed")
    counter = _read(git, ATTEMPTS + value["work_id"] + ".md", "production_attempts")
    number = successor["attempt"]
    if len(counter["attempts"]) < number:
        if require_claim:
            _fail("timeout_retry_claim_missing")
    else:
        row = counter["attempts"][number - 1]
        if any(row.get(k) != successor[k] for k in successor) or row.get("timeout_retry_ref") != ref:
            _fail("timeout_retry_successor_changed")
        if row.get("revision_from_run") != expected_revision:
            _fail("timeout_retry_revision_changed")
        if expected_revision is not None:
            for key in ("revision_context_sha256", "revision_candidate_binding", "revision_source_plan_id"):
                if row.get(key) != value["original"].get(key):
                    _fail("timeout_retry_revision_changed")
            if row.get("revision_target_plan_id") != target["plan"]["plan_id"]:
                _fail("timeout_retry_revision_changed")
        elif any(key.startswith("revision_") for key in row):
            _fail("timeout_retry_revision_changed")
    if model_call is not None and model_call.operation_id != successor["operation_id"]:
        _fail("timeout_retry_audit_changed")
    return value


def unstarted_resume(git, ref, run_id, model_call):
    """The existing successor may resume only before any model-role start."""
    from .pipeline import _committed, _load, _run_path, _stage_path
    from .engine_provenance import read_reservation
    value = validate_ref(git, ref, model_call=model_call)
    successor = value["target"]["successor"]
    if successor is None or successor["run_id"] != run_id or model_call.operation_id != successor["operation_id"]:
        _fail()
    operation = model_call.ledger.operation(successor["operation_id"])
    if operation is not None and operation["calls"]:
        return None
    started_path = _run_path(run_id, "started")
    if not git.path(started_path).is_file():
        if git.text("log", "--full-history", "--format=%H", "HEAD", "--", started_path):
            _fail()
        return {}
    if (git.path(_stage_path(run_id)).exists()
            or git.text("log", "--full-history", "--format=%H", "HEAD", "--", _stage_path(run_id))
            or set(git.text("ls-tree", "-r", "--name-only", "HEAD", "--", f"memory/runs/processes/{run_id}/").splitlines())
               != {started_path, _run_path(run_id, "engine")}):
        return None
    started = _load(_committed(git, started_path), "processing_attempt")
    if started.get("timeout_retry_ref") != ref or read_reservation(git, run_id) is None:
        _fail()
    return {"contract": CONTRACT, "started": _snapshot(git, started_path),
            "engine": _snapshot(git, _run_path(run_id, "engine")),
            "operation_id": successor["operation_id"], "audit_pool": value["audit_pool"],
            "observed_calls": {}}


def verify_candidate(git, candidate):
    from .pipeline import _committed, _load, _run_path
    ref = candidate.get("timeout_retry_ref")
    assert_run_ref(git, candidate["run_id"], ref)
    if ref is None:
        if "timeout_retry_resume" in candidate:
            _fail()
        return
    value = validate_execution(git, ref, candidate["run_id"],
        source_paths=[s["path"] for s in [candidate["source"], *candidate.get("additional_sources", [])]],
        plan_id=candidate.get("batch_context", {}).get("plan_id"), execution_options={
            "batch_context": candidate.get("batch_context"), "source_views": candidate.get("source_views"),
            "skill_bundle": candidate.get("skill_bundle"), "review_strategy": candidate.get("review_strategy", "legacy-v1"),
            "rebuild_context": candidate.get("rebuild_context"),
            "revision_from_run": candidate.get("revision_context", {}).get("source_run_id"),
            "versions": candidate["versions"]})
    from .production import ATTEMPTS, _read
    target = _load(_read_snapshot(git, value["target"]["queue_ref"]), "production_queue")
    row = _read(git, ATTEMPTS + value["work_id"] + ".md", "production_attempts")["attempts"][value["target"]["successor"]["attempt"] - 1]
    if (candidate["versions"] != target["plan"]["snapshot"]["versions"]
            or candidate.get("revision_context") is not None
               and _sha(candidate["revision_context"]).removeprefix("sha256-") != row.get("revision_context_sha256")):
        _fail("timeout_retry_revision_changed")
    started = _load(_committed(git, _run_path(candidate["run_id"], "started")), "processing_attempt")
    if started.get("timeout_retry_ref") != ref:
        _fail()
    resume = candidate.get("timeout_retry_resume")
    if resume is not None:
        if (set(resume) != {"contract", "started", "engine", "operation_id", "audit_pool", "observed_calls"}
                or resume["contract"] != CONTRACT or resume["observed_calls"] != {}
                or resume["operation_id"] != value["target"]["successor"]["operation_id"]
                or resume["audit_pool"] != value["audit_pool"]
                or resume["started"]["path"] != _run_path(candidate["run_id"], "started")
                or resume["engine"]["path"] != _run_path(candidate["run_id"], "engine")):
            _fail()
        for name in ("started", "engine"):
            if git._existing(resume[name]["path"]) != _read_snapshot(git, resume[name]):
                _fail()


def authority_refs(git, ref, visited=()):
    if ref["path"] in visited or len(visited) >= 4:
        _fail()
    value = validate_ref(git, ref)
    entries = [ref, value["counter_ref"], value["original_plan"]["ref"], value["target"]["queue_ref"],
               *value["original"]["artifacts"]]
    for row in value["attempt_prefix"]:
        if row.get("timeout_retry_ref") is not None:
            entries.extend(authority_refs(git, row["timeout_retry_ref"], (*visited, ref["path"])))
    return list({(r["path"], r["commit_sha"]): r for r in entries}.values())


def assert_run_ref(git, run_id, ref):
    """A reserved successor cannot silently omit its lineage on direct entry."""
    for path in _receipt_paths(git):
        if _REF.fullmatch(path) is None:
            _fail()
        value = _json(git._existing(path))
        successor = value.get("target", {}).get("successor")
        if successor is not None and successor.get("run_id") == run_id and ref != _snapshot(git, path):
            _fail("timeout_retry_reference_required")


def assert_publishable(git, run_ids, *, historical=False):
    """Retired outputs remain inspectable, but cannot become current authority."""
    if historical or not run_ids:
        return
    names = _receipt_paths(git)
    if not names:
        return
    retired = {}
    for path in names:
        match = _REF.fullmatch(path)
        if match is None or match[2] in retired:
            _fail()
        # Metadata names are sufficient to select a retirement check. Loading
        # unrelated receipts' source evidence would exceed the caller's scope.
        retired[match[2]] = path
    from .pipeline import _load, _committed, _stage_path, _run_path, _parse_saved, _unique_pairs
    visited = set()
    def visit(run):
        if run in retired:
            # Presence of the immutable retirement path is already sufficient
            # to reject adoption; do not open an out-of-scope dependency's
            # original source evidence merely to refuse its late output.
            _fail("unknown_retry_retired_run")
        if run in visited:
            return
        if len(visited) > 256:
            _fail()
        visited.add(run)
        path = _stage_path(run)
        if not git.path(path).is_file():
            manifest = _run_path(run, "manifest")
            if git.path(manifest).is_file():
                metadata, body = _parse_saved(_committed(git, manifest))
                if metadata.get("kind") == "batch_group_manifest":
                    group = json.loads(body, object_pairs_hook=_unique_pairs)
                    for proof in group.get("proofs", []):
                        visit(proof["run_id"])
            return
        candidate = _load(_committed(git, path), "candidate")
        dependencies = list(candidate.get("related_runs", []))
        dependencies += [v["run_id"] for v in candidate.get("merge_inputs", [])]
        dependencies += [v["run_id"] for v in candidate.get("assembly", {}).get("inputs", [])]
        if candidate.get("extraction_reused_from"):
            dependencies.append(candidate["extraction_reused_from"]["run_id"])
        if candidate.get("revision_context"):
            dependencies.append(candidate["revision_context"]["candidate"]["run_id"])
        for dependency in dependencies:
            visit(dependency)
    for run in run_ids:
        visit(run)
