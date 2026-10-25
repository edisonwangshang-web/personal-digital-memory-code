"""Private bodies stay untouched while tested public code identity is checked."""

import contextlib
from copy import deepcopy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_dual_repo.py"
SPEC = importlib.util.spec_from_file_location("pdm_dual_repo_check", SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class DualRepositoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-dual-repo-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.code, self.data = self.base / "code", self.base / "data"
        for repo, name in ((self.code, "code"), (self.data, "data")):
            repo.mkdir()
            self.git(repo, "init", "-q", "-b", "dev")
            self.git(repo, "config", "user.name", "Synthetic Test")
            self.git(repo, "config", "user.email", "fixture@example.invalid")
            self.git(repo, "config", "commit.gpgsign", "false")
            self.git(repo, "config", "core.hooksPath", "/dev/null")
            self.git(repo, "remote", "add", "origin", f"https://github.com/fixture/{name}.git")
            for path, text in {
                "pyproject.toml": '[project]\nname="synthetic"\nversion="0.1.0"\n',
                "project/src/pdm/__init__.py": '__version__="0.1.0"\n',
                "project/prompts/extract.md": "Synthetic general rule.\n",
                "project/skills/shared/policy.md": "Synthetic shared policy.\n",
                "README.md": "Code instructions.\n" if name == "code" else "Private navigation.\n",
                ".gitignore": "__pycache__/\nignored.py\n*.so\n*.pyd\n*.pyc\n",
            }.items():
                self.write(repo, path, text)
            if name == "data":
                self.write(repo, "memory/sources/private.md", "PRIVATE SYNTHETIC BODY MUST STAY PRIVATE\n")
            self.save(repo)
        self.ref = self.git(self.code, "rev-parse", "HEAD")
        self.program = self.git(self.data, "rev-parse", "HEAD")
        self.write(self.data, "memory/records/private.md", "Synthetic private knowledge.\n")
        self.save(self.data)
        self.responses = {
            "repos/fixture/code": {"full_name": "fixture/code", "private": False, "visibility": "public"},
            "repos/fixture/data": {"full_name": "fixture/data", "private": True, "visibility": "private"},
            "repos/fixture/code/actions/runs/123": {
                "id": 123, "run_attempt": 1, "head_sha": self.ref,
                "event": "push", "head_branch": "dev", "path": bridge.WORKFLOW,
                "status": "completed", "conclusion": "success",
                "repository": {"full_name": "fixture/code"},
                "head_repository": {"full_name": "fixture/code"},
            },
            "repos/fixture/code/actions/runs/123/attempts/1/jobs?per_page=100": {
                "total_count": 8,
                "jobs": [{"name": name, "run_id": 123, "run_attempt": 1,
                          "head_sha": self.ref, "status": "completed", "conclusion": "success"}
                         for name in sorted(bridge.EXPECTED_JOBS)],
            },
        }
        self.requests = []

    def git(self, repo, *args):
        return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL,
                                       text=True).strip()

    def write(self, repo, path, text):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def save(self, repo):
        self.git(repo, "add", "--all")
        self.git(repo, "commit", "-qm", "synthetic fixture")

    def api(self, endpoint):
        self.requests.append(endpoint)
        return deepcopy(self.responses[endpoint])

    def check(self, **kwargs):
        values = {"code_repo": self.code, "data_repo": self.data,
                  "code_ref": self.ref, "ci_run": "123", "api": self.api}
        values.update(kwargs)
        return bridge.check(**values)

    def blocked(self, code, **kwargs):
        with self.assertRaises(bridge.CheckError) as caught:
            self.check(**kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_success_records_separate_commits_without_body_reads_or_writes(self):
        before = {repo: (self.git(repo, "rev-parse", "HEAD"),
                         self.git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
                         self.git(repo, "status", "--porcelain")) for repo in (self.code, self.data)}
        target = self.data / "project/src/pdm/__init__.py"
        info = target.stat()
        os.utime(target, (info.st_atime + 10, info.st_mtime + 10))
        index_before = (self.data / ".git/index").read_bytes()
        reads = []
        original = bridge._file_bytes

        def bounded(path):
            self.assertNotIn("memory", path.parts)
            reads.append(path)
            return original(path)

        with patch.object(bridge, "_file_bytes", side_effect=bounded):
            result = self.check()
        self.assertEqual((self.data / ".git/index").read_bytes(), index_before)
        self.assertEqual(result["status"], "matched")
        self.assertEqual((result["code_branch"], result["data_branch"]), ("dev", "dev"))
        self.assertEqual(result["core_files"], 4)
        self.assertEqual(result["private_program_commit"], self.program)
        self.assertNotEqual(result["code_commit"], result["private_program_commit"])
        self.assertNotEqual(result["data_head"], result["private_program_commit"])
        self.assertIn(str(self.data / "project/src"), result["private_doctor_command"])
        self.assertEqual((result["writes"], result["model_calls"]), (0, 0))
        self.assertEqual(len(reads), 8)
        self.assertEqual(len(self.requests), 4)
        for repo, state in before.items():
            self.assertEqual((self.git(repo, "rev-parse", "HEAD"),
                              self.git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
                              self.git(repo, "status", "--porcelain")), state)

    def test_master_pair_with_master_ci_succeeds_without_changing_commits(self):
        before = {repo: self.git(repo, "rev-parse", "HEAD") for repo in (self.code, self.data)}
        for repo in before:
            self.git(repo, "branch", "-m", "master")
        self.responses["repos/fixture/code/actions/runs/123"]["head_branch"] = "master"
        result = self.check()
        self.assertEqual(result["status"], "matched")
        self.assertEqual((result["code_branch"], result["data_branch"]), ("master", "master"))
        self.assertEqual(result["code_commit"], self.ref)
        self.assertEqual(result["data_head"], before[self.data])
        for repo, commit in before.items():
            self.assertEqual(self.git(repo, "rev-parse", "HEAD"), commit)
            self.assertEqual(self.git(repo, "rev-parse", "--abbrev-ref", "HEAD"), "master")
            self.assertEqual(self.git(repo, "status", "--porcelain"), "")

    def test_master_pair_still_rejects_committed_private_core_differences(self):
        for repo in (self.code, self.data):
            self.git(repo, "branch", "-m", "master")
        self.responses["repos/fixture/code/actions/runs/123"]["head_branch"] = "master"
        self.assertEqual(self.check()["status"], "matched")
        self.write(self.data, "project/prompts/extract.md", "Different committed production rule.\n")
        self.save(self.data)
        self.blocked("core_content_mismatch")

    def test_core_byte_mismatch_blocks_even_when_private_change_is_committed(self):
        self.write(self.data, "project/prompts/extract.md", "Different committed rule.\n")
        self.save(self.data)
        self.blocked("core_content_mismatch")

    def test_additional_ignored_python_module_is_not_hidden_by_clean_git_status(self):
        self.write(self.data, "project/src/pdm/ignored.py", "EXTRA = True\n")
        self.assertEqual(self.git(self.data, "status", "--porcelain"), "")
        self.blocked("core_path_mismatch")

    def test_ignored_native_and_sourceless_modules_are_rejected(self):
        for suffix in ("so", "pyd", "pyc"):
            with self.subTest(suffix=suffix):
                path = self.data / ("project/src/pdm/ignored." + suffix)
                path.write_bytes(b"synthetic unused module")
                self.assertEqual(self.git(self.data, "status", "--porcelain"), "")
                self.blocked("unexpected_core_file")
                path.unlink()

    def test_private_head_or_clean_state_change_during_api_check_is_rejected(self):
        for mutation in ("commit", "dirty"):
            with self.subTest(mutation=mutation):
                changed = False

                def provider(endpoint):
                    nonlocal changed
                    if not changed and endpoint.endswith("/runs/123"):
                        changed = True
                        if mutation == "commit":
                            self.write(self.data, "memory/records/race.md", "Synthetic concurrent knowledge.\n")
                            self.save(self.data)
                        else:
                            self.write(self.data, "README.md", "Concurrent dirty navigation.\n")
                    return self.api(endpoint)

                self.blocked("repository_changed_during_check", api=provider)

    def test_branch_changes_during_check_are_rejected_even_when_heads_stay_equal(self):
        for repos in ((self.code,), (self.data,), (self.code, self.data)):
            for branch in ("master", "feature", "HEAD"):
                with self.subTest(repos=[repo.name for repo in repos], branch=branch):
                    before = {repo: self.git(repo, "rev-parse", "HEAD") for repo in repos}
                    changed = False

                    def provider(endpoint):
                        nonlocal changed
                        if not changed and endpoint.endswith("/runs/123"):
                            changed = True
                            for repo in repos:
                                if branch == "HEAD":
                                    self.git(repo, "checkout", "-q", "--detach")
                                else:
                                    self.git(repo, "checkout", "-q", "-B", branch)
                                self.assertEqual(self.git(repo, "rev-parse", "HEAD"), before[repo])
                        return self.api(endpoint)

                    self.blocked("repository_changed_during_check", api=provider)
                    for repo in repos:
                        self.git(repo, "checkout", "-q", "dev")

    def test_dirty_code_or_private_checkout_blocks(self):
        for repo in (self.code, self.data):
            with self.subTest(repo=repo.name):
                path = repo / "README.md"
                before = path.read_text()
                path.write_text(before + "Dirty.\n")
                self.blocked("dirty_repository")
                path.write_text(before)

    def test_same_repository_is_rejected(self):
        self.blocked("repositories_not_separate", data_repo=self.code)

    def test_actual_visibility_must_be_public_code_and_private_data(self):
        for name, private, visibility, code in (
            ("code", True, "private", "code_repository_not_public"),
            ("data", False, "public", "data_repository_not_private"),
        ):
            with self.subTest(name=name):
                key = "repos/fixture/" + name
                original = deepcopy(self.responses[key])
                self.responses[key].update(private=private, visibility=visibility)
                self.blocked(code)
                self.responses[key] = original

    def test_ci_identity_and_success_must_bind_the_exact_public_code(self):
        key = "repos/fixture/code/actions/runs/123"
        cases = [{"head_sha": "0" * 40}, {"conclusion": "failure"}, {"status": "in_progress"},
                 {"event": "pull_request"}, {"head_branch": "feature"},
                 {"path": ".github/workflows/other.yml"}, {"run_attempt": 0},
                 {"head_repository": {"full_name": "attacker/fork"}}]
        for change in cases:
            with self.subTest(change=change):
                original = deepcopy(self.responses[key])
                self.responses[key].update(change)
                self.blocked("ci_run_not_approved")
                self.responses[key] = original

    def test_jobs_must_be_complete_successful_and_from_the_same_attempt(self):
        key = "repos/fixture/code/actions/runs/123/attempts/1/jobs?per_page=100"
        for mutation in ("missing", "duplicate", "failed", "attempt", "sha"):
            with self.subTest(mutation=mutation):
                original = deepcopy(self.responses[key])
                jobs = self.responses[key]["jobs"]
                if mutation == "missing":
                    jobs.pop()
                elif mutation == "duplicate":
                    jobs[0]["name"] = jobs[1]["name"]
                else:
                    jobs[0].update({"failed": {"conclusion": "failure"},
                                    "attempt": {"run_attempt": 2}, "sha": {"head_sha": "0" * 40}}[mutation])
                self.blocked("ci_jobs_not_approved")
                self.responses[key] = original

    def test_checked_out_code_ref_and_matching_branches_are_required(self):
        self.blocked("full_code_commit_required", code_ref="HEAD")
        self.blocked("code_checkout_mismatch", code_ref="0" * 40)
        self.git(self.data, "branch", "-m", "master")
        self.blocked("repository_branch_mismatch")

    def test_both_directions_of_crossed_checkout_branches_are_rejected(self):
        for master_repo in (self.code, self.data):
            with self.subTest(master_repo=master_repo.name):
                self.git(master_repo, "branch", "-m", "master")
                self.blocked("repository_branch_mismatch")
                self.git(master_repo, "branch", "-m", "dev")
        self.assertEqual(self.requests, [])

    def test_unsupported_and_detached_branches_are_rejected_for_either_checkout(self):
        for repo in (self.code, self.data):
            for branch in ("main", "feature", "HEAD"):
                with self.subTest(repo=repo.name, branch=branch):
                    if branch == "HEAD":
                        self.git(repo, "checkout", "-q", "--detach")
                    else:
                        self.git(repo, "branch", "-m", branch)
                    self.blocked("unsupported_branch")
                    if branch == "HEAD":
                        self.git(repo, "checkout", "-q", "dev")
                    else:
                        self.git(repo, "branch", "-m", "dev")
        self.assertEqual(self.requests, [])

    def test_ci_branch_must_match_the_pair_even_when_code_sha_is_identical(self):
        run = self.responses["repos/fixture/code/actions/runs/123"]
        for branch, ci_branch in (("dev", "master"), ("master", "dev")):
            with self.subTest(branch=branch, ci_branch=ci_branch):
                if branch == "master":
                    for repo in (self.code, self.data):
                        self.git(repo, "branch", "-m", branch)
                run["head_branch"] = ci_branch
                self.assertEqual(run["head_sha"], self.git(self.code, "rev-parse", "HEAD"))
                self.blocked("ci_run_not_approved")

    def test_core_symlink_and_mode_mismatch_are_rejected(self):
        path = self.data / "project/src/pdm/__init__.py"
        path.chmod(0o755)
        self.save(self.data)
        self.blocked("core_content_mismatch")
        path.unlink()
        path.symlink_to(self.data / "memory/sources/private.md")
        self.save(self.data)
        self.blocked("invalid_core_file")

    def test_core_parent_symlink_target_is_not_read(self):
        source = self.data / "project/src/pdm"
        moved = self.base / "moved-core"
        source.rename(moved)
        source.symlink_to(moved, target_is_directory=True)
        original = bridge._file_bytes

        def safe_read(path):
            self.assertFalse(path.is_relative_to(source), "must reject symlink parent before reading its target")
            return original(path)

        with patch.object(bridge, "_file_bytes", side_effect=safe_read):
            with self.assertRaises(bridge.CheckError) as caught:
                bridge._snapshot(self.data, "HEAD")
        self.assertEqual(caught.exception.code, "core_symlink_rejected")

    def test_credential_bearing_origin_is_rejected_without_leaking_it(self):
        value = "https://not-a-real-private-token@github.com/fixture/data.git"
        self.git(self.data, "remote", "set-url", "origin", value)
        with self.assertRaises(bridge.CheckError) as caught:
            self.check()
        self.assertEqual(str(caught.exception), "invalid_github_remote")
        self.assertNotIn("not-a-real", str(caught.exception))

    def test_cli_has_no_offline_ci_bypass_and_masks_provider_errors(self):
        args = ["--code-repo", str(self.code), "--data-repo", str(self.data),
                "--code-ref", self.ref, "--ci-run", "123"]
        output = io.StringIO()
        with patch.object(bridge, "_gh_api", side_effect=self.api) as provider:
            with contextlib.redirect_stdout(output):
                self.assertEqual(bridge.main(args), 0)
            self.assertEqual(provider.call_count, 4)
        self.assertEqual(json.loads(output.getvalue())["status"], "matched")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(bridge.main(args + ["--ci-evidence", "offline.json"]), 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "invalid_arguments")
        self.assertNotIn("offline.json", output.getvalue())
        output = io.StringIO()
        with patch.object(bridge, "_gh_api", side_effect=RuntimeError("private provider body")):
            with contextlib.redirect_stdout(output):
                self.assertEqual(bridge.main(args), 1)
        self.assertEqual(json.loads(output.getvalue())["code"], "check_failed")
        self.assertNotIn("private provider body", output.getvalue())


if __name__ == "__main__":
    unittest.main()
