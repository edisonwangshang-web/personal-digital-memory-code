"""Persistent daily orchestration; the actual GPT cloud runner remains external.

This module never creates timers or claims that caller supplied trigger metadata
proves a real cloud execution. All durable state is append-only private Git data.
"""

from copy import deepcopy
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .contracts import (CaptureError, QUALITY_SCHEMA_VERSIONS, canonical, markdown,
                        parse_markdown, quality_schema_version)
from .transaction import PipelineGit

SCHEMA = "0.4"
BUDGET_SCHEMA = "0.5"
CONTINUOUS_SCHEMA = "0.6"
PRODUCTION_SCHEMA = "0.7"
QUALITY_SCHEMA = "0.10"
PRODUCTION_ENGINE = "production-v1"
CONTINUOUS_POLICY = "continuous-v1"
ROOT = "memory/runs/schedules/"
MAX_EVENTS = 10000
ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
SOURCE_ID = re.compile(r"src-[0-9a-f]{64}")
RUN_ID = re.compile(r"run-[0-9a-f]{32}")
SOURCE_PATH = re.compile(r"memory/sources/(?:conversations|inbox)/(src-[0-9a-f]{64})/(sha256-[0-9a-f]{64})\.md")
CONTEXT_FIELDS = {"schedule_id", "tick_key", "attempt_id", "run_id", "reservation_path", "reservation_sha256"}


def _budget_pool(value):
    if (not isinstance(value, dict) or set(value) != {"pool_id", "authorization_sha256", "grant_sha256"}
            or not isinstance(value["pool_id"], str) or not re.fullmatch(r"budget-[0-9a-f]{32}", value["pool_id"])
            or any(not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
                   for key in ("authorization_sha256", "grant_sha256"))):
        _fail("unknown_schedule_state")
    return value


def _budget_binding(value):
    if (not isinstance(value, dict) or set(value) != {"pool_id", "authorization_sha256", "grant_sha256", "operation_id", "reserved_calls", "reuse_run_id"}
            or not re.fullmatch(r"budget-[0-9a-f]{32}", str(value.get("pool_id", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("authorization_sha256", "")))
            or not isinstance(value["operation_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value["operation_id"])
            or type(value["reserved_calls"]) is not int or value["reserved_calls"] not in {0, 2}
            or (value["reserved_calls"] == 2 and value["reuse_run_id"] is not None)
            or (value["reserved_calls"] == 0 and not RUN_ID.fullmatch(str(value["reuse_run_id"])))):
        _fail("unknown_schedule_state")
    _budget_pool({key: value[key] for key in ("pool_id", "authorization_sha256", "grant_sha256")})
    return value


def _budget_event(kind, data, *, continuous=False):
    if kind == "tick_started":
        fields = {"tick_key", "trigger", "budget_pool"}
        if continuous:
            fields.add("queue")
            _queue(data.get("queue"))
        if set(data) != fields:
            _fail("unknown_schedule_state")
        _budget_pool(data["budget_pool"])
        return
    _budget_binding(data.get("budget_binding"))
    if kind == "attempt_reserved":
        fields = {"attempt_id", "run_id", "tick_key", "config_digest", "item_key", "input_path",
                  "rules_version", "source_paths", "related_runs", "reserved_calls", "lease_expires_at", "budget_binding"}
        if set(data) != fields:
            _fail("unknown_schedule_state")
    elif kind in {"attempt_result", "attempt_registration_repaired"}:
        required = {"attempt_id", "run_id", "status", "reserved_calls", "usage", "budget_binding", "budget_started_calls"}
        optional = {"observed_calls", "recovered", "publication_run_id", "reason", "validation_ref",
                    "operator_confirmed_stopped", "previous_status", "execution_stopped_statement", "uncertainty"}
        count = data.get("budget_started_calls")
        if (not required <= data.keys() or data.keys() - required - optional
                or (count is not None and (type(count) is not int or not 0 <= count <= 2))
                or (count is not None and count > data["budget_binding"]["reserved_calls"])
                or (count is None and data.get("operator_confirmed_stopped") is not True)
                or ("observed_calls" in data and (type(data["observed_calls"]) is not int or not 0 <= data["observed_calls"] <= 2))
                or ("recovered" in data and type(data["recovered"]) is not bool)):
            _fail("unknown_schedule_state")
    else:
        _fail("unknown_schedule_state")


def _budget_state(state, model_call, binding=None):
    """Resolve the committed pool/operation; a different ledger cannot reset it."""
    from .budget import BudgetBlocked, BudgetedModelCall
    if state["config"].get("execution_policy") == CONTINUOUS_POLICY:
        if not isinstance(model_call, BudgetedModelCall):
            raise BudgetBlocked("budget_binding_required")
        if model_call.ledger.snapshot().get("mode") != "audit":
            raise BudgetBlocked("audit_ledger_required")
    pool = state.get("budget_pool_id")
    if pool is None and binding is None:
        return None
    if not isinstance(model_call, BudgetedModelCall):
        raise BudgetBlocked("budget_binding_required")
    snapshot = model_call.ledger.snapshot()
    if snapshot["pool_id"] != (binding["pool_id"] if binding else pool):
        raise BudgetBlocked("budget_pool_mismatch")
    if snapshot["authorization_sha256"] != (binding["authorization_sha256"] if binding else state["budget_authorization_sha256"]):
        raise BudgetBlocked("budget_authorization_mismatch")
    if _budget_grant(snapshot)["grant_sha256"] != (binding["grant_sha256"] if binding else state["budget_grant_sha256"]):
        raise BudgetBlocked("budget_grant_mismatch")
    if binding is None:
        return None
    operation = model_call.ledger.operation(binding["operation_id"])
    if binding["reserved_calls"] == 0:
        if operation is not None:
            raise BudgetBlocked("budget_operation_conflict", operation_id=binding["operation_id"])
    elif operation is None or operation["reserved_roles"] != ["extract", "review"]:
        raise BudgetBlocked("budget_operation_conflict", operation_id=binding["operation_id"])
    return operation


def _budget_grant(snapshot):
    fields = {key: snapshot[key] for key in ("pool_id", "authorization_sha256", "limit", "initial_used")}
    if snapshot.get("mode") == "audit":
        fields.update(schema_version=snapshot["schema_version"], mode="audit")
    return {key: fields[key] for key in ("pool_id", "authorization_sha256")} | {"grant_sha256": _sha(canonical(fields))}


def _fail(code):
    raise CaptureError(code, code)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _queue(value):
    fields = {"root_tick_key", "parent_tick_key", "slice_index", "source_paths"}
    if (not isinstance(value, dict) or set(value) != fields
            or not isinstance(value["root_tick_key"], str) or not 1 <= len(value["root_tick_key"]) <= 512
            or type(value["slice_index"]) is not int or not 0 <= value["slice_index"] < MAX_EVENTS
            or not isinstance(value["source_paths"], list) or len(value["source_paths"]) > MAX_EVENTS
            or any(not isinstance(path, str) or not SOURCE_PATH.fullmatch(path) for path in value["source_paths"])
            or value["source_paths"] != sorted(set(value["source_paths"]))):
        _fail("invalid_schedule_continuation")
    index, root = value["slice_index"], value["root_tick_key"]
    expected_parent = None if index == 0 else (root if index == 1 else root + ":continuation:" + str(index - 1))
    if value["parent_tick_key"] != expected_parent:
        _fail("invalid_schedule_continuation")
    return value


def _queue_key(value):
    value = _queue(value)
    return value["root_tick_key"] + (":continuation:" + str(value["slice_index"]) if value["slice_index"] else "")


def _uncertain_operation(operation):
    return operation is not None and any(call["status"] in {"started", "unknown"} for call in operation["calls"].values())


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("unknown_schedule_state")
        result[key] = value
    return result


def _json(value):
    try:
        return json.loads(value, object_pairs_hook=_unique,
                          parse_constant=lambda value: _fail("unknown_schedule_state"))
    except (ValueError, TypeError, RecursionError):
        _fail("unknown_schedule_state")


def _time(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        _fail("invalid_schedule_time")


def _identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        _fail("invalid_schedule_id")
    return value


def _day(value):
    return _time(value).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def normalize_config(value):
    """Accept explicit scopes only; configuration contains no credentials."""
    required = {"schedule_id", "daily_time", "allowed_source_ids", "allowed_source_paths"}
    optional = {"timezone", "related_run_ids", "max_items", "max_calls", "max_attempts",
                "timeout_seconds", "lease_seconds", "grace_seconds", "remote_mode", "remote", "execution_policy", "processing_engine",
                "skill_bundle"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        _fail("invalid_schedule_config")
    continuous = value.get("execution_policy") == CONTINUOUS_POLICY
    if "execution_policy" in value and not continuous:
        _fail("invalid_schedule_policy")
    if "processing_engine" in value and (not continuous or value["processing_engine"] not in {PRODUCTION_ENGINE, "legacy-v1"}):
        _fail("invalid_schedule_policy")
    if "skill_bundle" in value:
        if value.get("processing_engine") != PRODUCTION_ENGINE:
            _fail("invalid_schedule_policy")
        from .skill_support import validate_skill_bundle
        try:
            validate_skill_bundle(value["skill_bundle"])
        except (ValueError, TypeError):
            _fail("invalid_schedule_skill_bundle")
    config = {"timezone": "Asia/Shanghai", "related_run_ids": [], "max_items": 8,
              "max_calls": None if continuous else 16, "max_attempts": 4 if continuous else 3, "timeout_seconds": 120,
              "lease_seconds": 1800, "grace_seconds": 1800, "remote_mode": "required",
              "remote": "origin", **value}
    if "skill_bundle" in config:
        config["skill_bundle"] = deepcopy(config["skill_bundle"])
    _identifier(config["schedule_id"])
    if config["timezone"] != "Asia/Shanghai" or not isinstance(config["daily_time"], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", config["daily_time"]):
        _fail("invalid_schedule_config")
    for field, pattern in (("allowed_source_ids", SOURCE_ID), ("allowed_source_paths", SOURCE_PATH), ("related_run_ids", RUN_ID)):
        items = config[field]
        if (not isinstance(items, list) or len(items) > (8 if field == "related_run_ids" and config.get("processing_engine") != PRODUCTION_ENGINE else 256) or any(not isinstance(item, str) or not pattern.fullmatch(item) for item in items)
                or len(set(items)) != len(items)):
            _fail("invalid_schedule_scope")
        config[field] = sorted(items)
    if not config["allowed_source_ids"] and not config["allowed_source_paths"]:
        _fail("empty_schedule_scope")
    if continuous and (config["max_calls"] is not None or type(config["max_attempts"]) is not int or config["max_attempts"] != 4):
        _fail("invalid_schedule_budget")
    for field, lower, upper in (("max_items", 1, 256), ("max_calls", 2, 512), ("max_attempts", 1, 20),
                                ("timeout_seconds", 1, 600), ("lease_seconds", 1, 86400), ("grace_seconds", 0, 86400)):
        if continuous and field == "max_calls":
            continue
        number = config[field]
        if isinstance(number, bool) or not isinstance(number, int) or not lower <= number <= upper:
            _fail("invalid_schedule_budget")
    if config["lease_seconds"] < config["timeout_seconds"] * 2:
        _fail("invalid_schedule_budget")
    if config["remote_mode"] not in {"required", "local_only"} or not isinstance(config["remote"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", config["remote"]):
        _fail("invalid_schedule_remote")
    return config


def _read(git, path, commit=None):
    data = git.read(commit or git.head(), path)
    if commit is None and git.path(path).read_bytes() != data:
        _fail("schedule_state_changed")
    try:
        header, body = parse_markdown(data)
        _json(data.decode("utf-8")[4:].split("\n---\n", 1)[0])
        value = _json(body)
        version = header.get("schema_version")
        if (version not in {SCHEMA, BUDGET_SCHEMA, CONTINUOUS_SCHEMA, PRODUCTION_SCHEMA, *QUALITY_SCHEMA_VERSIONS} or header != {"schema_version": version, "kind": "schedule_event"}
                or not isinstance(value, dict) or not isinstance(value.get("data"), dict)):
            raise ValueError
        if version in {PRODUCTION_SCHEMA, *QUALITY_SCHEMA_VERSIONS}:
            from .schedule_production import validate_event
            validate_event(value.get("kind"), value["data"])
        elif version == BUDGET_SCHEMA:
            _budget_event(value.get("kind"), value["data"])
        elif version == CONTINUOUS_SCHEMA:
            if value.get("kind") in {"tick_started", "attempt_reserved", "attempt_result", "attempt_registration_repaired"}:
                _budget_event(value["kind"], value["data"], continuous=True)
        elif {"budget_binding", "budget_started_calls", "budget_pool"} & value["data"].keys():
            raise ValueError
    except (ValueError, TypeError):
        _fail("unknown_schedule_state")
    return value, data


def _state(git, schedule_id, commit=None):
    _identifier(schedule_id)
    prefix = ROOT + schedule_id + "/events/"
    snapshot = commit or git.head()
    if git.run("log", "--format=%H", "--diff-filter=MD", snapshot, "--", prefix):
        _fail("unknown_schedule_state")
    paths = git.run("ls-tree", "-r", "--name-only", snapshot, "--", prefix).decode().splitlines()
    if len(paths) > MAX_EVENTS:
        _fail("schedule_state_limit")
    events = []
    previous = None
    for sequence, path in enumerate(sorted(paths)):
        if not re.fullmatch(re.escape(prefix) + r"[0-9]{8}\.md", path) or path != prefix + f"{sequence:08d}.md":
            _fail("unknown_schedule_state")
        event, _ = _read(git, path, commit)
        if (set(event) != {"schedule_id", "sequence", "previous_event", "kind", "at", "data"}
                or event["schedule_id"] != schedule_id or event["sequence"] != sequence or event["previous_event"] != previous
                or event["kind"] not in {"configured", "paused", "resumed", "tick_started", "attempt_reserved", "attempt_result", "attempt_registration_repaired", "tick_finished", "production_discovered", "production_started", "production_progress", "production_finished", "production_tick_finished"}
                or not isinstance(event["data"], dict)):
            _fail("unknown_schedule_state")
        if not isinstance(event["at"], str):
            _fail("unknown_schedule_state")
        _time(event["at"])
        events.append({**event, "path": path})
        previous = path
    if not events:
        return None
    if events[0]["kind"] != "configured" or any(event["kind"] == "configured" for event in events[1:]):
        _fail("unknown_schedule_state")
    config = normalize_config(events[0]["data"])
    if config["schedule_id"] != schedule_id:
        _fail("unknown_schedule_state")
    continuous = config.get("execution_policy") == CONTINUOUS_POLICY
    production = config.get("processing_engine") == PRODUCTION_ENGINE
    quality = "skill_bundle" in config
    for event in events:
        header, _ = parse_markdown(git.read(snapshot, event["path"]))
        if ((header["schema_version"] in {CONTINUOUS_SCHEMA, PRODUCTION_SCHEMA, *QUALITY_SCHEMA_VERSIONS}) != continuous
                or (header["schema_version"] in {PRODUCTION_SCHEMA, *QUALITY_SCHEMA_VERSIONS}) != production
                or (header["schema_version"] in QUALITY_SCHEMA_VERSIONS) != quality
                or quality and header["schema_version"] != quality_schema_version(config["skill_bundle"])):
            _fail("unknown_schedule_state")
    if production:
        from .schedule_production import validate_history
        validate_history(config, events)
    paused = False
    pools = set()
    for event in events:
        if "budget_pool" in event["data"]:
            bound = _budget_pool(event["data"]["budget_pool"])
            pools.add((bound["pool_id"], bound["authorization_sha256"], bound["grant_sha256"]))
        if "budget_binding" in event["data"]:
            bound = _budget_binding(event["data"]["budget_binding"])
            pools.add((bound["pool_id"], bound["authorization_sha256"], bound["grant_sha256"]))
        if event["kind"] == "paused":
            paused = True
        elif event["kind"] == "resumed":
            if not isinstance(event["data"].get("validation_ref"), str) or not event["data"]["validation_ref"]:
                _fail("unknown_schedule_state")
            paused = False
    if len(pools) > 1:
        _fail("unknown_schedule_state")
    pool = next(iter(pools), (None, None, None))
    return {"config": config, "config_digest": _sha(canonical(config)), "events": events, "paused": paused,
            "budget_pool_id": pool[0], "budget_authorization_sha256": pool[1], "budget_grant_sha256": pool[2]}


def _append(git, schedule_id, kind, data, at, expected_head=None):
    with git.lock():
        git.recover()
        return _append_locked(git, schedule_id, kind, data, at, expected_head)


def _append_locked(git, schedule_id, kind, data, at, expected_head=None):
    head = git.check(expected_head)
    state = _state(git, schedule_id)
    sequence = len(state["events"]) if state else 0
    if sequence >= MAX_EVENTS:
        _fail("schedule_state_limit")
    path = ROOT + schedule_id + f"/events/{sequence:08d}.md"
    event = {"schedule_id": schedule_id, "sequence": sequence,
             "previous_event": state["events"][-1]["path"] if state else None,
             "kind": kind, "at": _time(at).isoformat(), "data": data}
    continuous = (state["config"].get("execution_policy") if state else data.get("execution_policy")) == CONTINUOUS_POLICY
    if continuous and state:
        # Check the proposed append while holding the shared Git lock. A stale
        # concurrent caller must not poison the log with duplicate slice claims.
        _attempts({**state, "events": [*state["events"], {**event, "path": path}]})
    configuration = state["config"] if state else data
    version = (quality_schema_version(configuration["skill_bundle"]) if "skill_bundle" in configuration
               else PRODUCTION_SCHEMA if configuration.get("processing_engine") == PRODUCTION_ENGINE
               else CONTINUOUS_SCHEMA if continuous else BUDGET_SCHEMA if {"budget_binding", "budget_pool"} & data.keys() else SCHEMA)
    if version in {PRODUCTION_SCHEMA, *QUALITY_SCHEMA_VERSIONS}:
        from .schedule_production import validate_event, validate_history
        validate_event(kind, data)
        validate_history(state["config"] if state else data, [*(state["events"] if state else []), {**event, "path": path}])
    if version in {BUDGET_SCHEMA, CONTINUOUS_SCHEMA} and kind in {"tick_started", "attempt_reserved", "attempt_result", "attempt_registration_repaired"}:
        _budget_event(kind, data, continuous=continuous)
    contents = markdown({"schema_version": version, "kind": "schedule_event"}, canonical(event).decode())
    commit = git.write_files({path: contents}, head, "schedule: persist " + kind)
    return path, contents, commit


def _confirm(git, config, commit, files, callback):
    if config["remote_mode"] == "local_only":
        return "not_requested"
    if callback is None:
        # Explicit use of the existing configured Git remote preserves its auth.
        git.sync(config["remote"], files, commit)
    else:
        callback(git, commit, files)
        # A callback can inject transport faults, never manufacture success.
        # Independently verify its result using the configured real remote.
        branch = git.text("symbolic-ref", "--quiet", "--short", "HEAD")
        git.run("fetch", "--no-tags", config["remote"], "refs/heads/" + branch)
        remote_head = git.text("rev-parse", "FETCH_HEAD")
        try:
            git.run("merge-base", "--is-ancestor", commit, remote_head)
        except CaptureError:
            _fail("remote_unconfirmed")
        for path, data in files.items():
            if git.read(remote_head, path) != data:
                _fail("remote_mismatch")
    return "confirmed"


def _event(git, state, kind, data, at, callback=None):
    path, contents, commit = _append(git, state["config"]["schedule_id"], kind, data, at, git.head())
    _confirm(git, state["config"], commit, {path: contents}, callback)
    return path, contents, commit


def configure_schedule(repo, config, expected_head=None, *, now=None, use_default_skills=False):
    git = PipelineGit(repo)
    git.check(expected_head)
    if not isinstance(config, dict):
        _fail("invalid_schedule_config")
    _identifier(config.get("schedule_id"))
    state = _state(git, config["schedule_id"])
    if (config.get("execution_policy") == CONTINUOUS_POLICY and "processing_engine" not in config
            and (state is None or state["config"].get("processing_engine") == PRODUCTION_ENGINE)):
        config = {**config, "processing_engine": PRODUCTION_ENGINE}
    # Reopening an existing schedule inherits its frozen rules. Upgrading the
    # local package neither loads new rules nor drops the prior quality gate.
    if state is not None and "skill_bundle" not in config and "skill_bundle" in state["config"]:
        config = {**config, "skill_bundle": state["config"]["skill_bundle"]}
    if (use_default_skills and state is None and "skill_bundle" not in config
            and config.get("processing_engine") == PRODUCTION_ENGINE):
        from .skill_support import resolve_skill_refs
        config = {**config, "skill_bundle": resolve_skill_refs()}
    config = normalize_config(config)
    if state:
        if state["config"] != config:
            _fail("schedule_already_configured")
        return {"schedule_id": config["schedule_id"], "status": "already_configured", "head": git.head(), "scheduled_environment": "unverified"}
    if config.get("processing_engine") == PRODUCTION_ENGINE and config["remote_mode"] == "required":
        from .runtime import require_environment
        require_environment(repo, require_binding=True, remote=config["remote"])
    path, _, commit = _append(git, config["schedule_id"], "configured", config, now, expected_head)
    return {"schedule_id": config["schedule_id"], "status": "configured", "config_path": path,
            "head": commit, "scheduled_environment": "unverified"}


def _require(git, schedule_id):
    state = _state(git, schedule_id)
    if state is None:
        _fail("schedule_not_configured")
    return state


def _scope_paths(git, config):
    paths = set(config["allowed_source_paths"])
    for source_id in config["allowed_source_ids"]:
        prefixes = ["memory/sources/" + folder + "/" + source_id + "/" for folder in ("conversations", "inbox")]
        listed = git.run("ls-tree", "-r", "--name-only", git.head(), "--", *prefixes).decode().splitlines()
        if len(paths) + len(listed) > MAX_EVENTS:
            _fail("schedule_scope_limit")
        if any(not SOURCE_PATH.fullmatch(path) for path in listed):
            _fail("invalid_schedule_scope")
        paths.update(listed)
    return sorted(paths)


def _item_key(path, rules_version, execution_policy=None):
    match = SOURCE_PATH.fullmatch(path)
    if execution_policy == CONTINUOUS_POLICY:
        return _sha(canonical({"source_id": match[1], "source_version": match[2], "execution_policy": execution_policy}))
    return _sha(canonical({"source_id": match[1], "source_version": match[2], "rules_version": rules_version}))


def _authorized(config, path):
    match = SOURCE_PATH.fullmatch(path) if isinstance(path, str) else None
    return match is not None and (path in config["allowed_source_paths"] or match[1] in config["allowed_source_ids"])


def _related_paths(git, config, related_runs=None):
    from .pending import _publication_source_refs

    paths = set()
    for run in config["related_run_ids"] if related_runs is None else related_runs:
        # Formal group IDs deliberately have no extraction staging document.
        # Read only committed manifest bindings before publication verification
        # is allowed to open any source or independently reviewed object body.
        for binding in _publication_source_refs(git, run):
            path = binding["path"]
            if not _authorized(config, path):
                _fail("invalid_schedule_scope")
            paths.add(path)
    if len(paths) > 8:
        _fail("schedule_scope_limit")
    return paths


def _derived_related(git, state):
    """Advance only through this schedule's verified, still authorized results."""
    from .pipeline import verify_publication
    from .controls import read_controls, source_policy, object_policy
    config = state["config"]
    controls = read_controls(git.repo)
    known = list(config["related_run_ids"])
    for event in state["events"]:
        data = event["data"]
        if event["kind"] == "attempt_result" and data.get("status") in {"published", "already_processed"}:
            run = data.get("publication_run_id", data["run_id"])
            if run not in known:
                known.append(run)
    selected, covered, stale_configured = [], set(), []
    for run in reversed(known):
        scoped = _related_paths(git, config, [run])  # Scope before body reads.
        _, manifest_body = parse_markdown(git.read(git.head(), f"memory/runs/processes/{run}/manifest.md"))
        manifest = _json(manifest_body)
        files = manifest.get("files")
        if (not isinstance(files, dict) or not files or len(files) > 256
                or any(not isinstance(path, str) or not re.fullmatch(
                    r"memory/(?:records|topics)/[a-z][a-z0-9-]{0,95}\.md", path) for path in files)):
            _fail("unknown_schedule_state")
        excluded = any(source_policy(controls, path, purpose="history")["excluded"] for path in scoped)
        excluded = excluded or any(object_policy(controls, path, purpose="history")["excluded"] for path in files)
        if excluded:
            if run in config["related_run_ids"]:
                _fail("needs_context_configuration")
            continue
        publication = verify_publication(git.repo, run, source_paths=sorted(scoped), historical=True)
        files = publication["files"]
        matches = True
        for path, digest in files.items():
            try:
                actual = _sha(git.read(git.head(), path))
            except CaptureError:
                matches = False
                break
            if digest not in {actual, "sha256-" + actual}:
                matches = False
                break
        if matches and not set(files) & covered:
            selected.append(run)
            covered.update(files)
        elif run in config["related_run_ids"]:
            stale_configured.append(set(files))
    if any(not paths.issubset(covered) for paths in stale_configured) or len(selected) > 8:
        _fail("needs_context_configuration")
    return sorted(selected)


def _attempts(state):
    reservations, results = {}, {}
    started, calls, counts, item_counts = set(), {}, {}, {}
    continuous = state["config"].get("execution_policy") == CONTINUOUS_POLICY
    queues, finished_ticks = {}, set()
    allowed_related = set(state["config"]["related_run_ids"])
    for event in state["events"]:
        data = event["data"]
        if event["kind"] == "tick_started":
            trigger, key = _trigger(data.get("trigger"), state["config"])
            if continuous:
                queue = _queue(data.get("queue"))
                if (queue["root_tick_key"] != key or any(not _authorized(state["config"], path) for path in queue["source_paths"])
                        or (queue["parent_tick_key"] is not None and queue["parent_tick_key"] not in finished_ticks)):
                    _fail("unknown_schedule_state")
                if queue["slice_index"] and queue["source_paths"] != queues[queue["root_tick_key"]]["source_paths"]:
                    _fail("unknown_schedule_state")
                key = _queue_key(queue)
                queues[key] = queue
            fields = ({"tick_key", "trigger", "budget_pool", "queue"},) if continuous else ({"tick_key", "trigger"}, {"tick_key", "trigger", "budget_pool"})
            if (set(data) not in fields
                    or key != data.get("tick_key") or key in started):
                _fail("unknown_schedule_state")
            started.add(key)
        elif event["kind"] == "attempt_reserved":
            attempt = data.get("attempt_id")
            if (not isinstance(attempt, str) or not re.fullmatch(r"attempt-[0-9a-f]{32}", attempt) or attempt in reservations
                    or not RUN_ID.fullmatch(data.get("run_id", "")) or data.get("reserved_calls") != 2
                    or not isinstance(data.get("source_paths"), list) or not 1 <= len(data["source_paths"]) <= 8
                    or data["source_paths"] != sorted(set(data["source_paths"]))
                    or any(not _authorized(state["config"], path) for path in data["source_paths"])
                    or not isinstance(data.get("related_runs"), list) or len(data["related_runs"]) > 8
                    or any(not isinstance(run, str) or run not in allowed_related for run in data["related_runs"])
                    or data["related_runs"] != sorted(set(data["related_runs"]))
                    or data.get("tick_key") not in started or not isinstance(data.get("lease_expires_at"), str)
                    or not isinstance(data.get("rules_version"), str)
                    or data.get("input_path") not in data["source_paths"]
                    or not isinstance(data.get("item_key"), str) or not re.fullmatch(r"[0-9a-f]{64}", data["item_key"])
                    or data.get("config_digest") != state["config_digest"]):
                _fail("unknown_schedule_state")
            if data["item_key"] != _item_key(data["input_path"], data["rules_version"], state["config"].get("execution_policy")):
                _fail("unknown_schedule_state")
            if continuous and data["input_path"] not in queues[data["tick_key"]]["source_paths"]:
                _fail("unknown_schedule_state")
            if continuous and any(previous["data"]["item_key"] == data["item_key"]
                    and (prior not in results or results[prior]["data"]["status"] == "reconciliation_required")
                    for prior, previous in reservations.items()):
                _fail("schedule_item_inflight")
            _time(data.get("lease_expires_at"))
            reservations[attempt] = event
            day = _day(event["at"])
            calls[day] = calls.get(day, 0) + 2
            counts[day] = counts.get(day, 0) + 1
            item_counts[data["item_key"]] = item_counts.get(data["item_key"], 0) + 1
            per_tick = sum(item["data"]["tick_key"] == data["tick_key"] for item in reservations.values())
            if ((not continuous and (calls[day] > state["config"]["max_calls"] or counts[day] > state["config"]["max_items"]))
                    or (continuous and per_tick > state["config"]["max_items"])
                    or item_counts[data["item_key"]] > state["config"]["max_attempts"]):
                _fail("unknown_schedule_state")
        elif event["kind"] in {"attempt_result", "attempt_registration_repaired"}:
            attempt = data.get("attempt_id")
            repair = event["kind"] == "attempt_registration_repaired"
            previous = results.get(attempt, {}).get("data", {})
            if (attempt not in reservations or (attempt in results and not repair)
                    or repair and (previous.get("status") != "reconciliation_required" or previous.get("reason") != "registration_required" or data.get("status") != "awaiting_confirmation")
                    or data.get("status") not in {
                    "published", "already_processed", "awaiting_confirmation", "failed", "needs_revision", "reconciliation_required", "abandoned", "abandoned_uncertain"}):
                _fail("unknown_schedule_state")
            if data.get("run_id") != reservations[attempt]["data"]["run_id"] or data.get("reserved_calls") != 2:
                _fail("unknown_schedule_state")
            if data.get("budget_binding") != reservations[attempt]["data"].get("budget_binding"):
                _fail("unknown_schedule_state")
            if data["status"] in {"published", "already_processed"}:
                publication_run = data.get("publication_run_id")
                if not isinstance(publication_run, str) or not RUN_ID.fullmatch(publication_run) or (data["status"] == "published" and publication_run != data["run_id"]):
                    _fail("unknown_schedule_state")
                allowed_related.add(publication_run)
            results[attempt] = event
        elif continuous and event["kind"] == "tick_finished":
            key = data.get("tick_key")
            if key not in started or key in finished_ticks:
                _fail("unknown_schedule_state")
            finished_ticks.add(key)
    return reservations, results


def _confirmation_identity(git, reservation, result):
    """Bind a registry item to the original, unchanged confirmation Review."""
    from .pipeline import _candidate_binding, _candidate_bindings, _committed, _hash, _last_commit, _load
    run = reservation["data"]["run_id"]
    snapshot = _last_commit(git, git.head(), result["path"])
    stage_path, review_path = "memory/staging/" + run + ".md", "memory/runs/processes/" + run + "/review.md"
    saved = {}
    for path in (stage_path, review_path):
        saved[path] = _committed(git, path)
        if (git.read(snapshot, path) != saved[path]
                or _last_commit(git, git.head(), path) != _last_commit(git, snapshot, path)):
            _fail("schedule_pending_mismatch")
    candidate = _load(saved[stage_path], "candidate", SCHEMA)
    bindings = _candidate_bindings(candidate)
    if sorted(binding["path"] for binding in bindings) != reservation["data"]["source_paths"]:
        _fail("invalid_schedule_scope")
    review = _load(saved[review_path], "review", SCHEMA)
    stage_commit, review_commit = (_last_commit(git, snapshot, path) for path in (stage_path, review_path))
    context = {key: reservation["data"][key] for key in ("tick_key", "attempt_id", "run_id")}
    context.update(schedule_id=reservation["schedule_id"], reservation_path=reservation["path"],
                   reservation_sha256=_sha(git.read(snapshot, reservation["path"])))
    if (candidate.get("run_id") != run or candidate.get("binding") != _candidate_binding(candidate)
            or candidate.get("execution_context") != context or review.get("run_id") != run
            or review.get("binding") != candidate["binding"] or review.get("stage_path") != stage_path
            or review.get("stage_commit") != stage_commit or review.get("stage_sha256") != _hash(saved[stage_path])
            or git.text("rev-parse", stage_commit + "^") != candidate.get("base_head")
            or git.text("rev-parse", review_commit + "^") != stage_commit):
        _fail("schedule_pending_mismatch")
    output = review.get("output", {})
    reasons = output.get("reasons")
    if (output.get("verdict") != "needs_confirmation" or not isinstance(reasons, list) or not reasons
            or any(not isinstance(reason, str) for reason in reasons)):
        _fail("schedule_pending_mismatch")
    try:
        objects = [{"path": obj["path"], "id": obj["metadata"]["id"],
                    "content_version": obj["metadata"]["content_version"]} for obj in candidate["objects"]]
        return {"question": "\n".join(reasons),
                "source_refs": sorted(({key: binding[key] for key in ("source_id", "source_version", "path")}
                                       for binding in bindings), key=lambda ref: ref["path"]),
                "object_refs": sorted(objects, key=lambda ref: ref["path"])}
    except (KeyError, TypeError):
        _fail("schedule_pending_mismatch")


def _confirmation_resolution(git, state, reservation, result, registry):
    """Verify a manual follow-up without publishing or importing its context."""
    from .pending import _identity, _publication_scope, _reply_association_commit
    from .pipeline import _last_commit, load_source, verify_publication
    run = reservation["data"]["run_id"]
    candidates = [item for item in registry["items"] if run in item["run_ids"] and item["pending_status"] == "resolved"]
    if not candidates:
        return None
    identity = _confirmation_identity(git, reservation, result)
    key = "sha256-" + _sha(canonical(identity))
    item = next((item for item in candidates if item["key"] == key), None)
    if item is None:
        return None
    if _identity(item) != identity:
        _fail("schedule_pending_mismatch")
    resolution = item["resolution"]
    # Registry/manifest preflight is metadata only. Both visible manifest scope
    # and the candidate's own binding must pass before any source body opens.
    paths = _publication_scope(git, resolution["run_id"], item)
    if any(not _authorized(state["config"], ref["path"]) for ref in [*item["source_refs"], *item["reply_sources"]]):
        _fail("invalid_schedule_scope")
    if _last_commit(git, git.head(), resolution["manifest_path"]) != resolution["commit_sha"]:
        _fail("pending_publication_mismatch")
    publication = verify_publication(git.repo, resolution["run_id"], source_paths=paths, historical=True)
    if resolution != {"run_id": publication["run_id"], "commit_sha": publication["commit_sha"],
                      "manifest_path": publication["manifest_path"]}:
        _fail("pending_publication_mismatch")
    referenced = set()
    for path in publication["files"]:
        if path.startswith("memory/records/"):
            metadata, _ = parse_markdown(git.read(publication["commit_sha"], path))
            if metadata.get("review_status") != "passed":
                _fail("pending_publication_mismatch")
            referenced.update((ref["source_id"], ref["source_version"]) for ref in metadata["sources"])
    if not {(ref["source_id"], ref["source_version"]) for ref in item["reply_sources"]} <= referenced:
        _fail("pending_reply_not_applied")
    for ref in item["reply_sources"]:
        source = load_source(git.repo, ref["path"], purpose="history")
        if any(source[key] != ref[key] for key in ("source_id", "source_version", "path", "commit_sha")):
            _fail("pending_reply_conflict")
        associated = _reply_association_commit(git, item, ref)
        try:
            git.run("merge-base", "--is-ancestor", associated, publication["commit_sha"])
        except CaptureError:
            _fail("pending_publication_mismatch")
    return {"pending_item_id": item["item_id"], "original_run_id": run, "publication_run_id": publication["run_id"],
            "commit_sha": publication["commit_sha"], "manifest_path": publication["manifest_path"]}


def plan_daily(repo, schedule_id, now=None):
    """Queue authorized immutable versions; original event dates never filter work."""
    from .pipeline import execution_versions
    from .controls import read_controls, source_policy, _metadata_only
    git = PipelineGit(repo)
    head = git.check()
    state = _require(git, schedule_id)
    if state["config"].get("processing_engine") == PRODUCTION_ENGINE:
        from .schedule_production import plan_schedule
        return plan_schedule(repo, schedule_id, now=now)
    reservations, results = _attempts(state)
    rules = execution_versions(git)["rules_version"]
    related_runs = _derived_related(git, state)
    related_paths = _related_paths(git, state["config"], related_runs)
    controls = read_controls(repo)
    items, registry = [], None
    for path in _scope_paths(git, state["config"]):
        key = _item_key(path, rules, state["config"].get("execution_policy"))
        attempted = [attempt for attempt, event in reservations.items() if event["data"]["item_key"] == key]
        finished = [results[attempt]["data"] for attempt in attempted if attempt in results]
        inflight = [attempt for attempt in attempted if attempt not in results or results[attempt]["data"]["status"] == "reconciliation_required"]
        resolutions = []
        if source_policy(controls, path, as_of=_time(now))["excluded"]:
            status = "excluded"
        elif _metadata_only(git, path).get("acquisition_status") == "link_only":
            status = "waiting_text"
        elif any(result["status"] in {"published", "already_processed"} for result in finished):
            from .pipeline import verify_publication
            for attempt in attempted:
                result = results.get(attempt, {}).get("data", {})
                if result.get("status") == "published":
                    verify_publication(repo, result["run_id"], historical=True)
                elif result.get("status") == "already_processed":
                    if _pipeline_recovery(repo, result["run_id"])["status"] != "already_processed":
                        _fail("unknown_schedule_state")
            status = "completed"
        elif inflight:
            status = "reconciliation_required"
        elif any(result["status"] == "awaiting_confirmation" for result in finished):
            status = "awaiting_confirmation"
            if registry is None:
                from .pending import read_registry
                registry = read_registry(repo, head=head)
            waiting = [attempt for attempt in attempted if results.get(attempt, {}).get("data", {}).get("status") == "awaiting_confirmation"]
            for attempt in waiting:
                resolution = _confirmation_resolution(git, state, reservations[attempt], results[attempt], registry)
                if resolution is None:
                    break
                resolutions.append(resolution)
            if len(resolutions) == len(waiting):
                status = "resolved"
        elif len(attempted) >= state["config"]["max_attempts"]:
            status = "attempt_limit"
        else:
            status = "eligible"
        source_paths = sorted({path, *related_paths})
        if len(source_paths) > 8:
            _fail("schedule_scope_limit")
        item = {"item_key": key, "input_path": path, "source_paths": source_paths, "attempts": len(attempted), "status": status}
        if status == "resolved":
            item["confirmation_resolutions"] = resolutions
        items.append(item)
    git.check(head)
    return {"schedule_id": schedule_id, "paused": state["paused"], "rules_version": rules, "related_run_ids": related_runs, "items": items,
            "at": _time(now).isoformat(), "scheduled_environment": "unverified"}


def assert_publication_allowed(repo, execution_context=None):
    """Gate both manual and scheduled writes; unknown schedule state fails closed."""
    git = PipelineGit(repo)
    if git.run("log", "--format=%H", "--diff-filter=MD", git.head(), "--", ROOT):
        _fail("unknown_schedule_state")
    listed = git.run("ls-tree", "-r", "--name-only", git.head(), "--", ROOT).decode().splitlines()
    if len(listed) > MAX_EVENTS:
        _fail("schedule_state_limit")
    ids = set()
    for path in listed:
        match = re.fullmatch(re.escape(ROOT) + r"([A-Za-z0-9][A-Za-z0-9_-]{0,63})/events/[0-9]{8}\.md", path)
        if not match:
            _fail("unknown_schedule_state")
        ids.add(match[1])
    states = {sid: _require(git, sid) for sid in ids}
    # Uncommitted/symlink schedule controls cannot silently disappear from a gate.
    root = git.repo / ROOT
    current = git.repo
    for part in ROOT.rstrip("/").split("/"):
        current = current / part
        if current.is_symlink():
            _fail("path_rejected")
    if root.exists():
        if not root.is_dir():
            _fail("unknown_schedule_state")
        actual = set()
        for count, path in enumerate(root.rglob("*")):
            if count > MAX_EVENTS:
                _fail("schedule_state_limit")
            if path.is_symlink():
                _fail("path_rejected")
            if path.is_file():
                actual.add(path.relative_to(git.repo).as_posix())
        if actual != set(listed):
            _fail("unknown_schedule_state")
    if any(state["paused"] for state in states.values()):
        _fail("publication_paused")
    for state in states.values():
        _attempts(state)
    if execution_context is None:
        for state in states.values():
            if state["config"]["remote_mode"] == "required":
                _remote_gate(git, state)
        return {"allowed": True}
    context = execution_context
    if not isinstance(context, dict) or set(context) != CONTEXT_FIELDS or any(not isinstance(value, str) for value in context.values()):
        _fail("invalid_execution_context")
    state = states.get(context["schedule_id"])
    if state is None:
        _fail("invalid_execution_context")
    reservations, results = _attempts(state)
    event = reservations.get(context["attempt_id"])
    if event is None or event["path"] != context["reservation_path"]:
        _fail("invalid_execution_context")
    data = event["data"]
    _, contents = _read(git, event["path"])
    if (_sha(contents) != context["reservation_sha256"] or data["tick_key"] != context["tick_key"] or data["run_id"] != context["run_id"]
            or context["attempt_id"] in results):
        _fail("invalid_execution_context")
    from .pipeline import execution_versions
    if data["rules_version"] != execution_versions(git)["rules_version"]:
        _fail("processing_context_changed")
    for checked in states.values():
        if checked["config"]["remote_mode"] == "required":
            _remote_gate(git, checked, context if context["schedule_id"] == checked["config"]["schedule_id"] else None)
    return {"allowed": True, "source_paths": data["source_paths"], "related_runs": data["related_runs"], "reserved_calls": data["reserved_calls"]}


def _remote_gate(git, local_state, context=None, *, allow_paused=False):
    """Fetch current control state before model calls and local formal commits.

    A later concurrent remote write is still rejected by the final non-force
    push. This check does not claim to atomically lock a separate Git server.
    """
    branch = git.text("symbolic-ref", "--quiet", "--short", "HEAD")
    git.run("fetch", "--no-tags", local_state["config"]["remote"], "refs/heads/" + branch)
    remote_head = git.text("rev-parse", "FETCH_HEAD")
    listed = git.run("ls-tree", "-r", "--name-only", remote_head, "--", ROOT).decode().splitlines()
    if len(listed) > MAX_EVENTS or git.run("log", "--format=%H", "--diff-filter=MD", remote_head, "--", ROOT):
        _fail("unknown_schedule_state")
    ids = set()
    for path in listed:
        match = re.fullmatch(re.escape(ROOT) + r"([A-Za-z0-9][A-Za-z0-9_-]{0,63})/events/[0-9]{8}\.md", path)
        if match is None:
            _fail("unknown_schedule_state")
        ids.add(match[1])
    states = {sid: _state(git, sid, remote_head) for sid in ids}
    for state in states.values():
        _attempts(state)
        if state["paused"] and not allow_paused:
            _fail("publication_paused")
    remote_state = states.get(local_state["config"]["schedule_id"])
    if remote_state is None or remote_state["config_digest"] != local_state["config_digest"]:
        _fail("remote_schedule_unconfirmed")
    try:
        git.run("merge-base", "--is-ancestor", remote_head, git.head())
    except CaptureError:
        _fail("remote_head_changed")
    if context is not None:
        _, data = _read(git, context["reservation_path"], remote_head)
        if _sha(data) != context["reservation_sha256"]:
            _fail("remote_schedule_unconfirmed")


def pause_schedule(repo, schedule_id, reason, expected_head=None, *, now=None, remote_confirm=None):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
        _fail("invalid_pause_reason")
    git = PipelineGit(repo)
    git.check(expected_head)
    state = _require(git, schedule_id)
    _event(git, state, "paused", {"reason": reason}, now, remote_confirm)
    return {"status": "paused", "head": git.head(), "external_trigger_status": "unverified"}


def resume_schedule(repo, schedule_id, validation_ref, expected_head=None, *, now=None, remote_confirm=None):
    if not isinstance(validation_ref, str) or not validation_ref.strip() or len(validation_ref) > 1024:
        _fail("schedule_validation_required")
    git = PipelineGit(repo)
    git.check(expected_head)
    state = _require(git, schedule_id)
    if state["config"].get("processing_engine") == PRODUCTION_ENGINE:
        from .schedule_production import assert_can_resume
        assert_can_resume(repo, state)
    reservations, results = _attempts(state)
    if any(attempt not in results or results[attempt]["data"]["status"] == "reconciliation_required" for attempt in reservations):
        _fail("schedule_reconciliation_required")
    _event(git, state, "resumed", {"validation_ref": validation_ref}, now, remote_confirm)
    return {"status": "resumed", "head": git.head(), "external_trigger_status": "unverified"}


def abandon_attempt(repo, schedule_id, attempt_id, reason, expected_head=None, *, validation_ref, remote_confirm=None, execution_stopped=False):
    """Explicitly close a stopped attempt; published work is reconciled instead.

    Unknown calls require an explicit stopped-execution statement, pause and an
    expired lease. This records the operator's statement; it cannot stop an
    external runner or cancel a request. Reservations are never refunded.
    """
    from .pipeline import _read_reviewed, publish, verify_publication
    if (type(execution_stopped) is not bool or not isinstance(reason, str) or not reason.strip() or len(reason) > 1024
            or not isinstance(validation_ref, str) or not validation_ref.strip() or len(validation_ref) > 1024):
        _fail("schedule_validation_required")
    git = PipelineGit(repo)
    for repair in range(2):
        with git.lock():
            git.recover()
            head = git.check(expected_head)
            state = _require(git, schedule_id)
            reservations, results = _attempts(state)
            event = reservations.get(attempt_id)
            if event is None:
                _fail("unknown_schedule_attempt")
            if state["config"]["remote_mode"] == "required":
                _remote_gate(git, state, allow_paused=True)
            if attempt_id in results:
                files = {item["path"]: git.read(head, item["path"]) for item in state["events"]}
                remote_status = _confirm(git, state["config"], head, files, remote_confirm)
                return {"status": results[attempt_id]["data"]["status"], "attempt_id": attempt_id,
                        "already_terminal": True, "head": git.head(), "remote_status": remote_status}
            data = event["data"]
            run_id = data["run_id"]
            manifest = "memory/runs/processes/" + run_id + "/manifest.md"
            current_manifest = git.run("ls-tree", "--name-only", head, "--", manifest)
            if not current_manifest and git.run("log", "--format=%H", head, "--", manifest):
                _fail("publication_reconciliation_required")
            recovered = _pipeline_recovery(repo, run_id)
            status = recovered["status"]
            if status in {"published", "already_processed"}:
                publication = recovered["publication"]
                if not publication["receipt_saved"]:
                    if repair:
                        _fail("publication_reconciliation_required")
                    repair_run = publication["run_id"]
                    # Release the shared lock before the existing receipt-only
                    # publish recovery acquires it; recheck all state afterward.
                    needs_receipt = True
                else:
                    _confirm_publication(git, state["config"], publication, state, remote_confirm)
                    needs_receipt = False
            else:
                needs_receipt = False
                if status == "passed":
                    _read_reviewed(git, run_id)  # Verify complete candidate/Review bindings.
                elif status in {"needs_revision", "needs_confirmation"}:
                    _, checked_review, _ = _read_reviewed(git, run_id, allow_nonpassed=True)
                    if checked_review["output"]["verdict"] != status:
                        _fail("schedule_reconciliation_required")
                elif status != "failed":
                    if not execution_stopped:
                        _fail("schedule_reconciliation_required")
                    if not state["paused"]:
                        _fail("schedule_pause_required")
                    if _time() < _time(data["lease_expires_at"]):
                        _fail("schedule_lease_active")
                    status = "abandoned_uncertain"
                if status != "abandoned_uncertain":
                    status = "abandoned"
            if not needs_receipt:
                outcome = {"attempt_id": attempt_id, "run_id": run_id, "status": status, "reserved_calls": 2,
                           "usage": None, "reason": reason, "validation_ref": validation_ref,
                           "operator_confirmed_stopped": True, "previous_status": recovered["status"]}
                if "budget_binding" in data:
                    outcome.update(budget_binding=data["budget_binding"], budget_started_calls=None)
                if status in {"published", "already_processed"}:
                    outcome["publication_run_id"] = publication["run_id"]
                if status == "abandoned_uncertain":
                    outcome["execution_stopped_statement"] = True
                    outcome["uncertainty"] = "model_call_outcome_unknown"
                path, contents, commit = _append_locked(git, schedule_id, "attempt_result", outcome, None, head)
                remote_status = _confirm(git, state["config"], commit, {path: contents}, remote_confirm)
                return {**outcome, "head": git.head(), "remote_status": remote_status, "scheduled_environment": "unverified"}
        publish(repo, repair_run, git.head())
        expected_head = git.head()
    _fail("publication_reconciliation_required")


def _trigger(value, config):
    if (not isinstance(value, dict) or set(value) != {"kind", "trigger_id", "scheduled_for"}
            or value["kind"] not in {"manual", "test", "scheduled"} or not isinstance(value["trigger_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value["trigger_id"])):
        _fail("invalid_schedule_trigger")
    scheduled = _time(value["scheduled_for"])
    if value["kind"] == "scheduled" and scheduled.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M:%S") != config["daily_time"] + ":00":
        _fail("invalid_schedule_trigger")
    key = (config["schedule_id"] + ":" + scheduled.isoformat()) if value["kind"] == "scheduled" else config["schedule_id"] + ":" + value["kind"] + ":" + value["trigger_id"]
    return {**value, "scheduled_for": scheduled.isoformat()}, key


def _pipeline_recovery(repo, run_id):
    """Read only this reserved run. Missing output never proves no call occurred."""
    from .pipeline import verify_publication
    git = PipelineGit(repo)
    prefix = "memory/runs/processes/" + run_id + "/"
    paths = set(git.run("ls-tree", "-r", "--name-only", git.head(), "--", prefix).decode().splitlines())
    if prefix + "reused.md" in paths:
        header, body = parse_markdown(git.read(git.head(), prefix + "reused.md"))
        reused = _json(body)
        if header.get("schema_version") != SCHEMA or header.get("kind") != "processing_reuse" or reused.get("run_id") != run_id or reused.get("attempted_calls") != 0:
            _fail("unknown_schedule_state")
        publication = verify_publication(repo, reused["original_run_id"], historical=True)
        return {"status": "already_processed", "publication": publication}
    if prefix + "manifest.md" in paths:
        return {"status": "published", "publication": verify_publication(repo, run_id)}
    if prefix + "review.md" in paths:
        header, body = parse_markdown(git.read(git.head(), prefix + "review.md"))
        if header.get("schema_version") not in {"0.2", "0.3", SCHEMA}:
            _fail("unknown_schedule_state")
        verdict = _json(body)["output"]["verdict"]
        return {"status": verdict}
    if prefix + "failure.md" in paths:
        header, body = parse_markdown(git.read(git.head(), prefix + "failure.md"))
        failure = _json(body)
        if (header.get("schema_version") not in {"0.2", "0.3", SCHEMA} or header.get("kind") != "failure"
                or failure.get("run_id") != run_id or failure.get("status") != "failed"
                or type(failure.get("attempted_calls")) is not int or failure["attempted_calls"] not in {0, 1, 2}):
            _fail("unknown_schedule_state")
        return {"status": "failed"}
    return {"status": "reconciliation_required"}


def _publication_bytes(git, publication, state):
    files = {path: git.read(git.head(), path) for path in publication.get("authority_files", publication["files"])}
    for field in ("manifest_path", "receipt_path"):
        files[publication[field]] = git.read(git.head(), publication[field])
    state = _require(git, state["config"]["schedule_id"])
    for event in state["events"]:
        files[event["path"]] = git.read(git.head(), event["path"])
    return files


def _confirm_publication(git, config, publication, state, callback):
    _confirm(git, config, git.head(), _publication_bytes(git, publication, state), callback)
    if config["remote_mode"] == "required":
        # The default adapter also validates related objects at their original
        # commits; legitimate current updates must not be compared to old bytes.
        remote_head = git.text("rev-parse", "FETCH_HEAD")
        for snapshot in publication.get("authority_snapshots", []):
            git.run("merge-base", "--is-ancestor", snapshot["commit_sha"], remote_head)
            actual = _sha(git.read(snapshot["commit_sha"], snapshot["path"]))
            if snapshot["sha256"] not in {actual, "sha256-" + actual}:
                _fail("remote_mismatch")


def run_daily(repo, schedule_id, trigger, expected_head=None, *, model_call=None, remote_confirm=None, clock=None,
              continuation=None):
    """Run a bounded tick. External runner provisioning is intentionally absent.

    A reservation consumes two call slots even if output or usage is unknown.
    Incomplete reservations are inspected, never automatically called again.
    """
    from .model_calls import call_model
    from .budget import BudgetBlocked, BudgetedModelCall
    from .pipeline import lookup_processed, process_sources, publish, verify_publication
    now = clock or (lambda: datetime.now(timezone.utc))
    git = PipelineGit(repo)
    git.check(expected_head)
    state = _require(git, schedule_id)
    if state["config"].get("processing_engine") == PRODUCTION_ENGINE:
        from .schedule_production import run_schedule
        return run_schedule(repo, schedule_id, trigger, model_call=model_call,
                            remote_confirm=remote_confirm, clock=clock, continuation=continuation)
    _budget_state(state, model_call)
    config = state["config"]
    trigger, tick_key = _trigger(trigger, config)
    continuous = config.get("execution_policy") == CONTINUOUS_POLICY
    root_tick_key = tick_key
    # A durable needs-confirmation Review with a failed registry CAS is known,
    # not an unknown model call. Repair it under the original reservation scope.
    reservations, results = _attempts(state)
    for attempt, event in reservations.items():
        previous = results.get(attempt, {}).get("data", {})
        if previous.get("status") != "reconciliation_required" or previous.get("reason") != "registration_required":
            continue
        from .pipeline import resume_pending_registration
        repaired = resume_pending_registration(repo, event["data"]["run_id"], allowed_source_paths=event["data"]["source_paths"])
        if repaired["status"] == "registration_required":
            return {"status": "registration_required", "next_action": "repair_registration", "tick_key": tick_key, "scheduled_environment": "unverified"}
        _event(git, state, "attempt_registration_repaired", {**previous, "status": "awaiting_confirmation", "recovered": True}, now(), remote_confirm)
        state = _require(git, schedule_id)
    queue = None
    if continuation is not None:
        if not continuous:
            _fail("invalid_schedule_continuation")
        queue = _continuation_queue(state, root_tick_key, continuation)
        tick_key = _queue_key(queue)
    if trigger["kind"] == "scheduled" and _time(now()) < _time(trigger["scheduled_for"]):
        return {"status": "not_due", "tick_key": tick_key, "scheduled_environment": "unverified"}
    if state["paused"]:
        return {"status": "skipped_paused", "tick_key": tick_key, "scheduled_environment": "unverified"}
    finished = [event for event in state["events"] if event["kind"] == "tick_finished" and event["data"].get("tick_key") == tick_key]
    if finished:
        files = {event["path"]: git.read(git.head(), event["path"]) for event in state["events"]}
        _confirm(git, config, git.head(), files, remote_confirm)
        return {"status": "skipped_duplicate", "tick_key": tick_key, "scheduled_environment": "unverified"}
    started = [event for event in state["events"] if event["kind"] == "tick_started" and event["data"].get("tick_key") == tick_key]
    if continuous:
        if started:
            saved_queue = _queue(started[0]["data"].get("queue"))
            if queue is not None and queue != saved_queue:
                _fail("invalid_schedule_continuation")
            queue = saved_queue
        elif queue is None:
            queue = {"root_tick_key": root_tick_key, "parent_tick_key": None,
                     "slice_index": 0, "source_paths": _scope_paths(git, config)}
    reservations, results = _attempts(state)
    for attempt, event in reservations.items():
        binding = event["data"].get("budget_binding")
        if binding is not None:
            operation = _budget_state(state, model_call, binding)
            recorded = results.get(attempt, {}).get("data", {}).get("budget_started_calls")
            if recorded is not None and recorded != (operation["started_calls"] if operation else 0):
                raise BudgetBlocked("budget_operation_conflict", operation_id=binding["operation_id"])
    if started or any(attempt not in results for attempt in reservations):
        # Reconciliation may need the unchanged Review parent: do not append a
        # new tick event before recovering a previous tick's publication.
        files = {event["path"]: git.read(git.head(), event["path"]) for event in state["events"]}
        _confirm(git, config, git.head(), files, remote_confirm)
    consumed = sum(event["data"]["reserved_calls"] for event in reservations.values() if event["data"]["tick_key"] == tick_key)
    daily_consumed = sum(event["data"]["reserved_calls"] for event in reservations.values() if _day(event["at"]) == _day(now()))
    count = sum(_day(event["at"]) == _day(now()) for event in reservations.values())
    outcomes = []
    # Reconcile old attempts before choosing any new work. No model invocation.
    for attempt_id, event in reservations.items():
        if attempt_id in results:
            continue
        data = event["data"]
        binding = data.get("budget_binding")
        operation = _budget_state(state, model_call, binding) if binding is not None else None
        recovered = _pipeline_recovery(repo, data["run_id"])
        registration_path = f"memory/runs/processes/{data['run_id']}/pending-registration.md"
        if recovered["status"] == "needs_confirmation" and git.path(registration_path).is_file():
            from .pipeline import resume_pending_registration
            registration = resume_pending_registration(repo, data["run_id"], allowed_source_paths=data["source_paths"])
            if registration["status"] == "registration_required":
                return {"status": "registration_required", "next_action": "repair_registration", "tick_key": tick_key, "scheduled_environment": "unverified"}
        if continuous and _uncertain_operation(operation) and recovered["status"] not in {"published", "already_processed"}:
            return {"status": "reconciliation_required", "attempt_id": attempt_id,
                    "tick_key": tick_key, "lease_expired": _time(now()) >= _time(data["lease_expires_at"]),
                    "reason": "model_call_outcome_unknown", "scheduled_environment": "unverified"}
        if binding is not None and binding["reserved_calls"] == 0 and recovered["status"] == "reconciliation_required":
            existing = lookup_processed(repo, data["source_paths"], git.head(), related_runs=data["related_runs"])
            if existing is None or existing["run_id"] != binding["reuse_run_id"]:
                raise BudgetBlocked("budget_processing_mismatch", operation_id=binding["operation_id"])
            _, contents = _read(git, event["path"])
            context = {"schedule_id": schedule_id, "tick_key": data["tick_key"], "attempt_id": attempt_id,
                       "run_id": data["run_id"], "reservation_path": event["path"], "reservation_sha256": _sha(contents)}
            def reuse_only(role, payload, timeout=120):
                raise BudgetBlocked("budget_processing_mismatch", operation_id=binding["operation_id"])
            reuse_only._budget_adapter = model_call.for_operation(binding["operation_id"])
            reuse_only._budget_reserved_calls = 0
            process_sources(repo, data["source_paths"], git.head(), related_runs=data["related_runs"],
                            run_id=data["run_id"], execution_context=context,
                            model_call=reuse_only)
            recovered = _pipeline_recovery(repo, data["run_id"])
        if (binding is not None and binding["reserved_calls"] == 2
                and recovered["status"] in {"passed", "published", "needs_revision", "needs_confirmation"}
                and operation["started_calls"] != 2):
            raise BudgetBlocked("budget_operation_conflict", operation_id=binding["operation_id"])
        if recovered["status"] == "passed":
            recovered = {"status": "published", "publication": publish(repo, data["run_id"], git.head())}
        if recovered["status"] == "published":
            publication = verify_publication(repo, data["run_id"])
            if not publication["receipt_saved"]:
                publication = publish(repo, data["run_id"], git.head())
            _confirm_publication(git, config, publication, state, remote_confirm)
        elif recovered["status"] == "already_processed":
            _confirm_publication(git, config, recovered["publication"], state, remote_confirm)
        if recovered["status"] in {"published", "already_processed", "failed", "needs_revision", "needs_confirmation"}:
            status = "awaiting_confirmation" if recovered["status"] == "needs_confirmation" else recovered["status"]
            outcome = {"attempt_id": attempt_id, "run_id": data["run_id"], "status": status,
                       "reserved_calls": 2, "usage": None, "recovered": True}
            if binding is not None:
                outcome.update(budget_binding=binding, budget_started_calls=operation["started_calls"] if operation else 0)
            if status in {"published", "already_processed"}:
                outcome["publication_run_id"] = recovered["publication"]["run_id"]
            _event(git, state, "attempt_result", outcome, now(), remote_confirm)
            outcomes.append(outcome)
        else:
            # Lease expiry is diagnostic, never permission to repeat an unknown call.
            return {"status": "reconciliation_required", "attempt_id": attempt_id,
                    "lease_expired": _time(now()) >= _time(data["lease_expires_at"]),
                    "reserved_calls": consumed, "inflight_reserved_calls": data["reserved_calls"],
                    "daily_reserved_calls": daily_consumed, "usage": None, "scheduled_environment": "unverified"}
    if not started:
        start_data = {"tick_key": tick_key, "trigger": trigger}
        if isinstance(model_call, BudgetedModelCall):
            snapshot = model_call.ledger.snapshot()
            start_data["budget_pool"] = _budget_grant(snapshot)
        if continuous:
            start_data["queue"] = queue
        _event(git, state, "tick_started", start_data, now(), remote_confirm)
    attempted_this_tick = {event["data"]["item_key"] for event in reservations.values() if event["data"]["tick_key"] == tick_key}
    budget_stop = None
    while True:
        plan = plan_daily(repo, schedule_id, now())
        item = next((item for item in plan["items"] if item["status"] == "eligible" and item["item_key"] not in attempted_this_tick
                     and (not continuous or item["input_path"] in queue["source_paths"])), None)
        if item is None:
            break
        tick_count = sum(event["data"]["tick_key"] == tick_key for event in reservations.values()) + len([
            item for item in outcomes if item.get("recovered") is False])
        if ((continuous and tick_count >= config["max_items"])
                or (not continuous and (count >= config["max_items"] or daily_consumed + 2 > config["max_calls"]))):
            break
        state = _require(git, schedule_id)
        assert_publication_allowed(repo)
        attempt_id, run_id = "attempt-" + uuid.uuid4().hex, "run-" + uuid.uuid4().hex
        delegate = model_call or call_model
        binding = None
        try:
            if delegate is call_model:
                raise BudgetBlocked("budget_binding_required")
            if isinstance(delegate, BudgetedModelCall):
                operation_id = "schedule-" + _sha(canonical({"operation_id": delegate.operation_id, "run_id": run_id}))
                delegate = delegate.for_operation(operation_id)
                existing = lookup_processed(repo, item["source_paths"], git.head(), related_runs=plan["related_run_ids"])
                if existing is None:
                    delegate.reserve_calls(2)
                snapshot = delegate.ledger.snapshot()
                binding = {**_budget_grant(snapshot), "operation_id": operation_id, "reserved_calls": 0 if existing is not None else 2,
                           "reuse_run_id": existing["run_id"] if existing is not None else None}
        except BudgetBlocked as error:
            budget_stop = error.code
            break
        reservation = {"attempt_id": attempt_id, "run_id": run_id, "tick_key": tick_key,
                       "config_digest": state["config_digest"], "item_key": item["item_key"],
                       "input_path": item["input_path"], "rules_version": plan["rules_version"],
                       "source_paths": item["source_paths"], "related_runs": plan["related_run_ids"],
                       "reserved_calls": 2, "lease_expires_at": (_time(now()) + timedelta(seconds=config["lease_seconds"])).isoformat()}
        if binding is not None:
            reservation["budget_binding"] = binding
        path, contents, _ = _event(git, state, "attempt_reserved", reservation, now(), remote_confirm)
        context = {"schedule_id": schedule_id, "tick_key": tick_key, "attempt_id": attempt_id,
                   "run_id": run_id, "reservation_path": path, "reservation_sha256": _sha(contents)}
        consumed += 2
        daily_consumed += 2
        count += 1
        attempted_this_tick.add(item["item_key"])
        calls = []
        def bounded_call(role, payload, timeout=120):
            if binding is not None and binding["reserved_calls"] == 0:
                raise BudgetBlocked("budget_processing_mismatch", operation_id=binding["operation_id"])
            if len(calls) >= 2:
                _fail("daily_call_budget_exhausted")
            before = delegate.observed_started_count if isinstance(delegate, BudgetedModelCall) else None
            blocked = False
            try:
                return delegate(role, payload, timeout=min(timeout, config["timeout_seconds"]))
            except BudgetBlocked:
                blocked = True
                raise
            finally:
                if before is not None:
                    if delegate.observed_started_count > before:
                        calls.append(role)
                elif not blocked:
                    calls.append(role)
        if isinstance(delegate, BudgetedModelCall):
            bounded_call._budget_adapter = delegate
            bounded_call._budget_reserved_calls = binding["reserved_calls"]
        try:
            result = process_sources(repo, item["source_paths"], git.head(), related_runs=plan["related_run_ids"],
                                     run_id=run_id, execution_context=context, model_call=bounded_call)
        except CaptureError as error:
            if calls:
                raise
            # The live wrapper observed no model start. This is narrower than
            # inferring zero consumption after a vanished or unknown process.
            result = {"status": "failed", "error": error.code}
        status = result["status"]
        registration_required = status == "registration_required"
        if status == "passed":
            publication = publish(repo, run_id, git.head())
            _confirm_publication(git, config, publication, state, remote_confirm)
            status = "published"
        elif status == "already_processed":
            _confirm_publication(git, config, result["publication"], state, remote_confirm)
        elif status == "needs_confirmation":
            status = "awaiting_confirmation"
        elif registration_required:
            status = "reconciliation_required"
        outcome = {"attempt_id": attempt_id, "run_id": run_id, "status": status,
                   "reserved_calls": 2, "observed_calls": len(calls), "usage": None, "recovered": False}
        if registration_required:
            outcome["reason"] = "registration_required"
        if binding is not None:
            operation = _budget_state(state, delegate, binding)
            if continuous and _uncertain_operation(operation) and status not in {"published", "already_processed"}:
                return {"status": "reconciliation_required", "attempt_id": attempt_id, "tick_key": tick_key,
                        "reason": "model_call_outcome_unknown", "scheduled_environment": "unverified"}
            outcome.update(budget_binding=binding, budget_started_calls=operation["started_calls"] if operation else 0)
        if status == "published":
            outcome["publication_run_id"] = run_id
        elif status == "already_processed":
            outcome["publication_run_id"] = result["run_id"]
        _event(git, state, "attempt_result", outcome, now(), remote_confirm)
        outcomes.append(outcome)
    remaining = plan_daily(repo, schedule_id, now())["items"]
    if continuous:
        remaining = [item for item in remaining if item["input_path"] in queue["source_paths"]]
    incomplete = [item for item in remaining if item["status"] not in {"completed", "resolved", "excluded"}]
    summary = "completed_empty" if not outcomes and not incomplete else ("completed" if not incomplete else "partial")
    data = {"tick_key": tick_key, "status": summary, "reserved_calls": consumed, "daily_reserved_calls": daily_consumed,
            "outcomes": outcomes, "remaining": incomplete, "remote_status": "confirmed" if config["remote_mode"] == "required" else "not_requested"}
    if budget_stop is not None:
        data["budget_error"] = budget_stop
    _event(git, state, "tick_finished", data, now(), remote_confirm)
    return {**data, **({"status": "budget_exhausted"} if budget_stop is not None else {}),
            "head": git.head(), "scheduled_environment": "unverified", "real_daily_verified": False}


def _continuation_queue(state, root_tick_key, token):
    required = {"schedule_id", "root_tick_key", "parent_tick_key", "slice_index"}
    if (not isinstance(token, dict) or set(token) != required
            or token["schedule_id"] != state["config"]["schedule_id"] or token["root_tick_key"] != root_tick_key):
        _fail("invalid_schedule_continuation")
    roots = [event for event in state["events"] if event["kind"] == "tick_started" and event["data"].get("tick_key") == root_tick_key]
    if not roots:
        _fail("invalid_schedule_continuation")
    root = _queue(roots[0]["data"].get("queue"))
    queue = _queue({key: token[key] for key in ("root_tick_key", "parent_tick_key", "slice_index")} | {
        "source_paths": root["source_paths"]})
    if queue["parent_tick_key"] is not None and not any(event["kind"] == "tick_finished"
            and event["data"].get("tick_key") == queue["parent_tick_key"] for event in state["events"]):
        _fail("invalid_schedule_continuation")
    return queue


def _continuation_token(schedule_id, queue, *, next_slice=False):
    index = queue["slice_index"] + int(next_slice)
    root = queue["root_tick_key"]
    return {"schedule_id": schedule_id, "root_tick_key": root, "slice_index": index,
            "parent_tick_key": None if index == 0 else (root if index == 1 else root + ":continuation:" + str(index - 1))}


def drain_schedule(repo, schedule_id, trigger, expected_head=None, *, model_call=None, remote_confirm=None,
                   clock=None, max_slices=8, window_seconds=300, continuation=None):
    """Advance finite slices of a durable queue; return an explicit continuation.

    Daily discovery snapshots authorized source paths once. Continuations retain
    that snapshot and committed lineage; this function never provisions a wakeup
    or counts a caller-supplied trigger as proof of a cloud platform execution.
    """
    if (type(max_slices) is not int or not 1 <= max_slices <= 256
            or type(window_seconds) is not int or not 1 <= window_seconds <= 86400):
        _fail("invalid_schedule_window")
    now = clock or (lambda: datetime.now(timezone.utc))
    begin = _time(now())
    git = PipelineGit(repo)
    git.check(expected_head)
    state = _require(git, schedule_id)
    if state["config"].get("execution_policy") != CONTINUOUS_POLICY:
        _fail("continuous_schedule_required")
    if state["config"].get("processing_engine") == PRODUCTION_ENGINE:
        from .schedule_production import drain_schedule_production
        return drain_schedule_production(repo, schedule_id, trigger, model_call=model_call,
                                         remote_confirm=remote_confirm, clock=clock, max_slices=max_slices,
                                         window_seconds=window_seconds, continuation=continuation)
    _budget_state(state, model_call)
    _, root = _trigger(trigger, state["config"])
    if continuation is not None:
        _continuation_queue(state, root, continuation)
    slices, current, last = [], continuation, None
    while len(slices) < max_slices and _time(now()) - begin < timedelta(seconds=window_seconds):
        state = _require(git, schedule_id)
        related = [event for event in state["events"] if event["kind"] == "tick_started"
                   and event["data"].get("queue", {}).get("root_tick_key") == root]
        if current is None and related:
            latest = max(related, key=lambda event: event["data"]["queue"]["slice_index"])
            queue = _queue(latest["data"]["queue"])
            finished = any(event["kind"] == "tick_finished" and event["data"].get("tick_key") == _queue_key(queue)
                           for event in state["events"])
            current = _continuation_token(schedule_id, queue, next_slice=finished)
            existing_remaining = [item for item in plan_daily(repo, schedule_id, now())["items"]
                                  if item["input_path"] in queue["source_paths"]
                                  and item["status"] not in {"completed", "resolved", "excluded"}]
            if finished and not any(item["status"] == "eligible" for item in existing_remaining):
                last = {"status": "skipped_duplicate", "tick_key": _queue_key(queue)}
                break
        last = run_daily(repo, schedule_id, trigger, git.head(), model_call=model_call,
                         remote_confirm=remote_confirm, clock=now, continuation=current)
        slices.append(last)
        state = _require(git, schedule_id)
        related = [event for event in state["events"] if event["kind"] == "tick_started"
                   and event["data"].get("queue", {}).get("root_tick_key") == root]
        if not related:
            break
        latest = max(related, key=lambda event: event["data"]["queue"]["slice_index"])
        queue = _queue(latest["data"]["queue"])
        finished = any(event["kind"] == "tick_finished" and event["data"].get("tick_key") == _queue_key(queue)
                       for event in state["events"])
        current = _continuation_token(schedule_id, queue, next_slice=finished)
        remaining = [item for item in plan_daily(repo, schedule_id, now())["items"]
                     if item["input_path"] in queue["source_paths"] and item["status"] not in {"completed", "resolved", "excluded"}]
        if (last["status"] in {"reconciliation_required", "not_due", "skipped_paused", "budget_exhausted"}
                or not any(item["status"] == "eligible" for item in remaining)):
            break
    state = _require(git, schedule_id)
    related = [event for event in state["events"] if event["kind"] == "tick_started"
               and event["data"].get("queue", {}).get("root_tick_key") == root]
    discovered = related[0]["data"]["queue"]["source_paths"] if related else _scope_paths(git, state["config"])
    remaining = [item for item in plan_daily(repo, schedule_id, now())["items"]
                 if item["input_path"] in discovered and item["status"] not in {"completed", "resolved", "excluded"}]
    status = last["status"] if last else "window_ended"
    if state["paused"]:
        action = "paused"
    elif status == "reconciliation_required" or any(item["status"] == "reconciliation_required" for item in remaining):
        action = "reconcile"
    elif status == "not_due":
        action = "wait_until_due"
    elif status == "budget_exhausted":
        action = "repair_accounting"
    elif any(item["status"] == "eligible" for item in remaining):
        action = "continue"
    elif any(item["status"] == "awaiting_confirmation" for item in remaining):
        action = "await_confirmation"
    elif remaining:
        action = "attention_required"
    else:
        action = "idle"
    return {"schedule_id": schedule_id, "status": status, "slices": slices, "remaining": remaining,
            "next_action": action, "continuation": current if action in {"continue", "reconcile"} else None,
            "head": git.head(), "scheduled_environment": "unverified", "real_daily_verified": False,
            "external_wakeup": "not_provisioned"}


def schedule_status(repo, schedule_id, now=None, platform_observation=None):
    """Separate absent platform evidence from confirmed absence of a trigger."""
    git = PipelineGit(repo)
    state = _require(git, schedule_id)
    if state["config"].get("processing_engine") == PRODUCTION_ENGINE:
        from .schedule_production import production_schedule_status
        return production_schedule_status(repo, schedule_id, now=now, platform_observation=platform_observation)
    config = state["config"]
    current = _time(now)
    local = current.astimezone(ZoneInfo("Asia/Shanghai"))
    hour, minute = map(int, config["daily_time"].split(":"))
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due > local:
        due -= timedelta(days=1)
    expected = due.astimezone(timezone.utc)
    key = schedule_id + ":" + expected.isoformat()
    started = [event for event in state["events"] if event["kind"] == "tick_started" and event["data"].get("tick_key") == key]
    finished = [event for event in state["events"] if event["kind"] == "tick_finished" and event["data"].get("tick_key") == key]
    def family_receipt(root_key):
        family = [event for event in state["events"] if event["kind"] == "tick_started"
                  and event["data"].get("queue", {}).get("root_tick_key") == root_key]
        if not family:
            return None
        latest = max(family, key=lambda event: event["data"]["queue"]["slice_index"])
        terminal = [event for event in state["events"] if event["kind"] == "tick_finished"
                    and event["data"].get("tick_key") == latest["data"]["tick_key"]]
        return terminal[-1]["data"]["status"] if terminal else "incomplete"
    continuous_receipt = family_receipt(key) if config.get("execution_policy") == CONTINUOUS_POLICY else None
    if state["paused"]:
        status = "paused"
    elif expected < _time(state["events"][0]["at"]):
        status = "not_due"
    elif continuous_receipt is not None:
        status = continuous_receipt
    elif finished:
        status = finished[-1]["data"]["status"]
    elif started:
        status = "incomplete"
    elif current <= expected + timedelta(seconds=config["grace_seconds"]):
        status = "within_grace"
    elif platform_observation is None or (isinstance(platform_observation, dict) and platform_observation.get("status") == "unavailable"):
        status = "trigger_unknown"
    else:
        if not isinstance(platform_observation, dict):
            _fail("invalid_platform_observation")
        if (set(platform_observation) != {"status", "window_start", "window_end", "trigger_ids"}
                or platform_observation["status"] != "available"
                or not isinstance(platform_observation["trigger_ids"], list)
                or _time(platform_observation["window_start"]) > expected
                or _time(platform_observation["window_end"]) < expected + timedelta(seconds=config["grace_seconds"])):
            _fail("invalid_platform_observation")
        status = "incomplete" if platform_observation["trigger_ids"] else "no_trigger"
    successes = [event for event in state["events"] if event["kind"] == "tick_finished" and event["data"].get("status") in {"completed", "completed_empty"}]
    windows = []
    for offset in range(32):
        slot = expected - timedelta(days=offset)
        if slot < _time(state["events"][0]["at"]):
            break
        slot_key = schedule_id + ":" + slot.isoformat()
        terminal = [event for event in state["events"] if event["kind"] == "tick_finished" and event["data"].get("tick_key") == slot_key]
        begins = [event for event in state["events"] if event["kind"] == "tick_started" and event["data"].get("tick_key") == slot_key]
        paused_at_slot = False
        for event in state["events"]:
            if _time(event["at"]) <= slot and event["kind"] in {"paused", "resumed"}:
                paused_at_slot = event["kind"] == "paused"
        receipt = terminal[-1]["data"]["status"] if terminal else ("incomplete" if begins else ("paused" if paused_at_slot else "missing_receipt"))
        if config.get("execution_policy") == CONTINUOUS_POLICY:
            receipt = family_receipt(slot_key) or receipt
        windows.append({"scheduled_for": slot.isoformat(), "receipt_status": receipt})
    return {"schedule_id": schedule_id, "status": status, "expected_for": expected.isoformat(),
            "latest_success_at": successes[-1]["at"] if successes else None,
            "expected_windows": list(reversed(windows)),
            "history_truncated": expected - _time(state["events"][0]["at"]) >= timedelta(days=32),
            "remaining": [item for item in plan_daily(repo, schedule_id, current)["items"] if item["status"] not in {"completed", "resolved", "excluded"}], "scheduled_environment": "unverified",
            "external_trigger_status": "unverified"}
