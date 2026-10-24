"""Ordinary auto fallback must retain the completed extraction's operation."""

import hashlib
from pathlib import Path
import tempfile
import unittest

import test_decomposed_pipeline
from pdm.budget import BudgetLedger, BudgetedModelCall, _json
from pdm.contracts import CaptureError
from pdm.model_calls import ModelCallError
from pdm.pipeline import (process_sources, publish, resume_decomposed_processing,
                          verify_publication)
from pdm.transaction import PipelineGit


class AutoReviewOperationTests(unittest.TestCase):
    setUp = test_decomposed_pipeline.DecomposedPipelineTests.setUp
    sources = test_decomposed_pipeline.DecomposedPipelineTests.sources
    prepare = test_decomposed_pipeline.DecomposedPipelineTests.prepare
    model = test_decomposed_pipeline.DecomposedPipelineTests.model
    candidate = test_decomposed_pipeline.DecomposedPipelineTests.candidate
    no_formal = test_decomposed_pipeline.DecomposedPipelineTests.no_formal

    def paused(self, *, review_started):
        paths, _, projection = self.prepare()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = BudgetLedger.create(Path(temporary.name) / "audit.json", None, 0,
            "artificial ordinary auto operation", repo=self.repo, mode="audit")
        self.operation = "artificial-ordinary-auto"
        def transport(role, payload, **options):
            if role == "review":
                raise KeyboardInterrupt("Artificial ordinary Review outcome is unknown")
            return self.model(role, payload)
        class SafePrelaunch(BudgetedModelCall):
            def __call__(adapter, role, payload, timeout=120):
                if role == "review":
                    raise ModelCallError("input_limit", "model-" + "e" * 32)
                return super().__call__(role, payload, timeout=timeout)
        adapter = (BudgetedModelCall if review_started else SafePrelaunch)(
            self.ledger, self.operation, transport=transport)
        run_id = "run-" + "a" * 32
        def execute():
            return process_sources(self.repo, paths, related_runs=[], model_call=adapter, run_id=run_id,
                batch_context=projection["batch_context"], source_views=projection["source_views"],
                review_strategy="auto")
        if review_started:
            with self.assertRaises(KeyboardInterrupt):
                execute()
            result = {"run_id": run_id, "stage_path": f"memory/staging/{run_id}.md"}
        else:
            result = execute()
            self.assertEqual(result["status"], "failed", result)
        candidate = self.candidate(result)
        self.assertIsNone(candidate.get("execution_context"))
        self.assertNotIn("revision_resume", candidate)
        self.assertNotIn("revision_revalidation", candidate)
        self.assertEqual(self.ledger.operation(self.operation)["calls"]["extract"]["status"], "completed")
        return paths, result, candidate

    def adapter(self, ledger=None, operation=None):
        return BudgetedModelCall(ledger or self.ledger, operation or self.operation,
            transport=lambda role, payload, **options: self.model(role, payload))

    def test_unknown_review_cannot_fallback_through_empty_pool_or_operation(self):
        paths, result, _ = self.paused(review_started=True)
        self.assertEqual(self.ledger.operation(self.operation)["calls"]["review"]["status"], "unknown")
        self.ledger.reserve("different-operation", ["extract", "review"])
        other = BudgetLedger.create(self.ledger.path.parent / "other.json", None, 0,
            "artificial alternate pool", repo=self.repo, mode="audit")
        other.reserve(self.operation, ["extract", "review"])
        before = PipelineGit(self.repo).head(), self.number, self.ledger.path.read_bytes(), other.path.read_bytes()
        for adapter in (self.adapter(self.ledger, "different-operation"), self.adapter(other)):
            with self.subTest(pool=adapter.ledger.path.name, operation=adapter.operation_id):
                with self.assertRaises(CaptureError) as rejected:
                    resume_decomposed_processing(self.repo, result["run_id"], allowed_source_paths=paths,
                        model_call=adapter)
                self.assertEqual(rejected.exception.code, "review_extraction_operation_mismatch")
                self.assertEqual(adapter.observed_started_count, 0)
        same = resume_decomposed_processing(self.repo, result["run_id"], allowed_source_paths=paths,
            model_call=self.adapter())
        self.assertEqual((same["status"], same["attempted_calls"]), ("unknown", 0))
        self.assertEqual((PipelineGit(self.repo).head(), self.number,
            self.ledger.path.read_bytes(), other.path.read_bytes()), before)
        self.no_formal()

    def test_completed_extraction_and_proven_unstarted_review_can_continue(self):
        paths, result, _ = self.paused(review_started=False)
        self.assertEqual(set(self.ledger.operation(self.operation)["calls"]), {"extract"})
        before = self.number
        resumed = resume_decomposed_processing(self.repo, result["run_id"], allowed_source_paths=paths,
            model_call=self.adapter())
        self.assertEqual(resumed["status"], "passed", resumed)
        self.assertEqual(self.roles.count("extract"), 1)
        self.assertEqual(resumed["attempted_calls"], self.number - before)
        self.assertGreaterEqual(resumed["attempted_calls"], 4)
        self.assertEqual(set(self.ledger.operation(self.operation)["calls"]), {"extract"})
        publish(self.repo, result["run_id"])
        for historical in (False, True):
            verify_publication(self.repo, result["run_id"], source_paths=paths, historical=historical)

    def test_completed_record_must_match_original_payload_and_invocation(self):
        paths, result, candidate = self.paused(review_started=False)
        digest = hashlib.sha256(_json(self.contexts[0])).hexdigest()
        invocation = candidate["extraction"]["invocation_id"]
        for label, payload_sha, invocation_id in (("wrong-payload", "f" * 64, invocation),
                ("wrong-invocation", digest, "model-" + "f" * 32)):
            with self.subTest(label=label):
                operation = "artificial-" + label
                self.ledger.reserve(operation, ["extract", "review"])
                self.ledger.start(operation, "extract", payload_sha)
                self.ledger.finish(operation, "extract", "completed", invocation_id=invocation_id)
                before = PipelineGit(self.repo).head(), self.number, self.ledger.path.read_bytes()
                adapter = self.adapter(operation=operation)
                with self.assertRaises(CaptureError) as rejected:
                    resume_decomposed_processing(self.repo, result["run_id"], allowed_source_paths=paths,
                        model_call=adapter)
                self.assertEqual(rejected.exception.code, "review_extraction_operation_mismatch")
                self.assertEqual(adapter.observed_started_count, 0)
                self.assertEqual((PipelineGit(self.repo).head(), self.number, self.ledger.path.read_bytes()), before)
        self.no_formal()
