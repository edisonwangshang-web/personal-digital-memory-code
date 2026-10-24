"""Explicit zero-start revision recovery, separate from unknown-call recovery.

An operator must independently review the supplied reservation attestation and
its canonical hash. Older started files did not bind a budget pool; this API
cannot infer that missing historical association from an arbitrary empty pool.
Runtime verification checks the actual private ledger without changing it.
Historical Git verification checks the persisted attestation and completed call
binding; it never needs the external ledger to remain empty or even available.
"""

from copy import deepcopy
import hashlib
import os
import re

from .contracts import CaptureError, canonical

CONTRACT = "unstarted-revision-resume-v1"
_IDENTITY = ("schema_version", "pool_id", "authorization_sha256", "limit", "initial_used")


def _fail(code="invalid_revision_resume"):
    raise CaptureError(code, "零调用返工续接的原预约、账本身份或调用证据不一致；不重发未知调用")


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def _legacy_quality_guard(*values):
    """The v1 operator proof never froze an original Skill request.

    Its empty ledger proves that no call started; it cannot prove which Skill
    was selected for the interrupted attempt.  Keep the historical recovery
    contract intact instead of inferring rules from the rejected candidate.
    Normal quality revisions and decomposed Review recovery use other paths.
    """
    fields = {"skill_bundle", "quality_contract", "quality_context", "extraction_request_binding"}
    if any(isinstance(value, dict) and fields.intersection(value) for value in values):
        _fail("skill_quality_recovery_attestation_required")


def _reservation(value, expected_sha):
    fields = {"schema_version", "contract", "run_id", "revision_from_run", "head", "started", "revision_attempt",
              "processing_key", "revision_context_sha256", "source_paths", "related_runs", "budget", "external_receipts_sha256"}
    if (not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != "0.9"
            or value.get("contract") != CONTRACT or not isinstance(expected_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha) or _sha(value) != expected_sha):
        _fail("revision_resume_attestation_mismatch")
    budget = value["budget"]
    if (not isinstance(budget, dict) or set(budget) != {*_IDENTITY, "mode", "operation_id", "reserved_roles", "ledger_sha256"}
            or budget["reserved_roles"] != ["extract", "review"]
            or budget["mode"] != ("audit" if budget["schema_version"] == "2" else "budget")
            or budget["schema_version"] not in {"1", "2"}
            or not isinstance(budget["ledger_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", budget["ledger_sha256"])):
        _fail("revision_resume_budget_mismatch")
    receipts = value["external_receipts_sha256"]
    if (not isinstance(receipts, dict) or not 1 <= len(receipts) <= 32
            or any(not isinstance(path, str) or len(path) > 256 or path.startswith("/") or ".." in path.split("/")
                   or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                   for path, digest in receipts.items())):
        _fail("revision_resume_attestation_mismatch")
    return value


def _scope(git, run_id, context, source_paths, related_runs, reservation, *, at_commit, require_unstarted):
    from .pipeline import (_ancestor, _committed, _hash, _last_commit, _load, _revision_attempt_path, _time,
                           _run_path, _stage_path, normalize_source_paths, _related_run_ids, control_guard)
    from .revision_context import verify_revision_context

    if not isinstance(context, dict) or not isinstance(context.get("candidate"), dict):
        _fail("revision_resume_scope_mismatch")
    _legacy_quality_guard(context["candidate"])
    maximum = 256 if "batch_context" in context.get("candidate", {}) else 8
    selected = normalize_source_paths(source_paths, allow_versions=True, max_sources=maximum)
    related = _related_run_ids(related_runs or [], max_runs=maximum)
    if (reservation["run_id"] != run_id or reservation["revision_from_run"] != context.get("source_run_id")
            or reservation["revision_context_sha256"] != _hash(context)
            or reservation["source_paths"] != selected or reservation["related_runs"] != related):
        _fail("revision_resume_scope_mismatch")
    # Scope equality precedes the original source/proof loader.
    if sorted(item["path"] for item in context["source_bindings"]) != selected:
        _fail("revision_resume_scope_mismatch")
    head = reservation["head"]
    if require_unstarted and git.head() != head:
        _fail("revision_resume_head_changed")
    _ancestor(git, head, at_commit)
    expected_paths = {"started": _run_path(run_id, "started"), "revision_attempt": _revision_attempt_path(context)}
    artifacts = {}
    for key, path in expected_paths.items():
        pointer = reservation[key]
        if (not isinstance(pointer, dict) or set(pointer) != {"path", "commit_sha", "sha256"}
                or pointer["path"] != path or pointer["commit_sha"] != head
                or _last_commit(git, at_commit, path) != head):
            _fail("revision_resume_reservation_changed")
        data = _committed(git, path, at_commit)
        if _hash(data) != pointer["sha256"] or git.read(head, path) != data:
            _fail("revision_resume_reservation_changed")
        artifacts[key] = _load(data, "processing_attempt" if key == "started" else "revision_attempt",
                               "0.4" if key == "started" else "0.9")
    started, pointer = artifacts["started"], artifacts["revision_attempt"]
    _time(started.get("started_at"))
    if not isinstance(reservation["processing_key"], str) or not re.fullmatch(r"sha256-[0-9a-f]{64}", reservation["processing_key"]):
        _fail("revision_resume_reservation_changed")
    if (started != {"run_id": run_id, "processing_key": reservation["processing_key"], "execution_context": None,
                    "source_paths": selected, "related_runs": related, "reserved_calls": 2,
                    "started_at": started.get("started_at")}
            or pointer != {"run_id": run_id, "revision_context_sha256": _hash(context), "source_paths": selected,
                           "related_runs": related, "processing_key": reservation["processing_key"]}):
        _fail("revision_resume_reservation_changed")
    if require_unstarted:
        absent = [_stage_path(run_id), *[_run_path(run_id, name) for name in
                  ("review", "failure", "manifest", "receipt", "pending", "pending-registration", "reused")]]
        if any(git._existing(path) is not None or _last_commit(git, git.head(), path) for path in absent):
            _fail("revision_resume_already_processed")
    if control_guard(git, at_commit)["digest"] != context["controls_digest"]:
        _fail("revision_resume_controls_changed")
    authority = verify_revision_context(git, context, source_paths=selected, related_runs=related,
                                         at_commit=at_commit, historical=True)
    return authority + [deepcopy(reservation[key]) for key in expected_paths]


def _adapter(model_call):
    from .pipeline import _budget_adapter
    value = _budget_adapter(model_call)
    if value is None:
        _fail("revision_resume_budget_binding_required")
    return value


def _state(adapter, budget, *, exact_raw=False):
    """Read one locked ledger snapshot; the helper never reserves or writes."""
    from .budget import MAX_LEDGER_BYTES
    ledger = adapter.ledger
    with ledger._locked() as directory:
        state = ledger._read(directory)
        if exact_raw:
            fd = os.open(ledger.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(MAX_LEDGER_BYTES + 1)
            if len(raw) > MAX_LEDGER_BYTES or _sha(raw) != budget["ledger_sha256"]:
                _fail("revision_resume_ledger_changed")
    if (adapter.operation_id != budget["operation_id"]
            or any(state[key] != budget[key] for key in _IDENTITY)
            or state.get("mode", "budget") != budget["mode"]):
        _fail("revision_resume_budget_mismatch")
    operation = state["operations"].get(adapter.operation_id)
    if not isinstance(operation, dict) or operation["reserved_roles"] != ["extract", "review"]:
        _fail("revision_resume_missing_reservation")
    return operation


def verify_unstarted_revision(git, run_id, fullcontext, *, model_call, source_paths, related_runs,
                              reservation, expected_reservation_sha256):
    """Authorize only the original committed run with an actually empty call map."""
    _legacy_quality_guard(fullcontext.get("candidate") if isinstance(fullcontext, dict) else None)
    value = _reservation(reservation, expected_reservation_sha256)
    adapter = _adapter(model_call)
    operation = _state(adapter, value["budget"], exact_raw=True)
    if operation["calls"]:
        _fail("revision_resume_call_already_started")
    _scope(git, run_id, fullcontext, source_paths, related_runs, value,
           at_commit=git.head(), require_unstarted=True)
    return {"contract": CONTRACT, "reservation": deepcopy(value),
            "expected_reservation_sha256": expected_reservation_sha256}


def attach_revision_resume_extraction(model_call, resume_context, extraction, payload):
    """Freeze the actual completed extraction's ledger record into the candidate."""
    _legacy_quality_guard(payload)
    from .budget import _json
    if (not isinstance(resume_context, dict) or set(resume_context) != {"contract", "reservation", "expected_reservation_sha256"}
            or resume_context.get("contract") != CONTRACT):
        _fail()
    value = _reservation(resume_context["reservation"], resume_context["expected_reservation_sha256"])
    adapter = _adapter(model_call)
    operation = _state(adapter, value["budget"])
    call = operation["calls"].get("extract")
    if (set(operation["calls"]) != {"extract"} or not isinstance(call, dict) or call["status"] != "completed"
            or call["payload_sha256"] != _sha(_json(payload)) or call["invocation_id"] != extraction.get("invocation_id")):
        _fail("revision_resume_extraction_audit_mismatch")
    return {**deepcopy(resume_context), "extraction_audit": {"operation_id": adapter.operation_id,
            "pool_id": value["budget"]["pool_id"], "authorization_sha256": value["budget"]["authorization_sha256"],
            "call": deepcopy(call)}}


def verify_revision_resume(git, candidate, *, historical=False):
    """Verify Git authority and the completed call, without opening a ledger."""
    from .budget import _validate
    from .pipeline import _candidate_bindings, _candidate_payload, _candidate_sources
    _legacy_quality_guard(candidate)
    context = candidate.get("revision_resume")
    if (not isinstance(context, dict) or set(context) != {"contract", "reservation", "expected_reservation_sha256", "extraction_audit"}
            or context.get("contract") != CONTRACT):
        _fail()
    value = _reservation(context["reservation"], context["expected_reservation_sha256"])
    paths = [item["path"] for item in _candidate_bindings(candidate)]
    authority = _scope(git, candidate["run_id"], candidate["revision_context"], paths,
        candidate["related_runs"], value, at_commit=candidate["base_head"], require_unstarted=False)
    budget, audit = value["budget"], context["extraction_audit"]
    if (not isinstance(audit, dict) or set(audit) != {"operation_id", "pool_id", "authorization_sha256", "call"}
            or any(audit[key] != budget[key] for key in ("operation_id", "pool_id", "authorization_sha256"))):
        _fail("revision_resume_extraction_audit_mismatch")
    call = audit["call"]
    synthetic = {key: budget[key] for key in _IDENTITY}
    if budget["schema_version"] == "2":
        synthetic["mode"] = budget["mode"]
    synthetic.update(revision=0, operations={budget["operation_id"]: {"reserved_roles": ["extract", "review"], "calls": {"extract": call}}})
    _validate(synthetic)
    sources = _candidate_sources(git, candidate, historical=True)
    payload = (candidate["revision_revalidation"]["original_payload"] if "revision_revalidation" in candidate
               else _candidate_payload(git, candidate, sources))
    if (call["status"] != "completed" or call["payload_sha256"] != _sha(payload)
            or call["invocation_id"] != candidate["extraction"]["invocation_id"]):
        _fail("revision_resume_extraction_audit_mismatch")
    return authority
