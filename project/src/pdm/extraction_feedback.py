"""Current failed-draft feedback, bound to a completed extraction and Git proof.

This is input for the next ordinary extraction, never a candidate, a Review,
or permission to replay an unknown call. Old skill contracts do not opt in.
"""

from copy import deepcopy
import hashlib
import json
import re

from .contracts import CaptureError


CONTRACT = "extraction-validation-feedback-v1"
_RUN = re.compile(r"run-[0-9a-f]{32}\Z")
_SHA = re.compile(r"sha256-[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_POOL_FIELDS = ("schema_version", "mode", "pool_id", "authorization_sha256", "limit", "initial_used")


def _fail(code="extraction_feedback_changed"):
    raise CaptureError(code, "提取校验反馈的原返回、调用身份或固定资料范围不一致；未重发未知调用")


def _raw(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(value):
    return "sha256-" + hashlib.sha256(value if isinstance(value, bytes) else _raw(value)).hexdigest()


def enabled(bundle):
    skills = bundle.get("skills", {}) if isinstance(bundle, dict) else {}
    return (isinstance(skills, dict)
            and all(isinstance(skills.get(role), dict) and skills[role].get("ref") == ref
                    for role, ref in (("extract", "knowledge-extract@12"),
                                      ("review", "knowledge-review@10"))))


def _scope(payload):
    # Existing prepare_quality supplies no volatile system facts. Do not strip
    # facts, source bodies, related objects, range identities, or rule material.
    value = deepcopy(payload)
    value.pop("extraction_feedback", None)
    if isinstance(value.get("controls"), dict):
        value["controls"].pop("head", None)
    return _sha(value)


def _pool(ledger):
    state = ledger.snapshot()
    return {field: state.get(field) for field in _POOL_FIELDS}


def failure_context(git, payload, versions, processing_key, run_id, model_call):
    """Freeze a small identity only; the visible response stays in diagnostics."""
    if not enabled(payload.get("skill_bundle")):
        return None
    from .model_calls import request_binding
    from .pipeline import _budget_adapter
    context = {"contract": CONTRACT, "run_id": run_id, "processing_key": processing_key,
               "scope_sha256": _scope(payload), "versions": deepcopy(versions),
               # BudgetLedger uses the bare canonical-payload hex digest;
               # rendered request and Git proof hashes use their own format.
               "payload_sha256": hashlib.sha256(_raw(payload)).hexdigest(),
               "request_binding": request_binding("extract", payload),
               "audit_pool": None,
               "operation_id": None, "completed_extract": None}
    adapter = _budget_adapter(model_call)
    if adapter is not None:
        operation = adapter.ledger.operation(adapter.operation_id)
        call = operation.get("calls", {}).get("extract") if operation is not None else None
        context.update(audit_pool=_pool(adapter.ledger), operation_id=adapter.operation_id)
        if (isinstance(call, dict) and call.get("status") == "completed"
                and call.get("payload_sha256") == context["payload_sha256"]):
            context["completed_extract"] = deepcopy(call)
    return context


def _snapshot(git, path, commit=None):
    from .pipeline import _committed, _last_commit
    commit = commit or _last_commit(git, git.head(), path)
    if not commit:
        _fail("extraction_feedback_unavailable")
    return {"path": path, "commit_sha": commit, "sha256": _sha(_committed(git, path, commit))}


def _read_snapshot(git, ref, path):
    from .pipeline import _committed
    if (not isinstance(ref, dict) or set(ref) != {"path", "commit_sha", "sha256"} or ref["path"] != path
            or not isinstance(ref["commit_sha"], str) or _COMMIT.fullmatch(ref["commit_sha"]) is None
            or not isinstance(ref["sha256"], str) or _SHA.fullmatch(ref["sha256"]) is None):
        _fail()
    git.run("merge-base", "--is-ancestor", ref["commit_sha"], git.head())
    raw = _committed(git, path, ref["commit_sha"])
    if _sha(raw) != ref["sha256"] or _committed(git, path) != raw:
        _fail()
    return raw


def _checked_failure(git, ref):
    from .engine_provenance import read_reservation
    from .pipeline import _call_metadata, _load, _run_path
    fields = {"contract", "source_run_id", "failure", "started", "engine",
              "scope_sha256", "audit_pool", "operation_id", "completed_extract"}
    if (not isinstance(ref, dict) or set(ref) != fields or ref["contract"] != CONTRACT
            or not isinstance(ref["source_run_id"], str) or _RUN.fullmatch(ref["source_run_id"]) is None):
        _fail()
    run = ref["source_run_id"]
    failure = _load(_read_snapshot(git, ref["failure"], _run_path(run, "failure")), "failure")
    started = _load(_read_snapshot(git, ref["started"], _run_path(run, "started")), "processing_attempt")
    engine = _load(_read_snapshot(git, ref["engine"], _run_path(run, "engine")), "engine_provenance")
    diagnostic = failure.get("extraction_diagnostic")
    if (failure.get("run_id") != run or failure.get("status") != "failed" or failure.get("phase") != "extract"
            or not isinstance(diagnostic, dict) or diagnostic.get("status") != "unvalidated_not_publishable"
            or not isinstance(diagnostic.get("response"), dict)
            or not isinstance(diagnostic.get("validation_context"), dict)):
        _fail("extraction_feedback_unavailable")
    response = diagnostic["response"]
    if (set(response) != {"output", "model", "invocation_id", "elapsed_seconds", "usage"}
            or diagnostic.get("response_sha256") != _sha(response)
            or diagnostic.get("response_bytes") != len(_raw(response))
            or diagnostic.get("validation_target") not in {"response.output", "validated_output"}
            or (diagnostic["validation_target"] == "validated_output"
                and not isinstance(diagnostic.get("validated_output"), dict))
            or (diagnostic["validation_target"] == "response.output" and "validated_output" in diagnostic)):
        _fail()
    metadata = _call_metadata(response)
    context = diagnostic["validation_context"]
    context_fields = {"contract", "run_id", "processing_key", "scope_sha256", "versions",
                      "payload_sha256", "request_binding", "audit_pool", "operation_id", "completed_extract"}
    call = context.get("completed_extract")
    parents = git.text("rev-list", "--parents", "-n", "1", ref["failure"]["commit_sha"]).split()[1:]
    error = diagnostic.get("validation_error")
    if (set(context) != context_fields or context["contract"] != CONTRACT or context["run_id"] != run
            or context["processing_key"] != started.get("processing_key")
            or context["versions"] != engine.get("versions") or engine != read_reservation(git, run)
            or ref["started"]["commit_sha"] != ref["engine"]["commit_sha"]
            or parents != [ref["started"]["commit_sha"]]
            or context["scope_sha256"] != ref["scope_sha256"]
            or context["audit_pool"] != ref["audit_pool"]
            or context["operation_id"] != ref["operation_id"]
            or context["request_binding"] != diagnostic.get("request_binding")
            or not isinstance(context["request_binding"], dict)
            or set(context["request_binding"]) != {"sha256", "bytes"}
            or call != ref["completed_extract"] or not isinstance(call, dict)
            or call.get("status") != "completed" or call.get("payload_sha256") != context["payload_sha256"]
            or call.get("invocation_id") != metadata["invocation_id"]
            or failure.get("call", {}).get("invocation_id") != metadata["invocation_id"]
            or not isinstance(error, dict) or error.get("code") != failure.get("error")):
        _fail()
    return failure, diagnostic, context


def prepare_feedback(git, failed_attempt, model_call):
    """Select one finished extraction; current ledger authority is mandatory."""
    from .pipeline import _budget_adapter, _committed, _load, _run_path
    run = failed_attempt.get("run_id") if isinstance(failed_attempt, dict) else None
    if not isinstance(run, str) or _RUN.fullmatch(run) is None:
        _fail()
    failure = _load(_committed(git, _run_path(run, "failure")), "failure")
    if failure.get("phase") != "extract":
        return None
    diagnostic = failure.get("extraction_diagnostic", {})
    context = diagnostic.get("validation_context") if isinstance(diagnostic, dict) else None
    if (failed_attempt.get("status") != "failed" or failed_attempt.get("error") != failure.get("error")
            or not isinstance(context, dict)):
        _fail("extraction_feedback_unavailable")
    started = _snapshot(git, _run_path(run, "started"))
    ref = {"contract": CONTRACT, "source_run_id": run,
           "failure": _snapshot(git, _run_path(run, "failure")), "started": started,
           "engine": _snapshot(git, _run_path(run, "engine"), started["commit_sha"]),
           **{key: deepcopy(context.get(key)) for key in
              ("scope_sha256", "audit_pool", "operation_id", "completed_extract")}}
    _, _, context = _checked_failure(git, ref)
    adapter = _budget_adapter(model_call)
    if (adapter is None or context["audit_pool"] != _pool(adapter.ledger)
            or context["operation_id"] != failed_attempt.get("operation_id")):
        _fail("extraction_feedback_audit_mismatch")
    operation = adapter.ledger.operation(context["operation_id"])
    if (operation is None or operation["reserved_roles"] != ["extract", "review"]
            or set(operation["calls"]) != {"extract"}
            or operation["calls"]["extract"] != context["completed_extract"]):
        _fail("extraction_feedback_audit_mismatch")
    return ref


def _model_view(diagnostic, run):
    output = (diagnostic["validated_output"] if diagnostic.get("validation_target") == "validated_output"
              else diagnostic["response"]["output"])
    if not isinstance(output, dict):
        output = {}
    draft = {key: deepcopy(output[key]) for key in ("objects", "wiki", "wikis", "reuse_refs") if key in output}
    error = deepcopy(diagnostic["validation_error"])
    details = error.get("details") or {}
    path = details.get("field_path", "") if isinstance(details, dict) else ""
    code = error.get("code", "")
    component = ("temporal_claims" if "temporal" in code or path.startswith("/quality/temporal_claims")
                 else "statistic_claims" if "statistic" in code or path.startswith("/quality/statistic_claims")
                 else "coverage_disposition" if "coverage" in code or "body_mapping" in code
                 or "deferral" in code or path.startswith("/quality/coverage_disposition") else None)
    quality = output.get("quality", {})
    if component is not None and isinstance(quality, dict):
        declarations = quality.get(component, [])
        index = details.get("claim_index") if isinstance(details, dict) else None
        missing = details.get("missing_claims") if isinstance(details, dict) else None
        if component == "statistic_claims" and isinstance(declarations, list) and isinstance(missing, list):
            pairs = {(item.get("record_id"), item.get("stat_id")) for item in missing if isinstance(item, dict)}
            declarations = [item for item in declarations if isinstance(item, dict)
                            and (item.get("record_id"), item.get("stat_id")) in pairs]
        elif isinstance(declarations, list) and type(index) is int and 0 <= index < len(declarations):
            declarations = [declarations[index]]
        elif isinstance(declarations, list) and isinstance(details, dict) and isinstance(details.get("record_id"), str):
            declarations = [item for item in declarations
                            if isinstance(item, dict) and item.get("record_id") == details["record_id"]]
        draft["quality"] = {component: deepcopy(declarations)}
    return {"contract": CONTRACT, "source_run_id": run, "draft": draft,
            "validation_error": error}


def load_feedback(git, ref, payload, versions):
    """Recheck fixed Git feedback and current complete scope, without a ledger."""
    if not enabled(payload.get("skill_bundle")):
        _fail("extraction_feedback_contract_required")
    _, diagnostic, context = _checked_failure(git, ref)
    if context["versions"] != versions or context["scope_sha256"] != _scope(payload):
        _fail("extraction_feedback_scope_changed")
    return _model_view(diagnostic, ref["source_run_id"])


def authority_refs(git, ref):
    """Return fixed diagnostic evidence for the existing publication proof DAG."""
    _checked_failure(git, ref)
    return [deepcopy(ref[name]) for name in ("failure", "started", "engine")]
