"""Runtime contracts use only temporary Git data and never invoke a model."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.git_adapter import GitAdapter
from pdm.runtime import (CAPABILITIES, bind_environment, probe_runner,
                         prepare_promotion, promotion_check, promotion_delivery_check,
                         remote_identity, require_environment,
                         validate_runner_config)
from pdm.transaction import PipelineGit


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          check=True).stdout.decode().strip()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Isolated Runtime")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "isolated baseline")
        self.base = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "master", self.base)
        git(self.repo, "remote", "add", "origin", "https://github.com/fixture/private.git")
        self.state = self.root / "private-state"
        self.state.mkdir(mode=0o700)
        self.binding = {"schema_version": "1", "environment": "dev", "repo_root": str(self.repo),
                        "remote_name": "origin", "remote_identity": "https://github.com/fixture/private",
                        "branch": "dev", "ref": "refs/heads/dev", "state_root": str(self.state),
                        "credential_refs": {"git": "existing-git-auth", "model": "existing-model-auth"}}
        self.config = {"schema_version": "1", "environment": "dev", "platform": "gpt-cloud",
                       "platform_task_id": "task-fixture", "schedule_id": "fixture-daily",
                       "code_commit": self.base, "transport": "native-codex",
                       "allowed_source_ids": ["src-" + "1" * 64], "allowed_source_paths": [],
                       "trigger_observer": "platform-history", "state_root": str(self.state),
                       "daily_time": "22:00", "timezone": "Asia/Shanghai"}

    def error(self, code, operation):
        with self.assertRaises(CaptureError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_legacy_without_binding_remains_compatible_but_runner_requires_it(self):
        self.assertIsNone(require_environment(self.repo))
        self.assertEqual(GitAdapter(self.repo).check(), self.base)
        self.error("environment_binding_required", lambda: probe_runner(self.repo, self.config))

    def test_binding_is_clone_local_private_and_idempotent(self):
        self.assertEqual(bind_environment(self.repo, self.binding)["status"], "bound")
        path = self.repo / ".git/pdm-environment.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(bind_environment(self.repo, self.binding)["status"], "already_bound")
        self.assertEqual(require_environment(self.repo), self.binding)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(git(self.repo, "rev-parse", "master"), self.base)

    def test_bound_capture_uses_only_dev_and_preserves_production(self):
        bind_environment(self.repo, self.binding)
        result = capture(self.repo, {"source_key": "runtime-fixture", "source_type": "note",
                         "platform": "isolated", "original_url": None, "acquisition_status": "full_text",
                         "body": "隔离输入", "original_time": None})
        self.assertTrue((self.repo / result["path"]).is_file())
        self.assertEqual(git(self.repo, "rev-parse", "master"), self.base)

    def test_existing_adapter_rejects_branch_switch_before_write_or_recovery_lock(self):
        bind_environment(self.repo, self.binding)
        adapter = PipelineGit(self.repo)
        git(self.repo, "switch", "master")
        self.error("runtime_branch_mismatch", adapter.check)
        self.error("runtime_branch_mismatch", adapter.recover)
        self.error("runtime_branch_mismatch", lambda: adapter.sync("origin", {}, self.base))
        with self.assertRaises(CaptureError) as caught:
            with adapter.lock():
                adapter.recover()
        self.assertEqual(caught.exception.code, "runtime_branch_mismatch")
        self.assertFalse((self.repo / ".git/pdm-capture.lock").exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)

    def test_remote_name_and_push_url_drift_are_rejected_before_transport(self):
        bind_environment(self.repo, self.binding)
        adapter = GitAdapter(self.repo)
        self.error("runtime_remote_mismatch", lambda: adapter.sync("another", {}, self.base))
        git(self.repo, "remote", "set-url", "--push", "origin", "https://github.com/fixture/other.git")
        self.error("runtime_remote_mismatch", adapter.check)

    def test_binding_copied_to_another_clone_cannot_authorize_that_root(self):
        bind_environment(self.repo, self.binding)
        clone = self.root / "another"
        git(self.root, "clone", "--no-hardlinks", str(self.repo), str(clone))
        shutil.copyfile(self.repo / ".git/pdm-environment.json", clone / ".git/pdm-environment.json")
        (clone / ".git/pdm-environment.json").chmod(0o600)
        self.error("runtime_repository_mismatch", lambda: GitAdapter(clone))

    def test_linked_worktree_uses_its_own_git_directory_binding(self):
        git(self.repo, "switch", "master")
        linked = self.root / "linked"
        git(self.repo, "worktree", "add", str(linked), "dev")
        bind_environment(linked, {**self.binding, "repo_root": str(linked)})
        self.assertEqual(GitAdapter(linked).check(), self.base)
        self.assertFalse((self.repo / ".git/pdm-environment.json").exists())
        git(linked, "switch", "--detach", self.base)
        self.error("runtime_branch_mismatch", lambda: GitAdapter(linked))

    def test_state_directory_permissions_and_repository_boundary_are_enforced(self):
        nested = self.repo / "state"
        nested.mkdir(mode=0o700)
        self.error("runtime_state_inside_repository", lambda: bind_environment(self.repo, {**self.binding, "state_root": str(nested)}))
        nested.rmdir()
        bind_environment(self.repo, self.binding)
        self.state.chmod(0o755)
        self.error("invalid_runtime_state", lambda: GitAdapter(self.repo).check())

    def test_binding_symlink_hardlink_and_unsafe_permissions_are_rejected(self):
        path = self.repo / ".git/pdm-environment.json"
        external = self.root / "external.json"
        external.write_text(json.dumps(self.binding))
        external.chmod(0o600)
        path.symlink_to(external)
        self.error("invalid_environment_binding", lambda: require_environment(self.repo))
        path.unlink()
        os.link(external, path)
        self.error("invalid_environment_binding", lambda: require_environment(self.repo))
        path.unlink()
        bind_environment(self.repo, self.binding)
        path.chmod(0o644)
        self.error("invalid_environment_binding", lambda: require_environment(self.repo))

    def test_credential_values_are_references_and_urls_cannot_embed_credentials(self):
        self.error("invalid_credential_reference", lambda: bind_environment(self.repo, {
            **self.binding, "credential_refs": {"git": "https://example.invalid/token"}}))
        self.error("invalid_runtime_remote", lambda: remote_identity("https://secret@github.com/a/b.git"))
        self.assertEqual(remote_identity("git@github.com:fixture/private.git"), self.binding["remote_identity"])

    def test_probe_cannot_accept_caller_supplied_capability_success(self):
        bind_environment(self.repo, self.binding)
        head = git(self.repo, "rev-parse", "HEAD")
        with patch("pdm.runtime.shutil.which", return_value="/fixture/tool"):
            result = probe_runner(self.repo, self.config, receipt_refs=["actual-platform-record-reference"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(set(result["capabilities"]), set(CAPABILITIES))
        self.assertTrue(all(item["status"] == "unverified" for item in result["capabilities"].values()))
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse(result["task_enabled_by_probe"])
        self.assertEqual(result["platform_task_status"], "unverified")
        self.error("invalid_capability_receipt", lambda: probe_runner(self.repo, self.config, receipt_refs=[{"passed": True}]))
        self.error("invalid_runner_config", lambda: validate_runner_config({**self.config, "capabilities": {"passed": True}}))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_runner_scope_environment_time_and_identity_must_match(self):
        bind_environment(self.repo, self.binding)
        self.error("runner_environment_mismatch", lambda: probe_runner(self.repo, {**self.config, "environment": "prod"}))
        self.error("invalid_runner_config", lambda: validate_runner_config({**self.config, "daily_time": "10:00"}))
        self.error("invalid_runner_scope", lambda: validate_runner_config({**self.config, "allowed_source_ids": []}))
        self.error("invalid_environment_binding", lambda: bind_environment(self.repo, {**self.binding, "environment": []}))

    def test_promotion_checks_the_proposed_tree_and_never_mutates_refs_or_data(self):
        for path in ("memory/records/prod.md", "fixtures/old.md", "archive/old.md"):
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("production snapshot")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "production data baseline")
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "code.py").write_text("new code")
        (self.repo / "memory/records/prod.md").write_text("development data")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "development code and data")
        dev = git(self.repo, "rev-parse", "HEAD")
        bad_tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        bad = git(self.repo, "commit-tree", bad_tree, "-p", base, "-p", dev, "-m", "proposed unfiltered merge")
        git(self.repo, "checkout", base, "--", "memory", "fixtures", "archive")
        good_tree = git(self.repo, "write-tree")
        good = git(self.repo, "commit-tree", good_tree, "-p", base, "-p", dev, "-m", "proposed code-only merge")
        before = (git(self.repo, "show-ref"), (self.repo / ".git/index").read_bytes(),
                  (self.repo / "memory/records/prod.md").read_bytes())
        denied = promotion_check(self.repo, base, bad, dev_commit=dev)
        self.assertEqual(denied["status"], "blocked")
        self.assertEqual(denied["protected_changes"], [{"status": "M", "path": "memory/records/prod.md", "preserve_from": base}])
        self.assertEqual(promotion_check(self.repo, base, good, dev_commit=dev)["status"], "passed")
        self.assertEqual(promotion_check(self.repo, base, dev)["status"], "blocked")
        self.assertEqual(before, (git(self.repo, "show-ref"), (self.repo / ".git/index").read_bytes(),
                                 (self.repo / "memory/records/prod.md").read_bytes()))

    def test_promotion_rejects_environment_state_files_and_wrong_dev_parent(self):
        (self.repo / "pdm-environment.json").write_text("fixture state")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "invalid tracked runtime state")
        dev = git(self.repo, "rev-parse", "HEAD")
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        candidate = git(self.repo, "commit-tree", tree, "-p", self.base, "-p", dev, "-m", "candidate")
        result = promotion_check(self.repo, self.base, candidate, dev_commit=self.base)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("unexpected_development_parent", result["reasons"])
        self.assertEqual(result["protected_changes"][0]["path"], "pdm-environment.json")

    def prepare_fixture(self):
        for name in ("memory/prod.md", "fixtures/original.md", "archive/original.md", ".pdm/state.json",
                     "deploy/pdm-environment.json"):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("production bytes")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "production snapshot")
        base = git(self.repo, "rev-parse", "HEAD")
        for name in ("memory/prod.md", "fixtures/original.md", "archive/original.md", ".pdm/state.json",
                     "deploy/pdm-environment.json"):
            (self.repo / name).write_text("development bytes")
        (self.repo / "memory/dev-only.md").write_text("development only")
        (self.repo / "program.py").write_text("print('development code')")
        (self.repo / "program.py").chmod(0o755)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "development code and isolated data")
        return base, git(self.repo, "rev-parse", "HEAD")

    def test_prepare_preserves_production_data_and_exact_development_code_without_moving_refs(self):
        base, dev = self.prepare_fixture()
        before = (git(self.repo, "show-ref"), (self.repo / ".git/index").read_bytes(),
                  {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*")
                   if path.is_file() and ".git" not in path.parts})
        prepared = prepare_promotion(self.repo, base, dev)
        candidate = prepared["candidate_commit"]
        self.assertEqual(git(self.repo, "rev-list", "--parents", "-n", "1", candidate).split(), [candidate, base, dev])
        for name in ("memory/prod.md", "fixtures/original.md", "archive/original.md", ".pdm/state.json",
                     "deploy/pdm-environment.json"):
            self.assertEqual(git(self.repo, "show", candidate + ":" + name), "production bytes")
        self.assertEqual(git(self.repo, "ls-tree", candidate, "program.py"), git(self.repo, "ls-tree", dev, "program.py"))
        self.assertEqual(git(self.repo, "ls-tree", "-r", "--name-only", candidate, "memory/dev-only.md"), "")
        self.assertEqual(prepared["report"]["status"], "passed")
        self.assertTrue(prepared["git_objects_created"])
        self.assertFalse(prepared["refs_changed"])
        self.assertEqual(before, (git(self.repo, "show-ref"), (self.repo / ".git/index").read_bytes(),
                  {str(path.relative_to(self.repo)): path.read_bytes() for path in self.repo.rglob("*")
                   if path.is_file() and ".git" not in path.parts}))

    def test_promotion_rejects_extra_code_and_unrelated_development_parent(self):
        base, dev = self.prepare_fixture()
        prepared = prepare_promotion(self.repo, base, dev)
        (self.repo / "injected.py").write_text("unexpected code")
        git(self.repo, "add", ".")
        tree = git(self.repo, "write-tree")
        extra = git(self.repo, "commit-tree", tree, "-p", base, "-p", dev, "-m", "invalid extra code")
        result = promotion_check(self.repo, base, extra, dev_commit=dev)
        self.assertIn("injected.py", result["unexpected_code_paths"])
        tree = prepared["candidate_tree"]
        orphan = git(self.repo, "commit-tree", tree, "-m", "unrelated code snapshot")
        candidate = git(self.repo, "commit-tree", tree, "-p", base, "-p", orphan, "-m", "invalid development parent")
        self.assertIn("development_parent_not_in_dev_history", promotion_check(self.repo, base, candidate)["reasons"])

    def test_delivery_verifies_actual_outer_merge_tree_and_exact_prepared_branch_name(self):
        base, dev = self.prepare_fixture()
        prepared = prepare_promotion(self.repo, base, dev)
        head, tree = prepared["candidate_commit"], prepared["candidate_tree"]
        actual = git(self.repo, "commit-tree", tree, "-p", base, "-p", head, "-m", "outer GitHub merge fixture")
        self.assertEqual(promotion_delivery_check(self.repo, base, actual, head, head_ref="promotion/" + head)["status"], "passed")
        changed = git(self.repo, "commit-tree", dev + "^{tree}", "-p", base, "-p", head, "-m", "polluted outer merge")
        self.assertEqual(promotion_delivery_check(self.repo, base, changed, head, head_ref="promotion/" + head)["status"], "blocked")
        self.error("invalid_promotion_delivery_branch", lambda: promotion_delivery_check(self.repo, base, actual, head, head_ref="promotion/anything"))


if __name__ == "__main__":
    unittest.main()
