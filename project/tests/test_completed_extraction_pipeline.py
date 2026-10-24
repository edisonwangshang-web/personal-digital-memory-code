"""The actual completed output is revalidated, with zero extraction calls."""

from copy import deepcopy
from unittest.mock import patch
import hashlib
import unittest

import test_known_extraction
import test_pipeline
import test_decomposed_pipeline
import test_revision_pipeline
import test_revision_resume_pipeline
from pdm.budget import BudgetedModelCall
from pdm.contracts import CaptureError, canonical
from pdm.model_calls import ModelCallError
from pdm.pipeline import (_call_metadata, _load, _objects, _load_sources, revalidate_completed_extraction,
                          publish, verify_publication)
from pdm.transaction import PipelineGit


class CompletedExtractionPipelineTests(unittest.TestCase):
    setUp = test_known_extraction.KnownExtractionTests.setUp
    prepare = test_known_extraction.KnownExtractionTests.prepare
    verify = test_known_extraction.KnownExtractionTests.verify
    completed = test_known_extraction.KnownExtractionTests.completed

    def model(self, role, payload, timeout=120):
        batch = payload.get("output_contract") == "batch-v1"
        response = (test_decomposed_pipeline.DecomposedPipelineTests.model(self, role, payload) if batch
                    else test_pipeline.PipelineTests.model(self, role, payload, timeout))
        if role == "extract" and "revision_context" in payload:
            response["output"]["objects"][0]["body"] += " 原文明确尚未完成。"
            # Keep the model's exact 40-reference output, including duplicates.
            # Recovery cannot trim or rewrite it to fit the former 32 bound.
            (response["output"]["wikis"][0] if batch else response["output"]["wiki"])["sources"] *= 40
        return response

    def call(self, fixture, model_call=None, **extra):
        return revalidate_completed_extraction(self.repo, fixture["run_id"],
            allowed_source_paths=fixture["source_paths"], related_runs=fixture["related_runs"],
            model_call=model_call or fixture["model_call"], attestation=fixture["attestation"],
            expected_attestation_sha256=fixture["expected_attestation_sha256"],
            original_payload=fixture["original_payload"], response=fixture["response"],
            original_resume_context=fixture["original_resume_context"], review_strategy="legacy-v1", **extra)

    def preserved(self, fixture):
        refs = [fixture["attestation"][key] for key in ("failure", "started", "revision_attempt")]
        return {ref["path"]: (self.repo / ref["path"]).read_bytes() for ref in refs}

    def test_exact_completed_40_ref_output_gets_only_new_review_and_self_contained_publication(self):
        from pdm.evidence import build_current_evidence
        from pdm.portable import prepare_reading_package, verify_reading_package
        fixture = self.completed()
        self.verdict = "passed"
        original = self.preserved(fixture)
        response_bytes = canonical(fixture["response"])
        self.assertEqual(len(fixture["response"]["output"]["wiki"]["sources"]), 40)
        with patch("pdm.pipeline.MAX_OBJECT_SOURCE_REFS", 32), self.assertRaises(CaptureError) as old:
            _objects(fixture["response"]["output"], _load_sources(self.repo, fixture["source_paths"], allow_versions=True, purpose="history"), fixture["run_id"], fixture["response"], fixture["attestation"]["original_extraction_versions"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(old.exception.code, "invalid_model_output")
        before = self.number
        def review_only(role, payload, **kwargs):
            self.assertEqual(role, "review", "An extraction replay was attempted")
            return self.model(role, payload)
        # Reuse the same audited adapter that observed the old extraction, so
        # this invocation's counts must exclude its earlier completed call.
        adapter = fixture["model_call"]
        adapter.transport = review_only
        result = self.call(fixture, adapter)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(self.number - before, 1)
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["attempted_calls"], 1)
        candidate = _load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        self.assertEqual(candidate["extraction_output"], fixture["response"]["output"])
        self.assertEqual(candidate["extraction"], _call_metadata(fixture["response"]))
        self.assertEqual(candidate["base_head"], fixture["attestation"]["head"])
        self.assertNotEqual(candidate["processing_key"], fixture["attestation"]["processing_key"])
        self.assertEqual(candidate["revision_revalidation"]["original_payload"], fixture["original_payload"])
        self.assertEqual(canonical(fixture["response"]), response_bytes)
        published = publish(self.repo, result["run_id"])
        for historical in (False, True):
            verified = verify_publication(self.repo, result["run_id"], source_paths=fixture["source_paths"], historical=historical)
            self.assertEqual(verified["files"], published["files"])
            self.assertTrue(set(original) <= {item["path"] for item in verified["authority_snapshots"]})
        self.assertEqual(len(build_current_evidence(self.repo, fixture["source_paths"], [result["run_id"]])["publications"][0]["documents"]), 2)
        output = self.repo.parent / (self.repo.name + "-revalidated-reading.json")
        self.addCleanup(lambda: output.unlink(missing_ok=True))
        package = prepare_reading_package(self.repo, fixture["source_paths"], ["What remains unfinished?"], [result["run_id"]], output=output)
        self.assertEqual(verify_reading_package(output, expected_sha256=package["sha256"])["status"], "integrity_verified")
        head, calls = fixture["git"].head(), self.number
        repeated = self.call(fixture, adapter)
        self.assertEqual(repeated["attempted_calls"], 0)
        self.assertEqual((fixture["git"].head(), self.number), (head, calls))
        self.assertEqual(self.preserved(fixture), original)
        self.assertEqual(fixture["git"].text("status", "--porcelain"), "")

    def test_revalidated_batch_consumes_complete_decomposed_review_and_group_proof(self):
        from pdm.batching import plan_batches, merge_batch_sources
        from pdm.batch_groups import publish_final_group, verify_group_publication
        from pdm.revision_context import build_revision_context
        # Reuse actual batch and zero-start fixture builders, then complete its
        # one extraction under the original operation before revalidation.
        normal_prepare = self.prepare
        def prepare_batch():
            self.prepare = lambda count=1: test_decomposed_pipeline.DecomposedPipelineTests.prepare(self, count)
            self.sources = lambda count=1: test_decomposed_pipeline.DecomposedPipelineTests.sources(self, count)
            self.run_batch = lambda *args, **kwargs: test_decomposed_pipeline.DecomposedPipelineTests.run_batch(self, *args, **kwargs)
            self.rejected = lambda strategy="auto": test_revision_pipeline.RevisionPipelineTests.rejected(self, strategy)
            try:
                paths, rejected, reservation, original = test_revision_resume_pipeline.RevisionResumePipelineTests.paused(self)
            finally:
                self.prepare = prepare_batch
            git = PipelineGit(self.repo)
            full = build_revision_context(git, rejected["run_id"], source_paths=paths)
            adapter = BudgetedModelCall(self.ledger, self.operation)
            return git, self.run_id, full, paths, adapter, reservation
        self.prepare = prepare_batch
        fixture = self.completed()
        self.prepare = normal_prepare
        self.verdict = "passed"
        original = self.preserved(fixture)
        plan = plan_batches(self.repo, fixture["source_paths"], max_input_bytes=100_000)
        projection = merge_batch_sources(self.repo, plan, [batch["batch_id"] for batch in plan["batches"]],
            source_paths=fixture["source_paths"], max_bytes=100_000)
        def review_only(role, payload, **kwargs):
            self.assertEqual(role, "review")
            return self.model(role, payload)
        fixture["model_call"].transport = review_only
        before = self.number
        result = revalidate_completed_extraction(self.repo, fixture["run_id"],
            allowed_source_paths=fixture["source_paths"], model_call=fixture["model_call"],
            attestation=fixture["attestation"], expected_attestation_sha256=fixture["expected_attestation_sha256"],
            original_payload=fixture["original_payload"], response=fixture["response"],
            original_resume_context=fixture["original_resume_context"], review_strategy="decomposed-v1",
            batch_context=projection["batch_context"], source_views=projection["source_views"])
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["extract_calls"], 0)
        self.assertEqual(self.number - before, result["attempted_calls"])
        self.assertGreaterEqual(result["attempted_calls"], 4)
        candidate = _load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        from pdm.pipeline import resume_decomposed_processing
        with self.assertRaises(CaptureError):
            resume_decomposed_processing(self.repo, result["run_id"], allowed_source_paths=fixture["source_paths"],
                model_call=BudgetedModelCall(fixture["model_call"].ledger, "different-review-operation", transport=review_only))
        group = publish_final_group(self.repo, plan, candidate["objects"], [result["run_id"]], source_paths=fixture["source_paths"])
        for history in (False, True):
            verified = verify_group_publication(self.repo, group["run_id"], source_paths=fixture["source_paths"], historical=history)
            self.assertTrue(set(original) <= {entry["path"] for entry in verified["authority_snapshots"]})
        self.assertEqual(self.preserved(fixture), original)

    def test_original_completed_failure_and_output_cannot_be_substituted(self):
        fixture = self.completed()
        before = fixture["git"].head(), self.number, fixture["model_call"].ledger.path.read_bytes()
        changed = deepcopy(fixture["response"])
        changed["output"]["wiki"]["sources"] = changed["output"]["wiki"]["sources"][:32]
        with self.assertRaises(CaptureError):
            self.call({**fixture, "response": changed})
        self.assertEqual((fixture["git"].head(), self.number, fixture["model_call"].ledger.path.read_bytes()), before)

    def test_stage_repetition_rejects_another_budget_operation_before_review(self):
        fixture = self.completed()
        class CrashBeforeReview(BudgetedModelCall):
            def __call__(adapter, role, payload, timeout=120):
                if role == "extract":
                    raise AssertionError("An extraction replay was attempted")
                raise KeyboardInterrupt("Fixture stop before Review budget start")
        adapter = CrashBeforeReview(fixture["model_call"].ledger, fixture["model_call"].operation_id)
        with self.assertRaises(KeyboardInterrupt):
            self.call(fixture, adapter)
        head, calls = fixture["git"].head(), self.number
        wrong = BudgetedModelCall(adapter.ledger, "different-review-operation", transport=lambda role, payload, **kw: self.model(role, payload))
        with self.assertRaises(CaptureError):
            self.call(fixture, wrong)
        self.assertEqual((fixture["git"].head(), self.number), (head, calls))

    def test_private_completed_switch_requires_original_run_and_revision(self):
        head, count = PipelineGit(self.repo).head(), self.number
        from pdm.pipeline import process_sources
        with self.assertRaises(CaptureError):
            process_sources(self.repo, [self.saved["path"]], model_call=self.model, _revalidate_completed={})
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, count))
