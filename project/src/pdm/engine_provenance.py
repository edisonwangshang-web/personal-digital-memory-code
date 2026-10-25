"""Immutable engineering identity, independent of the knowledge Git checkout.

The generic 0.4 Markdown envelope carries a separate strict v1 contract. Old
reservations have no sidecar and retain their original historical contract.
"""

from copy import deepcopy
from pathlib import Path
import re

from .contracts import CaptureError
from .git_adapter import GitAdapter


CONTRACT = "public-engine-v1"
REPOSITORY = "pdm-public-engine"
CORE_PATHS = ("project/src/pdm", "project/prompts", "project/skills", "pyproject.toml")
SCHEMA = "0.4"


def _fail():
    raise CaptureError("engine_provenance_changed", "工程来源证明与原预约不一致；未改写原证明或猜测工程版本")


def loaded_engine():
    """Locate the checkout containing this imported module, never the data root."""
    return GitAdapter(Path(__file__).resolve().parents[3])


def program_commit():
    value = loaded_engine().text("log", "-1", "--format=%H", "--", *CORE_PATHS)
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value):
        _fail()
    return value


def reservation_file(run_id, processing_key, data_base_head, started_bytes, versions):
    from .pipeline import _document, _hash, _run_path
    value = {"run_id": run_id, "contract": CONTRACT, "repository": REPOSITORY,
             "code_commit": loaded_engine().head(), "data_base_head": data_base_head,
             "processing_key": processing_key, "started_sha256": _hash(started_bytes),
             "versions": deepcopy(versions)}
    return {_run_path(run_id, "engine"): _document("engine_provenance", value, SCHEMA)}


def read_reservation(git, run_id):
    """Read an original same-commit sidecar; absence is allowed only if never present."""
    from .pipeline import _committed, _hash, _last_commit, _load, _run_path
    path, started_path = _run_path(run_id, "engine"), _run_path(run_id, "started")
    head = git.head()
    history = git.text("log", "--full-history", "--format=%H", head, "--", path).splitlines()
    if not history:
        if git.path(path).exists():
            _fail()
        return None
    commit = _last_commit(git, head, started_path)
    if commit not in history or not git.path(path).is_file():
        _fail()
    original_tree = git.run("ls-tree", "-z", commit, "--", path)
    for change in history:
        if change == commit:
            continue
        # Full history includes TREESAME merges that merely carry the original
        # reservation across branches. Keep those, while inspecting every
        # branch's actual edits (including edits later restored to old bytes).
        parents = git.text("rev-list", "--parents", "-n", "1", change).split()[1:]
        if len(parents) < 2 or git.run("ls-tree", "-z", change, "--", path) != original_tree:
            _fail()
    raw = _committed(git, path, commit)
    value = _load(raw, "engine_provenance", SCHEMA)
    started_bytes = _committed(git, started_path, commit)
    started = _load(started_bytes, "processing_attempt")
    versions = value.get("versions")
    fields = {"run_id", "contract", "repository", "code_commit", "data_base_head", "processing_key",
              "started_sha256", "versions"}
    version_fields = {"program_sha256", "rules_version", "domain_policy_version", "domain_policy_sha256", "program_commit"}
    if (set(value) != fields or value["run_id"] != run_id or value["contract"] != CONTRACT
            or value["repository"] != REPOSITORY or value["data_base_head"] != git.text("rev-parse", commit + "^")
            or value["processing_key"] != started.get("processing_key") or value["started_sha256"] != _hash(started_bytes)
            or not isinstance(versions, dict) or set(versions) != version_fields
            or any(not isinstance(versions[key], str) or not re.fullmatch(r"sha256-[0-9a-f]{64}", versions[key])
                   for key in ("program_sha256", "rules_version", "domain_policy_sha256"))
            or not isinstance(versions["domain_policy_version"], str) or not versions["domain_policy_version"]
            or any(not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha)
                   for sha in (value["code_commit"], versions["program_commit"]))):
        _fail()
    return value
