"""Exercise real Git maintenance triggers in disposable repositories only."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from git_fixtures import configure_test_repository


class GitFixtureIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pdm-git-isolation-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def git(self, repo, *args, trace=None):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_")}
        if trace is not None:
            environment["GIT_TRACE2_EVENT"] = str(trace)
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-C", str(repo), *args],
            check=True, capture_output=True, text=True, env=environment, timeout=30,
        ).stdout.strip()

    def initialize(self, repo, *, bare=False):
        repo.mkdir()
        self.git(repo, "init", "--bare" if bare else "--initial-branch=main")
        configure_test_repository(repo)
        if not bare:
            self.git(repo, "config", "user.name", "Isolated Fixture")
            self.git(repo, "config", "user.email", "fixture@example.invalid")

    def maintenance_children(self, trace):
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertTrue(any(event.get("event") == "start" for event in events))
        return [event for event in events if event.get("event") == "child_start"
                and any(argument in {"maintenance", "gc"}
                        for argument in event.get("argv", []))]

    def test_commit_keeps_maintenance_out_of_fixture_cleanup(self):
        control_trace = self.root / "foreground-control.jsonl"
        isolated_trace = self.root / "isolated-commit.jsonl"
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            repo = Path(directory) / "repo"
            self.initialize(repo)
            # A foreground positive control proves Trace2 observes maintenance;
            # it cannot outlive this command or race the fixture's cleanup.
            self.git(repo, "-c", "maintenance.auto=true", "-c", "maintenance.autoDetach=false",
                     "-c", "gc.autoDetach=false", "commit", "--allow-empty", "-m", "control",
                     trace=control_trace)
            self.assertTrue(self.maintenance_children(control_trace))
            (repo / "fixture.txt").write_text("Artificial fixture only.\n")
            self.git(repo, "add", "fixture.txt")
            self.git(repo, "commit", "-m", "isolated commit", trace=isolated_trace)
            self.assertEqual(self.git(repo, "show", "HEAD:fixture.txt"), "Artificial fixture only.")
            self.assertEqual(self.maintenance_children(isolated_trace), [])
        self.assertFalse(repo.exists())

    def test_local_push_keeps_bare_remote_maintenance_out_of_cleanup(self):
        trace = self.root / "isolated-push.jsonl"
        with tempfile.TemporaryDirectory(dir=self.root) as directory:
            repo = Path(directory) / "repo"
            remote = Path(directory) / "remote.git"
            self.initialize(repo)
            self.initialize(remote, bare=True)
            (repo / "fixture.txt").write_text("Artificial round-trip fixture.\n")
            self.git(repo, "add", "fixture.txt")
            self.git(repo, "commit", "-m", "fixture source")
            self.git(repo, "push", str(remote), "HEAD:refs/heads/main", trace=trace)
            self.assertEqual(self.git(remote, "show", "main:fixture.txt"),
                             "Artificial round-trip fixture.")
            self.assertEqual(self.maintenance_children(trace), [])
        self.assertFalse(repo.exists())
        self.assertFalse(remote.exists())


if __name__ == "__main__":
    unittest.main()
