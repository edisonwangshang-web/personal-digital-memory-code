import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch, sentinel

from git_fixtures import configure_test_repository
from pdm.cli import main
from pdm.doctor import inspect_environment


class CliTests(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        )

    def test_doctor_does_not_infer_service_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            result = inspect_environment(Path(directory))
        self.assertFalse(result["local_ready"])
        for key in ("remote_read_write", "independent_model_call", "scheduled_environment"):
            self.assertEqual(result[key], "unverified")

    @unittest.skipUnless(shutil.which("git"), "Git is required for the repository probe")
    def test_doctor_requires_a_committed_head(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.git(repo, "init", "--quiet")
            configure_test_repository(repo)
            result = inspect_environment(repo)
            self.assertTrue(result["git_worktree"])
            self.assertFalse(result["head_available"])
            self.assertFalse(result["local_ready"])
            self.git(
                repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture",
            )
            result = inspect_environment(repo)
            self.assertTrue(result["head_available"])
            self.assertTrue(result["local_ready"])

    @unittest.skipUnless(shutil.which("git"), "Git is required for the repository probe")
    def test_doctor_ignores_inherited_git_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.git(repo, "init", "--quiet")
            configure_test_repository(repo)
            self.git(
                repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture",
            )
            with patch.dict(os.environ, {
                "GIT_DIR": str(repo / "missing.git"),
                "GIT_WORK_TREE": str(repo / "missing-tree"),
                "GIT_CONFIG_COUNT": "invalid",
            }):
                result = inspect_environment(repo)
            self.assertTrue(result["git_worktree"])
            self.assertTrue(result["head_available"])
            self.assertTrue(result["local_ready"])

    def test_bad_input_does_not_echo_personal_text(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            source.write_text('{"private-content": SECRET_INVALID}', encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                result = main(["capture", "--repo", directory, "--input", str(source)])
            self.assertEqual(result, 2)
            self.assertNotIn("SECRET_INVALID", output.getvalue())
            self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_repeated_sources_use_joint_workflow_and_keep_explicit_head(self):
        sources = ["memory/sources/inbox/one.md", "memory/sources/inbox/two.md"]
        output = io.StringIO()
        with patch("pdm.pipeline.process_sources", return_value={"status": "passed"}) as joint, \
                patch("pdm.cli._model_budget", return_value=sentinel.model_call), \
                patch("pdm.cli._selected_skills", return_value=sentinel.skill_bundle), \
                contextlib.redirect_stdout(output):
            result = main(["process", "--repo", "/tmp/isolated", "--source", sources[0],
                           "--source", sources[1], "--expected-head", "test-head",
                           "--budget-ledger", "/tmp/isolated-budget.json", "--operation-id", "test-process"])
        self.assertEqual(result, 0)
        joint.assert_called_once_with(Path("/tmp/isolated"), sources, "test-head",
                                      model_call=sentinel.model_call, skill_bundle=sentinel.skill_bundle)
        self.assertEqual(json.loads(output.getvalue())["status"], "passed")

    def test_single_source_uses_quality_processing_entrypoint(self):
        output = io.StringIO()
        with patch("pdm.pipeline.process") as single, \
                patch("pdm.cli._model_budget", return_value=sentinel.model_call), \
                patch("pdm.cli._selected_skills", return_value=sentinel.skill_bundle), \
                patch("pdm.pipeline.process_sources", return_value={"status": "needs_confirmation"}) as joint, \
                contextlib.redirect_stdout(output):
            result = main(["process", "--repo", "/tmp/isolated", "--source", "selected.md",
                           "--budget-ledger", "/tmp/isolated-budget.json", "--operation-id", "test-process"])
        self.assertEqual(result, 4)
        joint.assert_called_once_with(Path("/tmp/isolated"), ["selected.md"], None,
                                      model_call=sentinel.model_call, skill_bundle=sentinel.skill_bundle)
        single.assert_not_called()

    def test_invalid_skill_reference_is_reported_before_processing(self):
        output = io.StringIO()
        with patch("pdm.cli._model_budget", return_value=sentinel.model_call), \
                patch("pdm.pipeline.process_sources") as process, contextlib.redirect_stderr(output):
            result = main(["process", "--repo", "/tmp/isolated", "--source", "selected.md",
                           "--extract-skill", "knowledge-review@1",
                           "--budget-ledger", "/tmp/audit.json", "--operation-id", "invalid-skill"])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["error"], "unsupported_skill_reference")
        process.assert_not_called()

    def test_joint_evidence_and_ask_forward_only_selected_sources_and_run(self):
        for command, target, extra in (
                ("evidence", "pdm.evidence.build_evidence_sources", []),
                ("ask", "pdm.evidence.ask_sources", ["--question", "what is known?"])):
            with self.subTest(command=command):
                output = io.StringIO()
                with patch(target, return_value={"scope": "selected"}) as call, \
                        patch("pdm.cli._model_budget", return_value=sentinel.model_call), \
                        contextlib.redirect_stdout(output):
                    budget = (["--budget-ledger", "/tmp/isolated-budget.json", "--operation-id", "test-answer"]
                              if command == "ask" else [])
                    result = main([command, "--repo", "/tmp/isolated", "--source", "one.md",
                                   "--source", "two.md", "--run", "selected-run", *extra, *budget])
                self.assertEqual(result, 0)
                positional = (Path("/tmp/isolated"), ["one.md", "two.md"])
                if command == "ask":
                    positional += ("what is known?",)
                options = {"model_call": sentinel.model_call} if command == "ask" else {}
                call.assert_called_once_with(*positional, "selected-run", **options)


if __name__ == "__main__":
    unittest.main()
