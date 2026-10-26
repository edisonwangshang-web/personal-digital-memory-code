"""Explicit recovery of a known completed quality extraction from original cache.

The old failure and ledger do not authenticate response bytes. An operator must
review the original private cache and attest its hashes. This trust boundary is
preserved verbatim, not described as a pre-existing Git/server output digest.
No model role, reservation, counter or original Git artifact is written here.
"""

from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat

from .contracts import CaptureError, canonical


CONTRACT = "quality-completed-extraction-revalidation-v1"
RESERVATION_CONTRACT = "quality-known-completed-reservation-v1"
CONTRACT_V2 = "quality-completed-extraction-revalidation-v2"
RESERVATION_CONTRACT_V2 = "quality-known-completed-reservation-v2"
CONTRACTS = (CONTRACT, CONTRACT_V2)
RESPONSE_PROVENANCE = "operator_attested_local_cache_no_prior_git_output_digest"
SCHEMA = "0.13"
_FILES = ("payload.json", "response.json", "request-binding.json", "request.bin")
_BUDGET_FIELDS = ("schema_version", "pool_id", "authorization_sha256", "limit", "initial_used")
_ATTESTED_KEYS = {"schema_version", "contract", "response_provenance", "run_id", "revision_from_run", "head",
    "validation_head", "failure", "started", "revision_attempt", "processing_key", "source_paths", "related_runs",
    "revision_context_sha256", "original_payload_sha256", "original_response_sha256", "original_resume_context_sha256",
    "original_extraction_versions", "original_native_request_binding", "budget", "cache_dir", "external_receipts_sha256"}
_CONTEXT_KEYS = {"contract", "response_provenance", "attestation", "expected_attestation_sha256", "original_payload",
    "original_response", "original_resume_context", "original_call_metadata", "original_extraction_versions",
    "validation_versions", "original_native_request_binding", "stable_identity"}


def _fail(code="invalid_quality_completed_extraction"):
    raise CaptureError(code, "已完成质量提取的原调用、缓存证明或固定资料不一致；未重新提取或重放未知调用")


def _profile(contract):
    if contract not in CONTRACTS:
        _fail("quality_completed_contract_changed")
    return (RESERVATION_CONTRACT_V2, "quality_temporal_mismatch") if contract == CONTRACT_V2 else (
        RESERVATION_CONTRACT, "candidate_too_large")


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def _binding(value):
    if not isinstance(value, dict) or type(value.get("bytes")) is not int:
        _fail("quality_completed_native_request_changed")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"(?:sha256-)?[0-9a-f]{64}", digest):
        _fail("quality_completed_native_request_changed")
    return {"sha256": "sha256-" + digest.removeprefix("sha256-"), "bytes": value["bytes"]}


def _cache(directory, operation_id, payload, response, native_binding, receipts):
    from .budget import _decode
    from .model_calls import render_model_request
    root = Path(directory)
    if (not root.is_absolute() or root.name != "extract"
            or root.parent.name != _sha(operation_id.encode())
            or any(parent.is_symlink() for parent in (root, *root.parents))
            or not isinstance(receipts, dict) or set(receipts) != set(_FILES)):
        _fail("quality_completed_cache_changed")
    for directory_path in (root.parent, root):
        info = directory_path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _fail("quality_completed_cache_changed")
    saved = {}
    for name in _FILES:
        descriptor = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                _fail("quality_completed_cache_changed")
            saved[name] = handle.read(4 * 1024 * 1024 + 1)
        if len(saved[name]) > 4 * 1024 * 1024 or _sha(saved[name]) != receipts[name]:
            _fail("quality_completed_cache_changed")
    cached_binding = _decode(saved["request-binding.json"])
    if (_decode(saved["payload.json"]) != payload or _decode(saved["response.json"]) != response
            or _binding(cached_binding) != _binding(native_binding)
            or cached_binding.get("operation_id") != operation_id or cached_binding.get("role") != "extract"
            or saved["request.bin"] != render_model_request("extract", payload)):
        _fail("quality_completed_cache_changed")


def _versions_at(git, revision):
    from .pipeline import _hash, _last_commit
    from .domains import DOMAIN_POLICY_VERSION, domain_policy
    paths = []
    for row in git.run("ls-tree", "-r", "-z", revision, "--", "project/src/pdm", "project/prompts", "project/skills").split(b"\0"):
        if not row:
            continue
        header, name = row.split(b"\t", 1)
        mode, kind, _ = header.split()
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            _fail("quality_completed_program_snapshot_missing")
        paths.append(name.decode("utf-8"))
    def engineering_bytes(path):
        return git.run("show", revision + ":" + path)
    code = {"pdm/" + Path(path).name: _hash(engineering_bytes(path)) for path in paths
            if path.startswith("project/src/pdm/") and path.count("/") == 3 and path.endswith(".py")}
    if not code:
        _fail("quality_completed_program_snapshot_missing")
    prompts = {role: _hash(engineering_bytes("project/prompts/" + role + ".md")) for role in ("extract", "review")}
    skills = {path.removeprefix("project/skills/"): _hash(engineering_bytes(path))
              for path in paths if path.startswith("project/skills/")}
    policy = _hash(domain_policy())
    return {"program_sha256": _hash(code), "rules_version": _hash({**prompts, "domain_policy": policy, "skills": skills}),
            "domain_policy_version": DOMAIN_POLICY_VERSION, "domain_policy_sha256": policy,
            "program_commit": git.text("log", "-1", "--format=%H", revision, "--", "project/src/pdm",
                                       "project/prompts", "project/skills", "pyproject.toml") or None}


def _snapshot(git, path):
    from .pipeline import _committed, _hash, _last_commit
    commit = _last_commit(git, git.head(), path)
    additions = git.text("log", "--reverse", "--diff-filter=A", "--format=%H", "HEAD", "--", path).splitlines()
    if additions != [commit]:
        _fail("quality_completed_original_proof_changed")
    raw = _committed(git, path, commit)
    return {"path": path, "commit_sha": commit, "sha256": _hash(raw)}, raw


def _material(git, run_id, paths, runs, payload, response, *, historical=False, contract=CONTRACT):
    """Rebuild the original reservation and complete request before any reuse."""
    from .domains import domain_policy
    from .knowledge_quality import prepare_quality
    from .model_calls import MAX_INPUT_BYTES, _valid, output_schema, request_binding
    from .pipeline import (_ancestor, _call_metadata, _hash, _load, _load_sources, _merge_candidates,
        _objects, _related_context, _reuse_requirements, _revision_attempt_path, _run_path,
        _validated_skill_bundle, control_guard)
    from .revision_context import build_revision_context, revision_model_context
    _, failure_error = _profile(contract)
    rebuilding = contract == CONTRACT_V2
    if (not isinstance(response, dict) or set(response) != {"output", "model", "invocation_id", "elapsed_seconds", "usage"}
            or payload.get("quality_contract") != "skill-quality-v1"):
        _fail("quality_completed_original_bytes_changed")
    bundle = _validated_skill_bundle(payload.get("skill_bundle"))
    if (bundle["skills"]["extract"]["ref"], bundle["skills"]["review"]["ref"]) != ("knowledge-extract@10", "knowledge-review@8"):
        _fail("quality_completed_skill_contract_required")
    if sorted(source["path"] for source in payload["sources"]) != paths:
        _fail("quality_completed_scope_mismatch")
    started_snapshot, raw = _snapshot(git, _run_path(run_id, "started"))
    started = _load(raw, "processing_attempt", "0.12")
    if (started != {"run_id": run_id, "processing_key": started.get("processing_key"), "execution_context": None,
                    "source_paths": paths, "related_runs": runs, "reserved_calls": 2, "started_at": started.get("started_at")}
            or not re.fullmatch(r"sha256-[0-9a-f]{64}", str(started.get("processing_key")))):
        _fail("quality_completed_reservation_changed")
    head = started_snapshot["commit_sha"]
    revision_from = payload["revision_context"]["source_run_id"]
    full = build_revision_context(git, revision_from, source_paths=paths, related_runs=runs, at_commit=head)
    rebuild = payload.get("rebuild_context")
    if rebuilding:
        from .rebuild import validate_rebuild_context
        if runs or rebuild is None or full["candidate"].get("rebuild_context") != rebuild:
            _fail("quality_completed_rebuild_changed")
        validate_rebuild_context(git, rebuild, source_paths=paths, at_commit=head)
    elif rebuild is not None:
        _fail("quality_completed_contract_changed")
    pointer_snapshot, pointer_raw = _snapshot(git, _revision_attempt_path(full))
    pointer = _load(pointer_raw, "revision_attempt", "0.12")
    if (pointer_snapshot["commit_sha"] != head
            or pointer != {"run_id": run_id, "revision_context_sha256": _hash(full), "source_paths": paths,
                           "related_runs": runs, "processing_key": started["processing_key"]}):
        _fail("quality_completed_reservation_changed")
    failure_snapshot, failure_raw = _snapshot(git, _run_path(run_id, "failure"))
    failure = _load(failure_raw, "failure", "0.12")
    extraction = _call_metadata(response)
    old_call = {key: extraction[key] for key in ("invocation_id", "model", "elapsed_seconds")}
    if (failure.get("run_id") != run_id or failure.get("phase") != "extract" or failure.get("status") != "failed"
            or failure.get("error") != failure_error or failure.get("attempted_calls") != 1
            or failure.get("call") != old_call):
        _fail("quality_completed_failure_changed")
    _ancestor(git, head, failure_snapshot["commit_sha"])
    _ancestor(git, failure_snapshot["commit_sha"], git.head())
    from .engine_provenance import read_reservation
    engine = read_reservation(git, run_id)
    if rebuilding and engine is None:
        _fail("quality_completed_program_snapshot_missing")
    # Pre-split reservations retain their exact private Git engineering history.
    # New data-only reservations bind the original loaded public engine before
    # either model call; never substitute the current checkout for that version.
    versions = engine["versions"] if engine is not None else _versions_at(git, head)
    sources = _load_sources(git.repo, paths, allow_versions=True, purpose="history", max_sources=256)
    related = _related_context(git, runs, sources, at_commit=head, max_runs=256)
    views = payload["sources"] if "batch_context" in payload else None
    context = prepare_quality(sources, source_views=views)
    expected = {"sources": [{"path": source["path"], "metadata": source["metadata"], "body": source["body"]} for source in sources],
                "related": related, "controls": control_guard(git, head), "rules_version": versions["rules_version"],
                "domain_policy": domain_policy(), "revision_context": revision_model_context(full),
                "processing_mode": "incremental", "quality_contract": "skill-quality-v1",
                "skill_bundle": bundle, "quality_context": context,
                "reuse_quality_requirements": _reuse_requirements(git, related, sources, context)}
    if rebuilding:
        expected["rebuild_context"] = deepcopy(rebuild)
    if views is not None:
        from .batching import validate_source_views
        validate_source_views(sources, views, payload["batch_context"])
        if (views != full["candidate"].get("source_views") or payload["batch_context"]["source_ranges"]
                != full["candidate"].get("batch_context", {}).get("source_ranges")):
            _fail("quality_completed_batch_changed")
        prior = _merge_candidates(git, [item["run_id"] for item in full["candidate"].get("merge_inputs", [])], paths)
        expected.update(sources=deepcopy(views), batch_context=deepcopy(payload["batch_context"]),
                        output_contract="batch-v1", prior_candidates=prior)
    if payload != expected or not _valid(response["output"], output_schema("extract", payload)):
        _fail("quality_completed_original_payload_changed")
    native_binding = request_binding("extract", payload)
    if native_binding["bytes"] > MAX_INPUT_BYTES:
        _fail("quality_completed_native_request_changed")
    # All existing deterministic quality, domain, dependency and scope checks
    # remain active; this trust contract only supplies the known original call.
    objects = _objects(response["output"], sources, run_id, response, versions, failure["failed_at"],
                       batch=views is not None, quality_context=context, related=related, skill_bundle=bundle)
    from .pipeline import _current_object_corrections, _reuse_obligation_objects, _updated_objects, _validate_domains
    from .reuse_quality import validate_reuse_quality
    if not historical:
        _current_object_corrections(objects, sources)
    operations = _updated_objects(git, objects, related, check_current=not historical,
                                  rebuild_context=rebuild if rebuilding else None)
    _validate_domains(objects, related, operations, versions)
    validate_reuse_quality(git, related, sources, context, _reuse_obligation_objects(objects, related), response["output"])
    material = {"full": full, "started": started_snapshot, "revision_attempt": pointer_snapshot, "failure": failure_snapshot,
            "processing_key": started["processing_key"], "head": head, "original_extraction_versions": versions,
            "original_native_request_binding": native_binding, "extraction": extraction, "sources": sources, "related": related}
    if rebuilding:
        material["rebuild_context"] = deepcopy(rebuild)
    return material


def _budget(model_call):
    from .pipeline import _budget_adapter
    from .revision_resume import _state
    adapter = _budget_adapter(model_call)
    if adapter is None or adapter.transport is not None:
        _fail("quality_completed_native_budget_required")
    state = adapter.ledger.snapshot()
    budget = {key: state[key] for key in _BUDGET_FIELDS}
    budget.update(mode=state.get("mode", "budget"), operation_id=adapter.operation_id,
                  reserved_roles=["extract", "review"], ledger_sha256=_sha(adapter.ledger.path.read_bytes()))
    operation = _state(adapter, budget, exact_raw=True)
    if set(operation["calls"]) != {"extract"} or operation["calls"]["extract"]["status"] != "completed":
        _fail("quality_completed_not_known_completed")
    budget["completed_call"] = deepcopy(operation["calls"]["extract"])
    return budget


def prepare_attestation(git, run_id, *, model_call, source_paths, related_runs=(), original_payload,
                        response, original_native_request_binding, external_receipts_sha256, cache_dir,
                        contract=CONTRACT):
    """Build reviewable operator evidence; never mutate the original cache/Git."""
    from .pipeline import _hash, _related_run_ids, normalize_source_paths
    try:
        paths = normalize_source_paths(source_paths, allow_versions=True, max_sources=256)
        runs = _related_run_ids(related_runs, max_runs=256)
        reservation_contract, _ = _profile(contract)
        material = _material(git, run_id, paths, runs, original_payload, response, contract=contract)
        budget = _budget(model_call)
        if (budget["completed_call"]["payload_sha256"] != _sha(original_payload)
                or budget["completed_call"]["invocation_id"] != material["extraction"]["invocation_id"]):
            _fail("quality_completed_audit_changed")
        if _binding(original_native_request_binding) != material["original_native_request_binding"]:
            _fail("quality_completed_native_request_changed")
        _cache(cache_dir, budget["operation_id"], original_payload, response,
               original_native_request_binding, external_receipts_sha256)
        reservation = {"schema_version": SCHEMA, "contract": reservation_contract, "run_id": run_id,
            "revision_from_run": material["full"]["source_run_id"], "head": material["head"],
            "started": material["started"], "revision_attempt": material["revision_attempt"],
            "processing_key": material["processing_key"], "revision_context_sha256": _hash(material["full"]),
            "source_paths": paths, "related_runs": runs,
            "budget": {key: value for key, value in budget.items() if key != "completed_call"}}
        if contract == CONTRACT_V2:
            reservation["rebuild_context"] = deepcopy(material["rebuild_context"])
        resume = {"contract": reservation_contract, "reservation": reservation,
                  "expected_reservation_sha256": _sha(reservation),
                  "extraction_audit": {key: budget[key] for key in ("pool_id", "authorization_sha256", "operation_id")}
                  | {"call": deepcopy(budget["completed_call"])}}
        value = {"schema_version": SCHEMA, "contract": contract, "response_provenance": RESPONSE_PROVENANCE,
            "run_id": run_id, "revision_from_run": material["full"]["source_run_id"],
            "head": material["failure"]["commit_sha"], "validation_head": git.head(),
            "failure": material["failure"], "started": material["started"], "revision_attempt": material["revision_attempt"],
            "processing_key": material["processing_key"], "source_paths": paths, "related_runs": runs,
            "revision_context_sha256": _hash(material["full"]), "original_payload_sha256": _sha(original_payload),
            "original_response_sha256": _sha(response), "original_resume_context_sha256": _sha(resume),
            "original_extraction_versions": material["original_extraction_versions"],
            "original_native_request_binding": material["original_native_request_binding"],
            "budget": budget, "cache_dir": str(Path(cache_dir)), "external_receipts_sha256": deepcopy(external_receipts_sha256)}
        if contract == CONTRACT_V2:
            value["rebuild_context"] = deepcopy(material["rebuild_context"])
        return {"attestation": value, "expected_attestation_sha256": _sha(value),
                "original_payload": deepcopy(original_payload), "response": deepcopy(response), "original_resume_context": resume}
    except (OSError, KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _fail()


def revalidation_identity(context):
    value = context["attestation"]
    identity = {"run_id": value["run_id"], "operation_id": value["budget"]["operation_id"],
        "invocation_id": value["budget"]["completed_call"]["invocation_id"], "payload_sha256": value["original_payload_sha256"]}
    if value["contract"] == CONTRACT_V2:
        identity.update(contract=CONTRACT_V2, rebuild_context=value["rebuild_context"])
    return "sha256-" + _sha(identity)


def _verify(git, run_id, attestation, digest, payload, response, resume, paths, runs, *, historical=False):
    from .budget import BudgetBlocked, _validate
    from .pipeline import _hash, _stage_path, _run_path
    contract = attestation.get("contract") if isinstance(attestation, dict) else None
    reservation_contract, _ = _profile(contract)
    keys = _ATTESTED_KEYS | ({"rebuild_context"} if contract == CONTRACT_V2 else set())
    if (not isinstance(attestation, dict) or set(attestation) != keys
            or _sha(attestation) != digest or attestation.get("schema_version") != SCHEMA
            or attestation.get("response_provenance") != RESPONSE_PROVENANCE
            or attestation.get("run_id") != run_id or attestation.get("source_paths") != paths or attestation.get("related_runs") != runs
            or attestation.get("original_payload_sha256") != _sha(payload)
            or attestation.get("original_response_sha256") != _sha(response)
            or attestation.get("original_resume_context_sha256") != _sha(resume)):
        _fail("quality_completed_attestation_changed")
    material = _material(git, run_id, paths, runs, payload, response, historical=historical, contract=contract)
    if contract == CONTRACT_V2 and attestation["rebuild_context"] != material["rebuild_context"]:
        _fail("quality_completed_rebuild_changed")
    if any(attestation.get(key) != material[key] for key in
           ("failure", "started", "revision_attempt", "processing_key", "original_extraction_versions", "original_native_request_binding")):
        _fail("quality_completed_original_proof_changed")
    budget = attestation["budget"]
    if set(budget) != {*_BUDGET_FIELDS, "mode", "operation_id", "reserved_roles", "ledger_sha256", "completed_call"}:
        _fail("quality_completed_audit_changed")
    synthetic = {key: budget[key] for key in _BUDGET_FIELDS}
    if budget["schema_version"] == "2":
        synthetic["mode"] = budget["mode"]
    synthetic.update(revision=0, operations={budget["operation_id"]: {"reserved_roles": budget["reserved_roles"],
                        "calls": {"extract": budget["completed_call"]}}})
    try:
        _validate(synthetic)
    except BudgetBlocked:
        _fail("quality_completed_audit_changed")
    call = budget["completed_call"]
    if (budget["reserved_roles"] != ["extract", "review"] or call["status"] != "completed"
            or call["payload_sha256"] != _sha(payload) or call["invocation_id"] != material["extraction"]["invocation_id"]):
        _fail("quality_completed_not_known_completed")
    reservation = resume.get("reservation", {})
    expected_reservation = {"schema_version": SCHEMA, "contract": reservation_contract, "run_id": run_id,
        "revision_from_run": material["full"]["source_run_id"], "head": material["head"], "started": material["started"],
        "revision_attempt": material["revision_attempt"], "processing_key": material["processing_key"],
        "revision_context_sha256": _hash(material["full"]), "source_paths": paths, "related_runs": runs,
        "budget": {key: value for key, value in budget.items() if key != "completed_call"}}
    if contract == CONTRACT_V2:
        expected_reservation["rebuild_context"] = deepcopy(material["rebuild_context"])
    if (resume != {"contract": reservation_contract, "reservation": expected_reservation,
                   "expected_reservation_sha256": _sha(expected_reservation),
                   "extraction_audit": {key: budget[key] for key in ("pool_id", "authorization_sha256", "operation_id")}
                   | {"call": call}} or reservation != expected_reservation
            or attestation.get("revision_context_sha256") != _hash(material["full"])
            or attestation.get("revision_from_run") != material["full"]["source_run_id"]
            or attestation.get("head") != material["failure"]["commit_sha"]):
        _fail("quality_completed_reservation_changed")
    return material


def verify_completed_extraction(git, run_id, *, model_call, source_paths, related_runs, attestation,
                                expected_attestation_sha256, original_payload, response, original_resume_context):
    """Authorize zero extract calls only for this known completed native call."""
    from .pipeline import (_budget_adapter, _hash, _related_context, _related_run_ids, _last_commit,
                           _run_path, _stage_path, control_guard, execution_versions, normalize_source_paths)
    from .revision_resume import _state
    try:
        paths = normalize_source_paths(source_paths, allow_versions=True, max_sources=256)
        runs = _related_run_ids(related_runs, max_runs=256)
        material = _verify(git, run_id, attestation, expected_attestation_sha256, original_payload,
                           response, original_resume_context, paths, runs)
        if git.head() != attestation["validation_head"]:
            _fail("quality_completed_validation_head_changed")
        adapter = _budget_adapter(model_call)
        if adapter is None or adapter.transport is not None:
            _fail("quality_completed_native_budget_required")
        operation = _state(adapter, attestation["budget"], exact_raw=True)
        if operation["calls"] != {"extract": attestation["budget"]["completed_call"]}:
            _fail("quality_completed_not_known_completed")
        _cache(attestation["cache_dir"], attestation["budget"]["operation_id"], original_payload, response,
               attestation["original_native_request_binding"], attestation["external_receipts_sha256"])
        if any(git._existing(path) is not None or _last_commit(git, git.head(), path) for path in
               [_stage_path(run_id), *[_run_path(run_id, name) for name in ("review", "manifest", "receipt", "pending", "pending-registration", "reused")]]):
            _fail("quality_completed_already_revalidated")
        versions = execution_versions(git)
        if (versions["rules_version"] != material["original_extraction_versions"]["rules_version"]
                or control_guard(git)["digest"] != original_payload["controls"]["digest"]
                or _related_context(git, runs, material["sources"], max_runs=256) != material["related"]):
            _fail("quality_completed_context_changed")
        context = {"contract": attestation["contract"], "response_provenance": RESPONSE_PROVENANCE,
            "attestation": deepcopy(attestation), "expected_attestation_sha256": expected_attestation_sha256,
            "original_payload": deepcopy(original_payload), "original_response": deepcopy(response),
            "original_resume_context": deepcopy(original_resume_context), "original_call_metadata": material["extraction"],
            "original_extraction_versions": material["original_extraction_versions"], "validation_versions": versions,
            "original_native_request_binding": material["original_native_request_binding"]}
        context["stable_identity"] = revalidation_identity(context)
        return context
    except (OSError, KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _fail()


def verify_revalidated_extraction(git, candidate, *, historical=False):
    """Verify persisted operator provenance and the new Git guard without cache."""
    from .pipeline import (_ancestor, _candidate_bindings, _committed, _hash, _last_commit, _load, _stage_path)
    try:
        context = candidate["revision_revalidation"]
        if (set(context) != _CONTEXT_KEYS or context.get("contract") not in CONTRACTS
                or context.get("response_provenance") != RESPONSE_PROVENANCE):
            _fail()
        attestation = context["attestation"]
        if context["contract"] != attestation.get("contract"):
            _fail("quality_completed_contract_changed")
        material = _verify(git, candidate["run_id"], attestation, context["expected_attestation_sha256"],
            context["original_payload"], context["original_response"], context["original_resume_context"],
            [item["path"] for item in _candidate_bindings(candidate)], candidate["related_runs"], historical=True)
        _ancestor(git, attestation["head"], candidate["base_head"])
        if (candidate["base_head"] != attestation["validation_head"] or context["validation_versions"] != candidate["versions"]
                or candidate["versions"]["rules_version"] != material["original_extraction_versions"]["rules_version"]
                or context["stable_identity"] != revalidation_identity(context)
                or context["original_extraction_versions"] != material["original_extraction_versions"]
                or context["original_native_request_binding"] != material["original_native_request_binding"]
                or context["original_call_metadata"] != material["extraction"]
                or candidate["extraction"] != material["extraction"]
                or candidate["extraction_output"] != context["original_response"]["output"]
                or candidate.get("revision_resume") != context["original_resume_context"]
                or candidate.get("source_views") != context["original_payload"].get("sources") and "batch_context" in candidate
                or candidate.get("batch_context", {}).get("source_ranges") != context["original_payload"].get("batch_context", {}).get("source_ranges")
                or candidate["related"] != material["related"]):
            _fail("quality_completed_revalidation_changed")
        if context["contract"] == CONTRACT_V2 and candidate.get("rebuild_context") != material["rebuild_context"]:
            _fail("quality_completed_rebuild_changed")
        guard_path = "memory/runs/processes/" + candidate["run_id"] + "/quality-revalidation-guard.md"
        guard_raw = _committed(git, guard_path)
        guard = _load(guard_raw, "quality_revalidation_guard", SCHEMA)
        expected_guard = {"run_id": candidate["run_id"], "contract": context["contract"], "response_provenance": RESPONSE_PROVENANCE,
            "revalidation_context_sha256": _hash(context), "original_failure": attestation["failure"],
            "original_call": {"invocation_id": material["extraction"]["invocation_id"],
                              "payload_sha256": attestation["original_payload_sha256"]}}
        commit = _last_commit(git, git.head(), guard_path)
        if (guard != expected_guard or candidate.get("quality_revalidation_guard_ref") != {"path": guard_path, "sha256": _hash(guard_raw)}
                or commit != _last_commit(git, git.head(), _stage_path(candidate["run_id"]))
                or git.text("rev-parse", commit + "^") != candidate["base_head"]):
            _fail("quality_completed_guard_changed")
        snapshots = [deepcopy(attestation[key]) for key in ("started", "revision_attempt", "failure")] + [
            {"path": guard_path, "commit_sha": commit, "sha256": _hash(guard_raw)}]
        engine_path = "memory/runs/processes/" + candidate["run_id"] + "/engine.md"
        # _material has already verified any sidecar's immutable same-commit
        # history. Include that authority when callers inspect recovery alone.
        if git.path(engine_path).is_file():
            snapshots.append({"path": engine_path, "commit_sha": attestation["started"]["commit_sha"],
                              "sha256": _hash(_committed(git, engine_path))})
        if context["contract"] == CONTRACT_V2:
            snapshots.append(deepcopy(material["rebuild_context"]))
        return snapshots
    except (OSError, KeyError, TypeError, ValueError, AttributeError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _fail()
