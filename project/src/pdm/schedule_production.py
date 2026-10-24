"""Durable daily discovery driving the actual batch production coordinator.

This is an execution bridge, not a platform timer. Legacy daily attempts keep
their original event and two-call contracts in :mod:`pdm.scheduler`.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
from zoneinfo import ZoneInfo

from .contracts import CaptureError, canonical
from .transaction import PipelineGit

ENGINE = "production-v1"
_WORK = re.compile(r"work-[0-9a-f]{64}\Z")
_RUN = re.compile(r"run-[0-9a-f]{32}\Z")
_PENDING = re.compile(r"pending-[0-9a-f]{64}\Z")
_STATUSES = {"ready", "failed", "needs_revision", "needs_confirmation", "registration_required",
             "unknown", "attempt_limit", "blocked_capacity", "blocked_context", "published", "resolved"}


def _fail(code="unknown_schedule_state"):
    raise CaptureError(code, code)


def _digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _paths(values, *, empty=False):
    from .scheduler import SOURCE_PATH
    if (not isinstance(values, list) or len(values) > 256 or not empty and not values
            or any(not isinstance(path, str) or not SOURCE_PATH.fullmatch(path) for path in values)
            or values != sorted(set(values))):
        _fail()


def _runs(values):
    if (not isinstance(values, list) or len(values) > 256
            or any(not isinstance(run, str) or not _RUN.fullmatch(run) for run in values)
            or values != sorted(set(values))):
        _fail()


def validate_event(kind, data):
    """Validate the new event shape without opening sources or model outputs."""
    from .scheduler import _budget_pool
    if kind in {"configured", "paused", "resumed"}:
        return
    if kind == "production_discovered":
        if set(data) != {"root_tick_key", "trigger", "source_paths", "budget_pool"}:
            _fail()
        _paths(data["source_paths"], empty=True)
        _budget_pool(data["budget_pool"])
    elif kind == "production_started":
        fields = {"work_id", "queue_id", "root_tick_key", "trigger", "source_paths", "allowed_source_paths",
                  "related_runs", "pending", "base_head", "budget_pool"}
        if set(data) != fields or not isinstance(data.get("work_id"), str) or not _WORK.fullmatch(data["work_id"]):
            _fail()
        if data["queue_id"] != "schedule-" + data["work_id"][5:53]:
            _fail()
        _paths(data["source_paths"])
        _paths(data["allowed_source_paths"])
        _runs(data["related_runs"])
        if not set(data["source_paths"]) <= set(data["allowed_source_paths"]):
            _fail()
        if not isinstance(data["base_head"], str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", data["base_head"]):
            _fail()
        pending = data["pending"]
        if pending is not None:
            if (not isinstance(pending, dict) or set(pending) != {"item_id", "job_id", "reply_sources", "parent_run_ids", "question", "source_refs", "object_refs"}
                    or not isinstance(pending["item_id"], str) or not _PENDING.fullmatch(pending["item_id"])
                    or not isinstance(pending["job_id"], str) or not re.fullmatch(r"continuation-[0-9a-f]{64}", pending["job_id"])
                    or not isinstance(pending["reply_sources"], list) or not pending["reply_sources"]):
                _fail()
            _runs(pending["parent_run_ids"])
        _budget_pool(data["budget_pool"])
    elif kind == "production_progress":
        if (set(data) != {"work_id", "status", "active_run_id", "publication_run_id", "resolved_item_id"}
                or data.get("status") not in _STATUSES):
            _fail()
        for field in ("active_run_id", "publication_run_id"):
            if data[field] is not None and (not isinstance(data[field], str) or not _RUN.fullmatch(data[field])):
                _fail()
        if data["resolved_item_id"] is not None and (not isinstance(data["resolved_item_id"], str) or not _PENDING.fullmatch(data["resolved_item_id"])):
            _fail()
    elif kind == "production_finished":
        if (set(data) != {"work_id", "publication_run_id", "resolved_item_id", "source_paths", "remote_status"}
                or not isinstance(data["publication_run_id"], str) or not _RUN.fullmatch(data["publication_run_id"])
                or data["remote_status"] not in {"confirmed", "not_requested"}):
            _fail()
        _paths(data["source_paths"])
    elif kind == "production_tick_finished":
        if (set(data) != {"root_tick_key", "status", "work_ids", "budget_pool"}
                or data["status"] not in {"completed", "completed_empty"}
                or not isinstance(data["work_ids"], list) or len(set(data["work_ids"])) != len(data["work_ids"])):
            _fail()
        _budget_pool(data["budget_pool"])
    else:
        _fail()


def validate_history(config, events):
    from .scheduler import _authorized, _trigger
    works, known = {}, set(config["related_run_ids"])
    roots, discoveries = set(), {}
    for event in events:
        kind, data = event["kind"], event["data"]
        if kind == "production_discovered":
            _, root = _trigger(data["trigger"], config)
            if (data["root_tick_key"] != root or root in discoveries
                    or any(not _authorized(config, path) for path in data["source_paths"])):
                _fail()
            discoveries[root] = data["source_paths"]
        elif kind == "production_started":
            _, root = _trigger(data["trigger"], config)
            identity = {key: data[key] for key in ("source_paths", "related_runs", "pending")}
            if (data["root_tick_key"] != root or data["work_id"] != "work-" + _digest(identity)
                    or root not in discoveries or not set(data["allowed_source_paths"]) <= set(discoveries[root])
                    or data["work_id"] in works or not set(data["related_runs"]) <= known
                    or any(not _authorized(config, path) for path in data["allowed_source_paths"])):
                _fail()
            works[data["work_id"]] = {"start": data, "progress": None, "finished": False}
        elif kind == "production_progress":
            work = works.get(data.get("work_id"))
            if work is None or work["finished"]:
                _fail()
            work["progress"] = data
            if data["active_run_id"]:
                known.add(data["active_run_id"])
            if data["publication_run_id"]:
                known.add(data["publication_run_id"])
        elif kind == "production_finished":
            work = works.get(data.get("work_id"))
            if (work is None or work["finished"] or work["progress"] is None
                    or work["progress"]["status"] not in {"published", "resolved"}
                    or data["publication_run_id"] != work["progress"]["publication_run_id"]
                    or data["resolved_item_id"] != work["progress"]["resolved_item_id"]
                    or data["source_paths"] != work["start"]["source_paths"]):
                _fail()
            work["finished"] = True
        elif kind == "production_tick_finished":
            if (not isinstance(data["root_tick_key"], str) or data["root_tick_key"] in roots
                    or any(work not in works or not works[work]["finished"] for work in data["work_ids"])):
                _fail()
            roots.add(data["root_tick_key"])
        elif kind not in {"configured", "paused", "resumed"}:
            _fail()


def _works(state):
    result = {}
    for event in state["events"]:
        kind, data = event["kind"], event["data"]
        if kind == "production_started":
            result[data["work_id"]] = {**data, "status": "ready", "active_run_id": None,
                                       "publication_run_id": None, "resolved_item_id": None, "finished": False}
        elif kind == "production_progress":
            result[data["work_id"]].update(data)
        elif kind == "production_finished":
            result[data["work_id"]].update(data, finished=True)
    return list(result.values())


def _known_runs(state):
    runs = set(state["config"]["related_run_ids"])
    for work in _works(state):
        runs.update(run for run in (work["active_run_id"], work["publication_run_id"]) if run)
    return sorted(runs)


def _discovery(repo, state, *, allowed_snapshot=None):
    from .context import discover_context, plan_answered_pending
    from .controls import read_controls, source_policy, _metadata_only
    from .scheduler import _scope_paths
    git = PipelineGit(repo)
    allowed = _scope_paths(git, state["config"])
    if allowed_snapshot is not None:
        allowed = sorted(set(allowed) & set(allowed_snapshot))
    if len(allowed) > 256:
        _fail("schedule_scope_capacity")
    controls = read_controls(repo)
    allowed = sorted(path for path in allowed if not source_policy(controls, path, purpose="history")["excluded"])
    completed = {path for work in _works(state) if work["finished"] for path in work["source_paths"]}
    inflight = {path for work in _works(state) if not work["finished"] for path in work["source_paths"]}
    runs = _known_runs(state)
    jobs = plan_answered_pending(repo, allowed_source_paths=allowed, allowed_run_ids=runs,
                                max_sources=256, max_runs=256)
    context = (discover_context(repo, allowed, allowed_source_paths=allowed, allowed_run_ids=runs,
                                max_sources=256, max_runs=256) if allowed else
               {"related_runs": [], "coverage": {"complete": True, "gaps": []}})
    waiting_text = {path for path in allowed if _metadata_only(git, path).get("acquisition_status") == "link_only"}
    new = sorted(set(allowed) - completed - inflight - waiting_text)
    return {"allowed": allowed, "new": new, "completed": completed, "context": context,
            "jobs": jobs, "waiting_text": waiting_text}


def plan_schedule(repo, schedule_id, *, now=None):
    from .scheduler import _require, _time
    state = _require(PipelineGit(repo), schedule_id)
    found = _discovery(repo, state)
    works = _works(state)
    active_paths = {path: work for work in works if not work["finished"] for path in work["source_paths"]}
    items = []
    for path in found["allowed"]:
        work = active_paths.get(path)
        status = ("completed" if path in found["completed"] else "waiting_text" if path in found["waiting_text"]
                  else "awaiting_confirmation" if work and work["status"] == "needs_confirmation"
                  else "reconciliation_required" if work and work["status"] == "unknown"
                  else work["status"] if work and work["status"] in {"attempt_limit", "blocked_capacity", "blocked_context"} else "eligible")
        items.append({"input_path": path, "source_paths": work["source_paths"] if work else found["new"],
                      "status": status, "work_id": work["work_id"] if work else None})
    return {"schedule_id": schedule_id, "processing_engine": ENGINE, "paused": state["paused"],
            "items": items, "works": works, "related_run_ids": found["context"]["related_runs"],
            "context_coverage": found["context"]["coverage"], "answered_coverage": found["jobs"]["coverage"],
            "answered_jobs": found["jobs"]["jobs"], "at": _time(now).isoformat(),
            "scheduled_environment": "unverified"}


@contextmanager
def _lock(repo):
    git = PipelineGit(repo)
    path = Path(git.text("rev-parse", "--absolute-git-dir")) / "pdm-schedule-production.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            _fail("unsafe_schedule_worker_lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("schedule_worker_locked")
        yield
    finally:
        os.close(fd)


def _checkpoint(repo, state, callback, *, base_head=None):
    from .scheduler import _confirm
    git = PipelineGit(repo)
    head = git.check()
    paths = {event["path"] for event in state["events"]}
    if base_head:
        paths.update(git.text("diff", "--name-only", "--diff-filter=ACM", base_head, head, "--", "memory").splitlines())
    files = {path: git.read(head, path) for path in paths}
    return _confirm(git, state["config"], head, files, callback)


def _guard(repo, schedule_id, work, callback):
    from .scheduler import _require, _remote_gate, assert_publication_allowed
    state = _require(PipelineGit(repo), schedule_id)
    if state["paused"]:
        _fail("publication_paused")
    _require_runtime(repo, state, None)
    if work["work_id"] not in {item["work_id"] for item in _works(state)}:
        _fail("schedule_work_changed")
    selected = set(work["source_paths"])
    from .pending import _publication_source_refs
    for run in work["related_runs"]:
        selected.update(ref["path"] for ref in _publication_source_refs(PipelineGit(repo), run))
    _legacy_reconciliation_gate(repo, selected)
    if work["pending"]:
        from .context import plan_answered_pending
        from .pending import read_registry
        item = next((item for item in read_registry(repo)["items"] if item["item_id"] == work["pending"]["item_id"]), None)
        if item is None:
            _fail("continuation_job_changed")
        if item["pending_status"] == "resolved":
            from .production import production_status
            queue = production_status(repo, work["queue_id"])
            if queue["status"] != "published" or queue["publication"]["run_id"] != item["resolution"]["run_id"]:
                _fail("continuation_job_changed")
        else:
            jobs = plan_answered_pending(repo, allowed_source_paths=work["allowed_source_paths"],
                                        allowed_run_ids=_known_runs(state), max_sources=256, max_runs=256)
            job = next((job for job in jobs["jobs"] if job["job_id"] == work["pending"]["job_id"]), None)
            if (job is None or job["source_paths"] != work["source_paths"] or job["related_runs"] != work["related_runs"]
                    or job["reply_sources"] != work["pending"]["reply_sources"]):
                _fail("continuation_job_changed")
    if state["config"]["remote_mode"] == "required":
        _remote_gate(PipelineGit(repo), state)
        _checkpoint(repo, state, callback, base_head=work["base_head"])
    assert_publication_allowed(repo)


def _progress(repo, state, work, status, *, active_run_id=None, publication_run_id=None,
              resolved_item_id=None, now=None, callback=None):
    from .scheduler import _event
    data = {"work_id": work["work_id"], "status": status, "active_run_id": active_run_id,
            "publication_run_id": publication_run_id, "resolved_item_id": resolved_item_id}
    previous = {key: work.get(key) for key in data}
    if data != previous:
        _event(PipelineGit(repo), state, "production_progress", data, now, callback)


def _legacy_reconciliation_gate(repo, selected):
    """A new engine must not replay a source with an old unknown reservation."""
    from .scheduler import ROOT, _attempts, _state
    git = PipelineGit(repo)
    paths = git.text("ls-tree", "-r", "--name-only", "HEAD", "--", ROOT).splitlines()
    ids = {path[len(ROOT):].split("/", 1)[0] for path in paths}
    for schedule_id in sorted(ids):
        state = _state(git, schedule_id)
        if state["config"].get("processing_engine") == ENGINE:
            continue
        reservations, results = _attempts(state)
        for attempt, event in reservations.items():
            result = results.get(attempt, {}).get("data", {})
            if (attempt not in results or result.get("status") == "reconciliation_required") and set(event["data"]["source_paths"]) & set(selected):
                _fail("legacy_schedule_reconciliation_required")


def _require_runtime(repo, state, model_call):
    if state["config"]["remote_mode"] != "required":
        return
    from .runtime import require_environment
    binding = require_environment(repo, require_binding=True, remote=state["config"]["remote"])
    if model_call is not None and not model_call.ledger.path.resolve().is_relative_to(Path(binding["state_root"])):
        _fail("environment_audit_path_mismatch")


def assert_can_resume(repo, state):
    """Read the durable queue and reservations even if its schedule ACK was lost."""
    from .production import ATTEMPTS, _path, _read
    git = PipelineGit(repo)
    for work in _works(state):
        if work["finished"]:
            continue
        if work["status"] == "unknown":
            _fail("schedule_reconciliation_required")
        queue = _read(git, _path(work["queue_id"]), "production_queue")
        if queue is None:
            continue
        if queue["status"] == "unknown":
            _fail("schedule_reconciliation_required")
        fragments = {fragment["fragment_id"] for fragment in queue["plan"]["fragments"]}
        for path in git.text("ls-tree", "-r", "--name-only", "HEAD", "--", ATTEMPTS).splitlines():
            attempts = _read(git, path, "production_attempts")
            if (set(attempts["identity"]["fragments"]) & fragments
                    and any(attempt["status"] in {"reserved", "unknown"} for attempt in attempts["attempts"])):
                _fail("schedule_reconciliation_required")


def _finish(repo, state, work, publication, *, resolved_item_id=None, now=None, callback=None):
    from .scheduler import _confirm_publication, _event, _require
    git = PipelineGit(repo)
    _checkpoint(repo, state, callback, base_head=work["base_head"])
    _confirm_publication(git, state["config"], publication, state, callback)
    _progress(repo, state, work, "resolved" if resolved_item_id else "published",
              active_run_id=work.get("active_run_id"), publication_run_id=publication["run_id"],
              resolved_item_id=resolved_item_id, now=now, callback=callback)
    state = _require(git, state["config"]["schedule_id"])
    _event(git, state, "production_finished", {"work_id": work["work_id"],
           "publication_run_id": publication["run_id"], "resolved_item_id": resolved_item_id,
           "source_paths": work["source_paths"], "remote_status": "confirmed" if state["config"]["remote_mode"] == "required" else "not_requested"}, now, callback)


def _resolved_waits(repo, state, *, now, callback):
    from .context import _pending_origin
    from .pending import read_registry, resolve_pending
    from .scheduler import _authorized
    registry = read_registry(repo)
    for work in _works(state):
        if work["finished"] or work["status"] != "needs_confirmation" or not work["active_run_id"]:
            continue
        items = [item for item in registry["items"] if work["active_run_id"] in item["run_ids"]]
        if items and all(item["pending_status"] == "resolved" for item in items):
            for item in items:
                if any(not _authorized(state["config"], ref["path"]) for ref in [*item["source_refs"], *item["reply_sources"]]):
                    _fail("invalid_schedule_scope")
                _pending_origin(PipelineGit(repo), item, work["active_run_id"])
                resolve_pending(repo, item["item_id"], item["resolution"]["run_id"])
            publication = _verified_publication(repo, state, items[-1]["resolution"]["run_id"], historical=True)
            _finish(repo, state, work, publication, resolved_item_id=items[-1]["item_id"], now=now, callback=callback)


def _verified_publication(repo, state, run, *, historical=False):
    from .pending import _publication_source_refs
    from .pipeline import verify_publication
    from .scheduler import _authorized
    paths = sorted(ref["path"] for ref in _publication_source_refs(PipelineGit(repo), run))
    if any(not _authorized(state["config"], path) for path in paths):
        _fail("invalid_schedule_scope")
    return verify_publication(repo, run, source_paths=paths, historical=historical)


def _finish_tick(repo, state, root, model_call, *, now, callback, extra_work_ids=()):
    from .scheduler import _budget_grant, _event
    if any(event["kind"] == "production_tick_finished" and event["data"]["root_tick_key"] == root for event in state["events"]):
        return
    finished = sorted({work["work_id"] for work in _works(state) if work["finished"] and work["root_tick_key"] == root} | set(extra_work_ids))
    _event(PipelineGit(repo), state, "production_tick_finished", {"root_tick_key": root,
           "status": "completed" if finished else "completed_empty", "work_ids": finished,
           "budget_pool": _budget_grant(model_call.ledger.snapshot())}, now, callback)


def run_schedule(repo, schedule_id, trigger, *, model_call, remote_confirm=None, clock=None, continuation=None):
    with _lock(repo):
        return _run_schedule(repo, schedule_id, trigger, model_call=model_call,
                             remote_confirm=remote_confirm, clock=clock, continuation=continuation)


def _run_schedule(repo, schedule_id, trigger, *, model_call, remote_confirm, clock, continuation):
    from .pending import read_registry, resolve_pending
    from .production import prepare_production, production_status, run_production
    from .scheduler import (_budget_grant, _budget_state, _event, _require, _time, _trigger)
    now = clock or (lambda: datetime.now(timezone.utc))
    git = PipelineGit(repo)
    state = _require(git, schedule_id)
    _budget_state(state, model_call)
    _require_runtime(repo, state, model_call)
    trigger, root = _trigger(trigger, state["config"])
    if continuation is not None:
        if (not isinstance(continuation, dict) or set(continuation) != {"schedule_id", "work_id", "root_tick_key"}
                or continuation["schedule_id"] != schedule_id or continuation["root_tick_key"] != root
                or continuation["work_id"] not in {work["work_id"] for work in _works(state)}):
            _fail("invalid_schedule_continuation")
    if state["paused"]:
        return {"status": "skipped_paused", "next_action": "paused", "root_tick_key": root}
    if trigger["kind"] == "scheduled" and _time(now()) < _time(trigger["scheduled_for"]):
        return {"status": "not_due", "next_action": "wait_until_due", "root_tick_key": root}
    _checkpoint(repo, state, remote_confirm)
    discovery = next((event["data"] for event in state["events"] if event["kind"] == "production_discovered" and event["data"]["root_tick_key"] == root), None)
    if discovery is None:
        from .scheduler import _scope_paths
        _event(git, state, "production_discovered", {"root_tick_key": root, "trigger": trigger,
               "source_paths": _scope_paths(git, state["config"]),
               "budget_pool": _budget_grant(model_call.ledger.snapshot())}, now(), remote_confirm)
        state = _require(git, schedule_id)
        discovery = next(event["data"] for event in state["events"] if event["kind"] == "production_discovered" and event["data"]["root_tick_key"] == root)
    _resolved_waits(repo, state, now=now(), callback=remote_confirm)
    state = _require(git, schedule_id)
    snapshot = discovery["source_paths"]
    found = _discovery(repo, state, allowed_snapshot=snapshot)
    active = [work for work in _works(state) if not work["finished"]]
    attention = {"attempt_limit", "blocked_capacity", "blocked_context"}
    work = next((work for work in active if work["status"] not in {"needs_confirmation", *attention}), None)
    if continuation is not None:
        selected = next(work for work in _works(state) if work["work_id"] == continuation["work_id"])
        if not selected["finished"] and selected["status"] not in {"needs_confirmation", *attention}:
            work = selected
    if work is None and found["jobs"]["jobs"]:
        job = found["jobs"]["jobs"][0]
        pending = {"item_id": job["parent_item_id"], "job_id": job["job_id"],
                   "reply_sources": job["reply_sources"], "parent_run_ids": job["parent_run_ids"],
                   "question": job["question"], "source_refs": job["source_refs"], "object_refs": job["object_refs"]}
        requested, related = job["source_paths"], job["related_runs"]
    elif work is None and found["new"]:
        if not found["context"]["coverage"]["complete"]:
            return {"status": "blocked_context", "next_action": "attention_required", "root_tick_key": root,
                    "coverage": found["context"]["coverage"]}
        pending, requested, related = None, found["new"], found["context"]["related_runs"]
    else:
        pending, requested, related = None, None, None
    if work is None and requested:
        _legacy_reconciliation_gate(repo, requested)
        identity = {"source_paths": requested, "related_runs": related, "pending": pending}
        identifier = "work-" + _digest(identity)
        data = {**identity, "work_id": identifier, "queue_id": "schedule-" + identifier[5:53],
                "allowed_source_paths": found["allowed"], "root_tick_key": root, "trigger": trigger,
                "base_head": git.head(), "budget_pool": _budget_grant(model_call.ledger.snapshot())}
        _event(git, state, "production_started", data, now(), remote_confirm)
        state = _require(git, schedule_id)
        work = next(work for work in _works(state) if work["work_id"] == identifier)
    if work is None:
        status = ("attention_required" if any(work["status"] in attention for work in active)
                  else "awaiting_confirmation" if active else "completed_empty")
        if not active:
            _finish_tick(repo, state, root, model_call, now=now(), callback=remote_confirm)
        return {"status": status, "next_action": "attention_required" if status == "attention_required" else "await_confirmation" if active else "idle", "root_tick_key": root}
    _guard(repo, schedule_id, work, remote_confirm)
    prepared = prepare_production(repo, work["queue_id"], work["source_paths"],
                                  allowed_source_paths=work["allowed_source_paths"], allowed_run_ids=work["related_runs"],
                                  required_context_sources=sorted(ref["path"] for ref in work["pending"]["reply_sources"]) if work["pending"] else [],
                                  continuation_context=({"parent_item_id": work["pending"]["item_id"],
                                      **{key: work["pending"][key] for key in ("job_id", "question", "parent_run_ids", "source_refs", "object_refs", "reply_sources")}}
                                      if work["pending"] else None),
                                  skill_bundle=state["config"].get("skill_bundle"))
    if prepared["status"] == "blocked_context":
        result = prepared
    else:
        def guard(repo, phase, details):
            _guard(repo, schedule_id, work, remote_confirm)
        result = run_production(repo, work["queue_id"], model_call=model_call,
                                max_steps=state["config"]["max_items"], execution_guard=guard)
    state = _require(git, schedule_id)
    active_run = result.get("active_run_id") or result.get("final_run")
    if result["status"] == "published":
        publication = _verified_publication(repo, state, result["publication"]["run_id"])
        if work["pending"]:
            item = next(item for item in read_registry(repo)["items"] if item["item_id"] == work["pending"]["item_id"])
            if item["pending_status"] == "answered":
                if item["reply_sources"] != work["pending"]["reply_sources"]:
                    _fail("continuation_job_changed")
                _guard(repo, schedule_id, work, remote_confirm)
                resolve_pending(repo, item["item_id"], publication["run_id"])
            elif item["pending_status"] != "resolved" or item["resolution"]["run_id"] != publication["run_id"]:
                _fail("continuation_job_changed")
            else:
                # An acknowledgement can be lost after registry CAS. Re-run
                # the idempotent resolver; a status label is not its proof.
                resolve_pending(repo, item["item_id"], publication["run_id"])
        _finish(repo, state, {**work, "active_run_id": active_run}, publication,
                resolved_item_id=work["pending"]["item_id"] if work["pending"] else None,
                now=now(), callback=remote_confirm)
        state = _require(git, schedule_id)
        _resolved_waits(repo, state, now=now(), callback=remote_confirm)
        state = _require(git, schedule_id)
        remaining = _discovery(repo, state, allowed_snapshot=snapshot)
        unfinished = [old for old in _works(state) if not old["finished"]]
        status, action = "completed", "continue" if remaining["new"] or unfinished else "idle"
        if action == "idle":
            _finish_tick(repo, state, root, model_call, now=now(), callback=remote_confirm, extra_work_ids=[work["work_id"]])
    else:
        status = result["status"]
        _progress(repo, state, work, status, active_run_id=active_run, now=now(), callback=remote_confirm)
        action = ("reconcile" if status == "unknown" else "await_confirmation" if status == "needs_confirmation"
                  else "attention_required" if status in {"attempt_limit", "blocked_capacity", "blocked_context"} else "continue")
    return {"status": status, "next_action": action, "root_tick_key": root, "work_id": work["work_id"],
            "queue_id": work["queue_id"], "processing": result,
            "continuation": {"schedule_id": schedule_id, "work_id": work["work_id"], "root_tick_key": root},
            "head": git.head(), "scheduled_environment": "unverified", "real_daily_verified": False,
            "external_wakeup": "not_provisioned"}


def drain_schedule_production(repo, schedule_id, trigger, *, model_call, remote_confirm=None,
                              clock=None, max_slices=8, window_seconds=300, continuation=None):
    from .scheduler import _time
    now = clock or (lambda: datetime.now(timezone.utc))
    begin, slices, current = _time(now()), [], continuation
    while len(slices) < max_slices and _time(now()) - begin < timedelta(seconds=window_seconds):
        result = run_schedule(repo, schedule_id, trigger, model_call=model_call,
                              remote_confirm=remote_confirm, clock=now, continuation=current)
        slices.append(result)
        current = result.get("continuation")
        if result["next_action"] != "continue":
            break
    plan = plan_schedule(repo, schedule_id, now=now())
    remaining = [item for item in plan["items"] if item["status"] != "completed"]
    last = slices[-1] if slices else {"status": "window_ended", "next_action": "continue"}
    return {"schedule_id": schedule_id, "status": last["status"], "slices": slices,
            "remaining": remaining, "next_action": last["next_action"],
            "continuation": current if last["next_action"] in {"continue", "reconcile"} else None,
            "head": PipelineGit(repo).head(), "scheduled_environment": "unverified",
            "real_daily_verified": False, "external_wakeup": "not_provisioned"}


def production_schedule_status(repo, schedule_id, *, now=None, platform_observation=None):
    from .scheduler import _require, _time
    state = _require(PipelineGit(repo), schedule_id)
    current = _time(now)
    local = current.astimezone(ZoneInfo("Asia/Shanghai"))
    hour, minute = map(int, state["config"]["daily_time"].split(":"))
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due > local:
        due -= timedelta(days=1)
    expected = due.astimezone(timezone.utc)
    receipts = {event["data"]["root_tick_key"]: event for event in state["events"]
                if event["kind"] == "production_tick_finished"}
    def status_at(slot):
        key = schedule_id + ":" + slot.isoformat()
        if key in receipts:
            return receipts[key]["data"]["status"]
        if any(work["root_tick_key"] == key for work in _works(state)):
            return "incomplete"
        return "missing_receipt"
    status = status_at(expected)
    if state["paused"]:
        status = "paused"
    elif expected < _time(state["events"][0]["at"]):
        status = "not_due"
    elif status == "missing_receipt":
        status = "within_grace" if current <= expected + timedelta(seconds=state["config"]["grace_seconds"]) else "trigger_unknown"
        if platform_observation is not None and not isinstance(platform_observation, dict):
            _fail("invalid_platform_observation")
        if platform_observation is not None and platform_observation.get("status") == "available":
            if (set(platform_observation) != {"status", "window_start", "window_end", "trigger_ids"}
                    or not isinstance(platform_observation["trigger_ids"], list)
                    or _time(platform_observation["window_start"]) > expected
                    or _time(platform_observation["window_end"]) < expected + timedelta(seconds=state["config"]["grace_seconds"])):
                _fail("invalid_platform_observation")
            status = "incomplete" if platform_observation["trigger_ids"] else "no_trigger"
    plan = plan_schedule(repo, schedule_id, now=current)
    windows = []
    for offset in range(32):
        slot = expected - timedelta(days=offset)
        if slot < _time(state["events"][0]["at"]):
            break
        windows.append({"scheduled_for": slot.isoformat(), "receipt_status": status_at(slot)})
    successes = list(receipts.values())
    return {"schedule_id": schedule_id, "processing_engine": ENGINE, "status": status,
            "expected_for": expected.isoformat(), "expected_windows": list(reversed(windows)),
            "latest_success_at": successes[-1]["at"] if successes else None,
            "history_truncated": expected - _time(state["events"][0]["at"]) >= timedelta(days=32),
            "remaining": [item for item in plan["items"] if item["status"] != "completed"],
            "scheduled_environment": "unverified", "external_trigger_status": "unverified"}
