"""Read-only checks; never infer external service access from local tooling."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import __version__
from .compatibility import inspect_memory_schema


def inspect_environment(repo: Path) -> dict:
    result = {
        "package_version": __version__,
        "python": sys.version.split()[0],
        "python_supported": sys.version_info >= (3, 11),
        "git_available": shutil.which("git") is not None,
        "repository": str(repo.resolve()),
        "git_worktree": False,
        "head_available": False,
        "remote_read_write": "unverified",
        "independent_model_call": "unverified",
        "scheduled_environment": "unverified",
        "production_deployment": "none",
    }
    if result["git_available"] and repo.is_dir():
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update({"GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"})
        try:
            probe = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10, check=False,
                env=environment,
            )
            result["git_worktree"] = (
                probe.returncode == 0
                and Path(probe.stdout.strip()).resolve() == repo.resolve()
            )
            if result["git_worktree"]:
                head = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD^{commit}"],
                    capture_output=True, text=True, timeout=10, check=False,
                    env=environment,
                )
                result["head_available"] = head.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            result["git_probe"] = "failed"
    result["memory_schema"] = inspect_memory_schema(repo)
    result["memory_writable"] = result["memory_schema"]["writable"]
    result["local_ready"] = all(
        result[k] for k in ("python_supported", "git_available", "git_worktree", "head_available", "memory_writable")
    )
    return result
