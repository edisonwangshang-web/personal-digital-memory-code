"""Clone-local runtime identity and read-only runner/promotion inspection.

Bindings contain references to credentials, never credentials.  Local probes do
not attest that a GPT scheduled execution has these capabilities.  No function
in this module provisions a task, calls a model, pushes, or merges branches.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit

from .contracts import CaptureError, canonical


BINDING_FILE = "pdm-environment.json"
SCHEMA = "1"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_SOURCE_ID = re.compile(r"src-[0-9a-f]{64}")
_SOURCE_PATH = re.compile(r"memory/sources/(?:conversations|inbox)/src-[0-9a-f]{64}/sha256-[0-9a-f]{64}\.md")
CAPABILITIES = ("scoped_read", "independent_model_calls", "program_validation",
                "conditional_git_write_readback", "persistent_recovery")


def _fail(code):
    # Do not repeat configuration values or Git stderr (which may contain auth).
    raise CaptureError(code, code)


def _git(repo, *args, input_data=None, index=None):
    environment = {key: value for key, value in os.environ.items() if key not in {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if index is not None:
        environment["GIT_INDEX_FILE"] = str(index)
    try:
        return subprocess.run(["git", *args], cwd=repo, env=environment,
                              input=input_data, capture_output=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        _fail("runtime_git_failed")


def _text(repo, *args):
    return _git(repo, *args).decode("utf-8").strip()


def _absolute(value):
    if not isinstance(value, str) or not value or "\0" in value:
        _fail("invalid_runtime_path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        _fail("invalid_runtime_path")
    try:
        if path != path.resolve():
            _fail("invalid_runtime_path")
    except (OSError, RuntimeError):
        _fail("invalid_runtime_path")
    return path


def remote_identity(value):
    """Canonical host/repository identity; reject credential-bearing URLs."""
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(c) < 33 for c in value):
        _fail("invalid_runtime_remote")
    if value.startswith("/"):
        return str(_absolute(value))
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            _fail("invalid_runtime_remote")
    else:
        try:
            url = urlsplit(value)
            if (url.scheme not in {"https", "ssh"} or not url.hostname or url.password is not None
                    or url.query or url.fragment or (url.username is not None and not (url.scheme == "ssh" and url.username == "git"))
                    or url.port is not None):
                _fail("invalid_runtime_remote")
            host, path = url.hostname, url.path.lstrip("/")
        except ValueError:
            _fail("invalid_runtime_remote")
    path = path.rstrip("/").removesuffix(".git")
    if not path or any(part in {"", ".", ".."} for part in path.split("/")) or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
        _fail("invalid_runtime_remote")
    return "https://" + host.lower() + "/" + path


def _state_root(value, repo, git_dir):
    path = _absolute(value)
    try:
        common = Path(_text(repo, "rev-parse", "--git-common-dir"))
        common = common if common.is_absolute() else repo / common
        if any(path.is_relative_to(root.resolve()) for root in (repo, git_dir, common)):
            _fail("runtime_state_inside_repository")
        for parent in (path, *path.parents):
            if parent.is_symlink():
                _fail("invalid_runtime_state")
            marker = parent / ".git"
            if marker.is_file() or (marker.is_dir() and (marker / "HEAD").is_file()):
                _fail("runtime_state_inside_repository")
            if (parent / "HEAD").is_file() and (parent / "objects").is_dir():
                _fail("runtime_state_inside_repository")
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            _fail("invalid_runtime_state")
    except OSError:
        _fail("invalid_runtime_state")
    return path


def _location(repo):
    repo = Path(repo).resolve()
    if _text(repo, "rev-parse", "--show-toplevel") != str(repo):
        _fail("invalid_repository")
    git_dir = Path(_text(repo, "rev-parse", "--absolute-git-dir"))
    return repo, git_dir, git_dir / BINDING_FILE


def _validate_binding(value, repo, git_dir, remote=None):
    required = {"schema_version", "environment", "repo_root", "remote_name", "remote_identity",
                "branch", "ref", "state_root", "credential_refs"}
    if not isinstance(value, dict) or set(value) != required or value["schema_version"] != SCHEMA:
        _fail("invalid_environment_binding")
    if not isinstance(value["environment"], str):
        _fail("invalid_environment_binding")
    branch = {"dev": "dev", "prod": "master"}.get(value["environment"])
    if branch is None or value["branch"] != branch or value["ref"] != "refs/heads/" + branch:
        _fail("invalid_environment_binding")
    if _absolute(value["repo_root"]) != repo:
        _fail("runtime_repository_mismatch")
    if not isinstance(value["remote_name"], str) or not _REMOTE.fullmatch(value["remote_name"]):
        _fail("invalid_environment_binding")
    if remote is not None and remote != value["remote_name"]:
        _fail("runtime_remote_mismatch")
    refs = value["credential_refs"]
    if not isinstance(refs, dict) or refs.keys() - {"git", "model"} or any(not isinstance(item, str) or not _LABEL.fullmatch(item) for item in refs.values()):
        _fail("invalid_credential_reference")
    expected = remote_identity(value["remote_identity"])
    if expected != value["remote_identity"]:
        _fail("invalid_environment_binding")
    try:
        actual_ref = _text(repo, "symbolic-ref", "--quiet", "HEAD")
    except CaptureError:
        _fail("runtime_branch_mismatch")
    if actual_ref != value["ref"]:
        _fail("runtime_branch_mismatch")
    urls = _text(repo, "remote", "get-url", "--all", value["remote_name"]).splitlines()
    push_urls = _text(repo, "remote", "get-url", "--push", "--all", value["remote_name"]).splitlines()
    if not urls or not push_urls or any(remote_identity(url) != expected for url in [*urls, *push_urls]):
        _fail("runtime_remote_mismatch")
    _state_root(value["state_root"], repo, git_dir)
    return dict(value)


def _load(path):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                _fail("invalid_environment_binding")
            value[key] = item
        return value
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                _fail("invalid_environment_binding")
            raw = stream.read(65537)
        if len(raw) > 65536:
            _fail("invalid_environment_binding")
        return json.loads(raw, object_pairs_hook=unique)
    except (OSError, UnicodeError, ValueError, RecursionError):
        _fail("invalid_environment_binding")


def require_environment(repo, *, require_binding=False, remote=None):
    """Validate an existing clone-local binding; legacy unbound repos opt out."""
    repo, git_dir, path = _location(repo)
    if not path.exists() and not path.is_symlink():
        if require_binding:
            _fail("environment_binding_required")
        return None
    return _validate_binding(_load(path), repo, git_dir, remote)


def bind_environment(repo, config):
    """Explicitly create one immutable, private binding in this clone's Git dir."""
    repo, git_dir, path = _location(repo)
    value = _validate_binding(config, repo, git_dir)
    if path.exists() or path.is_symlink():
        existing = require_environment(repo, require_binding=True)
        if existing != value:
            _fail("environment_already_bound")
        return {"status": "already_bound", "binding": existing}
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(git_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        _fail("environment_binding_write_failed")
    return {"status": "bound", "binding": require_environment(repo, require_binding=True)}


def validate_runner_config(value):
    """Validate deployment references, not claims that capabilities passed."""
    fields = {"schema_version", "environment", "platform", "platform_task_id", "schedule_id",
              "code_commit", "transport", "allowed_source_ids", "allowed_source_paths",
              "trigger_observer", "state_root", "daily_time", "timezone"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != SCHEMA:
        _fail("invalid_runner_config")
    if not isinstance(value["environment"], str) or value["environment"] not in {"dev", "prod"} or value["timezone"] != "Asia/Shanghai" or value["daily_time"] != "22:00":
        _fail("invalid_runner_config")
    for field in ("platform", "platform_task_id", "schedule_id", "transport", "trigger_observer"):
        if not isinstance(value[field], str) or not _LABEL.fullmatch(value[field]):
            _fail("invalid_runner_config")
    if not isinstance(value["code_commit"], str) or not _COMMIT.fullmatch(value["code_commit"]):
        _fail("invalid_runner_config")
    for field, pattern in (("allowed_source_ids", _SOURCE_ID), ("allowed_source_paths", _SOURCE_PATH)):
        items = value[field]
        if (not isinstance(items, list) or len(items) > 256 or any(not isinstance(item, str) or not pattern.fullmatch(item) for item in items)
                or len(set(items)) != len(items)):
            _fail("invalid_runner_scope")
    if not value["allowed_source_ids"] and not value["allowed_source_paths"]:
        _fail("invalid_runner_scope")
    _absolute(value["state_root"])
    return {**value, "allowed_source_ids": sorted(value["allowed_source_ids"]),
            "allowed_source_paths": sorted(value["allowed_source_paths"])}


def probe_runner(repo, config, *, receipt_refs=None):
    """Inspect this host only; references never manufacture platform attestation.

    A future platform adapter must query actual executions and verify their
    task/config/version binding before declaring any of the five capabilities
    passed. This offline entry deliberately cannot accept a ``passed: true``.
    """
    config = validate_runner_config(config)
    binding = require_environment(repo, require_binding=True)
    if binding["environment"] != config["environment"] or binding["state_root"] != config["state_root"]:
        _fail("runner_environment_mismatch")
    refs = [] if receipt_refs is None else receipt_refs
    if (not isinstance(refs, list) or len(refs) > 32 or any(not isinstance(ref, str) or not _LABEL.fullmatch(ref) for ref in refs)):
        _fail("invalid_capability_receipt")
    local = {"git_executable_found": shutil.which("git") is not None,
             "python_executable_found": shutil.which("python3") is not None,
             "codex_executable_found": shutil.which("codex") is not None,
             "binding": True, "state_directory_permissions": True}
    return {"status": "blocked", "reason": "platform_execution_unverified",
            "platform": config["platform"], "platform_task_id": config["platform_task_id"],
            "schedule_id": config["schedule_id"], "code_commit": config["code_commit"],
            "transport": config["transport"], "config_sha256": hashlib.sha256(canonical(config)).hexdigest(),
            "local_checks": local, "receipt_refs": refs,
            "capabilities": {capability: {"status": "unverified", "reason": "verified_platform_receipt_required"}
                             for capability in CAPABILITIES},
            "platform_task_status": "unverified", "task_created_by_probe": False,
            "task_enabled_by_probe": False, "model_calls": 0}


def _commit(repo, value):
    if not isinstance(value, str) or not value or len(value) > 256 or value.startswith("-") or "\0" in value:
        _fail("invalid_promotion_ref")
    return _text(repo, "rev-parse", "--verify", "--end-of-options", value + "^{commit}")


def _protected(path):
    roots = ("memory", "fixtures", "archive", ".pdm")
    return (any(path == root or path.startswith(root + "/") for root in roots)
            or any(part in {BINDING_FILE, ".pdm-environment.json"} for part in Path(path).parts))


def _tree(repo, commit):
    entries = {}
    for entry in _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if entry:
            header, path = entry.split(b"\t", 1)
            entries[path.decode("utf-8")] = header.decode("ascii")
    return entries


def _development_ancestor(repo, commit):
    for reference in ("refs/heads/dev", "refs/remotes/origin/dev"):
        try:
            tip = _commit(repo, reference)
            _git(repo, "merge-base", "--is-ancestor", commit, tip)
            return True
        except CaptureError:
            continue
    return False


def promotion_check(repo, base, candidate, *, dev_commit=None):
    """Read a proposed merge tree and require production data to stay intact."""
    base, candidate = _commit(repo, base), _commit(repo, candidate)
    parents = _text(repo, "rev-list", "--parents", "-n", "1", candidate).split()[1:]
    reasons = []
    if len(parents) != 2 or parents[0] != base:
        reasons.append("expected_two_parent_merge_from_production_base")
    development = _commit(repo, dev_commit) if dev_commit is not None else (parents[1] if len(parents) == 2 else None)
    if dev_commit is not None and (len(parents) != 2 or parents[1] != development):
        reasons.append("unexpected_development_parent")
    if development is not None and not _development_ancestor(repo, development):
        reasons.append("development_parent_not_in_dev_history")
    names = _git(repo, "diff-tree", "--no-commit-id", "--no-renames", "--name-status", "-r", "-z", base, candidate).decode("utf-8").split("\0")
    protected = []
    for index in range(0, len(names) - 1, 2):
        status_code, path = names[index:index + 2]
        if _protected(path):
            protected.append({"status": status_code, "path": path, "preserve_from": base})
    if protected:
        reasons.append("production_data_or_runtime_state_changed")
    unexpected_code = []
    if development is not None:
        candidate_tree, development_tree = _tree(repo, candidate), _tree(repo, development)
        unexpected_code = sorted(path for path in candidate_tree.keys() | development_tree.keys()
                                 if not _protected(path) and candidate_tree.get(path) != development_tree.get(path))
        if unexpected_code:
            reasons.append("code_tree_differs_from_development")
    return {"status": "blocked" if reasons else "passed", "base_commit": base,
            "candidate_commit": candidate, "dev_commit": development, "parents": parents, "reasons": reasons,
            "protected_changes": protected, "unexpected_code_paths": unexpected_code, "writes": 0,
            "environment_binding": "clone_local_not_part_of_git_tree"}


def prepare_promotion(repo, base, dev_commit):
    """Build an unattached two-parent code promotion; never move refs or files.

    Protected entries (including Git modes) come from production. Every other
    entry is exactly the selected dev snapshot. A private alternate index keeps
    the caller's real index, worktree and every branch untouched.
    """
    repo, git_dir, _ = _location(repo)
    base, development = _commit(repo, base), _commit(repo, dev_commit)
    if base == development or not _development_ancestor(repo, development):
        _fail("invalid_promotion_development")
    base_tree, development_tree = _tree(repo, base), _tree(repo, development)
    expected = {path: header for path, header in development_tree.items() if not _protected(path)}
    expected.update({path: header for path, header in base_tree.items() if _protected(path)})
    with tempfile.TemporaryDirectory(prefix="pdm-promotion-", dir=git_dir) as temporary:
        index = Path(temporary) / "index"
        _git(repo, "read-tree", "--empty", index=index)
        rows = []
        for path, header in sorted(expected.items()):
            mode, _, blob = header.split()
            rows.append((mode + " " + blob + "\t" + path).encode("utf-8") + b"\0")
        _git(repo, "update-index", "-z", "--index-info", input_data=b"".join(rows), index=index)
        tree = _git(repo, "write-tree", index=index).decode("ascii").strip()
        candidate = _text(repo, "-c", "commit.gpgsign=false", "commit-tree", tree, "-p", base,
                          "-p", development, "-m", "release: prepare isolated code promotion")
    report = promotion_check(repo, base, candidate, dev_commit=development)
    if report["status"] != "passed":
        _fail("promotion_candidate_invalid")
    return {"status": "prepared", "candidate_commit": candidate, "candidate_tree": tree,
            "base_commit": base, "dev_commit": development, "report": report,
            "git_objects_created": True, "refs_changed": False, "worktree_changed": False, "pushed": False}


def promotion_delivery_check(repo, base, actual_merge, head_commit, *, head_ref):
    """Verify both an ephemeral delivery candidate and GitHub's outer merge."""
    base, actual, head = (_commit(repo, value) for value in (base, actual_merge, head_commit))
    if head_ref == "dev":
        return promotion_check(repo, base, actual, dev_commit=head)
    if head_ref != "promotion/" + head:
        _fail("invalid_promotion_delivery_branch")
    prepared = promotion_check(repo, base, head)
    parents = _text(repo, "rev-list", "--parents", "-n", "1", actual).split()[1:]
    matching = (parents == [base, head]
                and _text(repo, "rev-parse", actual + "^{tree}") == _text(repo, "rev-parse", head + "^{tree}"))
    return {"status": "passed" if prepared["status"] == "passed" and matching else "blocked",
            "prepared_candidate": prepared, "actual_merge_commit": actual,
            "actual_merge_matches_prepared": matching, "writes": 0}
