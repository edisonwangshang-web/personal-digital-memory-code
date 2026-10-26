"""Verbatim final assembly followed by the existing complete Review proof.

The program creates no new knowledge or extraction invocation. Original draft
objects retain their actual generator; the final Review is independently bound
to their complete union, source coverage and global relationships.
"""

from copy import deepcopy
from datetime import datetime, timezone

from .contracts import CaptureError
from .transaction import PipelineGit

SCHEMA = "0.14"
CONTRACT = "verbatim-draft-finalization-v1"


def _timestamp():
    # Keep preflight and actual stage metadata at the same exact byte width.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _fail(code="invalid_final_assembly"):
    raise CaptureError(code, "最终原样组装未通过完整草稿、来源、质量或审核核验；未发布部分知识")


def extraction_invocations(candidate):
    if "assembly" not in candidate:
        found = {candidate["extraction"]["invocation_id"]}
        if "quality_repair_context" in candidate:
            previous = candidate["revision_context"]
            found.update(extraction_invocations(previous["candidate"]))
            found.add(previous["review_record"]["call"]["invocation_id"])
        return found
    return ({item["extraction"]["invocation_id"] for item in candidate["assembly"]["inputs"]}
            | {call["invocation_id"] for item in candidate["assembly"]["inputs"] for call in item["review_calls"]}
            | {invocation for item in candidate["assembly"]["inputs"] for invocation in item.get("generation_invocations", [])})


def review_projection(repo, plan):
    """Program-only full view; never send this union as one model request."""
    from .batching import _context, _views, validate_plan
    from .pipeline import _hash
    sources = validate_plan(repo, plan, source_paths=plan["source_paths"])
    parents = sorted(item["batch_id"] for item in plan["batches"])
    fragments = plan["fragments"]
    identifier = "batch-" + _hash({"plan_id": plan["plan_id"], "parent_batch_ids": parents,
                                   "fragment_ids": [item["fragment_id"] for item in fragments]})[7:]
    return {"source_views": _views(sources, fragments),
            "batch_context": _context(plan["plan_id"], identifier, fragments, parents),
            "input_bytes": 0, "coverage": {"complete": True},
            "transport": "program_only_complete_union_for_decomposed_review"}


def _material(git, plan, draft_runs, run_id, base_head, created_at, *, historical=True):
    from .batching import _context, _views, validate_plan
    from .pipeline import (_candidate_bindings, _committed, _hash, _last_commit,
                           _load, _read_reviewed, _related_context, _source_fields,
                           _stage_path, _run_path, _updated_objects, control_guard)
    from .knowledge_quality import prepare_quality, validate_quality_output

    sources = validate_plan(git.repo, plan, source_paths=plan["source_paths"],
                            require_current_versions=not historical, historical=historical, _locked=True)
    if (not isinstance(draft_runs, (list, tuple)) or not 2 <= len(draft_runs) <= 256
            or len(set(draft_runs)) != len(draft_runs)):
        _fail()
    if control_guard(git, base_head)["digest"] != plan["snapshot"]["controls_digest"]:
        _fail("assembly_snapshot_changed")
    expected_sources = {item["path"]: {key: value for key, value in item.items() if key != "line_count"}
                        for item in plan["sources"]}
    inputs, objects, raw_objects, raw_wikis, prior = [], {}, {}, {}, []
    quality = {"coverage_disposition": [], "statistic_claims": [], "temporal_claims": []}
    bundle = None
    seen_ranges = set()
    fragments = {item["fragment_id"]: item for item in plan["fragments"]}
    for source_run in sorted(draft_runs):
        if source_run == run_id:
            _fail("assembly_dependency_cycle")
        peek = _load(_committed(git, _stage_path(source_run)), "candidate")
        if "assembly" in peek or not peek.get("batch_material", {}).get("draft_only"):
            _fail("assembly_draft_mismatch")
        bound = _candidate_bindings(peek)
        if any(expected_sources.get(item["path"]) != item for item in bound):
            _fail("assembly_source_mismatch")
        candidate, review, review_commit = _read_reviewed(
            git, source_run, source_paths=sorted(item["path"] for item in bound), historical=True)
        if (candidate.get("assembly") is not None
                or not candidate.get("batch_material", {}).get("draft_only")
                or candidate.get("batch_context", {}).get("plan_id") != plan["plan_id"]
                or candidate.get("versions") != plan["snapshot"]["versions"]
                or candidate.get("controls_digest") != plan["snapshot"]["controls_digest"]
                or candidate.get("related_runs") != [] or candidate.get("related") != []
                or candidate.get("continuation_context") is not None or candidate.get("rebuild_context") is not None):
            _fail("assembly_draft_mismatch")
        if inputs and candidate.get("skill_bundle") != bundle:
            _fail("assembly_rules_mismatch")
        bundle = candidate.get("skill_bundle")
        for descriptor in candidate["batch_context"]["source_ranges"]:
            fragment = fragments.get(descriptor["fragment_id"])
            expected = ({key: fragment[key] for key in ("path", "source_id", "source_version", "locator", "fragment_id")}
                        if fragment is not None else None)
            if descriptor != expected:
                _fail("assembly_coverage_mismatch")
            seen_ranges.add(descriptor["fragment_id"])
        stage = _committed(git, _stage_path(source_run))
        stage_commit = _last_commit(git, base_head, _stage_path(source_run))
        git.run("merge-base", "--is-ancestor", review_commit, base_head)
        review_calls = [deepcopy(review["call"])]
        for node in review.get("decomposed_proof", {}).get("nodes", []):
            record = _load(_committed(git, node["path"]), "decomposed_review_result")
            if record["call"] not in review_calls:
                review_calls.append(deepcopy(record["call"]))
        inputs.append({"run_id": source_run, "binding": candidate["binding"],
                       "stage": {"path": _stage_path(source_run), "commit_sha": stage_commit, "sha256": _hash(stage)},
                       "review": {"path": _run_path(source_run, "review"), "commit_sha": review_commit,
                                  "sha256": _hash(_committed(git, _run_path(source_run, "review")))},
                       "extraction": deepcopy(candidate["extraction"]), "review_calls": review_calls})
        if "quality_repair_context" in candidate:
            inputs[-1]["generation_invocations"] = sorted(extraction_invocations(candidate))
        prior.append({"run_id": source_run, "binding": candidate["binding"], "objects": deepcopy(candidate["objects"]),
                      "review_id": review["review_id"], "batch_context": deepcopy(candidate["batch_context"])})
        for obj in candidate["objects"]:
            if obj["path"] in objects and objects[obj["path"]] != obj:
                _fail("assembly_object_conflict")
            objects[obj["path"]] = deepcopy(obj)
        output = candidate["extraction_output"]
        if output.get("reuse_refs"):
            _fail("assembly_reuse_unsupported")
        for values, indexed in ((output["objects"], raw_objects), (output["wikis"], raw_wikis)):
            for value in values:
                if value["id"] in indexed and indexed[value["id"]] != value:
                    _fail("assembly_object_conflict")
                indexed[value["id"]] = deepcopy(value)
        if bundle is not None:
            for key in quality:
                for value in output["quality"][key]:
                    if value not in quality[key]:
                        quality[key].append(deepcopy(value))
    if seen_ranges != set(fragments):
        _fail("assembly_coverage_mismatch")
    parents = sorted(item["batch_id"] for item in plan["batches"])
    selected = [item for item in plan["fragments"] if item["fragment_id"] in seen_ranges]
    batch_id = "batch-" + _hash({"plan_id": plan["plan_id"], "parent_batch_ids": parents,
                                 "fragment_ids": [item["fragment_id"] for item in selected]})[7:]
    context = _context(plan["plan_id"], batch_id, selected, parents)
    views = _views(sources, selected)
    output = {"objects": list(raw_objects.values()), "wikis": list(raw_wikis.values())}
    fields = {}
    if bundle is not None:
        qcontext = prepare_quality(sources, source_views=views)
        output.update(quality=quality, reuse_refs=[])
        try:
            validate_quality_output(output, qcontext)
        except CaptureError as error:
            raise CaptureError("assembly_quality_scope_mismatch", "分片质量口径不能原样合并；保留原稿，需完整范围重新加工") from error
        fields.update(quality_contract="skill-quality-v1", skill_bundle=deepcopy(bundle), quality_context=qcontext,
                      reuse_quality_requirements=[])
    ordered = list(objects.values())
    related = _related_context(git, [], sources, at_commit=base_head, max_runs=256)
    operations = _updated_objects(git, ordered, related, check_current=not historical)
    provenance = {"contract": CONTRACT, "mode": "verbatim_assembly_no_model_extract", "plan": deepcopy(plan),
                  "inputs": inputs, "knowledge_changed": False}
    key = _hash({"contract": CONTRACT, "plan_id": plan["plan_id"], "draft_bindings": inputs})
    candidate = {"run_id": run_id, "base_head": base_head, **_source_fields(sources),
                 "versions": deepcopy(plan["snapshot"]["versions"]), "created_at": created_at,
                 "mode": "incremental", "related_runs": [], "related": [], "execution_context": None,
                 "controls_digest": plan["snapshot"]["controls_digest"], "processing_key": key,
                 "batch_context": context, "source_views": views, "merge_inputs": prior,
                 "batch_material": {"draft_only": False, "merge_bindings": [
                     {"run_id": item["run_id"], "binding": item["binding"]} for item in prior],
                     "review_strategy": "decomposed-v1"}, "review_strategy": "decomposed-v1",
                 "assembly": provenance, "objects": ordered, "extraction_output": output,
                 "operations": operations, **fields}
    return candidate, sources


def preflight_assembly(repo, plan, draft_runs, run_id):
    """Check complete assembly/storage before a durable attempt reservation."""
    from .pipeline import _candidate_binding, _candidate_payload, _document, _hash, _run_path
    from .storage_limits import validate_file_bytes
    from .decomposed_review import (_input_bytes, _input_scope_inventory, _layout, _request,
                                    _prompt_metadata, REQUEST_CONTRACT)
    from .model_calls import MAX_INPUT_BYTES
    git = PipelineGit(repo)
    with git.lock():
        head = git.check()
        candidate, sources = _material(git, plan, draft_runs, run_id, head, _timestamp(), historical=False)
        started = {"run_id": run_id, "processing_key": candidate["processing_key"], "execution_context": None,
                   "source_paths": list(plan["source_paths"]), "related_runs": [], "reserved_calls": 0,
                   "started_at": candidate["created_at"], "assembly_contract": CONTRACT}
        # Include every actual stage field before reserving the source attempt.
        # The new base commit has the same fixed object-ID width as this HEAD.
        candidate.update(attempt_path=_run_path(run_id, "started"),
                         attempt_sha256=_hash(_document("processing_attempt", started, SCHEMA)))
        candidate["binding"] = _candidate_binding(candidate)
        validate_file_bytes("memory/staging/" + run_id + ".md", _document("candidate", candidate, SCHEMA))
        payload = _candidate_payload(git, candidate, sources)
        units, graph = _layout(candidate, payload)
        inventory = [{"path": obj["path"], "id": obj["metadata"]["id"],
                      "content_version": obj["metadata"]["content_version"], "object_sha256": _hash(obj)}
                     for obj in sorted(candidate["objects"], key=lambda obj: obj["path"])]
        provided = _input_scope_inventory(candidate, payload)
        prompt = ({"sha256": candidate["skill_bundle"]["sha256"], "source": "frozen-skill-bundle"}
                  if "skill_bundle" in candidate else _prompt_metadata())
        sizing_plan = {"plan_id": "decomposed-" + "0" * 64, "inventory": inventory, "relation_graph": graph,
                       "request_contract": REQUEST_CONTRACT, "input_scope_inventory": provided,
                       "input_scope_inventory_sha256": _hash(provided)}
        for unit in units:
            if unit["kind"] != "global" and _input_bytes(_request(sizing_plan, unit, candidate, payload), prompt) > MAX_INPUT_BYTES:
                return {"status": "blocked_capacity", "error": "decomposed_input_limit",
                        "model_calls": 0, "attempted_calls": 0}
    return {"status": "ready", "model_calls": 0, "attempted_calls": 0}


def finalize_drafts(repo, plan, draft_runs, run_id, *, model_call):
    """Preserve an exact assembled candidate; only real Review calls follow."""
    from .pipeline import (_candidate_binding, _candidate_payload, _committed, _document, _hash, _load,
                           _persist_review, _run_path, _stage_path,
                           _assert_fixed_current, resume_decomposed_processing)
    from .engine_provenance import reservation_file
    from .decomposed_review import run_decomposed_review
    git = PipelineGit(repo)
    if git.path(_stage_path(run_id)).is_file():
        return resume_decomposed_processing(repo, run_id, allowed_source_paths=plan["source_paths"], model_call=model_call)
    with git.lock():
        git.recover()
        head = git.check()
        candidate, sources = _material(git, plan, draft_runs, run_id, head, _timestamp(), historical=False)
        started_path = _run_path(run_id, "started")
        if git.path(started_path).is_file():
            # A pure-program assembly interrupted before its stage is safe to
            # repeat, with the same immutable reservation and no model replay.
            started_bytes = _committed(git, started_path)
            started = _load(started_bytes, "processing_attempt", SCHEMA)
            if started.get("processing_key") != candidate["processing_key"]:
                _fail("assembly_reservation_changed")
            candidate, sources = _material(git, plan, draft_runs, run_id, head, started["started_at"], historical=False)
        else:
            started = {"run_id": run_id, "processing_key": candidate["processing_key"], "execution_context": None,
                       "source_paths": list(plan["source_paths"]), "related_runs": [], "reserved_calls": 0,
                       "started_at": candidate["created_at"], "assembly_contract": CONTRACT}
            started_bytes = _document("processing_attempt", started, SCHEMA)
            files = {started_path: started_bytes}
            files.update(reservation_file(run_id, candidate["processing_key"], head, started_bytes, candidate["versions"]))
            head = git.write_files(files, head, "process: reserve verbatim final assembly without extraction")
            candidate["base_head"] = head
        candidate.update(attempt_path=started_path, attempt_sha256=_hash(started_bytes))
        candidate["binding"] = _candidate_binding(candidate)
        stage_bytes = _document("candidate", candidate, SCHEMA)
        stage_commit = git.write_files({_stage_path(run_id): stage_bytes}, git.check(), "process: preserve verbatim final union")
        payload = _candidate_payload(git, candidate, sources)
        outcome = run_decomposed_review(git, candidate, stage_commit, payload, model_call)
        calls = outcome["attempted_calls"]
        if "output" not in outcome:
            return {**outcome, "run_id": run_id, "stage_path": _stage_path(run_id),
                    "model_calls": calls, "attempted_calls": calls, "extract_calls": 0}
        _assert_fixed_current(git, candidate, sources, stage_bytes)
        result = _persist_review(git, candidate, stage_bytes, stage_commit, outcome["call"], outcome["output"],
                                 plan["source_paths"], git.check(outcome["head"]), proof=outcome["proof"], attempted_calls=calls)
        return {**result, "extract_calls": 0}


def read_assembled_candidate(git, run_id, candidate, stage_bytes, *, source_paths=None, historical=False, visited=()):
    """Rebuild the union from complete original draft computations and Reviews."""
    from .pipeline import (_candidate_binding, _committed, _hash, _last_commit, _load, _run_path,
                           _stage_path, normalize_source_paths)
    if run_id in visited or len(visited) >= 16:
        _fail("assembly_dependency_cycle")
    assembly = candidate.get("assembly")
    if (not isinstance(assembly, dict) or assembly.get("contract") != CONTRACT
            or assembly.get("mode") != "verbatim_assembly_no_model_extract"):
        _fail()
    paths = assembly["plan"]["source_paths"]
    if source_paths is not None and normalize_source_paths(source_paths, allow_versions=True, max_sources=256) != paths:
        _fail("evidence_source_mismatch")
    stage_commit = _last_commit(git, git.head(), _stage_path(run_id))
    if git.text("rev-parse", stage_commit + "^") != candidate["base_head"]:
        _fail("assembly_stage_changed")
    started_path = _run_path(run_id, "started")
    started_bytes = _committed(git, started_path)
    started = _load(started_bytes, "processing_attempt", SCHEMA)
    rebuilt, sources = _material(git, assembly["plan"], [item["run_id"] for item in assembly["inputs"]],
                                run_id, candidate["base_head"], started["started_at"])
    rebuilt.update(attempt_path=started_path, attempt_sha256=_hash(started_bytes))
    rebuilt["binding"] = _candidate_binding(rebuilt)
    if (rebuilt != candidate or git.read(stage_commit, _stage_path(run_id)) != stage_bytes
            or started != {"run_id": run_id, "processing_key": candidate["processing_key"], "execution_context": None,
                           "source_paths": paths, "related_runs": [], "reserved_calls": 0,
                           "started_at": candidate["created_at"], "assembly_contract": CONTRACT}):
        _fail("assembly_candidate_mismatch")
    git.run("merge-base", "--is-ancestor", _last_commit(git, git.head(), started_path), candidate["base_head"])
    from .engine_provenance import read_reservation
    engine = read_reservation(git, run_id)
    if engine is None or engine["versions"] != candidate["versions"]:
        _fail("assembly_engine_changed")
    return candidate, sources, stage_bytes, stage_commit
