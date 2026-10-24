"""Explicit sources → fixed candidate → Review → conditional publication.

Program, rules, source, candidate and Review versions are separate bindings. No
Legacy create-only runs and bounded incremental updates keep separate contracts.
"""

import copy
import hashlib
import json
import math
import re
import uuid
from datetime import date, datetime
from pathlib import Path

from .contracts import CaptureError, CONTINUOUS_SCHEMA_VERSION, DECOMPOSED_SCHEMA_VERSION, M1_SCHEMA_VERSION, MULTISOURCE_PIPELINE_SCHEMA_VERSION, ORCHESTRATED_SCHEMA_VERSION, REVISION_SCHEMA_VERSION, QUALITY_SCHEMA_VERSION, PIPELINE_SCHEMA_VERSION, canonical, markdown, parse_markdown, source_contract, timestamp
from .domains import DOMAIN_LABELS, DOMAIN_POLICY_VERSION, domain_policy
from .model_calls import ModelCallError, USAGE_FIELDS, call_model
from .transaction import PipelineGit

PIPELINE_SCHEMA = PIPELINE_SCHEMA_VERSION
MULTISOURCE_PIPELINE_SCHEMA = MULTISOURCE_PIPELINE_SCHEMA_VERSION
MAX_SOURCES = 8
MAX_OBJECT_SOURCE_REFS = 256
SOURCE_RE = re.compile(r"memory/sources/(conversations|inbox)/(src-[0-9a-f]{64})/(sha256-[0-9a-f]{64})\.md\Z")
RUN_RE = re.compile(r"run-[0-9a-f]{32}\Z")
ID_RE = re.compile(r"[a-z][a-z0-9-]{0,95}\Z")
CONTROL_PATHS = ("memory/controls", "memory/runs/controls", "memory/runs/deletions", "memory/runs/corrections", "memory/pending.md")
MAX_SAVED_BYTES = 2 * 1024 * 1024
SAFE_REVIEW_PRELAUNCH_ERRORS = frozenset({
    "codex_not_found", "invalid_request", "invalid_timeout", "invalid_payload_or_prompt", "input_limit",
    "publication_paused", "remote_unconfirmed", "remote_head_changed", "remote_sync_required",
})
REVIEW_STRATEGIES = frozenset({"legacy-v1", "auto", "decomposed-v1"})


def _error(code="invalid_pipeline_data"):
    raise CaptureError(code, "加工版本或证据校验未通过；保留已有资料，核对后重试")


def _hash(value):
    return "sha256-" + hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def _parse_saved(value):
    metadata, body = parse_markdown(value)
    # The foundation parser preserves raw body bytes; this stricter consumer also
    # rejects duplicate keys and non-JSON numeric constants in persisted headers.
    json.loads(value.decode("utf-8")[4:].split("\n---\n", 1)[0],
               object_pairs_hook=_unique_pairs, parse_constant=lambda item: _error())
    return metadata, body


def _load(value, kind, schema_version=None):
    if len(value) > MAX_SAVED_BYTES:
        _error()
    metadata, body = _parse_saved(value)
    try:
        result = json.loads(body, object_pairs_hook=_unique_pairs,
                            parse_constant=lambda value: _error())
    except (ValueError, TypeError, RecursionError):
        _error()
    supported = (PIPELINE_SCHEMA, MULTISOURCE_PIPELINE_SCHEMA, M1_SCHEMA_VERSION, CONTINUOUS_SCHEMA_VERSION, ORCHESTRATED_SCHEMA_VERSION, DECOMPOSED_SCHEMA_VERSION, REVISION_SCHEMA_VERSION, QUALITY_SCHEMA_VERSION) if schema_version is None else (schema_version,)
    if metadata.get("schema_version") not in supported or metadata.get("kind") != kind or not isinstance(result, dict):
        _error()
    if metadata.get("run_id") != result.get("run_id"):
        _error()
    return result


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _error()
        value[key] = item
    return value


def _document(kind, value, schema_version=PIPELINE_SCHEMA):
    return markdown({"schema_version": schema_version, "kind": kind, "run_id": value["run_id"]}, canonical(value).decode() + "\n")


def _run_path(run_id, name):
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        _error("invalid_run_id")
    return f"memory/runs/processes/{run_id}/{name}.md"


def _stage_path(run_id):
    _run_path(run_id, "review")
    return f"memory/staging/{run_id}.md"


def _committed(git, path, commit=None):
    snapshot = commit or git.head()
    data = git.read(snapshot, path)
    current = git.path(path)
    if not current.is_file() or current.read_bytes() != data:
        _error("saved_bytes_changed")
    return data


def _ancestor(git, older, newer):
    if not isinstance(older, str) or not re.fullmatch(r"[0-9a-f]{40,64}", older):
        _error("invalid_commit")
    git.run("merge-base", "--is-ancestor", older, newer)


def _last_commit(git, snapshot, path):
    return git.text("log", "-1", "--format=%H", snapshot, "--", path)


def _deleted_after_publication(git, path, origin, head):
    """Reject deletion of this published version, including later restoration.

    A fresh, independently reviewed create may be merged with an older cleanup
    branch. Deletions outside this object's publication ancestry do not delete
    that new version. Full history and merge diffs retain actual later deletions.
    Source and object controls are checked separately and are not scoped away.
    """
    return bool(git.text("log", "--full-history", "--ancestry-path", "--no-renames",
                         "-m", "--format=%H", "--diff-filter=D",
                         f"{origin}..{head}", "--", path))


def control_guard(git, commit=None):
    """Validate known controls; unknown control formats remain a hard stop."""
    from .controls import read_controls
    from .pending import read_registry
    read_registry(git.repo)
    return read_controls(git.repo, at_commit=commit) if commit is not None else read_controls(git.repo)


def load_source(repo, source_path, commit=None, *, purpose="current"):
    """Read one selected source, validated against its immutable capture receipt.

    Historical reads still require the source and receipt to remain present and
    unmodified in the current checkout; deleted sources are never resurrected.
    """
    if not isinstance(source_path, str) or not SOURCE_RE.fullmatch(source_path):
        _error("path_rejected")
    git = PipelineGit(repo)
    snapshot = commit or git.head()
    _ancestor(git, snapshot, git.head())
    from .controls import source_policy
    current_controls = control_guard(git)
    if source_policy(current_controls, source_path, purpose=purpose)["excluded"]:
        _error("source_excluded")
    match = SOURCE_RE.fullmatch(source_path)
    source_id, version = match.group(2), match.group(3)
    receipt_path = f"memory/runs/captures/{source_id}/{version}.md"
    for path in (source_path, receipt_path):
        if git.text("log", "--format=%H", "--diff-filter=D", "HEAD", "--", path):
            _error("source_deleted")
    source_bytes = _committed(git, source_path, snapshot)
    receipt_bytes = _committed(git, receipt_path, snapshot)
    metadata, body = _parse_saved(source_bytes)
    receipt, receipt_body = _parse_saved(receipt_bytes)
    payload = {key: metadata.get(key) for key in ("source_key", "source_type", "platform", "original_url", "acquisition_status", "original_time")}
    payload["body"] = body
    _, actual_id, actual_version = source_contract(payload)
    folder = "conversations" if payload["source_type"] == "conversation" else "inbox"
    if (actual_id != source_id or actual_version != version or match.group(1) != folder
            or metadata.get("schema_version") != "0.1" or metadata.get("source_id") != source_id
            or metadata.get("source_version") != version or receipt_body):
        _error("source_mismatch")
    source_commit = receipt.get("commit_sha")
    _ancestor(git, source_commit, snapshot)
    expected = {"schema_version": "0.1", "source_id": source_id, "source_version": version,
                "path": source_path, "commit_sha": source_commit, "save_result": "local_saved"}
    if any(receipt.get(key) != value for key, value in expected.items()):
        _error("receipt_mismatch")
    if _last_commit(git, snapshot, source_path) != source_commit or git.read(source_commit, source_path) != source_bytes:
        _error("source_mismatch")
    result = {"source_id": source_id, "source_version": version, "path": source_path,
            "commit_sha": source_commit, "body": body, "metadata": metadata,
            "source_bytes_sha256": _hash(source_bytes), "receipt_path": receipt_path,
            "receipt_sha256": _hash(receipt_bytes)}
    partial = [event for event in current_controls["events"] if event.get("scope") == "objects"
               and source_path in event["source_paths"] + event["replacement_source_paths"]]
    if partial:
        result["object_controls"] = {"digest": current_controls["digest"], "events": partial}
    return result


def _source_binding(source):
    return {key: source[key] for key in ("source_id", "source_version", "path", "commit_sha", "source_bytes_sha256", "receipt_path", "receipt_sha256")}


def normalize_source_paths(source_paths, *, allow_versions=False, max_sources=MAX_SOURCES):
    """Validate an explicit bounded selection without enumerating personal data."""
    if not isinstance(source_paths, (list, tuple)) or not source_paths:
        _error("invalid_source_selection")
    if type(max_sources) is not int or not 1 <= max_sources <= 256:
        _error("invalid_source_selection")
    if len(source_paths) > max_sources:
        _error("too_many_sources")
    selected, paths = {}, set()
    for path in source_paths:
        match = SOURCE_RE.fullmatch(path) if isinstance(path, str) else None
        if match is None:
            _error("path_rejected")
        source_id, version = match.group(2), match.group(3)
        if path in paths:
            _error("duplicate_source")
        paths.add(path)
        if source_id in selected and (not allow_versions or selected[source_id] == version):
            _error("duplicate_source" if selected[source_id] == version else "conflicting_source_versions")
        selected[source_id] = version
    return sorted(source_paths)


def _load_sources(repo, source_paths, *, allow_versions=False, purpose="current", max_sources=MAX_SOURCES):
    paths = normalize_source_paths(source_paths, allow_versions=allow_versions, max_sources=max_sources)
    if purpose == "current":
        return [load_source(repo, path) for path in paths]
    return [load_source(repo, path, purpose=purpose) for path in paths]


def _source_fields(sources):
    fields = {"source": _source_binding(sources[0])}
    if len(sources) > 1:
        fields["additional_sources"] = [_source_binding(source) for source in sources[1:]]
    return fields


def _pipeline_schema(candidate):
    if _quality_enabled(candidate):
        return QUALITY_SCHEMA_VERSION
    if "revision_context" in candidate:
        return REVISION_SCHEMA_VERSION
    if candidate.get("review_strategy", "legacy-v1") != "legacy-v1":
        return DECOMPOSED_SCHEMA_VERSION
    if "continuation_context" in candidate:
        return ORCHESTRATED_SCHEMA_VERSION
    if "batch_context" in candidate:
        return CONTINUOUS_SCHEMA_VERSION
    if candidate.get("mode") == "incremental":
        return M1_SCHEMA_VERSION
    return MULTISOURCE_PIPELINE_SCHEMA if "additional_sources" in candidate else PIPELINE_SCHEMA


def _quality_enabled(value):
    """A partial quality declaration cannot downgrade to the legacy contract."""
    fields = {"quality_contract", "skill_bundle", "quality_context"}
    if not fields.intersection(value):
        if "extraction_request_binding" in value:
            _error("invalid_quality_contract")
        return False
    if not fields <= set(value) or value.get("quality_contract") != "skill-quality-v1":
        _error("invalid_quality_contract")
    return True


def _validated_skill_bundle(bundle):
    from .skill_support import SkillError, validate_skill_bundle
    try:
        validate_skill_bundle(bundle)
    except (SkillError, TypeError, KeyError):
        _error("invalid_skill_bundle")
    return copy.deepcopy(bundle)


def _model_request_binding(role, payload):
    from .model_calls import request_binding
    try:
        return request_binding(role, payload)
    except (ValueError, TypeError, KeyError):
        _error("invalid_skill_request")


def _candidate_bindings(candidate):
    bindings = [candidate["source"]]
    if "additional_sources" in candidate:
        additional = candidate["additional_sources"]
        if not isinstance(additional, list) or not additional:
            _error("source_mismatch")
        bindings.extend(additional)
    return bindings


def _candidate_sources(git, candidate, *, historical=False):
    bindings = _candidate_bindings(candidate)
    sources = _load_sources(git.repo, [binding["path"] for binding in bindings],
                            allow_versions=candidate.get("mode") == "incremental",
                            purpose="history" if historical else "current",
                            max_sources=256 if "batch_context" in candidate else MAX_SOURCES)
    if [_source_binding(source) for source in sources] != bindings:
        _error("source_mismatch")
    return sources


def execution_versions(git):
    """Bind actual loaded program/rules bytes, separately from evolving data HEAD."""
    package = Path(__file__).resolve().parent
    project = package.parents[1]
    code = {}
    for path in sorted(package.glob("*.py")):
        code["pdm/" + path.name] = _hash(path.read_bytes())
    prompts = {}
    for role in ("extract", "review"):
        path = project / "prompts" / (role + ".md")
        if not path.is_file() or path.is_symlink():
            _error("missing_rules")
        prompts[role] = _hash(path.read_bytes())
    policy_digest = _hash(domain_policy())
    skills = {path.relative_to(project / "skills").as_posix(): _hash(path.read_bytes())
              for path in sorted((project / "skills").rglob("*")) if path.is_file() and not path.is_symlink()}
    return {"program_sha256": _hash(code), "rules_version": _hash({**prompts, "domain_policy": policy_digest, "skills": skills}),
            "domain_policy_version": DOMAIN_POLICY_VERSION, "domain_policy_sha256": policy_digest,
            "program_commit": git.text("log", "-1", "--format=%H", "--", "project/src/pdm", "project/prompts", "project/skills", "pyproject.toml") or None}


def _string(value, maximum=65536):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _error("invalid_model_output")
    return value


def _refs(refs, sources):
    if not isinstance(refs, list) or not 1 <= len(refs) <= MAX_OBJECT_SOURCE_REFS:
        _error("invalid_model_output")
    selected = {(source["source_id"], source["source_version"]): source for source in sources}
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"source_id", "source_version", "locator", "quote"}:
            _error("invalid_model_output")
        if not isinstance(ref["source_id"], str) or not isinstance(ref["source_version"], str):
            _error("source_reference_mismatch")
        source = selected.get((ref["source_id"], ref["source_version"]))
        if source is None:
            _error("source_reference_mismatch")
        lines = source["body"].splitlines(keepends=True)
        match = re.fullmatch(r"L([1-9][0-9]*)-L([1-9][0-9]*)", ref["locator"] if isinstance(ref["locator"], str) else "")
        if not match:
            _error("invalid_locator")
        start, end = map(int, match.groups())
        if not 1 <= start <= end <= len(lines) or _string(ref["quote"]) not in "".join(lines[start - 1:end]):
            _error("invalid_quote")


def _time(value):
    if value is not None:
        _string(value, 64)
        try:
            if re.fullmatch(r"\d{4}-\d{2}", value):
                date.fromisoformat(value + "-01")
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                date.fromisoformat(value)
            else:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError
        except ValueError:
            _error("invalid_model_output")


def _objects(output, sources, run_id, response, versions, created, *, batch=False, quality_context=None):
    fields = {"objects", "wikis"} if batch else {"objects", "wiki"}
    if quality_context is not None:
        from .knowledge_quality import validate_quality_output
        if not isinstance(output, dict) or set(output) != fields | {"quality"}:
            _error("invalid_model_output")
        validate_quality_output(output, quality_context)
        output = {key: value for key, value in output.items() if key != "quality"}
    if not isinstance(output, dict) or set(output) != fields or not isinstance(output["objects"], list) or not 1 <= len(output["objects"]) <= 20:
        _error("invalid_model_output")
    result, ids = [], set()
    record_fields = {"id", "type", "title", "source_kind", "sources", "domains", "valid_from", "valid_to", "verification_status", "lifecycle_status", "body"}
    for value in output["objects"]:
        if not isinstance(value, dict) or set(value) != record_fields:
            _error("invalid_model_output")
        if value["type"] not in ("knowledge", "state", "event", "goal", "decision") or value["source_kind"] not in ("user_statement", "external_source", "model_inference"):
            _error("invalid_model_output")
        if value["verification_status"] not in ("unverified", "supported", "contested", "undetermined") or value["lifecycle_status"] not in ("undetermined", "current", "historical", "superseded", "retracted"):
            _error("invalid_model_output")
        if not isinstance(value["domains"], list) or len(value["domains"]) > 20:
            _error("invalid_model_output")
        for domain in value["domains"]:
            _string(domain, 128)
        _time(value["valid_from"])
        _time(value["valid_to"])
        material = {key: value[key] for key in value if key != "body"}
        result.append(_object(material, value["body"], "records", sources, ids, run_id, response, versions, created))
    wikis = output["wikis"] if batch else [output["wiki"]]
    if not isinstance(wikis, list) or not 1 <= len(wikis) <= 20:
        _error("invalid_model_output")
    records = {obj["metadata"]["id"]: obj for obj in result}
    for wiki in wikis:
        if not isinstance(wiki, dict) or set(wiki) != ({"id", "title", "body", "sources", "record_ids"} if batch else {"id", "title", "body", "sources"}):
            _error("invalid_model_output")
        selected = wiki["record_ids"] if batch else list(records)
        if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected) or any(key not in records for key in selected):
            _error("invalid_wiki_dependency")
        material = {key: wiki[key] for key in wiki if key not in {"body", "record_ids"}}
        material.update(type="wiki", records=[{"id": key, "content_version": records[key]["metadata"]["content_version"]} for key in selected])
        result.append(_object(material, wiki["body"], "topics", sources, ids, run_id, response, versions, created))
    return result


def _object(material, body, folder, sources, ids, run_id, response, versions, created):
    identifier = material.get("id")
    if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier) or identifier in ids:
        _error("invalid_object_id")
    ids.add(identifier)
    _string(material.get("title"), 512)
    _string(body)
    _refs(material.get("sources"), sources)
    version = _hash({"metadata": material, "body": body})
    meta = {**material, "schema_version": PIPELINE_SCHEMA, "content_version": version,
            "recorded_at": created, "generated_by": {"run_id": run_id, "model": response["model"], "rules_version": versions["rules_version"]},
            "review_status": "pending", "review_ref": None}
    return {"path": f"memory/{folder}/{identifier}.md", "metadata": meta, "body": body}


def _current_object_corrections(objects, sources):
    # Apply today's controls only to newly generated output, never while
    # reconstructing an already-reviewed historical candidate.
    from .controls import source_locator_policy
    selected = {(source["source_id"], source["source_version"]): source for source in sources}
    for obj in objects:
        refs = obj["metadata"]["sources"]
        cited = {(ref["source_id"], ref["source_version"]) for ref in refs}
        for ref in refs:
            source = selected[(ref["source_id"], ref["source_version"])]
            controls = source.get("object_controls")
            if controls is None:
                continue
            policy = source_locator_policy(controls, source["path"], ref["locator"])
            if policy["excluded"]:
                replacements = {path for event in controls["events"] if event["control_id"] in policy["control_ids"]
                                for path in event["replacement_source_paths"]}
                replacement_refs = {(SOURCE_RE.fullmatch(path).group(2), SOURCE_RE.fullmatch(path).group(3)) for path in replacements}
                if not cited.intersection(replacement_refs):
                    _error("correction_evidence_missing")


def _validate_domain_policy(versions):
    """Old candidates retain their original free-label contract."""
    if "domain_policy_version" not in versions and "domain_policy_sha256" not in versions:
        return False
    if (versions.get("domain_policy_version") != DOMAIN_POLICY_VERSION
            or versions.get("domain_policy_sha256") != _hash(domain_policy())):
        _error("invalid_domain_policy")
    return True


def _validate_domains(objects, related, operations, versions):
    if not _validate_domain_policy(versions):
        return
    previous = {document["path"]: document for item in related for document in item["documents"]}
    generated = {"schema_version", "content_version", "recorded_at", "generated_by", "review_status", "review_ref"}
    for obj in objects:
        metadata = obj["metadata"]
        if operations.get(obj["path"], {}).get("action") == "reuse":
            old = previous.get(obj["path"])
            if (old is None or obj["body"] != old["body"]
                    or metadata["content_version"] != old["content_version"]
                    or {key: value for key, value in metadata.items() if key not in generated}
                       != {key: value for key, value in old["metadata"].items() if key not in generated}):
                _error("invalid_domain_reuse")
            continue
        if metadata["type"] == "wiki":
            continue
        labels = metadata["domains"]
        if len(labels) != len(set(labels)) or any(label not in DOMAIN_LABELS for label in labels):
            _error("invalid_domains")


def _call_metadata(response):
    if not isinstance(response, dict) or not isinstance(response.get("output"), dict):
        _error("invalid_model_output")
    invocation = response.get("invocation_id")
    if not isinstance(invocation, str) or not re.fullmatch(r"model-[0-9a-f]{32}", invocation):
        _error("invalid_model_invocation")
    model = response.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model):
        _error("invalid_model_invocation")
    elapsed = response.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or isinstance(elapsed, bool) or not math.isfinite(elapsed) or not 0 <= elapsed <= 86400:
        _error("invalid_model_invocation")
    usage = response.get("usage")
    if usage is not None:
        if (not isinstance(usage, dict) or set(usage) - USAGE_FIELDS
                or any(type(value) is not int or value < 0 for value in usage.values())):
            _error("invalid_model_invocation")
    return {"invocation_id": invocation, "model": model, "elapsed_seconds": elapsed, "usage": usage,
            "output_sha256": _hash(response["output"])}


def _review_output(output, *, required_checks=None, quality=False):
    quality = quality or required_checks is not None
    fields = {"verdict", "reasons", "checks"} if quality else {"verdict", "reasons"}
    if (not isinstance(output, dict) or set(output) != fields
            or output["verdict"] not in ("passed", "needs_revision", "needs_confirmation")
            or not isinstance(output["reasons"], list) or not 1 <= len(output["reasons"]) <= 32):
        _error("invalid_model_output")
    for reason in output["reasons"]:
        _string(reason, 4096)
    if quality:
        if not isinstance(output["checks"], list):
            _error("invalid_model_output")
        if required_checks is not None:
            from .knowledge_quality import validate_quality_review
            validate_quality_review(output, required_checks)


def _candidate_binding(candidate):
    return _hash({key: value for key, value in candidate.items() if key != "binding"})


def _targets_clear(git, objects):
    for obj in objects:
        path = obj["path"]
        if git.path(path).exists() or git.text("log", "-1", "--format=%H", "--", path):
            _error("target_exists_or_deleted")


def _execution_guard(repo, context, *, run_id=None, source_paths=None, related_runs=None):
    from .scheduler import assert_publication_allowed
    if context is not None:
        fields = {"schedule_id", "tick_key", "attempt_id", "run_id", "reservation_path", "reservation_sha256"}
        if not isinstance(context, dict) or set(context) != fields or any(not isinstance(value, str) or not value for value in context.values()):
            _error("invalid_execution_context")
        if run_id is not None and context["run_id"] != run_id:
            _error("invalid_execution_context")
    reservation = assert_publication_allowed(repo, context)
    if context is not None and isinstance(reservation, dict):
        if source_paths is not None and "source_paths" in reservation and sorted(reservation["source_paths"]) != source_paths:
            _error("invalid_execution_context")
        if related_runs is not None and "related_runs" in reservation and sorted(reservation["related_runs"]) != related_runs:
            _error("invalid_execution_context")


def _related_run_ids(related_runs, *, max_runs=8):
    if type(max_runs) is not int or not 1 <= max_runs <= 256:
        _error("invalid_related_runs")
    if not isinstance(related_runs, (list, tuple)) or len(related_runs) > max_runs:
        _error("invalid_related_runs")
    for run in related_runs:
        _run_path(run, "manifest")
    if len(set(related_runs)) != len(related_runs):
        _error("invalid_related_runs")
    return sorted(related_runs)


def _related_context(git, related_runs, sources, *, at_commit=None, visited=(), max_runs=8):
    """Check every old run's source scope before opening any old source body."""
    related_runs = _related_run_ids(related_runs, max_runs=max_runs)
    scope = {source["path"] for source in sources}
    scopes = {}
    for run in related_runs:
        manifest_path = _run_path(run, "manifest")
        meta, body = _parse_saved(_committed(git, manifest_path))
        if meta.get("kind") == "batch_group_manifest":
            manifest = json.loads(body, object_pairs_hook=_unique_pairs)
            bindings = [manifest["source"], *manifest.get("additional_sources", [])]
            paths = normalize_source_paths([binding["path"] for binding in bindings], allow_versions=True, max_sources=256)
        else:
            candidate = _load(_committed(git, _stage_path(run)), "candidate")
            if candidate.get("run_id") != run or candidate.get("binding") != _candidate_binding(candidate):
                _error("candidate_mismatch")
            paths = normalize_source_paths([binding["path"] for binding in _candidate_bindings(candidate)], allow_versions=True, max_sources=256 if "batch_context" in candidate else MAX_SOURCES)
        if not set(paths).issubset(scope):
            _error("evidence_source_mismatch")
        scopes[run] = paths
    result, seen = [], set()
    snapshot = at_commit or git.head()
    from .controls import object_policy
    controls = control_guard(git)
    for run in related_runs:
        publication = verify_publication(git.repo, run, source_paths=scopes[run], historical=True, _visited=visited)
        _ancestor(git, publication["commit_sha"], snapshot)
        documents = []
        for path, digest in sorted(publication["files"].items()):
            if path in seen:
                _error("conflicting_related_versions")
            seen.add(path)
            if object_policy(controls, path, purpose="history")["excluded"]:
                _error("object_excluded")
            data = git.read(publication["commit_sha"], path)
            if _hash(data) != digest or git.read(snapshot, path) != data:
                _error("related_version_changed")
            if at_commit is None and _committed(git, path) != data:
                _error("related_version_changed")
            metadata, body = _parse_saved(data)
            documents.append({"path": path, "sha256": digest, "content_version": metadata.get("content_version"),
                              "metadata": metadata, "body": body})
        result.append({"run_id": run, "commit_sha": publication["commit_sha"],
                       "manifest_path": publication["manifest_path"], "documents": documents})
    return result


def _updated_objects(git, objects, related, *, check_current=False):
    known = {document["path"]: document for item in related for document in item["documents"]}
    if check_current:
        returned = {obj["path"] for obj in objects}
        old_records = {path for path in known if path.startswith("memory/records/")}
        if not old_records.issubset(returned):
            _error("related_records_omitted")
        changed_ids = {obj["metadata"]["id"] for obj in objects if obj["path"] in old_records
                       and obj["metadata"]["content_version"] != known[obj["path"]]["content_version"]}
        if changed_ids:
            from .controls import _metadata_only
            topics = git.run("ls-tree", "-r", "--name-only", git.head(), "--", "memory/topics").decode().splitlines()
            if len(topics) > 1000:
                _error("related_context_limit")
            for path in topics:
                if Path(path).name in {"README.md", ".gitkeep"}:
                    continue
                if not re.fullmatch(r"memory/topics/[a-z][a-z0-9-]{0,95}\.md", path):
                    _error("path_rejected")
                metadata = _metadata_only(git, path)
                refs = metadata.get("records", [])
                if not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs):
                    _error("invalid_related_context")
                if any(ref.get("id") in changed_ids for ref in refs) and (path not in known or path not in returned):
                    _error("required_related_context")
    operations = {}
    from .controls import object_policy
    controls = control_guard(git)
    for obj in objects:
        path = obj["path"]
        policy = object_policy(controls, path, purpose="history")
        if policy["excluded"]:
            _error("object_excluded")
        if check_current and object_policy(controls, path, content_version=obj["metadata"]["content_version"])["excluded"]:
            _error("object_excluded")
        if check_current and policy.get("replacement_paths"):
            replacement_refs = {(SOURCE_RE.fullmatch(item).group(2), SOURCE_RE.fullmatch(item).group(3))
                                for item in policy["replacement_paths"]}
            if not replacement_refs.intersection((ref["source_id"], ref["source_version"]) for ref in obj["metadata"]["sources"]):
                _error("correction_evidence_missing")
        previous = known.get(path)
        if previous is None:
            if check_current:
                _targets_clear(git, [obj])
            operations[path] = {"action": "create", "before_sha256": None, "before_content_version": None}
        else:
            if obj["metadata"]["id"] != previous["metadata"].get("id") or obj["metadata"]["type"] != previous["metadata"].get("type"):
                _error("related_identity_changed")
            obj["metadata"]["recorded_at"] = previous["metadata"]["recorded_at"]
            action = "reuse" if obj["metadata"]["content_version"] == previous["content_version"] else "update"
            operations[path] = {"action": action, "before_sha256": previous["sha256"],
                                "before_content_version": previous["content_version"]}
    return operations


def _merge_candidates(git, run_ids, source_paths, *, visited=()):
    if (not isinstance(run_ids, (list, tuple)) or len(run_ids) > 256
            or len(set(run_ids)) != len(run_ids) or len(visited) >= 16):
        _error("invalid_merge_context")
    result = []
    for run_id in sorted(run_ids):
        _run_path(run_id, "review")
        if run_id in visited:
            _error("publication_dependency_cycle")
        stage = _load(_committed(git, _stage_path(run_id)), "candidate")
        if not {binding["path"] for binding in _candidate_bindings(stage)} <= set(source_paths):
            _error("evidence_source_mismatch")
        candidate, review, _ = _read_reviewed(git, run_id, historical=True, visited=(*visited, run_id))
        result.append({"run_id": run_id, "binding": candidate["binding"], "objects": candidate["objects"],
                       "review_id": review["review_id"], "batch_context": candidate.get("batch_context")})
    return result


def _reused_extraction(git, run_id, source_fields, versions, controls_digest, source_views, batch_material,
                       continuation_context=None, review_strategy="legacy-v1", revision_context=None,
                       skill_bundle=None, quality_context=None):
    """Reuse a valid extraction only when Review demonstrably never started.

    A semantic Review rejection and any uncertain Review are deliberately not
    eligible. The caller's durable operation log must also report zero Review
    starts before choosing this path.
    """
    stage = _stage_path(run_id)
    data = _committed(git, stage)
    old = _load(data, "candidate")
    failure = _load(_committed(git, _run_path(run_id, "failure")), "failure")
    if (failure.get("phase") != "review" or failure.get("error") not in SAFE_REVIEW_PRELAUNCH_ERRORS
            or git.path(_run_path(run_id, "review")).exists()
            or old.get("binding") != _candidate_binding(old) or old.get("versions") != versions
            or old.get("controls_digest") != controls_digest or old.get("source_views") != source_views
            or old.get("batch_material") != batch_material
            or old.get("continuation_context") != continuation_context
            or old.get("revision_context") != revision_context
            or old.get("skill_bundle") != skill_bundle or old.get("quality_context") != quality_context
            or old.get("review_strategy", "legacy-v1") != review_strategy
            or {key: old[key] for key in source_fields} != source_fields):
        _error("extraction_not_reusable")
    sources = _candidate_sources(git, old, historical=True)
    _validate_candidate_quality(git, old, sources)
    extraction = _call_metadata({**old["extraction"], "output": old["extraction_output"]})
    expected = _objects(old["extraction_output"], sources, run_id, extraction, versions, old["created_at"],
                        batch=True, quality_context=old.get("quality_context"))
    _updated_objects(git, expected, old["related"])
    if old["objects"] != expected:
        _error("candidate_mismatch")
    return {**extraction, "output": old["extraction_output"]}, {"run_id": run_id, "binding": old["binding"], "stage_sha256": _hash(data)}


def _processing_key(source_fields, versions, controls_digest, batch_context=None, batch_material=None,
                    continuation_context=None, review_strategy="legacy-v1", revision_context=None, revision_resume=None,
                    revision_revalidation=None, skill_bundle=None):
    bindings = [source_fields["source"], *source_fields.get("additional_sources", [])]
    material = {"sources": [{"source_id": item["source_id"], "source_version": item["source_version"]} for item in bindings],
                "rules_version": versions["rules_version"], "controls_digest": controls_digest}
    if batch_context is not None:
        material["batch_context"] = batch_context
        material["batch_material"] = batch_material
    if continuation_context is not None:
        material["continuation_context"] = continuation_context
    if review_strategy != "legacy-v1":
        material["review_strategy"] = review_strategy
    if revision_context is not None:
        material["revision_context"] = revision_context
    if revision_resume is not None:
        material["revision_resume"] = {key: revision_resume[key] for key in ("contract", "expected_reservation_sha256")}
    if revision_revalidation is not None:
        material["revision_revalidation"] = {key: revision_revalidation[key] for key in ("contract", "expected_attestation_sha256")}
    if skill_bundle is not None:
        material["skill_bundle_sha256"] = skill_bundle["sha256"]
    return _hash(material)


def _assert_substantive_revision(candidate):
    """A new invocation or timestamp is not a revision of rejected knowledge."""
    if "revision_context" not in candidate:
        return
    previous = candidate["revision_context"]["candidate"]
    dynamic = {"generated_by", "recorded_at", "review_status", "review_ref"}
    def material(objects):
        return {obj["path"]: {"body": obj["body"], "metadata": {
                    key: value for key, value in obj["metadata"].items() if key not in dynamic}}
                for obj in objects}
    if (material(candidate["objects"]) == material(previous["objects"])
            and candidate.get("operations", {}) == previous.get("operations", {})
            and candidate.get("extraction_output", {}).get("quality")
                == previous.get("extraction_output", {}).get("quality")):
        _error("revision_unchanged")


def _revision_attempt_path(context):
    # Stable across new code/rules/queue names: an unknown launch cannot gain a
    # fresh identity by changing extraction material or starting another run.
    return "memory/runs/processes/revision-attempts/" + _hash(context) + ".md"


def _existing_revision_attempt(git, context, source_paths, related_runs):
    path = _revision_attempt_path(context)
    if not git.path(path).exists():
        if _last_commit(git, git.head(), path):
            _error("revision_attempt_deleted")
        return None
    value = _load(_committed(git, path), "revision_attempt", REVISION_SCHEMA_VERSION)
    run = value.get("run_id")
    if value != {"run_id": run, "revision_context_sha256": _hash(context),
            "source_paths": source_paths, "related_runs": related_runs,
            "processing_key": value.get("processing_key")}:
        _error("revision_attempt_mismatch")
    _run_path(run, "review")
    started = _load(_committed(git, _run_path(run, "started")), "processing_attempt", M1_SCHEMA_VERSION)
    if (started.get("processing_key") != value["processing_key"]
            or started.get("source_paths") != source_paths or started.get("related_runs") != related_runs
            or _last_commit(git, git.head(), _run_path(run, "started")) != _last_commit(git, git.head(), path)):
        _error("revision_attempt_mismatch")
    result = {"run_id": run, "head": git.head(), "model_calls": 0, "attempted_calls": 0, "reused": True}
    review_path = _run_path(run, "review")
    if git.path(review_path).is_file():
        candidate, _, _, _ = _read_fixed_candidate(git, run, source_paths=source_paths, historical=True)
        if candidate.get("revision_context") != context:
            _error("revision_attempt_mismatch")
        _, review, _ = _read_reviewed(git, run, source_paths=source_paths, historical=True,
                                      allow_nonpassed=True)
        return {**result, "status": review["output"]["verdict"], "review_id": review["review_id"],
                "stage_path": _stage_path(run), "review_path": review_path}
    failure_path = _run_path(run, "failure")
    if git.path(failure_path).is_file():
        failure = _load(_committed(git, failure_path), "failure")
        metadata, _ = _parse_saved(_committed(git, failure_path))
        if metadata.get("schema_version") not in {REVISION_SCHEMA_VERSION, QUALITY_SCHEMA_VERSION}:
            _error("revision_attempt_mismatch")
        return {**result, "status": "failed", "error": failure["error"], "next_action": "operator_reconcile"}
    return {**result, "status": "unknown", "next_action": "reconcile"}


def _processed_path(key):
    if not isinstance(key, str) or not re.fullmatch(r"sha256-[0-9a-f]{64}", key):
        _error("invalid_processing_key")
    return f"memory/runs/processes/processed/{key}.md"


def _existing_processing(git, key, source_paths, related_runs):
    path = _processed_path(key)
    if not git.path(path).exists():
        if _last_commit(git, git.head(), path):
            _error("processing_marker_deleted")
        return None
    marker = _load(_committed(git, path), "processed_inputs", M1_SCHEMA_VERSION)
    if marker != {"run_id": marker.get("run_id"), "processing_key": key,
                  "manifest_path": _run_path(marker.get("run_id"), "manifest")}:
        _error("processing_marker_mismatch")
    publication = verify_publication(git.repo, marker["run_id"], source_paths=source_paths, historical=True)
    if publication.get("processing_key") != key or _last_commit(git, git.head(), path) != publication["commit_sha"]:
        _error("processing_marker_mismatch")
    original = _load(_committed(git, _stage_path(marker["run_id"])), "candidate")
    covered = {obj["path"] for obj in original["objects"]}
    covered.update(document["path"] for related in original["related"] for document in related["documents"])
    related_scopes = {}
    for run in related_runs:
        requested = _load(_committed(git, _stage_path(run)), "candidate")
        if requested.get("run_id") != run or requested.get("binding") != _candidate_binding(requested):
            _error("candidate_mismatch")
        paths = normalize_source_paths([binding["path"] for binding in _candidate_bindings(requested)], allow_versions=True, max_sources=256 if "batch_context" in requested else MAX_SOURCES)
        if not set(paths).issubset(source_paths):
            _error("evidence_source_mismatch")
        related_scopes[run] = paths
    for run, paths in related_scopes.items():
        selected = publication if run == marker["run_id"] else verify_publication(git.repo, run, source_paths=paths, historical=True)
        if not set(selected["files"]).issubset(covered):
            _error("processing_context_changed")
    return {"status": "already_processed", "run_id": marker["run_id"], "processing_key": key,
            "publication": publication, "head": git.head()}


def _failure_call(error, response):
    response = response if isinstance(response, dict) else {}
    invocation = getattr(error, "invocation_id", None) or response.get("invocation_id")
    elapsed = getattr(error, "elapsed_seconds", None)
    if elapsed is None:
        elapsed = response.get("elapsed_seconds")
    model = response.get("model")
    return {
        "invocation_id": invocation if isinstance(invocation, str) and re.fullmatch(r"model-[0-9a-f]{32}", invocation) else None,
        "elapsed_seconds": elapsed if type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 86400 else None,
        "model": model if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", model) else "unknown",
    }


def lookup_processed(repo, source_paths, expected_head=None, *, related_runs=(), skill_bundle=None):
    """Verify an existing incremental publication without reserving model calls."""
    source_paths = normalize_source_paths(source_paths, allow_versions=True)
    related_runs = _related_run_ids(related_runs)
    git = PipelineGit(repo)
    head = git.check(expected_head)
    sources = _load_sources(repo, source_paths, allow_versions=True, purpose="history")
    controls = control_guard(git)
    if skill_bundle is not None:
        skill_bundle = _validated_skill_bundle(skill_bundle)
    key = _processing_key(_source_fields(sources), execution_versions(git), controls["digest"], skill_bundle=skill_bundle)
    existing = _existing_processing(git, key, source_paths, related_runs)
    git.check(head)
    return existing


def _budget_adapter(model_call):
    from .budget import BudgetedModelCall
    adapter = model_call if isinstance(model_call, BudgetedModelCall) else getattr(model_call, "_budget_adapter", None)
    return adapter if isinstance(adapter, BudgetedModelCall) else None


def _assert_revalidation_operation(candidate, model_call):
    context = candidate.get("revision_revalidation")
    resume = candidate.get("revision_resume")
    if context is None and resume is None:
        return
    from .revision_resume import _adapter, _state
    budget = context["attestation"]["budget"] if context is not None else resume["reservation"]["budget"]
    completed = budget["completed_call"] if context is not None else resume["extraction_audit"]["call"]
    operation = _state(_adapter(model_call), budget)
    if operation["calls"].get("extract") != completed:
        _error("completed_extraction_not_known_completed")


def _actual_calls(model_call):
    adapter = _budget_adapter(model_call)
    return adapter.observed_started_count if adapter is not None else None


def _reserve_processing_calls(model_call):
    from .budget import BudgetBlocked
    if model_call is call_model:
        raise BudgetBlocked("budget_binding_required")
    adapter = _budget_adapter(model_call)
    if adapter is not None:
        if getattr(model_call, "_budget_reserved_calls", 2) == 0:
            raise BudgetBlocked("budget_processing_mismatch", operation_id=adapter.operation_id)
        started = adapter.started_count
        if started:
            raise BudgetBlocked("budget_call_already_started", operation_id=adapter.operation_id,
                                started_count=started)
        adapter.reserve_calls(2)


def _failure(git, run_id, phase, code, head, call=None, schema_version=PIPELINE_SCHEMA, attempted_calls=None, failure_name="failure"):
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        code = "model_failed"
    value = {"run_id": run_id, "phase": phase, "status": "failed", "error": code,
             "call": call, "attempted_calls": attempted_calls if attempted_calls is not None else (1 if phase == "extract" else 2),
             "failed_at": timestamp()}
    path = _run_path(run_id, failure_name)
    final = git.write_files({path: _document("failure", value, schema_version)}, head, f"process: preserve {phase} failure")
    return {"run_id": run_id, "status": "failed", "error": code, "failure_path": path, "head": final}


def _confirmation_item(candidate, review):
    return {"question": "\n".join(review["output"]["reasons"]),
            "source_refs": sorted(({key: binding[key] for key in ("source_id", "source_version", "path")}
                                   for binding in _candidate_bindings(candidate)), key=lambda ref: ref["path"]),
            "object_refs": sorted(({"id": obj["metadata"]["id"], "content_version": obj["metadata"]["content_version"],
                                    "path": obj["path"]} for obj in candidate["objects"]), key=lambda ref: ref["path"]),
            "run_ids": [candidate["run_id"]]}


def _registration_marker(candidate, review, stage_bytes, review_bytes):
    return {"run_id": candidate["run_id"], "status": "registration_required",
            "source_paths": sorted(binding["path"] for binding in _candidate_bindings(candidate)),
            "candidate_binding": candidate["binding"], "stage_path": _stage_path(candidate["run_id"]),
            "stage_sha256": _hash(stage_bytes), "review_path": _run_path(candidate["run_id"], "review"),
            "review_sha256": _hash(review_bytes), "controls_digest": candidate["controls_digest"],
            "versions": copy.deepcopy(candidate["versions"]), "item_sha256": _hash(_confirmation_item(candidate, review)),
            "item_id": None}


def _resume_pending_registration(git, run_id, allowed_source_paths, *, expected_head=None):
    """Caller holds the repository lock. Known Review results never relaunch."""
    from .pending import _identity, pending_update, read_registry

    head = git.check(expected_head)
    path = _run_path(run_id, "pending-registration")
    marker_bytes = _committed(git, path)
    marker = _load(marker_bytes, "pending_registration", "0.7")
    stage_bytes = _committed(git, _stage_path(run_id))
    candidate = _load(stage_bytes, "candidate")
    # Authorize manifest-like bindings before any capture or source body read.
    maximum = 256 if "batch_context" in candidate else MAX_SOURCES
    bound = normalize_source_paths([binding["path"] for binding in _candidate_bindings(candidate)],
                                  allow_versions=True, max_sources=maximum)
    allowed = normalize_source_paths(allowed_source_paths, allow_versions=True, max_sources=256)
    if not set(bound) <= set(allowed):
        _error("pending_registration_scope")
    review_bytes = _committed(git, _run_path(run_id, "review"))
    review = _load(review_bytes, "review", _pipeline_schema(candidate))
    initial = _registration_marker(candidate, review, stage_bytes, review_bytes)
    if (set(marker) != set(initial) or marker.get("status") not in {"registration_required", "registered"}
            or any(marker.get(key) != value for key, value in initial.items() if key not in {"status", "item_id"})
            or marker["status"] == "registration_required" and marker["item_id"] is not None):
        _error("pending_registration_changed")
    review_commit = _last_commit(git, head, _run_path(run_id, "review"))
    # The initial durable marker was created with this exact immutable Review.
    if git.read(review_commit, path) != _document("pending_registration", initial, "0.7"):
        _error("pending_registration_changed")
    _read_reviewed(git, run_id, source_paths=bound, historical=True, allow_nonpassed=True)
    if review["output"]["verdict"] != "needs_confirmation":
        _error("pending_registration_verdict")
    if execution_versions(git) != candidate["versions"]:
        _error("processing_context_changed")
    if control_guard(git)["digest"] != candidate["controls_digest"]:
        _error("controls_changed")
    item = _confirmation_item(candidate, review)
    result = {"run_id": run_id, "stage_path": _stage_path(run_id), "review_path": _run_path(run_id, "review"),
              "pending_path": _run_path(run_id, "pending"), "registration_path": path,
              "review_verdict": "needs_confirmation", "model_calls": 0, "attempted_calls": 0}
    if marker["status"] == "registered":
        saved = next((saved for saved in read_registry(git.repo)["items"] if saved["item_id"] == marker["item_id"]), None)
        if saved is None or _identity(saved) != {key: item[key] for key in ("question", "source_refs", "object_refs")} or run_id not in saved["run_ids"]:
            _error("pending_registration_changed")
        return {**result, "status": "needs_confirmation", "registration_status": "registered",
                "registration_error": None, "item_id": marker["item_id"], "head": head}
    try:
        files, expected, saved = pending_update(git.repo, item, head=head)
        registered = {**marker, "status": "registered", "item_id": saved["item_id"]}
        files[path] = _document("pending_registration", registered, "0.7")
        expected[path] = marker_bytes
        final = git.write_changes(files, expected, head, "pending: register preserved independent Review")
    except (CaptureError, OSError) as error:
        # Do not retry a CAS conflict, reset HEAD, rewrite Review, or discard a
        # transaction journal. The durable required marker supports a later
        # explicit recovery, even when the registry or filesystem is full.
        code = error.code if isinstance(error, CaptureError) else "pending_registration_io"
        return {**result, "status": "registration_required", "registration_status": "registration_required",
                "registration_error": code, "error": code, "head": git.head()}
    return {**result, "status": "needs_confirmation", "registration_status": "registered",
            "registration_error": None, "item_id": saved["item_id"], "head": final}


def resume_pending_registration(repo, run_id, *, allowed_source_paths, expected_head=None):
    """Register a fixed needs-confirmation Review with zero model invocations."""
    _run_path(run_id, "pending-registration")
    git = PipelineGit(repo)
    with git.lock():
        git.recover()
        return _resume_pending_registration(git, run_id, allowed_source_paths, expected_head=expected_head)


def _validate_continuation_context(git, context, source_paths, related_runs, *, at_commit=None, draft_only=False):
    """Bind a question and reply to a real answered registry snapshot.

    Context is additional interpretation, never a new fact or authorization to
    open another source. Historical verification uses the candidate's original
    answered snapshot, so a later legitimate resolution cannot invalidate it.
    """
    from .context import _pending_origin
    from .evidence import _capture_metadata
    from .pending import (REGISTRY_PATH, _parse, _reply_association_commit,
                          _source_tuple, read_registry)

    fields = {"job_id", "parent_item_id", "question", "parent_run_ids", "source_refs", "object_refs", "reply_sources"}
    if not isinstance(context, dict) or set(context) != fields:
        _error("invalid_continuation_context")
    try:
        scoped = {_source_tuple(ref)[2] for ref in [*context["source_refs"], *context["reply_sources"]]}
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, CaptureError):
            raise
        _error("invalid_continuation_context")
    if not scoped <= set(source_paths):
        _error("continuation_source_scope")
    snapshot = at_commit or git.head()
    _ancestor(git, snapshot, git.head())
    registry = (_parse(git.read(snapshot, REGISTRY_PATH)) if at_commit is not None
                else read_registry(git.repo))
    item = next((item for item in registry["items"] if item["item_id"] == context["parent_item_id"]), None)
    if item is None or item["pending_status"] != "answered":
        _error("continuation_context_changed")
    identity = {"parent_item_id": item["item_id"], "question": item["question"],
                "parent_run_ids": item["run_ids"], "source_refs": item["source_refs"],
                "object_refs": item["object_refs"], "reply_sources": item["reply_sources"]}
    expected = {**identity, "job_id": "continuation-" + hashlib.sha256(canonical(identity)).hexdigest()}
    if context != expected:
        _error("continuation_context_changed")
    origin_related = set()
    for run in item["run_ids"]:
        origin_related.update(_pending_origin(git, item, run))
    if not draft_only and not origin_related <= set(related_runs):
        _error("continuation_related_scope")
    for ref in item["reply_sources"]:
        receipt = _capture_metadata(git, ref["path"], git.head())
        if receipt["commit_sha"] != ref["commit_sha"]:
            _error("continuation_reply_mismatch")
        associated = _reply_association_commit(git, item, ref)
        _ancestor(git, associated, snapshot)


def _candidate_payload(git, candidate, sources):
    """Rebuild the exact tool-free Review input from immutable candidate data."""
    payload = {"sources": [{"path": source["path"], "metadata": source["metadata"], "body": source["body"]} for source in sources],
               "related": [], "controls": {}, "rules_version": candidate["versions"]["rules_version"],
               "domain_policy": domain_policy()}
    if "continuation_context" in candidate:
        payload["continuation_context"] = copy.deepcopy(candidate["continuation_context"])
    if "revision_context" in candidate:
        from .revision_context import revision_model_context
        payload["revision_context"] = revision_model_context(candidate["revision_context"])
    if "batch_context" in candidate:
        payload.update(sources=copy.deepcopy(candidate["source_views"]), batch_context=copy.deepcopy(candidate["batch_context"]),
                       output_contract="batch-v1", prior_candidates=copy.deepcopy(candidate["merge_inputs"]))
    if candidate.get("mode") == "incremental":
        payload.update(processing_mode="incremental", related=copy.deepcopy(candidate["related"]),
                       controls=control_guard(git, candidate["base_head"]))
    if _quality_enabled(candidate):
        payload.update({key: copy.deepcopy(candidate[key]) for key in
                        ("quality_contract", "skill_bundle", "quality_context")})
    return payload


def _review_payload(candidate, payload):
    request = {**payload, "candidate": candidate}
    if _quality_enabled(candidate):
        from .knowledge_quality import build_review_checks
        # The fixed final objects are authoritative. Avoid resending the same
        # rules, source views and quality material inside the candidate as well
        # as at the request root; the full persisted candidate remains bound.
        request["candidate"] = {key: copy.deepcopy(value) for key, value in candidate.items()
                                if key not in {"skill_bundle", "quality_context", "source_views",
                                               "batch_context", "related", "merge_inputs", "extraction_output"}}
        request["candidate"]["extraction_output"] = {
            "quality": copy.deepcopy(candidate["extraction_output"]["quality"])}
        request["required_checks"] = build_review_checks(candidate, candidate["quality_context"])
    return request


def _validate_candidate_quality(git, candidate, sources):
    if not _quality_enabled(candidate):
        return
    from .knowledge_quality import prepare_quality, validate_quality_output
    _validated_skill_bundle(candidate["skill_bundle"])
    context = prepare_quality(sources, source_views=candidate.get("source_views"))
    if candidate["quality_context"] != context:
        _error("quality_context_mismatch")
    validate_quality_output(candidate["extraction_output"], context)
    payload = _candidate_payload(git, candidate, sources)
    # Revalidation may adopt a new program version but must retain the actual
    # completed request, whose prior quality snapshot cannot be exchanged.
    original = candidate.get("revision_revalidation", {}).get("original_payload", payload)
    if any(original.get(key) != payload.get(key) for key in ("quality_contract", "skill_bundle", "quality_context")):
        _error("quality_context_mismatch")
    if candidate.get("extraction_request_binding") != _model_request_binding("extract", original):
        _error("extraction_request_mismatch")


def _use_decomposed_review(candidate, payload):
    strategy = candidate.get("review_strategy", "legacy-v1")
    if strategy == "decomposed-v1":
        return True
    if strategy != "auto":
        return False
    from .model_calls import MAX_INPUT_BYTES
    return _model_request_binding("review", _review_payload(candidate, payload))["bytes"] > MAX_INPUT_BYTES


def _assert_fixed_current(git, candidate, sources, stage_bytes):
    incremental = candidate.get("mode") == "incremental"
    batch = "batch_context" in candidate
    source_paths = [source["path"] for source in sources]
    maximum = 256 if batch else MAX_SOURCES
    if (execution_versions(git) != candidate["versions"]
            or _committed(git, _stage_path(candidate["run_id"])) != stage_bytes
            or _source_fields(_load_sources(git.repo, source_paths, allow_versions=incremental,
                  purpose="history" if incremental else "current", max_sources=maximum)) != _source_fields(sources)):
        _error("processing_context_changed")
    if incremental and (control_guard(git)["digest"] != candidate["controls_digest"]
            or _related_context(git, candidate["related_runs"], sources, max_runs=256 if batch else 8) != candidate["related"]):
        _error("processing_context_changed")


def _persist_review(git, candidate, stage_bytes, stage_commit, review_call, output,
                    source_paths, head, *, proof=None, attempted_calls=0):
    """Preserve the real final/first rejecting Review before registry mutation."""
    run_id, schema = candidate["run_id"], _pipeline_schema(candidate)
    quality = _quality_enabled(candidate)
    request = None
    if quality and proof is None:
        sources = _candidate_sources(git, candidate, historical=True)
        request = _review_payload(candidate, _candidate_payload(git, candidate, sources))
    _review_output(output, quality=quality, required_checks=request["required_checks"] if request else None)
    if (_call_metadata({**review_call, "output": output}) != review_call
            or review_call["invocation_id"] == candidate["extraction"]["invocation_id"]):
        _error("review_not_independent")
    review_path = _run_path(run_id, "review")
    review = {"run_id": run_id, "review_id": "rev-" + uuid.uuid4().hex, "binding": candidate["binding"],
              "stage_path": _stage_path(run_id), "stage_commit": stage_commit, "stage_sha256": _hash(stage_bytes),
              "call": copy.deepcopy(review_call), "output": copy.deepcopy(output), "reviewed_at": timestamp()}
    if request is not None:
        review["request_binding"] = _model_request_binding("review", request)
    if proof is not None:
        from .decomposed_review import verify_decomposed_review
        review["decomposed_proof"] = copy.deepcopy(proof)
        sources = _candidate_sources(git, candidate, historical=True)
        verify_decomposed_review(git, candidate, review, _candidate_payload(git, candidate, sources), sources)
    files = {review_path: _document("review", review, schema)}
    if output["verdict"] == "needs_confirmation":
        files[_run_path(run_id, "pending")] = _document("pending", {"run_id": run_id, "status": "open", "review_path": review_path,
            "reasons": output["reasons"], "user_reply": None}, schema)
    if candidate.get("mode") == "incremental" and output["verdict"] == "needs_confirmation":
        path = _run_path(run_id, "pending-registration")
        files[path] = _document("pending_registration", _registration_marker(candidate, review, stage_bytes, files[review_path]), "0.7")
        head = git.write_files(files, head, "review: preserve verdict before confirmation registration")
        registered = _resume_pending_registration(git, run_id, source_paths, expected_head=head)
        return {**registered, "attempted_calls": attempted_calls, "model_calls": attempted_calls}
    head = git.write_files(files, head, "review: preserve independent model verdict")
    result = {"run_id": run_id, "status": output["verdict"], "review_verdict": output["verdict"],
              "stage_path": _stage_path(run_id), "review_path": review_path, "head": head,
              "attempted_calls": attempted_calls, "model_calls": attempted_calls}
    if output["verdict"] == "needs_confirmation":
        result["pending_path"] = _run_path(run_id, "pending")
    return result


def resume_decomposed_processing(repo, run_id, *, allowed_source_paths, model_call):
    """Continue one fixed candidate with no extraction or unknown-call replay."""
    git = PipelineGit(repo)
    with git.lock():
        git.recover()
        head = git.check()
        # Only metadata is inspected before caller authorization; no source body
        # is loaded through the full candidate verifier until the complete scope fits.
        peek = _load(_committed(git, _stage_path(run_id)), "candidate")
        bound = normalize_source_paths([item["path"] for item in _candidate_bindings(peek)], allow_versions=True, max_sources=256)
        allowed = normalize_source_paths(allowed_source_paths, allow_versions=True, max_sources=256)
        if not set(bound) <= set(allowed):
            _error("evidence_source_mismatch")
        if peek.get("review_strategy") not in {"auto", "decomposed-v1"} or "batch_context" not in peek:
            _error("invalid_review_strategy")
        candidate, sources, stage_bytes, stage_commit = _read_fixed_candidate(git, run_id, source_paths=bound, historical=True)
        _assert_revalidation_operation(candidate, model_call)
        _assert_fixed_current(git, candidate, sources, stage_bytes)
        _execution_guard(repo, candidate.get("execution_context"), run_id=run_id, source_paths=bound, related_runs=candidate["related_runs"])
        base = {"run_id": run_id, "stage_path": _stage_path(run_id), "head": head, "attempted_calls": 0, "model_calls": 0}
        review_path = _run_path(run_id, "review")
        if git.path(review_path).exists():
            _, review, _ = _read_reviewed(git, run_id, source_paths=bound, historical=True,
                                          allow_nonpassed=True)
            if review["output"]["verdict"] == "needs_confirmation":
                return _resume_pending_registration(git, run_id, bound, expected_head=head)
            return {**base, "status": review["output"]["verdict"], "review_verdict": review["output"]["verdict"], "review_path": review_path}
        payload = _candidate_payload(git, candidate, sources)
        plan_path = _run_path(run_id, "decomposed-plan")
        if candidate["review_strategy"] == "auto" and not git.path(plan_path).exists():
            adapter = _budget_adapter(model_call)
            state = adapter.operation_state() if adapter is not None else None
            if adapter is not None:
                from .budget import _json
                extraction = state["calls"].get("extract") if state is not None else None
                original_payload = candidate.get("revision_revalidation", {}).get("original_payload", payload)
                if (not isinstance(extraction, dict) or extraction["status"] != "completed"
                        or extraction["invocation_id"] != candidate["extraction"]["invocation_id"]
                        or extraction["payload_sha256"] != hashlib.sha256(_json(original_payload)).hexdigest()):
                    _error("review_extraction_operation_mismatch")
            # Any root Review start has an uncertain or completed independent
            # outcome. Changing to child operations would be a replay bypass.
            if state is not None and "review" in state["calls"]:
                return {**base, "status": "unknown", "error": "review_outcome_unknown"}
            if adapter is None:
                failure_path = _run_path(run_id, "failure")
                failure = _load(_committed(git, failure_path), "failure") if git.path(failure_path).exists() else None
                if not failure or failure.get("phase") != "review" or failure.get("error") not in SAFE_REVIEW_PRELAUNCH_ERRORS:
                    return {**base, "status": "unknown", "error": "review_outcome_unknown"}
            # Proven unstarted ordinary Review can safely gain a full decomposed
            # proof on this same candidate. No partial/local pass is published.
        from .decomposed_review import run_decomposed_review
        outcome = run_decomposed_review(git, candidate, stage_commit, payload, model_call)
        calls = outcome["attempted_calls"]
        if "output" not in outcome:
            return {**outcome, **{key: value for key, value in base.items() if key != "head"},
                    "attempted_calls": calls, "model_calls": calls}
        head = git.check(outcome["head"])
        _assert_fixed_current(git, candidate, sources, stage_bytes)
        return _persist_review(git, candidate, stage_bytes, stage_commit, outcome["call"], outcome["output"], bound,
                               head, proof=outcome["proof"], attempted_calls=calls)


def process(repo, source_path, expected_head=None, *, model_call=call_model, skill_bundle=None):
    """Two tool-free model invocations. Publication remains an explicit next step."""
    return process_sources(repo, [source_path], expected_head, model_call=model_call, skill_bundle=skill_bundle)


def process_sources(repo, source_paths, expected_head=None, *, model_call=call_model,
                    related_runs=None, run_id=None, execution_context=None,
                    batch_context=None, source_views=None, draft_only=False, merge_runs=(), reuse_extraction_run=None,
                    continuation_context=None, review_strategy="legacy-v1", revision_from_run=None, _resume_unstarted=None,
                    _revalidate_completed=None, skill_bundle=None):
    """Process selected sources; explicit revision retains a rejected fixed Review.

    Revision is interpretation work, never another fact source or permission to
    restart an uncertain production operation. Its complete original selection
    and batch context are restored before any model invocation.
    """
    if _revalidate_completed is not None and (_resume_unstarted is not None or revision_from_run is None or run_id is None):
        _error("invalid_extraction_revalidation")
    if _resume_unstarted is not None and (revision_from_run is None or run_id is None):
        _error("invalid_revision_resume")
    if revision_from_run is not None:
        _run_path(revision_from_run, "review")
        if (execution_context is not None or continuation_context is not None or draft_only
                or reuse_extraction_run is not None):
            _error("invalid_revision_context")
        scope = normalize_source_paths(source_paths, allow_versions=True, max_sources=256)
        peek = _load(_committed(PipelineGit(repo), _stage_path(revision_from_run)), "candidate")
        if peek.get("binding") != _candidate_binding(peek) or sorted(item["path"] for item in _candidate_bindings(peek)) != scope:
            _error("revision_source_scope")
        if skill_bundle is None and _quality_enabled(peek):
            skill_bundle = copy.deepcopy(peek["skill_bundle"])
        if "batch_context" in peek:
            if batch_context is None and source_views is None:
                batch_context, source_views = copy.deepcopy(peek["batch_context"]), copy.deepcopy(peek["source_views"])
            if (not isinstance(batch_context, dict) or batch_context.get("source_ranges") != peek["batch_context"]["source_ranges"]
                    or source_views != peek["source_views"]):
                _error("revision_batch_context_changed")
            old_merges = [item["run_id"] for item in peek.get("merge_inputs", [])]
            if merge_runs and list(merge_runs) != old_merges:
                _error("revision_batch_context_changed")
            merge_runs = old_merges
            if review_strategy == "legacy-v1":
                review_strategy = peek.get("review_strategy", "legacy-v1")
        elif batch_context is not None or source_views is not None or merge_runs:
            _error("revision_batch_context_changed")
    if _revalidate_completed is not None:
        original = _revalidate_completed.get("original_payload", {})
        if _quality_enabled(original):
            if skill_bundle is not None and skill_bundle != original["skill_bundle"]:
                _error("quality_context_mismatch")
            skill_bundle = copy.deepcopy(original["skill_bundle"])
    if skill_bundle is not None:
        skill_bundle = _validated_skill_bundle(skill_bundle)
        if _resume_unstarted is not None or _revalidate_completed is not None:
            _error("skill_quality_recovery_attestation_required")
    revalidation_call_baseline = _actual_calls(model_call) if _revalidate_completed is not None else None
    def current_call_count():
        observed = _actual_calls(model_call)
        return (observed - (revalidation_call_baseline or 0)
                if _revalidate_completed is not None and observed is not None else observed)
    batch = batch_context is not None
    if not isinstance(review_strategy, str) or review_strategy not in REVIEW_STRATEGIES or review_strategy != "legacy-v1" and not batch:
        _error("invalid_review_strategy")
    if batch != (source_views is not None) or type(draft_only) is not bool or (draft_only or merge_runs or reuse_extraction_run) and not batch:
        _error("invalid_batch_context")
    if continuation_context is not None and not batch:
        _error("invalid_continuation_context")
    incremental = related_runs is not None or batch or revision_from_run is not None
    if execution_context is not None and not incremental:
        _error("invalid_execution_context")
    related_limit = 256 if batch else 8
    related_runs = _related_run_ids(related_runs or [], max_runs=related_limit) if incremental else None
    source_limit = 256 if batch else MAX_SOURCES
    source_paths = normalize_source_paths(source_paths, allow_versions=incremental, max_sources=source_limit)
    requested_run = run_id
    run_id = run_id or "run-" + uuid.uuid4().hex
    _run_path(run_id, "review")
    git = PipelineGit(repo)
    with git.lock():
        git.recover()
        head = git.check(expected_head)
        _execution_guard(repo, execution_context, run_id=run_id, source_paths=source_paths, related_runs=related_runs)
        if continuation_context is not None:
            _validate_continuation_context(git, continuation_context, source_paths, related_runs, draft_only=draft_only)
        revision_context = None
        if revision_from_run is not None:
            from .revision_context import build_revision_context
            revision_context = build_revision_context(git, revision_from_run, source_paths=source_paths, related_runs=related_runs)
            if _resume_unstarted is None and _revalidate_completed is None:
                existing_revision = _existing_revision_attempt(git, revision_context, source_paths, related_runs)
                if existing_revision is not None:
                    return existing_revision
            elif _revalidate_completed is None:
                from .revision_resume import verify_unstarted_revision
                _resume_unstarted = verify_unstarted_revision(git, run_id, revision_context,
                    model_call=model_call, source_paths=source_paths, related_runs=related_runs,
                    reservation=_resume_unstarted["reservation"],
                    expected_reservation_sha256=_resume_unstarted["expected_reservation_sha256"])
            else:
                from .known_extraction import verify_completed_extraction
                _revalidate_completed = verify_completed_extraction(git, run_id, model_call=model_call,
                    source_paths=source_paths, related_runs=related_runs,
                    attestation=_revalidate_completed["attestation"],
                    expected_attestation_sha256=_revalidate_completed["expected_attestation_sha256"],
                    original_payload=_revalidate_completed["original_payload"],
                    response=_revalidate_completed["response"],
                    original_resume_context=_revalidate_completed["original_resume_context"])
        if requested_run is not None and _resume_unstarted is None and _revalidate_completed is None:
            paths = [_stage_path(run_id), *[_run_path(run_id, name) for name in ("started", "review", "failure", "manifest", "receipt", "pending", "reused")]]
            if any(git.path(path).exists() or _last_commit(git, head, path) for path in paths):
                _error("run_already_exists")
        sources = _load_sources(repo, source_paths, allow_versions=incremental,
                                purpose="history" if incremental else "current", max_sources=source_limit)
        controls = control_guard(git)
        if all(source["metadata"]["acquisition_status"] == "link_only" for source in sources):
            _error("source_has_no_text")
        source_fields = _source_fields(sources)
        schema_version = (QUALITY_SCHEMA_VERSION if skill_bundle is not None else REVISION_SCHEMA_VERSION if revision_context is not None else DECOMPOSED_SCHEMA_VERSION if review_strategy != "legacy-v1" else ORCHESTRATED_SCHEMA_VERSION if continuation_context is not None else CONTINUOUS_SCHEMA_VERSION
                          if batch else M1_SCHEMA_VERSION if incremental else _pipeline_schema(source_fields))
        versions = execution_versions(git)
        payload = {"sources": [{"path": source["path"], "metadata": source["metadata"], "body": source["body"]} for source in sources],
                   "related": [], "controls": {}, "rules_version": versions["rules_version"],
                   "domain_policy": domain_policy()}
        related, context_fields = [], {}
        if _resume_unstarted is not None:
            context_fields["revision_resume"] = copy.deepcopy(_resume_unstarted)
        if _revalidate_completed is not None:
            context_fields["revision_resume"] = copy.deepcopy(_revalidate_completed["original_resume_context"])
            context_fields["revision_revalidation"] = copy.deepcopy(_revalidate_completed)
        if revision_context is not None:
            from .revision_context import revision_model_context
            payload["revision_context"] = revision_model_context(revision_context)
            context_fields["revision_context"] = copy.deepcopy(revision_context)
        if continuation_context is not None:
            payload["continuation_context"] = copy.deepcopy(continuation_context)
            context_fields["continuation_context"] = copy.deepcopy(continuation_context)
        if batch:
            from .batching import validate_source_views
            validate_source_views(sources, source_views, batch_context)
            payload.update(sources=copy.deepcopy(source_views), batch_context=copy.deepcopy(batch_context), output_contract="batch-v1")
            context_fields.update(batch_context=copy.deepcopy(batch_context), source_views=copy.deepcopy(source_views))
            prior = _merge_candidates(git, merge_runs, source_paths)
            material = {"draft_only": draft_only, "merge_bindings": [{"run_id": item["run_id"], "binding": item["binding"]} for item in prior]}
            if review_strategy != "legacy-v1":
                material["review_strategy"] = review_strategy
                context_fields["review_strategy"] = review_strategy
            context_fields.update(batch_material=material, merge_inputs=prior)
            payload.update(prior_candidates=copy.deepcopy(prior))
        if skill_bundle is not None:
            from .knowledge_quality import prepare_quality
            quality = {"quality_contract": "skill-quality-v1", "skill_bundle": copy.deepcopy(skill_bundle),
                       "quality_context": prepare_quality(sources, source_views=source_views)}
            payload.update(copy.deepcopy(quality))
            context_fields.update(quality)
        if incremental:
            controls = control_guard(git)
            key = _processing_key(source_fields, versions, controls["digest"], batch_context, context_fields.get("batch_material"), continuation_context, review_strategy, revision_context, context_fields.get("revision_resume"), _revalidate_completed, skill_bundle)
            existing = _existing_processing(git, key, source_paths, related_runs)
            if existing is not None:
                if requested_run is not None:
                    reused = {"run_id": run_id, "status": "already_processed", "original_run_id": existing["run_id"],
                              "processing_key": key, "execution_context": copy.deepcopy(execution_context), "attempted_calls": 0}
                    path = _run_path(run_id, "reused")
                    final = git.write_files({path: _document("processing_reuse", reused, M1_SCHEMA_VERSION)}, head,
                                            "process: persist zero-call reuse for reserved attempt")
                    existing.update(attempt_run_id=run_id, reuse_path=path, head=final)
                return existing
            related = _related_context(git, related_runs, sources, max_runs=related_limit)
            payload.update(processing_mode="incremental", related=related, controls=controls)
            context_fields.update({"mode": "incremental", "related_runs": related_runs,
                              "controls_digest": controls["digest"], "processing_key": key,
                              "execution_context": copy.deepcopy(execution_context)})
        if _resume_unstarted is None and _revalidate_completed is None:
            _reserve_processing_calls(model_call)
        if incremental:
            started_path = _run_path(run_id, "started")
            if _resume_unstarted is not None or _revalidate_completed is not None:
                started_bytes = _committed(git, started_path)
            else:
                started = {"run_id": run_id, "processing_key": key, "execution_context": copy.deepcopy(execution_context),
                           "source_paths": source_paths, "related_runs": related_runs, "reserved_calls": 2, "started_at": timestamp()}
                started_bytes = _document("processing_attempt", started, M1_SCHEMA_VERSION)
                started_files = {started_path: started_bytes}
                if revision_context is not None:
                    pointer = {"run_id": run_id, "revision_context_sha256": _hash(revision_context),
                               "source_paths": source_paths, "related_runs": related_runs, "processing_key": key}
                    started_files[_revision_attempt_path(revision_context)] = _document("revision_attempt", pointer, REVISION_SCHEMA_VERSION)
                head = git.write_files(started_files, head, "process: reserve immutable run before model calls")
            # The reservation advances HEAD; both model calls must receive the
            # control snapshot at the same base used by the fixed candidate.
            controls = control_guard(git, head)
            if controls["digest"] != context_fields["controls_digest"]:
                _error("processing_context_changed")
            git.check(head)
            payload["controls"] = controls
            context_fields.update(attempt_path=started_path, attempt_sha256=_hash(started_bytes))
        response = None
        try:
            if skill_bundle is not None:
                original_payload = _revalidate_completed["original_payload"] if _revalidate_completed is not None else payload
                if any(original_payload.get(key) != payload.get(key) for key in ("quality_contract", "skill_bundle", "quality_context")):
                    _error("quality_context_mismatch")
                context_fields["extraction_request_binding"] = _model_request_binding("extract", original_payload)
            if _revalidate_completed is not None:
                response = copy.deepcopy(_revalidate_completed["original_response"])
            elif reuse_extraction_run is None:
                response = model_call("extract", copy.deepcopy(payload))
            else:
                response, reused = _reused_extraction(git, reuse_extraction_run, source_fields, versions,
                    context_fields["controls_digest"], source_views, context_fields["batch_material"], continuation_context, review_strategy, revision_context,
                    skill_bundle, context_fields.get("quality_context"))
                context_fields["extraction_reused_from"] = reused
            extraction = _call_metadata(response)
            if _resume_unstarted is not None:
                from .revision_resume import attach_revision_resume_extraction
                context_fields["revision_resume"] = attach_revision_resume_extraction(model_call, _resume_unstarted, extraction, payload)
            created = timestamp()
            objects = _objects(response["output"], sources, run_id, response, versions, created, batch=batch,
                               quality_context=context_fields.get("quality_context"))
            _current_object_corrections(objects, sources)
            if batch:
                from .batching import assert_fragment_refs
                for obj in objects:
                    assert_fragment_refs(obj["metadata"]["sources"], source_views)
        except (ModelCallError, CaptureError) as error:
            git.check(head)
            if _revalidate_completed is not None:
                return {"run_id": run_id, "status": "failed", "error": error.code,
                        "failure_path": _run_path(run_id, "failure"), "head": head,
                        "attempted_calls": 0, "model_calls": 0, "extract_calls": 0}
            return _failure(git, run_id, "extract", getattr(error, "code", "model_failed"), head,
                            _failure_call(error, response), schema_version, current_call_count())
        git.check(head)
        if execution_versions(git) != versions or _source_fields(_load_sources(repo, source_paths, allow_versions=incremental,
                purpose="history" if incremental else "current", max_sources=source_limit)) != source_fields:
            _error("processing_context_changed")
        if incremental:
            if control_guard(git)["digest"] != context_fields["controls_digest"] or _related_context(git, related_runs, sources, max_runs=related_limit) != related:
                _error("processing_context_changed")
            try:
                context_fields["operations"] = _updated_objects(git, objects, related, check_current=not draft_only)
            except CaptureError as error:
                if error.code not in {"related_records_omitted", "related_identity_changed",
                                      "correction_evidence_missing", "required_related_context"}:
                    raise
                git.check(head)
                return _failure(git, run_id, "extract", error.code, head, extraction, schema_version, current_call_count(),
                    failure_name="revalidation-failure" if _revalidate_completed is not None else "failure")
        else:
            _targets_clear(git, objects)
        try:
            _validate_domains(objects, related, context_fields.get("operations", {}), versions)
        except CaptureError as error:
            if error.code not in {"invalid_domains", "invalid_domain_reuse"}:
                raise
            git.check(head)
            return _failure(git, run_id, "extract", error.code, head, extraction, schema_version, current_call_count(),
                    failure_name="revalidation-failure" if _revalidate_completed is not None else "failure")
        candidate = {"run_id": run_id, "base_head": head, **source_fields, "versions": versions,
                     "related": related, "objects": objects, "extraction": extraction, "extraction_output": response["output"], "created_at": created,
                     **context_fields}
        candidate["binding"] = _candidate_binding(candidate)
        stage_path = _stage_path(run_id)
        stage_bytes = _document("candidate", candidate, schema_version)
        if len(stage_bytes) > 1024 * 1024:
            return _failure(git, run_id, "extract", "candidate_too_large", head, _failure_call(None, response), schema_version,
                            current_call_count(),
                            failure_name="revalidation-failure" if _revalidate_completed is not None else "failure")
        head = git.write_files({stage_path: stage_bytes}, head, "process: preserve fixed candidate")
        stage_commit = head
        try:
            _assert_substantive_revision(candidate)
        except CaptureError as error:
            if error.code != "revision_unchanged":
                raise
            result = _failure(git, run_id, "extract", error.code, head, extraction, schema_version, current_call_count(),
                    failure_name="revalidation-failure" if _revalidate_completed is not None else "failure")
            return {**result, "stage_path": stage_path}
        decomposed = _use_decomposed_review(candidate, payload)
        if decomposed:
            from .decomposed_review import run_decomposed_review
            outcome = run_decomposed_review(git, candidate, stage_commit, payload, model_call)
            calls = (0 if reuse_extraction_run is not None or _revalidate_completed is not None else 1) + outcome["attempted_calls"]
            if "output" not in outcome:
                result = {**outcome, "run_id": run_id, "stage_path": stage_path,
                          "model_calls": calls, "attempted_calls": calls}
                return {**result, **({"extract_calls": 0} if _revalidate_completed is not None else {})}
            review_call, output = outcome["call"], outcome["output"]
            proof = outcome["proof"]
            head = git.check(outcome["head"])
        else:
            reviewed = None
            try:
                request = _review_payload(candidate, payload)
                reviewed = model_call("review", copy.deepcopy(request))
                review_call = _call_metadata(reviewed)
                output = reviewed["output"]
                _review_output(output, required_checks=request.get("required_checks"))
                if review_call["invocation_id"] == extraction["invocation_id"]:
                    _error("review_not_independent")
            except (ModelCallError, CaptureError) as error:
                git.check(head)
                result = _failure(git, run_id, "review", getattr(error, "code", "model_failed"), head,
                                  _failure_call(error, reviewed), schema_version, current_call_count(),
                                  failure_name="revalidation-review-failure" if _revalidate_completed is not None else "failure")
                return {**result, "stage_path": stage_path}
            proof = None
            calls = current_call_count()
            calls = calls if calls is not None else (1 if reuse_extraction_run is not None or _revalidate_completed is not None else 2)
        git.check(head)
        _assert_fixed_current(git, candidate, sources, stage_bytes)
        result = _persist_review(git, candidate, stage_bytes, stage_commit, review_call, output,
                                 source_paths, head, proof=proof, attempted_calls=calls)
        return {**result, **({"extract_calls": 0} if _revalidate_completed is not None else {})}


def resume_unstarted_revision(repo, run_id, *, allowed_source_paths, model_call, reservation,
                              expected_reservation_sha256, related_runs=(), expected_head=None,
                              batch_context=None, source_views=None, review_strategy="decomposed-v1"):
    """Explicitly reconcile a proven zero-start reservation, retaining its run.

    A missing operation, a completed/failed call or any uncertain launch is not
    evidence of zero starts. This API never creates a replacement reservation.
    """
    if not isinstance(reservation, dict):
        _error("invalid_revision_resume")
    return process_sources(repo, allowed_source_paths, expected_head, model_call=model_call,
        run_id=run_id, related_runs=related_runs, batch_context=batch_context, source_views=source_views,
        review_strategy=review_strategy, revision_from_run=reservation.get("revision_from_run"),
        _resume_unstarted={"reservation": copy.deepcopy(reservation),
                           "expected_reservation_sha256": expected_reservation_sha256})


def revalidate_completed_extraction(repo, run_id, *, allowed_source_paths, model_call,
                                    attestation, expected_attestation_sha256, original_payload,
                                    response, original_resume_context, related_runs=(), expected_head=None,
                                    batch_context=None, source_views=None, review_strategy="decomposed-v1"):
    """Revalidate the exact known completed response; never invoke extraction.

    Independent Review remains mandatory. The original failure, operation,
    reservation, request and response are retained as separate provenance.
    """
    if not isinstance(attestation, dict):
        _error("invalid_extraction_revalidation")
    git = PipelineGit(repo)
    stage_path = _stage_path(run_id)
    if git.path(stage_path).is_file():
        head = git.check(expected_head)
        paths = normalize_source_paths(allowed_source_paths, allow_versions=True, max_sources=256)
        candidate, _, _, _ = _read_fixed_candidate(git, run_id, source_paths=paths, historical=True)
        context = candidate.get("revision_revalidation", {})
        if (context.get("expected_attestation_sha256") != expected_attestation_sha256
                or context.get("attestation") != attestation or context.get("original_payload") != original_payload
                or context.get("original_response") != response
                or context.get("original_resume_context") != original_resume_context):
            _error("extraction_revalidation_changed")
        _assert_revalidation_operation(candidate, model_call)
        git.check(head)
        if candidate.get("review_strategy") in {"auto", "decomposed-v1"}:
            return resume_decomposed_processing(repo, run_id, allowed_source_paths=paths, model_call=model_call)
        review_path = _run_path(run_id, "review")
        if git.path(review_path).is_file():
            _, review, _ = _read_reviewed(git, run_id, source_paths=paths, historical=True,
                                          allow_nonpassed=True)
            return {"run_id": run_id, "status": review["output"]["verdict"], "stage_path": stage_path,
                    "review_path": review_path, "head": git.head(), "model_calls": 0, "attempted_calls": 0}
        return {"run_id": run_id, "status": "unknown", "next_action": "reconcile", "head": head,
                "stage_path": stage_path, "model_calls": 0, "attempted_calls": 0}
    return process_sources(repo, allowed_source_paths, expected_head, model_call=model_call,
        run_id=run_id, related_runs=related_runs, batch_context=batch_context, source_views=source_views,
        review_strategy=review_strategy, revision_from_run=attestation.get("revision_from_run"),
        _revalidate_completed={"attestation": copy.deepcopy(attestation),
            "expected_attestation_sha256": expected_attestation_sha256,
            "original_payload": copy.deepcopy(original_payload), "response": copy.deepcopy(response),
            "original_resume_context": copy.deepcopy(original_resume_context)})


def _read_reviewed(git, run_id, *, source_paths=None, historical=False, visited=(), allow_nonpassed=False):
    try:
        return _read_reviewed_data(git, run_id, source_paths=source_paths, historical=historical,
                                   visited=visited, allow_nonpassed=allow_nonpassed)
    except (KeyError, TypeError, ValueError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _error()


def _read_fixed_candidate(git, run_id, *, source_paths=None, historical=False, visited=()):
    stage_path = _stage_path(run_id)
    stage_bytes = _committed(git, stage_path)
    candidate = _load(stage_bytes, "candidate")
    schema_version = _pipeline_schema(candidate)
    _load(stage_bytes, "candidate", schema_version)
    if candidate.get("run_id") != run_id or candidate.get("binding") != _candidate_binding(candidate):
        _error("candidate_mismatch")
    if ("revision_resume" in candidate or "revision_revalidation" in candidate) and "revision_context" not in candidate:
        _error("invalid_revision_resume")
    strategy = candidate.get("review_strategy", "legacy-v1")
    if not isinstance(strategy, str) or strategy not in REVIEW_STRATEGIES or strategy != "legacy-v1" and ("batch_context" not in candidate or candidate.get("mode") != "incremental"):
        _error("invalid_review_strategy")
    _validate_domain_policy(candidate["versions"])
    if source_paths is not None:
        maximum = 256 if "batch_context" in candidate else MAX_SOURCES
        scope = normalize_source_paths(source_paths, allow_versions=True, max_sources=maximum)
        bound_paths = normalize_source_paths([binding["path"] for binding in _candidate_bindings(candidate)], allow_versions=True, max_sources=maximum)
        if bound_paths != scope:
            _error("evidence_source_mismatch")
    incremental = candidate.get("mode") == "incremental"
    sources = _candidate_sources(git, candidate, historical=historical or incremental)
    related = []
    if incremental:
        related_limit = 256 if "batch_context" in candidate else 8
        runs = _related_run_ids(candidate["related_runs"], max_runs=related_limit)
        related = _related_context(git, runs, sources, at_commit=candidate["base_head"], visited=visited, max_runs=related_limit)
        controls_digest = control_guard(git, candidate["base_head"])["digest"]
        if (candidate.get("related") != related or candidate.get("controls_digest") != controls_digest
                or candidate.get("processing_key") != _processing_key(_source_fields(sources), candidate["versions"], controls_digest,
                    candidate.get("batch_context"), candidate.get("batch_material"), candidate.get("continuation_context"), strategy, candidate.get("revision_context"), candidate.get("revision_resume"), candidate.get("revision_revalidation"),
                    candidate.get("skill_bundle"))):
            _error("processing_context_changed")
        attempt_path = _run_path(run_id, "started")
        attempt_bytes = _committed(git, attempt_path)
        attempt = _load(attempt_bytes, "processing_attempt", M1_SCHEMA_VERSION)
        if (candidate.get("attempt_path") != attempt_path or candidate.get("attempt_sha256") != _hash(attempt_bytes)
                or _last_commit(git, git.head(), attempt_path) != candidate.get("revision_revalidation", {}).get("original_resume_context", {}).get("reservation", {}).get("head", candidate["base_head"])
                or attempt != {"run_id": run_id, "processing_key": candidate.get("revision_resume", {}).get("reservation", {}).get("processing_key", candidate["processing_key"]),
                    "execution_context": candidate["execution_context"], "source_paths": [source["path"] for source in sources],
                    "related_runs": runs, "reserved_calls": 2, "started_at": attempt.get("started_at")}):
            _error("processing_attempt_mismatch")
        _time(attempt["started_at"])
    elif candidate.get("related") != []:
        _error("source_mismatch")
    extraction = _call_metadata({**candidate["extraction"], "output": candidate["extraction_output"]})
    if extraction != candidate["extraction"]:
        _error("candidate_mismatch")
    batch = "batch_context" in candidate
    if "continuation_context" in candidate:
        if not batch:
            _error("invalid_continuation_context")
        _validate_continuation_context(git, candidate["continuation_context"], [source["path"] for source in sources],
            candidate["related_runs"], at_commit=candidate["base_head"],
            draft_only=candidate.get("batch_material", {}).get("draft_only", False))
    if "revision_context" in candidate:
        if not incremental or candidate.get("execution_context") is not None or "continuation_context" in candidate or "extraction_reused_from" in candidate:
            _error("invalid_revision_context")
        from .revision_context import verify_revision_context
        verify_revision_context(git, candidate["revision_context"], source_paths=[source["path"] for source in sources],
            related_runs=candidate["related_runs"], at_commit=candidate["base_head"], historical=historical)
        pointer_path = _revision_attempt_path(candidate["revision_context"])
        pointer = _load(_committed(git, pointer_path), "revision_attempt", REVISION_SCHEMA_VERSION)
        if (pointer != {"run_id": run_id, "revision_context_sha256": _hash(candidate["revision_context"]),
                "source_paths": [source["path"] for source in sources], "related_runs": candidate["related_runs"],
                "processing_key": candidate.get("revision_resume", {}).get("reservation", {}).get("processing_key", candidate["processing_key"])}
                or _last_commit(git, git.head(), pointer_path) != candidate.get("revision_revalidation", {}).get("original_resume_context", {}).get("reservation", {}).get("head", candidate["base_head"])):
            _error("revision_attempt_mismatch")
        if "revision_resume" in candidate:
            from .revision_resume import verify_revision_resume
            verify_revision_resume(git, candidate, historical=historical)
        if "revision_revalidation" in candidate:
            from .known_extraction import verify_revalidated_extraction
            verify_revalidated_extraction(git, candidate, historical=historical)
        old = candidate["revision_context"]["candidate"]
        if (candidate.get("batch_context", {}).get("source_ranges") != old.get("batch_context", {}).get("source_ranges") or candidate.get("source_views") != old.get("source_views")
                or candidate.get("merge_inputs", []) != old.get("merge_inputs", [])
                or candidate.get("batch_material", {}).get("draft_only", False)):
            _error("revision_batch_context_changed")
    if batch:
        from .batching import validate_source_views, assert_fragment_refs
        validate_source_views(sources, candidate.get("source_views"), candidate["batch_context"])
        prior = _merge_candidates(git, [item.get("run_id") for item in candidate.get("merge_inputs", [])], [source["path"] for source in sources], visited=visited)
        material = candidate.get("batch_material")
        if (not isinstance(material, dict) or set(material) != ({"draft_only", "merge_bindings", "review_strategy"} if strategy != "legacy-v1" else {"draft_only", "merge_bindings"})
                or strategy != "legacy-v1" and material.get("review_strategy") != strategy
                or type(material["draft_only"]) is not bool or prior != candidate.get("merge_inputs")
                or material["merge_bindings"] != [{"run_id": item["run_id"], "binding": item["binding"]} for item in prior]):
            _error("invalid_batch_context")
        if "extraction_reused_from" in candidate:
            reused = candidate["extraction_reused_from"]
            response, expected = _reused_extraction(git, reused.get("run_id"), _source_fields(sources), candidate["versions"],
                candidate["controls_digest"], candidate["source_views"], material, candidate.get("continuation_context"), strategy, candidate.get("revision_context"),
                candidate.get("skill_bundle"), candidate.get("quality_context"))
            if reused != expected or response["output"] != candidate["extraction_output"] or _call_metadata(response) != extraction:
                _error("extraction_not_reusable")
    _validate_candidate_quality(git, candidate, sources)
    expected_objects = _objects(candidate["extraction_output"], sources, run_id, extraction, candidate["versions"], candidate["created_at"],
                                batch=batch, quality_context=candidate.get("quality_context"))
    if batch:
        for obj in expected_objects:
            assert_fragment_refs(obj["metadata"]["sources"], candidate["source_views"])
    if incremental and candidate.get("operations") != _updated_objects(git, expected_objects, related):
        _error("candidate_mismatch")
    _validate_domains(expected_objects, related, candidate.get("operations", {}), candidate["versions"])
    if candidate.get("objects") != expected_objects:
        _error("candidate_mismatch")
    _assert_substantive_revision(candidate)
    stage_commit = _last_commit(git, git.head(), stage_path)
    if (git.text("rev-parse", stage_commit + "^") != candidate["base_head"]
            or git.read(stage_commit, stage_path) != stage_bytes):
        _error("candidate_mismatch")
    return candidate, sources, stage_bytes, stage_commit


def _read_reviewed_data(git, run_id, *, source_paths=None, historical=False, visited=(), allow_nonpassed=False):
    candidate, sources, stage_bytes, stage_commit = _read_fixed_candidate(
        git, run_id, source_paths=source_paths, historical=historical, visited=visited)
    stage_path, review_path = _stage_path(run_id), _run_path(run_id, "review")
    schema_version = _pipeline_schema(candidate)
    review_bytes = _committed(git, review_path)
    review = _load(review_bytes, "review", schema_version)
    review_commit = _last_commit(git, git.head(), review_path)
    if (review.get("run_id") != run_id or review.get("binding") != candidate["binding"]
            or review.get("stage_path") != stage_path or review.get("stage_commit") != stage_commit
            or review.get("stage_sha256") != _hash(stage_bytes)
            or git.text("rev-parse", stage_commit + "^") != candidate["base_head"]
            or git.read(stage_commit, stage_path) != stage_bytes or git.read(review_commit, review_path) != review_bytes):
        _error("review_binding_mismatch")
    call = _call_metadata({**review["call"], "output": review["output"]})
    _review_output(review["output"], quality=_quality_enabled(candidate))
    if call != review["call"] or call["invocation_id"] == candidate["extraction"]["invocation_id"]:
        _error("review_not_independent")
    if not isinstance(review.get("review_id"), str) or not re.fullmatch(r"rev-[0-9a-f]{32}", review["review_id"]):
        _error("review_binding_mismatch")
    proof = review.get("decomposed_proof")
    if proof is not None:
        if schema_version not in {DECOMPOSED_SCHEMA_VERSION, REVISION_SCHEMA_VERSION, QUALITY_SCHEMA_VERSION} or candidate.get("review_strategy") not in {"auto", "decomposed-v1"}:
            _error("review_binding_mismatch")
        from .decomposed_review import verify_decomposed_review
        _ancestor(git, stage_commit, review_commit)
        verify_decomposed_review(git, candidate, review, _candidate_payload(git, candidate, sources), sources, historical=historical)
    elif (candidate.get("review_strategy") == "decomposed-v1"
          or git.text("rev-parse", review_commit + "^") != stage_commit):
        _error("review_binding_mismatch")
    elif _quality_enabled(candidate):
        request = _review_payload(candidate, _candidate_payload(git, candidate, sources))
        _review_output(review["output"], required_checks=request["required_checks"])
        if review.get("request_binding") != _model_request_binding("review", request):
            _error("review_request_mismatch")
    elif "request_binding" in review:
        _error("review_binding_mismatch")
    # Callers inspecting a confirmed non-passing root Review still need every
    # dependency and the complete independent proof checked. Never propagate
    # this flag to candidate, related-run, or merge-input verification.
    if review["output"].get("verdict") != "passed" and not allow_nonpassed:
        _error("review_not_passed")
    return candidate, review, review_commit


def _publication_files(candidate, review):
    files = {}
    previous = {document["path"]: document for item in candidate.get("related", []) for document in item["documents"]}
    for obj in candidate["objects"]:
        if candidate.get("operations", {}).get(obj["path"], {}).get("action") == "reuse":
            old = previous[obj["path"]]
            files[obj["path"]] = markdown(old["metadata"], old["body"])
            continue
        metadata = {**obj["metadata"], "review_status": "passed", "review_ref": _run_path(candidate["run_id"], "review")}
        files[obj["path"]] = markdown(metadata, obj["body"])
    return files


def _incremental_manifest(candidate):
    previous = {document["path"]: document for item in candidate["related"] for document in item["documents"]}
    origins = {}
    for path, operation in candidate["operations"].items():
        if operation["action"] == "reuse":
            metadata = previous[path]["metadata"]
            origins[path] = {"run_id": metadata["generated_by"]["run_id"], "review_path": metadata["review_ref"]}
    return {"mode": "incremental", "related_runs": candidate["related_runs"],
            "operations": candidate["operations"], "object_origins": origins,
            "controls_digest": candidate["controls_digest"], "processing_key": candidate["processing_key"],
            "execution_context": candidate["execution_context"], "attempt_path": candidate["attempt_path"],
            "attempt_sha256": candidate["attempt_sha256"]}


def _computation_snapshots(git, candidate, visited=()):
    """Include intermediate extraction and Review-only reuse provenance."""
    run_id = candidate["run_id"]
    if run_id in visited or len(visited) >= 16:
        _error("publication_dependency_cycle")
    entries = []
    for name, path in (("stage", _stage_path(run_id)), ("review", _run_path(run_id, "review")),
                       ("failure", _run_path(run_id, "failure")), ("started", _run_path(run_id, "started")),
                       ("revalidation-failure", _run_path(run_id, "revalidation-failure")),
                       ("revalidation-review-failure", _run_path(run_id, "revalidation-review-failure"))):
        if git.path(path).is_file():
            commit = _last_commit(git, git.head(), path)
            entries.append({"commit_sha": commit, "path": path, "sha256": _hash(git.read(commit, path))})
    review_path = _run_path(run_id, "review")
    if git.path(review_path).is_file():
        review = _load(_committed(git, review_path), "review", _pipeline_schema(candidate))
        if review.get("decomposed_proof") is not None:
            from .decomposed_review import verify_decomposed_review
            sources = _candidate_sources(git, candidate, historical=True)
            entries.extend(verify_decomposed_review(git, candidate, review, _candidate_payload(git, candidate, sources), sources, historical=True))
    if "revision_context" in candidate:
        from .revision_context import verify_revision_context
        path = _revision_attempt_path(candidate["revision_context"])
        pointer_commit = _last_commit(git, git.head(), path)
        entries.append({"commit_sha": pointer_commit, "path": path, "sha256": _hash(git.read(pointer_commit, path))})
        entries.extend(verify_revision_context(git, candidate["revision_context"],
            source_paths=[item["path"] for item in _candidate_bindings(candidate)], related_runs=candidate["related_runs"],
            at_commit=candidate["base_head"], historical=True))
    previous = [item["run_id"] for item in candidate.get("merge_inputs", [])]
    if candidate.get("extraction_reused_from"):
        previous.append(candidate["extraction_reused_from"]["run_id"])
    for run in sorted(set(previous)):
        old = _load(_committed(git, _stage_path(run)), "candidate")
        entries.extend(_computation_snapshots(git, old, (*visited, run_id)))
    return list({(item["commit_sha"], item["path"]): item for item in entries}.values())


def _authority_snapshots(git, candidate, review, review_commit, publication_commit, *, visited=()):
    """List fixed Git snapshots after the caller has verified the dependency DAG."""
    if candidate["run_id"] in visited:
        return []
    visited = (*visited, candidate["run_id"])
    entries = _computation_snapshots(git, candidate)
    def add(commit, path):
        entries.append({"commit_sha": commit, "path": path, "sha256": _hash(git.read(commit, path))})
    for binding in _candidate_bindings(candidate):
        add(binding["commit_sha"], binding["path"])
        add(_last_commit(git, publication_commit, binding["receipt_path"]), binding["receipt_path"])
    add(review["stage_commit"], _stage_path(candidate["run_id"]))
    add(review_commit, _run_path(candidate["run_id"], "review"))
    add(publication_commit, _run_path(candidate["run_id"], "manifest"))
    for obj in candidate["objects"]:
        add(publication_commit, obj["path"])
    if candidate.get("mode") == "incremental":
        add(_last_commit(git, candidate["base_head"], candidate["attempt_path"]), candidate["attempt_path"])
        add(publication_commit, _processed_path(candidate["processing_key"]))
    receipt = _run_path(candidate["run_id"], "receipt")
    if git.path(receipt).exists():
        add(_last_commit(git, git.head(), receipt), receipt)
    for related in candidate.get("related", []):
        run = related["run_id"]
        manifest_meta, _ = _parse_saved(_committed(git, _run_path(run, "manifest")))
        if manifest_meta.get("kind") == "batch_group_manifest":
            from .batch_groups import verify_group_publication
            entries.extend(verify_group_publication(git.repo, run, historical=True).get("authority_snapshots", []))
            continue
        older = _load(_committed(git, _stage_path(run)), "candidate")
        older_review = _load(_committed(git, _run_path(run, "review")), "review", _pipeline_schema(older))
        entries.extend(_authority_snapshots(git, older, older_review,
            _last_commit(git, git.head(), _run_path(run, "review")), related["commit_sha"], visited=visited))
    return list({(item["commit_sha"], item["path"]): item for item in entries}.values())


def verify_publication(repo, run_id, *, source_paths=None, historical=False, _visited=()):
    """Verify every binding, respecting an optional exact source scope before reads."""
    if source_paths is not None:
        source_paths = normalize_source_paths(source_paths, allow_versions=True, max_sources=256)
    if run_id in _visited or len(_visited) >= 16:
        _error("publication_dependency_cycle")
    visited = (*_visited, run_id)
    git = PipelineGit(repo)
    control_guard(git)
    manifest_path = _run_path(run_id, "manifest")
    manifest_bytes = _committed(git, manifest_path)
    manifest_metadata, _ = _parse_saved(manifest_bytes)
    if manifest_metadata.get("kind") == "batch_group_manifest":
        from .batch_groups import verify_group_publication
        return verify_group_publication(repo, run_id, source_paths=source_paths, historical=historical)
    candidate, review, review_commit = _read_reviewed(git, run_id, source_paths=source_paths,
                                                    historical=historical, visited=visited)
    schema_version = _pipeline_schema(candidate)
    manifest = _load(manifest_bytes, "manifest", schema_version)
    commit = _last_commit(git, git.head(), manifest_path)
    files = _publication_files(candidate, review)
    expected = {"run_id": run_id, "binding": candidate["binding"], "review_path": _run_path(run_id, "review"),
                "review_commit": review_commit, "source": candidate["source"], "versions": candidate["versions"],
                "files": {path: _hash(data) for path, data in files.items()}, "published_at": manifest.get("published_at")}
    if "additional_sources" in candidate:
        expected["additional_sources"] = candidate["additional_sources"]
    if candidate.get("mode") == "incremental":
        expected.update(_incremental_manifest(candidate))
    if manifest != expected or git.text("rev-parse", commit + "^") != review_commit or git.read(commit, manifest_path) != manifest_bytes:
        _error("publication_mismatch")
    changes = {path: "A" for path in files}
    changes[manifest_path] = "A"
    if candidate.get("mode") == "incremental":
        changes = {path: ("A" if operation["action"] == "create" else "M") for path, operation in candidate["operations"].items()
                   if operation["action"] != "reuse"}
        changes.update({manifest_path: "A", _processed_path(candidate["processing_key"]): "A"})
    actual_changes = git.run("diff-tree", "--no-commit-id", "--no-renames", "--name-status", "-r", "-z", review_commit, commit).decode().split("\0")
    if actual_changes != [entry for path in sorted(changes) for entry in (changes[path], path)] + [""]:
        _error("publication_mismatch")
    for path, data in files.items():
        expected_commit = (_last_commit(git, commit, path) if candidate.get("operations", {}).get(path, {}).get("action") == "reuse" else commit)
        if (git.read(commit, path) != data
                or (not historical and (_committed(git, path) != data or _last_commit(git, git.head(), path) != expected_commit))
                or _deleted_after_publication(git, path, expected_commit, git.head())):
            _error("publication_mismatch")
    if candidate.get("mode") == "incremental":
        marker_path = _processed_path(candidate["processing_key"])
        marker = _load(_committed(git, marker_path), "processed_inputs", M1_SCHEMA_VERSION)
        if marker != {"run_id": run_id, "processing_key": candidate["processing_key"], "manifest_path": manifest_path} or _last_commit(git, git.head(), marker_path) != commit:
            _error("processing_marker_mismatch")
    receipt_path = _run_path(run_id, "receipt")
    if git.path(receipt_path).exists():
        receipt = _load(_committed(git, receipt_path), "publication_receipt", schema_version)
        if receipt != {"run_id": run_id, "commit_sha": commit, "manifest_path": manifest_path,
                       "manifest_sha256": _hash(manifest_bytes), "local_status": "confirmed", "remote_status": "not_requested"}:
            _error("publication_mismatch")
    result = {"status": "published_local", "run_id": run_id, "manifest_path": manifest_path, "commit_sha": commit,
            "receipt_path": receipt_path, "receipt_saved": git.path(receipt_path).exists(), "files": manifest["files"],
            "source": candidate["source"], "review_path": _run_path(run_id, "review"), "remote_status": "not_requested"}
    if "additional_sources" in candidate:
        result["additional_sources"] = candidate["additional_sources"]
    if candidate.get("mode") == "incremental":
        result.update(_incremental_manifest(candidate))
        snapshots = _authority_snapshots(git, candidate, review, review_commit, commit)
        result["authority_snapshots"] = snapshots
        result["authority_files"] = {entry["path"]: entry["sha256"] for entry in snapshots
            if not entry["path"].startswith(("memory/records/", "memory/topics/"))}
        if not historical:
            result["authority_files"].update(manifest["files"])
    if historical:
        result["historical"] = True
    return result


def publish(repo, run_id, expected_head=None):
    """Publish only a passed, unchanged Review; publication+manifest share one tree."""
    git = PipelineGit(repo)
    manifest_path = _run_path(run_id, "manifest")
    if git.path(manifest_path).is_file():
        metadata, body = _parse_saved(_committed(git, manifest_path))
        if metadata.get("kind") == "batch_group_manifest":
            from .batch_groups import publish_final_group
            manifest = json.loads(body, object_pairs_hook=_unique_pairs)
            objects = {}
            proofs = [proof["run_id"] for proof in manifest["proofs"]]
            for proof in proofs:
                candidate, _, _ = _read_reviewed(git, proof, historical=True)
                for obj in candidate["objects"]:
                    if obj["path"] in objects and objects[obj["path"]] != obj:
                        _error("batch_group_object_conflict")
                    objects[obj["path"]] = obj
            return publish_final_group(repo, manifest["plan"], list(objects.values()), proofs,
                source_paths=[binding["path"] for binding in [manifest["source"], *manifest.get("additional_sources", [])]],
                expected_head=expected_head, group_id=run_id)
    with git.lock():
        git.recover()
        head = git.check(expected_head)
        manifest_path = _run_path(run_id, "manifest")
        if git.path(manifest_path).exists():
            result = verify_publication(repo, run_id)
            commit = result["commit_sha"]
        else:
            control_guard(git)
            candidate, review, review_commit = _read_reviewed(git, run_id)
            if candidate.get("batch_material", {}).get("draft_only"):
                _error("batch_requires_final_review")
            _execution_guard(repo, candidate.get("execution_context"), run_id=run_id)
            if head != review_commit:
                _error("publication_head_changed")
            if execution_versions(git) != candidate["versions"]:
                _error("processing_context_changed")
            incremental = candidate.get("mode") == "incremental"
            if incremental:
                sources = _candidate_sources(git, candidate, historical=True)
                if (control_guard(git)["digest"] != candidate["controls_digest"]
                        or _related_context(git, candidate["related_runs"], sources,
                                            max_runs=256 if "batch_context" in candidate else 8) != candidate["related"]):
                    _error("processing_context_changed")
            else:
                _targets_clear(git, candidate["objects"])
            files = _publication_files(candidate, review)
            manifest = {"run_id": run_id, "binding": candidate["binding"], "review_path": _run_path(run_id, "review"),
                        "review_commit": review_commit, "source": candidate["source"], "versions": candidate["versions"],
                        "files": {path: _hash(data) for path, data in files.items()}, "published_at": timestamp()}
            if "additional_sources" in candidate:
                manifest["additional_sources"] = candidate["additional_sources"]
            if incremental:
                manifest.update(_incremental_manifest(candidate))
            files[manifest_path] = _document("manifest", manifest, _pipeline_schema(candidate))
            _execution_guard(repo, candidate.get("execution_context"), run_id=run_id)
            if incremental:
                known = {document["path"]: document for item in candidate["related"] for document in item["documents"]}
                expected = {}
                for path, operation in candidate["operations"].items():
                    if operation["action"] == "reuse":
                        files.pop(path)
                    elif operation["action"] == "create":
                        _targets_clear(git, [obj for obj in candidate["objects"] if obj["path"] == path])
                        expected[path] = None
                    else:
                        previous = known[path]
                        expected[path] = markdown(previous["metadata"], previous["body"])
                marker_path = _processed_path(candidate["processing_key"])
                marker = {"run_id": run_id, "processing_key": candidate["processing_key"], "manifest_path": manifest_path}
                files[marker_path] = _document("processed_inputs", marker, M1_SCHEMA_VERSION)
                expected.update({manifest_path: None, marker_path: None})
                _execution_guard(repo, candidate.get("execution_context"), run_id=run_id)
                commit = git.write_changes(files, expected, head, "publish: atomically persist reviewed changes and processed inputs")
            else:
                commit = git.write_files(files, head, "publish: atomically persist reviewed records and topic")
            head = commit
            result = verify_publication(repo, run_id)
        if not result["receipt_saved"]:
            if git.text("log", "-1", "--format=%H", "--", result["receipt_path"]):
                _error("publication_receipt_deleted")
            manifest_bytes = _committed(git, manifest_path)
            receipt = {"run_id": run_id, "commit_sha": commit, "manifest_path": manifest_path,
                       "manifest_sha256": _hash(manifest_bytes), "local_status": "confirmed", "remote_status": "not_requested"}
            schema_version = _pipeline_schema(_load(_committed(git, _stage_path(run_id)), "candidate"))
            git.write_files({result["receipt_path"]: _document("publication_receipt", receipt, schema_version)}, head, "publish: confirm existing publication commit")
        return {**verify_publication(repo, run_id), "head": git.head()}
