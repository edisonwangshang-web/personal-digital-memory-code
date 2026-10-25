"""Immutable Review feedback for an explicit, operator-driven revision.

A rejected candidate is work to repair, never another factual source.  Readers
verify its complete original computation before using its feedback or carrying
its authority snapshots into a later publication.
"""

from copy import deepcopy

from .contracts import CaptureError, quality_schema_version

SCHEMA = "0.9"
QUALITY_SCHEMA = "0.10"
CONTRACT = "review-feedback-v1"
MAX_REVISION_DEPTH = 8


def _fail(code="invalid_revision_context"):
    raise CaptureError(code, "返工上下文未通过原候选、独立审核、来源与控制核验")


def _chain(candidate):
    """Bound embedded feedback before recursive computation validation."""
    seen = set()
    for _ in range(MAX_REVISION_DEPTH + 1):
        run = candidate.get("run_id")
        if not isinstance(run, str) or run in seen:
            _fail("revision_dependency_cycle")
        seen.add(run)
        previous = candidate.get("revision_context")
        if previous is None:
            return
        if len(seen) >= MAX_REVISION_DEPTH or not isinstance(previous, dict):
            _fail("revision_dependency_limit")
        candidate = previous.get("candidate")
        if not isinstance(candidate, dict) or previous.get("source_run_id") != candidate.get("run_id"):
            _fail("revision_dependency_cycle")
    _fail("revision_dependency_limit")


def _read_rejected(git, run_id, source_paths, related_runs, at_commit):
    from .pipeline import (
        _ancestor, _candidate_bindings, _committed, _hash, _last_commit,
        _load, _read_fixed_candidate, _read_reviewed, _related_run_ids,
        _run_path, _stage_path, control_guard, normalize_source_paths,
    )

    stage_path = _stage_path(run_id)
    # The selected scope is checked from the committed derived metadata before
    # opening any original source body, including on a failed authorization.
    raw_candidate = _load(_committed(git, stage_path, at_commit), "candidate")
    _chain(raw_candidate)
    maximum = 256 if "batch_context" in raw_candidate else 8
    selected = normalize_source_paths(source_paths, allow_versions=True, max_sources=maximum)
    bindings = _candidate_bindings(raw_candidate)
    bound = normalize_source_paths([item["path"] for item in bindings], allow_versions=True, max_sources=maximum)
    if selected != bound:
        _fail("revision_source_mismatch")
    requested_related = _related_run_ids(related_runs or [], max_runs=maximum)
    if requested_related != raw_candidate.get("related_runs", []):
        _fail("revision_related_mismatch")
    if (raw_candidate.get("execution_context") is not None
            or raw_candidate.get("continuation_context") is not None
            or raw_candidate.get("batch_material", {}).get("draft_only", False)):
        _fail("revision_not_operator_candidate")
    frozen_stage = _committed(git, stage_path, at_commit)
    frozen_commit = _last_commit(git, at_commit, stage_path)
    _ancestor(git, frozen_commit, at_commit)
    if (_last_commit(git, git.head(), stage_path) != frozen_commit
            or git.read(frozen_commit, stage_path) != frozen_stage):
        _fail("revision_candidate_changed")
    candidate, sources, stage_bytes, stage_commit = _read_fixed_candidate(
        git, run_id, source_paths=selected, historical=True)
    _ancestor(git, stage_commit, at_commit)
    if git.read(at_commit, stage_path) != stage_bytes:
        _fail("revision_candidate_changed")
    review_path = _run_path(run_id, "review")
    review_bytes = _committed(git, review_path, at_commit)
    review = _load(review_bytes, "review")
    review_commit = _last_commit(git, at_commit, review_path)
    _ancestor(git, review_commit, at_commit)
    # Verify the ordinary or decomposed original chain, not merely a verdict
    # copied out of review.md.  Only its final non-passed gate is admissible.
    if review.get("output", {}).get("verdict") != "needs_revision":
        _fail("revision_requires_needs_revision")
    verified_candidate, verified_review, verified_commit = _read_reviewed(
        git, run_id, source_paths=selected, historical=True, allow_nonpassed=True)
    if (verified_candidate != candidate or verified_review != review or verified_commit != review_commit
            or _last_commit(git, git.head(), review_path) != review_commit
            or git.read(review_commit, review_path) != review_bytes):
        _fail("revision_review_changed")
    original_controls = control_guard(git, candidate["base_head"])["digest"]
    if control_guard(git, at_commit)["digest"] != original_controls:
        _fail("revision_controls_changed")
    if candidate.get("mode") == "incremental" and candidate.get("controls_digest") != original_controls:
        _fail("revision_controls_changed")
    return candidate, sources, stage_bytes, stage_commit, review, review_bytes, review_commit, original_controls


def build_revision_context(git, revision_from_run, *, source_paths, related_runs=(), at_commit=None):
    """Return exact, rebuildable feedback; no model, Git write, or retry reset."""
    from .pipeline import _candidate_bindings, _hash, _run_path, _stage_path

    anchor = at_commit or git.head()
    candidate, _, stage_bytes, stage_commit, review, review_bytes, review_commit, controls = _read_rejected(
        git, revision_from_run, source_paths, related_runs, anchor)
    context = {
        "schema_version": (quality_schema_version(candidate["skill_bundle"])
                           if candidate.get("quality_contract") == "skill-quality-v1" else SCHEMA),
        "contract": CONTRACT,
        "source_run_id": revision_from_run,
        "trust": "unpublished_candidate_and_review_feedback_not_source_facts",
        "candidate_binding": candidate["binding"],
        "stage": {"path": _stage_path(revision_from_run), "commit_sha": stage_commit, "sha256": _hash(stage_bytes)},
        "review": {"path": _run_path(revision_from_run, "review"), "commit_sha": review_commit, "sha256": _hash(review_bytes)},
        "source_bindings": deepcopy(_candidate_bindings(candidate)),
        "related_runs": deepcopy(candidate.get("related_runs", [])),
        "controls_digest": controls,
        "candidate": deepcopy(candidate),
        "review_record": deepcopy(review),
        "review_output": deepcopy(review["output"]),
    }
    context["binding"] = _hash(context)
    context["model_context"] = _model_projection(context)
    return context


def _model_projection(context):
    candidate = context["candidate"]
    keys = ("schema_version", "contract", "source_run_id", "trust", "candidate_binding",
            "stage", "review", "source_bindings", "related_runs", "controls_digest")
    projected = {**{key: deepcopy(context[key]) for key in keys}, "context_binding": context["binding"],
            "candidate": {"run_id": candidate["run_id"], "binding": candidate["binding"],
                          "versions": deepcopy(candidate["versions"]),
                          "objects": deepcopy(candidate["objects"]),
                          "operations": deepcopy(candidate.get("operations", {}))},
            "review_record": deepcopy(context["review_record"])}
    if candidate.get("quality_contract") == "skill-quality-v1":
        # Preserve the exact previous coverage/statistical/temporal decisions
        # for repair.  Prior Skill rules are provenance, not replacement
        # instructions for the newly selected extraction Skill.
        projected["candidate"]["quality_provenance"] = {
            "quality_contract": candidate["quality_contract"],
            "skill_bundle_sha256": candidate["skill_bundle"]["sha256"],
            "quality_context": deepcopy(candidate["quality_context"]),
            "quality": deepcopy(candidate["extraction_output"]["quality"]),
        }
    return projected


def _valid_schema(context):
    if not isinstance(context, dict) or context.get("contract") != CONTRACT:
        return False
    candidate = context.get("candidate")
    if not isinstance(candidate, dict):
        return False
    expected = (quality_schema_version(candidate.get("skill_bundle"))
                if candidate.get("quality_contract") == "skill-quality-v1" else SCHEMA)
    return context.get("schema_version") == expected


def revision_model_context(context):
    """Return the persisted, complete semantic feedback without duplicate raw.

    All proposed objects and the entire actual Review remain.  The unchanged
    original input is already in the new request, while its old exact envelope
    is bound by immutable pointers and the full context binding.
    """
    from .pipeline import _hash
    if not _valid_schema(context):
        _fail()
    material = {key: value for key, value in context.items() if key not in {"binding", "model_context"}}
    try:
        expected = _model_projection(context)
    except (KeyError, TypeError, ValueError):
        _fail("revision_context_mismatch")
    if context.get("binding") != _hash(material) or context.get("model_context") != expected:
        _fail("revision_context_mismatch")
    return deepcopy(context["model_context"])


def verify_revision_context(git, context, *, source_paths, related_runs=(), at_commit=None, historical=False):
    """Rebuild feedback and return the rejected computation's full authority DAG.

    ``historical`` does not relax binding checks.  The caller's candidate base
    chooses the control and ancestry snapshot for both current and old reads.
    """
    from .pipeline import _candidate_bindings, _computation_snapshots, _hash, _last_commit

    if not _valid_schema(context):
        _fail()
    expected = build_revision_context(git, context.get("source_run_id"), source_paths=source_paths,
                                      related_runs=related_runs, at_commit=at_commit)
    if context != expected:
        _fail("revision_context_mismatch")
    candidate = expected["candidate"]
    entries = _computation_snapshots(git, candidate)
    for binding in _candidate_bindings(candidate):
        entries.append({"path": binding["path"], "commit_sha": binding["commit_sha"],
                        "sha256": binding["source_bytes_sha256"]})
        commit = _last_commit(git, expected["stage"]["commit_sha"], binding["receipt_path"])
        entries.append({"path": binding["receipt_path"], "commit_sha": commit,
                        "sha256": _hash(git.read(commit, binding["receipt_path"]))})
    return list({(entry["commit_sha"], entry["path"]): entry for entry in entries}.values())
