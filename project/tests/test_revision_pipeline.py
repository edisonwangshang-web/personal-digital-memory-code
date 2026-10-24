"""Explicit real-Git revision loop; all model outputs are artificial fixtures."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_decomposed_pipeline
import test_pipeline
from pdm.batch_groups import publish_final_group, verify_group_publication
from pdm.batching import merge_batch_sources, plan_batches
from pdm.cli import parser
from pdm.contracts import CaptureError, parse_markdown
from pdm.pipeline import (_committed, _hash, _load, _revision_attempt_path,
                          execution_versions, process_sources, publish, verify_publication)
from pdm.production import prepare_production, production_status, run_production
from pdm.transaction import PipelineGit


class RevisionPipelineTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    assert_error = test_pipeline.PipelineTests.assert_error
    sources = test_decomposed_pipeline.DecomposedPipelineTests.sources
    prepare = test_decomposed_pipeline.DecomposedPipelineTests.prepare
    def model(self, role, payload):
        result = test_decomposed_pipeline.DecomposedPipelineTests.model(self, role, payload)
        if role == "extract" and "revision_context" in payload:
            # A genuine fixture repair: retain the source's unperformed boundary
            # explicitly in the record. It is not a new fact or a changed run ID.
            result["output"]["objects"][0]["body"] += " The stated plan remains unperformed."
        return result
    run_batch = test_decomposed_pipeline.DecomposedPipelineTests.run_batch
    candidate = test_decomposed_pipeline.DecomposedPipelineTests.candidate
    no_formal = test_decomposed_pipeline.DecomposedPipelineTests.no_formal

    def rejected(self, strategy="decomposed-v1"):
        paths, plan, projection = self.prepare()
        self.verdict = "needs_revision"
        result = self.run_batch(paths, projection, strategy)
        self.assertEqual(result["status"], "needs_revision", result)
        self.verdict = "passed"
        return paths, plan, projection, result

    def test_complete_revision_is_new_independent_review_then_ordinary_publish_and_read(self):
        from pdm.domain_catalog import build_domain_catalog
        from pdm.evidence import build_current_evidence
        from pdm.portable import prepare_reading_package, verify_reading_package

        paths, _, _, old = self.rejected()
        git = PipelineGit(self.repo)
        old_bytes = {path: (self.repo / path).read_bytes() for path in (old["stage_path"], old["review_path"], *paths)}
        rejected = self.candidate(old)
        before = self.number
        result = process_sources(self.repo, paths, related_runs=[], revision_from_run=old["run_id"], model_call=self.model)
        self.assertEqual(result["status"], "passed", result)
        self.assertGreaterEqual(self.number - before, 5)
        candidate = self.candidate(result)
        self.assertNotEqual(candidate["run_id"], rejected["run_id"])
        self.assertNotEqual(candidate["processing_key"], rejected["processing_key"])
        self.assertEqual(candidate["revision_context"]["candidate"], rejected)
        self.assertEqual(parse_markdown((self.repo / result["stage_path"]).read_bytes())[0]["schema_version"], "0.9")
        extraction = next(context for context in self.contexts[before:] if "revision_context" in context)
        self.assertEqual(extraction["revision_context"]["context_binding"], candidate["revision_context"]["binding"])
        self.assertEqual(extraction["revision_context"]["candidate"]["objects"], rejected["objects"])
        self.assertEqual(extraction["related"], [])
        head, calls = git.head(), self.number
        repeated = process_sources(self.repo, paths, related_runs=[], revision_from_run=old["run_id"], model_call=self.model)
        self.assertEqual(repeated["run_id"], result["run_id"])
        self.assertEqual(repeated["attempted_calls"], 0)
        self.assertEqual((git.head(), self.number), (head, calls))
        published = publish(self.repo, result["run_id"])
        verified = verify_publication(self.repo, result["run_id"], source_paths=paths)
        self.assertEqual(verified["commit_sha"], published["commit_sha"])
        authority = {item["path"] for item in verified["authority_snapshots"]}
        self.assertTrue({old["stage_path"], old["review_path"], _revision_attempt_path(candidate["revision_context"])} <= authority)
        current = build_current_evidence(self.repo, paths, [result["run_id"]])
        self.assertEqual(len(current["publications"][0]["documents"]), 2)
        self.assertTrue(build_domain_catalog(self.repo, paths, [result["run_id"]])["catalog_complete_within_selected_scope"])
        output = self.repo.parent / (self.repo.name + "-revision-reading.json")
        self.addCleanup(lambda: output.unlink(missing_ok=True))
        package = prepare_reading_package(self.repo, paths, ["What artificial plan is still unperformed?"], [result["run_id"]], output=output)
        self.assertEqual(verify_reading_package(output, expected_sha256=package["sha256"])["status"], "integrity_verified")
        verify_publication(self.repo, result["run_id"], source_paths=paths, historical=True)
        self.assertEqual(old_bytes, {path: (self.repo / path).read_bytes() for path in old_bytes})
        self.assertEqual(git.text("status", "--porcelain"), "")

    def test_new_plan_revision_group_is_discovered_before_original_queue_attempt(self):
        paths, _, _, old = self.rejected("auto")
        queue = prepare_production(self.repo, "revision-discovery", paths, max_input_bytes=100_000)
        state_path = queue["state_path"]
        state = _load((self.repo / state_path).read_bytes(), "production_queue", "0.8")
        plan = state["plan"]
        projection = merge_batch_sources(self.repo, plan, [item["batch_id"] for item in plan["batches"]], source_paths=paths, max_bytes=100_000)
        revised = self.run_batch(paths, projection, "auto", revision_from_run=old["run_id"])
        self.assertEqual(revised["status"], "passed", revised)
        published = publish_final_group(self.repo, plan, self.candidate(revised)["objects"], [revised["run_id"]], source_paths=paths)
        authority = {item["path"] for item in verify_group_publication(self.repo, published["run_id"], source_paths=paths)["authority_snapshots"]}
        self.assertTrue({old["stage_path"], old["review_path"]} <= authority)
        head, count, queue_bytes = PipelineGit(self.repo).head(), self.number, (self.repo / state_path).read_bytes()
        result = run_production(self.repo, "revision-discovery", model_call=self.model)
        self.assertEqual(result["status"], "published", result)
        self.assertTrue(result["reused"])
        self.assertEqual(result["publication"]["run_id"], published["run_id"])
        self.assertEqual(production_status(self.repo, "revision-discovery")["status"], "ready")
        self.assertEqual((PipelineGit(self.repo).head(), self.number, (self.repo / state_path).read_bytes()), (head, count, queue_bytes))
        same = process_sources(self.repo, paths, revision_from_run=old["run_id"], related_runs=[], model_call=self.model)
        self.assertEqual(same["attempted_calls"], 0)
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, count))

    def test_unknown_revision_cannot_restart_by_new_rules_or_requested_run(self):
        paths, _, _, old = self.rejected("auto")
        def interrupted(role, payload):
            self.assertEqual(role, "extract")
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            process_sources(self.repo, paths, revision_from_run=old["run_id"], model_call=interrupted)
        git = PipelineGit(self.repo)
        head, count = git.head(), self.number
        changed = {**execution_versions(git), "rules_version": "sha256-" + "f" * 64}
        with patch("pdm.pipeline.execution_versions", return_value=changed):
            result = process_sources(self.repo, paths, revision_from_run=old["run_id"],
                run_id="run-" + "b" * 32, model_call=self.model)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["attempted_calls"], 0)
        self.assertEqual((git.head(), self.number), (head, count))
        self.no_formal()

    def test_explicit_revision_does_not_expand_scope_or_accept_shortened_batch(self):
        paths, _, projection, old = self.rejected("auto")
        wrong = [self.saved["path"]]
        head, calls = PipelineGit(self.repo).head(), self.number
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("source read before scope check")):
            self.assert_error("revision_source_scope", lambda: process_sources(self.repo, wrong,
                revision_from_run=old["run_id"], model_call=self.model))
        shortened = deepcopy(projection)
        shortened["source_views"][0]["fragments"][0]["lines"] = []
        self.assert_error("revision_batch_context_changed", lambda: self.run_batch(paths, shortened, "auto", revision_from_run=old["run_id"]))
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, calls))

    def test_revision_cannot_join_scheduler_or_unreviewed_extraction_reuse(self):
        paths, _, _, old = self.rejected("auto")
        head, calls = PipelineGit(self.repo).head(), self.number
        for extra in ({"execution_context": {}}, {"continuation_context": {}},
                      {"draft_only": True}, {"reuse_extraction_run": old["run_id"]}):
            with self.subTest(extra=extra):
                self.assert_error("invalid_revision_context", lambda: process_sources(self.repo, paths,
                    revision_from_run=old["run_id"], model_call=self.model, **extra))
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, calls))

    def test_cli_explicit_revision_selects_pipeline_and_preserves_related_scope(self):
        from pdm.cli import main
        paths, _, _, old = self.rejected("auto")
        args = ["process", "--repo", str(self.repo), "--source", paths[0], "--revision-from-run", old["run_id"]]
        self.assertEqual(parser().parse_args(args).revision_from_run, old["run_id"])
        with patch("pdm.cli._model_budget", return_value=self.model), patch("builtins.print"):
            result = main(args)
        self.assertEqual(result, 0)
        self.assertEqual(self.roles.count("extract"), 2)

    def test_identical_objects_with_new_run_metadata_stop_before_any_new_review(self):
        paths, _, _, old = self.rejected("auto")
        before = self.number
        def noop(role, payload):
            return test_decomposed_pipeline.DecomposedPipelineTests.model(self, role, payload)
        failed = process_sources(self.repo, paths, revision_from_run=old["run_id"], model_call=noop)
        self.assertEqual(failed["status"], "failed", failed)
        self.assertEqual(failed["error"], "revision_unchanged")
        self.assertEqual(self.number - before, 1)
        self.assertEqual(self.roles[before:], ["extract"])
        candidate = self.candidate(failed)
        previous = self.candidate(old)
        self.assertNotEqual(candidate["run_id"], previous["run_id"])
        self.assertNotEqual(candidate["objects"][0]["metadata"]["generated_by"], previous["objects"][0]["metadata"]["generated_by"])
        self.assertEqual(candidate["objects"][0]["metadata"]["content_version"], previous["objects"][0]["metadata"]["content_version"])
        self.assertFalse((self.repo / f"memory/runs/processes/{failed['run_id']}/review.md").exists())
        head, count = PipelineGit(self.repo).head(), self.number
        repeated = process_sources(self.repo, paths, revision_from_run=old["run_id"], model_call=self.model)
        self.assertEqual(repeated["error"], "revision_unchanged")
        self.assertEqual(repeated["attempted_calls"], 0)
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, count))
        self.no_formal()

    def test_nonbatch_revision_keeps_legacy_limits_and_independent_new_review(self):
        def ordinary(role, payload):
            result = test_pipeline.PipelineTests.model(self, role, payload)
            if role == "extract" and "revision_context" in payload:
                result["output"]["objects"][0]["body"] += " 原文尚未说明已完成。"
            return result
        paths = [self.saved["path"]]
        self.verdict = "needs_revision"
        old = process_sources(self.repo, paths, related_runs=[], model_call=ordinary)
        self.verdict = "passed"
        result = process_sources(self.repo, paths, revision_from_run=old["run_id"], model_call=ordinary)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(self.roles, ["extract", "review", "extract", "review"])
        published = publish(self.repo, result["run_id"])
        from pdm.evidence import build_current_evidence
        self.assertEqual(len(build_current_evidence(self.repo, paths, [result["run_id"]])["publications"][0]["documents"]), 2)
        self.assertEqual(verify_publication(self.repo, result["run_id"], source_paths=paths)["commit_sha"], published["commit_sha"])

    def test_new_schema_rejects_prior_writer_without_changing_any_bytes(self):
        from pdm.compatibility import ensure_memory_compatible
        paths, _, _, old = self.rejected("auto")
        result = process_sources(self.repo, paths, revision_from_run=old["run_id"], model_call=self.model)
        self.assertEqual(result["status"], "passed")
        git = PipelineGit(self.repo)
        head = git.head()
        with patch("pdm.compatibility.SUPPORTED_SCHEMA_VERSIONS", tuple(f"0.{i}" for i in range(1, 9))):
            self.assert_error("incompatible_memory_schema", lambda: ensure_memory_compatible(self.repo))
        self.assertEqual(git.head(), head)
