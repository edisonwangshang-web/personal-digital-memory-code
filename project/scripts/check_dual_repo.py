#!/usr/bin/env python3
"""Read-only check: public and private checkouts match on dev or master.

No private source/knowledge bodies, reports, Git refs or worktree files are
written. The CLI always queries GitHub; API injection exists only for unit tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
from urllib.parse import urlsplit


ROOTS = ("project/src/pdm", "project/prompts", "project/skills")
CORE = (*ROOTS, "pyproject.toml")
WORKFLOW = ".github/workflows/engineering.yml"
EXPECTED_JOBS = {
    *(f"isolated (Python {version}, {shard})"
      for version in ("3.11", "3.12")
      for shard in ("decomposed", "other-a", "other-b")),
    "foundation (Python 3.11)", "foundation (Python 3.12)",
}
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
MAX_BYTES = 8 * 1024 * 1024


class CheckError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise CheckError(code)


def _git(repo, *args):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0",
                       GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1")
    try:
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(repo), *args], capture_output=True,
                                check=True, env=environment, timeout=30)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        _fail("git_operation_failed")


def _text(repo, *args):
    try:
        return _git(repo, *args).decode("utf-8").strip()
    except UnicodeError:
        _fail("invalid_git_metadata")


def _branch(repo):
    return _text(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _repository(value):
    try:
        repo = Path(value).resolve(strict=True)
    except (OSError, RuntimeError):
        _fail("invalid_repository")
    if _text(repo, "rev-parse", "--show-toplevel") != str(repo):
        _fail("invalid_repository")
    common = Path(_text(repo, "rev-parse", "--git-common-dir"))
    common = common if common.is_absolute() else repo / common
    return repo, common.resolve()


def _identity(value):
    if not value or any(ord(c) < 33 for c in value):
        _fail("invalid_github_remote")
    if value.startswith("git@github.com:"):
        name = value.removeprefix("git@github.com:")
    else:
        try:
            url = urlsplit(value)
            if (url.hostname != "github.com" or url.port is not None or url.query
                    or url.fragment or url.password is not None
                    or not ((url.scheme == "https" and url.username is None)
                            or (url.scheme == "ssh" and url.username == "git"))):
                _fail("invalid_github_remote")
            name = url.path.lstrip("/")
        except ValueError:
            _fail("invalid_github_remote")
    name = name.removesuffix("/").removesuffix(".git")
    if not IDENTITY.fullmatch(name):
        _fail("invalid_github_remote")
    return name


def _origin(repo):
    fetch = _text(repo, "remote", "get-url", "--all", "origin").splitlines()
    push = _text(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()
    if len(fetch) != 1 or len(push) != 1:
        _fail("invalid_github_remote")
    first, second = _identity(fetch[0]), _identity(push[0])
    if first.casefold() != second.casefold():
        _fail("origin_identity_mismatch")
    return first


def _gh_api(endpoint):
    try:
        result = subprocess.run(["gh", "api", "--hostname", "github.com", endpoint],
                                capture_output=True, check=True, timeout=45)
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        _fail("github_api_failed")


def _metadata(api, name, private):
    value = api("repos/" + name)
    if (not isinstance(value, dict)
            or str(value.get("full_name", "")).casefold() != name.casefold()
            or value.get("private") is not private
            or value.get("visibility") != ("private" if private else "public")):
        _fail("data_repository_not_private" if private else "code_repository_not_public")


def _approved_ci(api, name, commit, branch, run_id):
    run = api(f"repos/{name}/actions/runs/{run_id}")
    if not isinstance(run, dict):
        _fail("ci_run_not_approved")
    attempt = run.get("run_attempt")
    if (run.get("id") != run_id or type(attempt) is not int or attempt < 1
            or run.get("head_sha") != commit or run.get("event") != "push"
            or run.get("head_branch") != branch
            or run.get("path") != WORKFLOW or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or any(not isinstance(run.get(key), dict)
                   or str(run[key].get("full_name", "")).casefold() != name.casefold()
                   for key in ("repository", "head_repository"))):
        _fail("ci_run_not_approved")
    value = api(f"repos/{name}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    jobs = value.get("jobs") if isinstance(value, dict) else None
    if (not isinstance(jobs, list) or value.get("total_count") != 8 or len(jobs) != 8
            or any(not isinstance(job, dict) for job in jobs)
            or {job.get("name") for job in jobs} != EXPECTED_JOBS
            or any(type(job.get("run_id")) is not int or job.get("run_id") != run_id
                   or type(job.get("run_attempt")) is not int or job.get("run_attempt") != attempt
                   or job.get("head_sha") != commit or job.get("status") != "completed"
                   or job.get("conclusion") != "success" for job in jobs)):
        _fail("ci_jobs_not_approved")
    return attempt


def _allowed(name):
    if name == "pyproject.toml":
        return True
    if name.startswith(ROOTS[0] + "/"):
        return name.endswith(".py")
    if name.startswith(ROOTS[1] + "/"):
        return name.endswith((".md", ".txt"))
    return name.startswith(ROOTS[2] + "/") and name.endswith((".md", ".json"))


def _file_bytes(path):
    try:
        with path.open("rb") as handle:
            value = handle.read(MAX_BYTES + 1)
    except OSError:
        _fail("core_file_unreadable")
    if len(value) > MAX_BYTES:
        _fail("core_file_too_large")
    return value


def _snapshot(repo, commit):
    saved = {}
    for row in _git(repo, "ls-tree", "-r", "-z", commit, "--", *CORE).split(b"\0"):
        if not row:
            continue
        try:
            header, name = row.split(b"\t", 1)
            mode, kind, blob = header.decode("ascii").split()
            name = name.decode("utf-8")
        except (ValueError, UnicodeError):
            _fail("invalid_core_file")
        if (mode not in {"100644", "100755"} or kind != "blob" or not _allowed(name)
                or any(part in {"", ".", ".."} for part in name.split("/"))):
            _fail("invalid_core_file")
        path = repo / name
        if any(parent.is_symlink() for parent in path.parents if parent != repo and repo in parent.parents):
            _fail("core_symlink_rejected")
        try:
            info = path.lstat()
        except OSError:
            _fail("core_file_unreadable")
        if not stat.S_ISREG(info.st_mode) or bool(info.st_mode & 0o111) != (mode == "100755"):
            _fail("core_file_mode_mismatch")
        actual = _file_bytes(path)
        if int(_text(repo, "cat-file", "-s", blob)) > MAX_BYTES:
            _fail("core_file_too_large")
        committed = _git(repo, "cat-file", "blob", blob)
        if actual != committed:
            _fail("core_worktree_mismatch")
        saved[name] = {"mode": mode, "bytes": len(actual),
                       "sha256": hashlib.sha256(actual).hexdigest()}
    if ("pyproject.toml" not in saved or ROOTS[0] + "/__init__.py" not in saved
            or any(not any(name.startswith(root + "/") for name in saved) for root in ROOTS)):
        _fail("incomplete_core")
    live = {"pyproject.toml"}
    for root in ROOTS:
        start = repo / root
        if not start.is_dir() or start.is_symlink():
            _fail("invalid_core_file")
        for directory, directories, files in os.walk(start, followlinks=False):
            if any((Path(directory) / name).is_symlink() for name in directories + files):
                _fail("core_symlink_rejected")
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in files:
                relative = (Path(directory) / name).relative_to(repo).as_posix()
                if not _allowed(relative):
                    _fail("unexpected_core_file")
                live.add(relative)
    if live != set(saved):
        _fail("core_path_mismatch")
    return saved


def check(code_repo, data_repo, code_ref, ci_run, *, api=None):
    """Return private metadata only; do not execute code or load personal bodies."""
    if not isinstance(code_ref, str) or not SHA.fullmatch(code_ref):
        _fail("full_code_commit_required")
    if not re.fullmatch(r"[1-9][0-9]{0,17}", str(ci_run)):
        _fail("invalid_ci_run")
    code, code_common = _repository(code_repo)
    data, data_common = _repository(data_repo)
    if code == data or code_common == data_common:
        _fail("repositories_not_separate")
    code_head = _text(code, "rev-parse", "--verify", "HEAD^{commit}")
    data_head = _text(data, "rev-parse", "--verify", "HEAD^{commit}")
    code_branch, data_branch = _branch(code), _branch(data)
    if code_branch not in {"dev", "master"} or data_branch not in {"dev", "master"}:
        _fail("unsupported_branch")
    if code_branch != data_branch:
        _fail("repository_branch_mismatch")
    for repo in (code, data):
        if _git(repo, "status", "--porcelain", "--untracked-files=all"):
            _fail("dirty_repository")
    if code_head != code_ref:
        _fail("code_checkout_mismatch")
    code_name, data_name = _origin(code), _origin(data)
    if code_name.casefold() == data_name.casefold():
        _fail("repositories_not_separate")
    api = _gh_api if api is None else api
    _metadata(api, code_name, False)
    _metadata(api, data_name, True)
    attempt = _approved_ci(api, code_name, code_ref, code_branch, int(ci_run))
    code_files, data_files = _snapshot(code, code_head), _snapshot(data, data_head)
    if code_files.keys() != data_files.keys():
        _fail("core_path_mismatch")
    if code_files != data_files:
        _fail("core_content_mismatch")
    program_commit = _text(data, "log", "-1", "--format=%H", data_head, "--", *CORE)
    if not SHA.fullmatch(program_commit):
        _fail("private_program_commit_missing")
    digest = hashlib.sha256(json.dumps(code_files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    command = ["env", "PYTHONPATH=" + str(data / "project/src"), sys.executable,
               "-B", "-m", "pdm", "doctor", "--repo", str(data)]
    for repo, head, branch, name in ((code, code_head, code_branch, code_name),
                                     (data, data_head, data_branch, data_name)):
        if (_text(repo, "rev-parse", "--verify", "HEAD^{commit}") != head
                or _branch(repo) != branch
                or _git(repo, "status", "--porcelain", "--untracked-files=all")
                or _origin(repo).casefold() != name.casefold()):
            _fail("repository_changed_during_check")
    return {"status": "matched", "code_repository": code_name, "code_commit": code_ref,
            "code_branch": code_branch,
            "ci_run": int(ci_run), "ci_attempt": attempt, "ci_jobs": 8,
            "data_repository": data_name, "data_branch": data_branch,
            "data_head": data_head, "private_program_commit": program_commit,
            "core_files": len(code_files), "core_sha256": digest,
            "private_doctor_command": shlex.join(command), "writes": 0, "model_calls": 0}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        _fail("invalid_arguments")


def main(argv=None):
    try:
        parser = _Parser(description=__doc__)
        for name in ("code-repo", "data-repo", "code-ref", "ci-run"):
            parser.add_argument("--" + name, required=True)
        args = parser.parse_args(argv)
        result = check(args.code_repo, args.data_repo, args.code_ref, args.ci_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except CheckError as error:
        print(json.dumps({"status": "blocked", "code": error.code}))
        return 1
    except Exception:
        print(json.dumps({"status": "blocked", "code": "check_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
