"""Private, local shared-filesystem call accounting; no evidence or credentials.

Reservations are conservative and never automatically refunded.  File locks
coordinate trusted processes of the same uid, not remote hosts or hostile users.
Schema 1 preserves finite grants; schema 2 audits separately authorized ongoing
work without a cumulative call ceiling. Neither changes memory document schemas.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


BUDGET_SCHEMA = "1"
AUDIT_SCHEMA = "2"
MAX_LEDGER_BYTES = 4_000_000
ROLES = ("extract", "review", "answer")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{64}")
_TIMEOUT_UNSET = object()


class BudgetBlocked(RuntimeError):
    """Safe gate/storage error; attempted_calls distinguishes post-start errors."""
    def __init__(self, code, *, operation_id=None, started_count=0, attempted_calls=0):
        self.code = code
        self.operation_id = operation_id
        self.started_count = started_count
        self.attempted_calls = attempted_calls
        super().__init__("model budget: " + code)


def _blocked(code):
    raise BudgetBlocked(code)


def _label(value):
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        _blocked("invalid_budget_operation")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("constant")))


def _private(info, mode):
    return (stat.S_IMODE(info.st_mode) == mode and info.st_uid == os.getuid()
            and (mode == 0o700 or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)))


def _time_valid(value):
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _pointer(path):
    """Bounded nonblocking read of a regular, non-symlink Git metadata pointer."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            _blocked("unsafe_budget_path")
        raw = stream.read(4097)
    if len(raw) > 4096:
        _blocked("unsafe_budget_path")
    return raw


def _validate(state):
    required = {"schema_version", "pool_id", "limit", "initial_used", "authorization_sha256",
                "revision", "operations"}
    if not isinstance(state, dict):
        _blocked("invalid_budget_state")
    audit = state.get("schema_version") == AUDIT_SCHEMA
    if audit:
        required.add("mode")
    if (set(state) != required or state["schema_version"] not in {BUDGET_SCHEMA, AUDIT_SCHEMA}
            or (audit and (state["mode"] != "audit" or state["limit"] is not None))):
        _blocked("invalid_budget_state")
    if not isinstance(state["pool_id"], str) or not re.fullmatch(r"budget-[0-9a-f]{32}", state["pool_id"]):
        _blocked("invalid_budget_state")
    counters = ("initial_used", "revision") if audit else ("limit", "initial_used", "revision")
    if any(type(state[key]) is not int or state[key] < 0 for key in counters):
        _blocked("invalid_budget_state")
    if not audit and (state["limit"] > 1_000_000 or state["initial_used"] > state["limit"]):
        _blocked("invalid_budget_state")
    if not isinstance(state["authorization_sha256"], str) or not _SHA.fullmatch(state["authorization_sha256"]):
        _blocked("invalid_budget_state")
    if not isinstance(state["operations"], dict):
        _blocked("invalid_budget_state")
    charged = state["initial_used"]
    for operation_id, operation in state["operations"].items():
        if not isinstance(operation_id, str) or not _LABEL.fullmatch(operation_id):
            _blocked("invalid_budget_state")
        if not isinstance(operation, dict) or set(operation) != {"reserved_roles", "calls"}:
            _blocked("invalid_budget_state")
        roles, calls = operation["reserved_roles"], operation["calls"]
        if (not isinstance(roles, list) or not roles or any(role not in ROLES for role in roles)
                or len(set(roles)) != len(roles) or not isinstance(calls, dict) or not set(calls) <= set(roles)):
            _blocked("invalid_budget_state")
        charged += len(roles)
        for role, call in calls.items():
            if not isinstance(call, dict) or set(call) != {"payload_sha256", "status", "started_at", "finished_at", "invocation_id", "error_code"}:
                _blocked("invalid_budget_state")
            if not isinstance(call["payload_sha256"], str) or not _SHA.fullmatch(call["payload_sha256"]):
                _blocked("invalid_budget_state")
            if call["status"] not in {"started", "completed", "error", "unknown"}:
                _blocked("invalid_budget_state")
            if not _time_valid(call["started_at"]):
                _blocked("invalid_budget_state")
            if call["finished_at"] is not None and not _time_valid(call["finished_at"]):
                _blocked("invalid_budget_state")
            if ((call["status"] == "started") != (call["finished_at"] is None)
                    or (call["status"] in {"started", "completed"} and call["error_code"] is not None)):
                _blocked("invalid_budget_state")
            if call["invocation_id"] is not None and (not isinstance(call["invocation_id"], str) or not re.fullmatch(r"model-[0-9a-f]{32}", call["invocation_id"])):
                _blocked("invalid_budget_state")
            if call["error_code"] is not None and (not isinstance(call["error_code"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", call["error_code"])):
                _blocked("invalid_budget_state")
    if not audit and charged > state["limit"]:
        _blocked("invalid_budget_state")
    return charged


def _identity(state):
    """Bind both authorization and policy; never switch an open ledger's mode."""
    return (state["schema_version"], state.get("mode", "budget"),
            *(state[key] for key in ("pool_id", "authorization_sha256", "limit", "initial_used")))


class BudgetLedger:
    """Open an existing private ledger; opening never creates or repairs it."""
    def __init__(self, path, *, repo=None):
        if ".." in Path(path).parts:
            _blocked("unsafe_budget_path")
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.repo = Path(repo).resolve() if repo is not None else None
        self._check_path()
        initial = self.snapshot()
        self._identity = _identity(initial)

    @classmethod
    def create(cls, path, limit, initial_used, authorization_ref, *, repo=None, mode="budget"):
        if ".." in Path(path).parts:
            _blocked("unsafe_budget_path")
        if mode not in ("budget", "audit"):
            _blocked("invalid_budget_mode")
        if (type(initial_used) is not int or initial_used < 0
                or (mode == "audit" and limit is not None)
                or (mode == "budget" and (type(limit) is not int
                                           or not initial_used <= limit <= 1_000_000))):
            _blocked("invalid_budget_limit")
        if (not isinstance(authorization_ref, str) or not authorization_ref.strip()
                or len(authorization_ref) > 512 or any(ord(char) < 32 for char in authorization_ref)):
            _blocked("invalid_budget_authorization")
        try:
            authorization_digest = hashlib.sha256(authorization_ref.encode("utf-8")).hexdigest()
        except UnicodeError:
            _blocked("invalid_budget_authorization")
        ledger = object.__new__(cls)
        ledger.path = Path(os.path.abspath(os.fspath(path)))
        ledger.repo = Path(repo).resolve() if repo is not None else None
        ledger._check_path()
        state = {"schema_version": AUDIT_SCHEMA if mode == "audit" else BUDGET_SCHEMA,
                 "pool_id": "budget-" + uuid.uuid4().hex,
                 "limit": limit, "initial_used": initial_used,
                 "authorization_sha256": authorization_digest,
                 "revision": 0, "operations": {}}
        if mode == "audit":
            state["mode"] = "audit"
        with ledger._locked(create=True) as directory:
            try:
                fd = os.open(ledger.path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(_json(state))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(directory)
            except FileExistsError:
                _blocked("budget_already_exists")
            except OSError:
                _blocked("budget_write_failed")
        return cls(path, repo=repo)

    def _check_path(self):
        try:
            if "\0" in str(self.path):
                _blocked("unsafe_budget_path")
            if self.repo is not None and self.path.is_relative_to(self.repo):
                _blocked("budget_inside_repository")
            if self.repo is not None:
                marker = self.repo / ".git"
                if marker.is_file():
                    raw = _pointer(marker)
                    if not raw.startswith(b"gitdir: "):
                        _blocked("unsafe_budget_path")
                    gitdir = (self.repo / raw[8:].decode("utf-8").strip()).resolve()
                else:
                    gitdir = marker.resolve()
                roots = [gitdir]
                common = gitdir / "commondir"
                if common.is_file():
                    raw = _pointer(common)
                    roots.append((gitdir / raw.decode("utf-8").strip()).resolve())
                if any(self.path.is_relative_to(root) for root in roots):
                    _blocked("budget_inside_repository")
            for ancestor in (self.path, *self.path.parents):
                if ancestor.is_symlink():
                    _blocked("unsafe_budget_path")
            # A workspace mount may contain an empty protected .git marker;
            # only actual worktrees are repository boundaries.
            for ancestor in self.path.parents:
                marker = ancestor / ".git"
                if (marker.is_file() or (marker.is_dir() and (marker / "HEAD").is_file())
                        or ((ancestor / "HEAD").is_file() and (ancestor / "objects").is_dir())):
                    _blocked("budget_inside_repository")
            info = self.path.parent.stat()
            if not stat.S_ISDIR(info.st_mode) or not _private(info, 0o700):
                _blocked("unsafe_budget_permissions")
            if not self.path.name or self.path.name.endswith(".lock"):
                _blocked("unsafe_budget_path")
        except (OSError, ValueError, UnicodeError):
            _blocked("unsafe_budget_path")

    @contextmanager
    def _locked(self, *, create=False):
        self._check_path()
        directory = lock = None
        try:
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            if not _private(os.fstat(directory), 0o700):
                _blocked("unsafe_budget_permissions")
            flags = os.O_RDWR | os.O_NOFOLLOW
            if create:
                flags |= os.O_CREAT | os.O_EXCL
            lock = os.open(self.path.name + ".lock", flags, 0o600, dir_fd=directory)
            if not _private(os.fstat(lock), 0o600):
                _blocked("unsafe_budget_permissions")
            fcntl.flock(lock, fcntl.LOCK_EX)
            if create:
                os.fsync(lock)
                os.fsync(directory)
            yield directory
        except FileExistsError:
            _blocked("budget_already_exists")
        except FileNotFoundError:
            _blocked("budget_missing")
        except OSError:
            _blocked("budget_io_failed")
        finally:
            if lock is not None:
                os.close(lock)
            if directory is not None:
                os.close(directory)

    def _read(self, directory):
        fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not _private(info, 0o600):
                _blocked("unsafe_budget_permissions")
            raw = stream.read(MAX_LEDGER_BYTES + 1)
        if len(raw) > MAX_LEDGER_BYTES:
            _blocked("invalid_budget_state")
        try:
            state = _decode(raw)
            _validate(state)
            identity = _identity(state)
            if getattr(self, "_identity", identity) != identity:
                _blocked("budget_identity_changed")
            return state
        except (ValueError, TypeError, RecursionError, UnicodeError):
            _blocked("invalid_budget_state")

    def _write(self, directory, state):
        _validate(state)
        raw = _json(state)
        if len(raw) > MAX_LEDGER_BYTES:
            _blocked("budget_state_limit")
        temporary = "." + self.path.name + "." + uuid.uuid4().hex
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        except OSError:
            _blocked("budget_write_failed")
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass

    def snapshot(self):
        with self._locked() as directory:
            state = self._read(directory)
        charged = _validate(state)
        return {**deepcopy(state), "reserved_calls": charged - state["initial_used"],
                "started_calls": sum(len(op["calls"]) for op in state["operations"].values()),
                "used": charged, "remaining": (None if state["limit"] is None else state["limit"] - charged)}

    def operation(self, operation_id):
        _label(operation_id)
        state = self.snapshot()
        operation = state["operations"].get(operation_id)
        return (None if operation is None else {**operation, "reserved_calls": len(operation["reserved_roles"]),
                                               "started_calls": len(operation["calls"])})

    def reserve(self, operation_id, roles):
        _label(operation_id)
        if not isinstance(roles, (list, tuple)):
            _blocked("invalid_budget_reservation")
        roles = list(roles)
        if not roles or any(role not in ROLES for role in roles) or len(set(roles)) != len(roles):
            _blocked("invalid_budget_reservation")
        with self._locked() as directory:
            state = self._read(directory)
            existing = state["operations"].get(operation_id)
            if existing is not None:
                if existing["calls"]:
                    raise BudgetBlocked("budget_call_already_started", operation_id=operation_id,
                                        started_count=len(existing["calls"]))
                if existing["reserved_roles"] != roles:
                    raise BudgetBlocked("budget_operation_conflict", operation_id=operation_id,
                                        started_count=len(existing["calls"]))
                return deepcopy(existing)
            if state["limit"] is not None and _validate(state) + len(roles) > state["limit"]:
                raise BudgetBlocked("budget_exhausted", operation_id=operation_id)
            state["operations"][operation_id] = {"reserved_roles": roles, "calls": {}}
            state["revision"] += 1
            self._write(directory, state)
            return deepcopy(state["operations"][operation_id])

    def start(self, operation_id, role, digest, invocation_id=None):
        _label(operation_id)
        if role not in ROLES or not isinstance(digest, str) or not _SHA.fullmatch(digest):
            _blocked("invalid_budget_request")
        with self._locked() as directory:
            state = self._read(directory)
            operation = state["operations"].get(operation_id)
            if operation is None:
                if state["limit"] is not None and _validate(state) + 1 > state["limit"]:
                    raise BudgetBlocked("budget_exhausted", operation_id=operation_id)
                operation = {"reserved_roles": [role], "calls": {}}
                state["operations"][operation_id] = operation
            if role not in operation["reserved_roles"]:
                raise BudgetBlocked("budget_operation_conflict", operation_id=operation_id,
                                    started_count=len(operation["calls"]))
            if role in operation["calls"]:
                raise BudgetBlocked("budget_call_already_started", operation_id=operation_id,
                                    started_count=len(operation["calls"]))
            operation["calls"][role] = {"payload_sha256": digest, "status": "started", "started_at": _now(),
                                        "finished_at": None, "invocation_id": invocation_id, "error_code": None}
            state["revision"] += 1
            self._write(directory, state)
            return len(operation["calls"])

    def finish(self, operation_id, role, status, *, invocation_id=None, error_code=None):
        with self._locked() as directory:
            state = self._read(directory)
            operation = state["operations"].get(operation_id)
            call = operation["calls"].get(role) if operation else None
            if call is None or call["status"] != "started" or status not in {"completed", "error", "unknown"}:
                _blocked("budget_operation_conflict")
            call.update(status=status, finished_at=_now(), error_code=error_code)
            if isinstance(invocation_id, str) and re.fullmatch(r"model-[0-9a-f]{32}", invocation_id):
                call["invocation_id"] = invocation_id
            state["revision"] += 1
            self._write(directory, state)


class BudgetedModelCall:
    """Bind one durable operation; each role may start only once."""
    def __init__(self, ledger, operation_id, transport=None, *, default_timeout=120):
        from .model_calls import validate_timeout
        if not isinstance(ledger, BudgetLedger):
            _blocked("budget_binding_required")
        self.default_timeout = validate_timeout(default_timeout)
        self.ledger = ledger
        self.operation_id = _label(operation_id)
        self.transport = transport
        self.observed_started_count = 0

    def for_operation(self, operation_id, *, default_timeout=None):
        return type(self)(self.ledger, operation_id, self.transport,
                          default_timeout=self.default_timeout if default_timeout is None else default_timeout)

    def operation_state(self):
        return self.ledger.operation(self.operation_id)

    @property
    def started_count(self):
        state = self.operation_state()
        return state["started_calls"] if state else 0

    @property
    def reserved_count(self):
        state = self.operation_state()
        return state["reserved_calls"] if state else 0

    def reserve_calls(self, count):
        if type(count) is not int or count not in (1, 2):
            _blocked("invalid_budget_reservation")
        return self.ledger.reserve(self.operation_id, ["extract", "review"] if count == 2 else ["answer"])

    def __call__(self, role, payload, timeout=_TIMEOUT_UNSET):
        from .model_calls import ModelCallError, _call_transport, validate_timeout
        timeout = validate_timeout(self.default_timeout if timeout is _TIMEOUT_UNSET else timeout)
        try:
            raw = _json(payload)
            digest = hashlib.sha256(raw).hexdigest()
            payload = _decode(raw)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ModelCallError("invalid_payload_or_prompt", "model-" + uuid.uuid4().hex) from None
        started = False
        started_count = 0
        def on_start(invocation_id=None):
            nonlocal started, started_count
            started_count = self.ledger.start(self.operation_id, role, digest, invocation_id)
            started = True
            self.observed_started_count += 1
        def finish(status, **metadata):
            try:
                self.ledger.finish(self.operation_id, role, status, **metadata)
            except BudgetBlocked as error:
                raise BudgetBlocked(error.code, operation_id=self.operation_id,
                                    started_count=started_count, attempted_calls=1) from None
        try:
            if self.transport is None:
                result = _call_transport(role, payload, timeout=timeout, on_start=on_start)
            else:
                on_start()
                result = self.transport(role, payload, timeout=timeout)
        except BaseException as error:
            if started:
                code = getattr(error, "code", None)
                safe = code if isinstance(error, ModelCallError) and isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "transport_outcome_unknown"
                finish("unknown", error_code=safe, invocation_id=getattr(error, "invocation_id", None))
            elif isinstance(error, BudgetBlocked):
                # A failed Review reservation/start must not erase an extraction
                # already observed by this operation. Do not reopen a broken
                # ledger just to report the known count, or mark this call started.
                raise BudgetBlocked(error.code, operation_id=self.operation_id,
                                    started_count=max(error.started_count, self.observed_started_count),
                                    attempted_calls=error.attempted_calls) from None
            raise
        if started:
            invocation = result.get("invocation_id") if isinstance(result, dict) else None
            finish("completed", invocation_id=invocation)
        return result
