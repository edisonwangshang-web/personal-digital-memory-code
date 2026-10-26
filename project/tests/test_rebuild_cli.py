"""Explicit rebuilding must not accidentally enter legacy processing or calls."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, sentinel

from pdm.cli import main


class RebuildCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pdm-rebuild-cli-")
        self.addCleanup(self.temporary.cleanup)
        self.reference = {"path": "memory/runs/processes/rebuilds/" + "a" * 64 + ".md",
                          "commit_sha": "b" * 40, "sha256": "sha256-" + "c" * 64}
        self.input = Path(self.temporary.name) / "reference.json"
        self.input.write_text(json.dumps(self.reference))
        self.repo = Path(self.temporary.name) / "private-data"

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def process_arguments(self):
        return ["process", "--repo", str(self.repo), "--source", "selected.md",
                "--rebuild-context", str(self.input), "--budget-ledger", "/tmp/artificial-audit.json",
                "--operation-id", "artificial-rebuild"]

    def test_prepare_is_an_explicit_non_model_action_and_passes_exact_targets(self):
        with patch("pdm.rebuild.prepare_rebuild", return_value={"status": "prepared", "rebuild_context": self.reference}) as prepare, \
                patch("pdm.cli._model_budget") as budget:
            code, stdout, _ = self.invoke("rebuild-prepare", "--repo", str(self.repo),
                "--intent-id", "artificial-intent", "--source", "one.md", "--source", "two.md",
                "--target", "memory/records/fact.md", "--target", "memory/topics/topic.md",
                "--expected-head", "artificial-head")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["rebuild_context"], self.reference)
        prepare.assert_called_once_with(self.repo, "artificial-intent", ["one.md", "two.md"],
            ["memory/records/fact.md", "memory/topics/topic.md"], expected_head="artificial-head")
        budget.assert_not_called()

    def test_process_forwards_fresh_context_and_keeps_nonbatch_review_default(self):
        with patch("pdm.pipeline.process_sources", return_value={"status": "passed"}) as process, \
                patch("pdm.cli._model_budget", return_value=sentinel.adapter), \
                patch("pdm.cli._selected_skills", return_value=sentinel.skills):
            code, _, _ = self.invoke(*self.process_arguments())
        self.assertEqual(code, 0)
        process.assert_called_once_with(self.repo, ["selected.md"], None,
            model_call=sentinel.adapter, skill_bundle=sentinel.skills,
            rebuild_context=self.reference, related_runs=[])

    def test_rebuild_cannot_mix_old_knowledge_before_budget_open(self):
        with patch("pdm.cli._model_budget") as budget, patch("pdm.pipeline.process_sources") as process:
            code, _, stderr = self.invoke(*self.process_arguments(), "--related-run", "old-run")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["error"], "rebuild_requires_fresh_processing")
        budget.assert_not_called()
        process.assert_not_called()

    def test_revision_is_forwarded_for_core_to_verify_same_rebuild_generation(self):
        with patch("pdm.pipeline.process_sources", return_value={"status": "passed"}) as process, \
                patch("pdm.cli._model_budget", return_value=sentinel.adapter), \
                patch("pdm.cli._selected_skills", return_value=sentinel.skills):
            code, _, _ = self.invoke(*self.process_arguments(), "--revision-from-run", "this-rebuild-run")
        self.assertEqual(code, 0)
        process.assert_called_once_with(self.repo, ["selected.md"], None,
            model_call=sentinel.adapter, skill_bundle=sentinel.skills,
            rebuild_context=self.reference, related_runs=[], revision_from_run="this-rebuild-run")

    def test_plain_process_rebuild_reaches_real_core_extract_and_review(self):
        from test_rebuild_pipeline import RebuildPipelineTests
        fixture = RebuildPipelineTests("test_cleared_same_ids_fresh_extract_review_publish_and_read")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.initial()
        self.input.write_text(json.dumps(fixture.context))
        before = fixture.number
        with patch("pdm.cli._model_budget", return_value=fixture.quality_model):
            code, stdout, stderr = self.invoke("process", "--repo", str(fixture.repo),
                "--source", fixture.saved["path"], "--rebuild-context", str(self.input))
        self.assertEqual(code, 0, stderr or stdout)
        self.assertEqual(json.loads(stdout)["status"], "passed")
        self.assertEqual(fixture.number, before + 2)

    def test_plain_process_rejects_batch_only_strategy_before_budget_open(self):
        with patch("pdm.cli._model_budget") as budget, patch("pdm.pipeline.process_sources") as process:
            code, _, stderr = self.invoke(*self.process_arguments(), "--review-strategy", "auto")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["error"], "invalid_review_strategy")
        budget.assert_not_called()
        process.assert_not_called()

    def test_invalid_reference_is_rejected_without_opening_budget_or_echoing_content(self):
        for value in ({**self.reference, "body": "SECRET_PRIVATE"},
                      {**self.reference, "path": "../../SECRET_PRIVATE"},
                      {**self.reference, "commit_sha": "SECRET_PRIVATE"}, []):
            with self.subTest(value=value):
                self.input.write_text(json.dumps(value))
                with patch("pdm.cli._model_budget") as budget, patch("pdm.pipeline.process_sources") as process:
                    code, _, stderr = self.invoke(*self.process_arguments())
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(stderr)["error"], "invalid_rebuild_context")
                self.assertNotIn("SECRET_PRIVATE", stderr)
                budget.assert_not_called()
                process.assert_not_called()

    def test_missing_reference_file_is_safe_and_does_not_open_budget(self):
        self.input.unlink()
        with patch("pdm.cli._model_budget") as budget, patch("pdm.pipeline.process_sources") as process:
            code, _, stderr = self.invoke(*self.process_arguments())
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.input), stderr)
        budget.assert_not_called()
        process.assert_not_called()

    def test_production_prepare_forwards_verified_reference_option(self):
        with patch("pdm.production.prepare_production", return_value={"status": "ready"}) as prepare, \
                patch("pdm.cli._selected_skills", return_value=sentinel.skills):
            code, _, _ = self.invoke("production", "prepare", "--repo", str(self.repo),
                "--id", "artificial-queue", "--source", "selected.md",
                "--rebuild-context", str(self.input))
        self.assertEqual(code, 0)
        prepare.assert_called_once_with(self.repo, "artificial-queue", ["selected.md"],
            allowed_source_paths=None, allowed_run_ids=[], max_input_bytes=128000,
            payload_overhead_bytes=16000, skill_bundle=sentinel.skills, use_default_skills=True,
            rebuild_context=self.reference)

    def test_production_rebuild_refuses_old_run_allowlist_without_preparing_queue(self):
        with patch("pdm.production.prepare_production") as prepare:
            code, _, stderr = self.invoke("production", "prepare", "--repo", str(self.repo),
                "--id", "artificial-queue", "--source", "selected.md",
                "--rebuild-context", str(self.input), "--allowed-run", "old-run")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["error"], "rebuild_requires_fresh_processing")
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
