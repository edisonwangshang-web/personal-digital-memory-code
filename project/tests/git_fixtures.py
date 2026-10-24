"""Local configuration for disposable Git repositories created by tests."""

import os
import subprocess


def configure_test_repository(repo):
    """Keep automatic maintenance out of temporary-repository cleanup."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for key, value in (("maintenance.auto", "false"), ("gc.auto", "0")):
        subprocess.run(
            ["git", "-C", str(repo), "config", "--local", key, value],
            check=True, capture_output=True, env=environment,
        )
