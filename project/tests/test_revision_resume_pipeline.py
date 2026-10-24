"""Zero-start reconciliation preserves real Git reservations and real audit IDs."""

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_revision_pipeline
from pdm.batch_groups import publish_final_group, verify_group_publication
from pdm.batching import merge_batch_sources, plan_batches
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.contracts import CaptureError, canonical
from pdm.pipeline import (_committed, _hash, _load, _revision_attempt_path,
                          process_sources, resume_unstarted_revision)
from pdm.revision_context import build_revision_context
from pdm.transaction import PipelineGit


class RevisionResumePipelineTests(unittest.TestCase):
    setUp = test_revision_pipeline.RevisionPipelineTests.setUp
    assert_error = test_revision_pipeline.RevisionPipelineTests.assert_error
    sources = test_revision_pipeline.RevisionPipelineTests.sources
    prepare = test_revision_pipeline.RevisionPipelineTests.prepare
    model = test_revision_pipeline.RevisionPipelineTests.model
    run_batch = test_revision_pipeline.RevisionPipelineTests.run_batch
    candidate = test_revision_pipeline.RevisionPipelineTests.candidate
    rejected = test_revision_pipeline.RevisionPipelineTests.rejected
    no_formal = test_revision_pipeline.RevisionPipelineTests.no_formal

    def paused(self):
        paths, _, projection, rejected = self.rejected("auto")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = BudgetLedger.create(Path(temporary.name) / "audit.json", None, 0,
            "artificial explicit zero-start resume", repo=self.repo, mode="audit")
        self.operation = "artificial-revision:extract:1"
        class PreflightPaused(BudgetedModelCall):
            def __call__(adapter, role, payload, timeout=120):
                raise KeyboardInterrupt("known preflight pause before ledger.start")
        adapter = PreflightPaused(self.ledger, self.operation)
        self.run_id = "run-" + "d" * 32
        with self.assertRaises(KeyboardInterrupt):
            process_sources(self.repo, paths, related_runs=[], revision_from_run=rejected["run_id"],
                run_id=self.run_id, model_call=adapter, batch_context=projection["batch_context"],
                source_views=projection["source_views"], review_strategy="auto")
        git = PipelineGit(self.repo)
        context = build_revision_context(git, rejected["run_id"], source_paths=paths)
        started = f"memory/runs/processes/{self.run_id}/started.md"
        pointer = _revision_attempt_path(context)
        state = self.ledger.snapshot()
        def ref(path):
            return {"path": path, "commit_sha": git.head(), "sha256": _hash(_committed(git, path))}
        reservation = {"schema_version": "0.9", "contract": "unstarted-revision-resume-v1",
            "run_id": self.run_id, "revision_from_run": rejected["run_id"], "head": git.head(),
            "started": ref(started), "revision_attempt": ref(pointer),
            "processing_key": _load(_committed(git, started), "processing_attempt")["processing_key"],
            "revision_context_sha256": _hash(context), "source_paths": sorted(paths), "related_runs": [],
            "budget": {**{key: state[key] for key in ("schema_version", "mode", "pool_id", "authorization_sha256", "limit", "initial_used")},
                "operation_id": self.operation, "reserved_roles": ["extract", "review"],
                "ledger_sha256": hashlib.sha256(self.ledger.path.read_bytes()).hexdigest()},
            "external_receipts_sha256": {"original-operator-reservation": "a" * 64}}
        self.assertEqual(self.ledger.operation(self.operation)["calls"], {})
        return paths, rejected, reservation, {started: (self.repo / started).read_bytes(), pointer: (self.repo / pointer).read_bytes()}

    def resume(self, paths, reservation, adapter=None, **kwargs):
        if adapter is None:
            adapter = BudgetedModelCall(self.ledger, self.operation,
                transport=lambda role, payload, **options: self.model(role, payload))
        return resume_unstarted_revision(self.repo, self.run_id, allowed_source_paths=paths,
            model_call=adapter, reservation=reservation,
            expected_reservation_sha256=hashlib.sha256(canonical(reservation)).hexdigest(),
            review_strategy="auto", **kwargs)

    def test_same_run_operation_resumes_at_original_head_then_group_verifies_and_reads(self):
        from pdm.evidence import build_current_evidence
        paths, rejected, reservation, original = self.paused()
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        projection = merge_batch_sources(self.repo, plan, [batch["batch_id"] for batch in plan["batches"]], source_paths=paths, max_bytes=100_000)
        actual_extract_heads = []
        def transport(role, payload, **options):
            if role == "extract":
                actual_extract_heads.append((PipelineGit(self.repo).head(), payload["controls"]["head"]))
            return self.model(role, payload)
        result = self.resume(paths, reservation, BudgetedModelCall(self.ledger, self.operation, transport=transport),
            batch_context=projection["batch_context"], source_views=projection["source_views"])
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["run_id"], self.run_id)
        self.assertEqual(actual_extract_heads, [(reservation["head"], reservation["head"])])
        candidate = self.candidate(result)
        self.assertNotEqual(candidate["processing_key"], reservation["processing_key"])
        self.assertEqual(candidate["base_head"], reservation["head"])
        self.assertEqual(candidate["revision_resume"]["reservation"], reservation)
        self.assertEqual(candidate["revision_resume"]["extraction_audit"]["call"]["status"], "completed")
        self.assertEqual(original, {path: (self.repo / path).read_bytes() for path in original})
        publication = publish_final_group(self.repo, plan, candidate["objects"], [self.run_id], source_paths=paths)
        for historical in (False, True):
            verified = verify_group_publication(self.repo, publication["run_id"], source_paths=paths, historical=historical)
            authority = {item["path"] for item in verified["authority_snapshots"]}
            self.assertTrue(set(original) <= authority)
            self.assertTrue({rejected["stage_path"], rejected["review_path"]} <= authority)
        self.assertEqual(len(build_current_evidence(self.repo, paths, [publication["run_id"]])["publications"][0]["documents"]), 2)
        self.assertEqual(self.ledger.operation(self.operation)["started_calls"], 2)
        self.assertEqual(original, {path: (self.repo / path).read_bytes() for path in original})
        self.assertEqual(PipelineGit(self.repo).text("status", "--porcelain"), "")

    def test_started_and_unknown_call_are_never_replayed_by_resume(self):
        paths, _, reservation, original = self.paused()
        self.ledger.start(self.operation, "extract", "b" * 64)
        head, calls = PipelineGit(self.repo).head(), self.number
        for status in ("started", "unknown"):
            if status != "started":
                self.ledger.finish(self.operation, "extract", status)
            with self.subTest(status=status), self.assertRaises(CaptureError):
                self.resume(paths, reservation)
            self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, calls))
        self.assertEqual(original, {path: (self.repo / path).read_bytes() for path in original})
        self.no_formal()

    def test_public_resume_cannot_move_unknown_review_to_another_pool_or_operation(self):
        from pdm.model_calls import ModelCallError
        from pdm.pipeline import resume_decomposed_processing

        paths, _, reservation, original = self.paused()
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        projection = merge_batch_sources(self.repo, plan, [batch["batch_id"] for batch in plan["batches"]],
            source_paths=paths, max_bytes=100_000)
        def interrupted_review(role, payload, **options):
            if role == "review":
                raise ModelCallError("transport_outcome_unknown", "model-" + "e" * 32)
            return self.model(role, payload)
        adapter = BudgetedModelCall(self.ledger, self.operation, transport=interrupted_review)
        result = self.resume(paths, reservation, adapter,
            batch_context=projection["batch_context"], source_views=projection["source_views"])
        self.assertEqual(result["status"], "failed", result)
        candidate = self.candidate(result)
        self.assertIn("revision_resume", candidate)
        self.assertNotIn("revision_revalidation", candidate)
        self.assertEqual(self.ledger.operation(self.operation)["calls"]["review"]["status"], "unknown")
        self.ledger.reserve("different-operation", ["extract", "review"])
        other_pool = BudgetLedger.create(self.ledger.path.parent / "other-pool.json", None, 0,
            "artificial alternate pool", repo=self.repo, mode="audit")
        other_pool.reserve(self.operation, ["extract", "review"])
        head, calls = PipelineGit(self.repo).head(), self.number
        before = self.ledger.path.read_bytes(), other_pool.path.read_bytes()
        for ledger, operation in ((self.ledger, "different-operation"), (other_pool, self.operation)):
            with self.subTest(operation=operation, pool=ledger.path.name):
                replacement = BudgetedModelCall(ledger, operation,
                    transport=lambda role, payload, **options: self.model(role, payload))
                with self.assertRaises(CaptureError) as rejected:
                    resume_decomposed_processing(self.repo, self.run_id, allowed_source_paths=paths,
                        model_call=replacement)
                self.assertEqual(rejected.exception.code, "revision_resume_budget_mismatch")
                self.assertEqual(replacement.observed_started_count, 0)
        same = resume_decomposed_processing(self.repo, self.run_id, allowed_source_paths=paths, model_call=adapter)
        self.assertEqual((same["status"], same["attempted_calls"]), ("unknown", 0))
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, calls))
        self.assertEqual((self.ledger.path.read_bytes(), other_pool.path.read_bytes()), before)
        self.assertEqual(original, {path: (self.repo / path).read_bytes() for path in original})
        self.assertFalse((self.repo / f"memory/runs/processes/{self.run_id}/decomposed-plan.md").exists())
        self.no_formal()

    def test_same_ledger_does_not_authorize_new_operation_or_pool(self):
        paths, _, reservation, _ = self.paused()
        head, calls = PipelineGit(self.repo).head(), self.number
        self.ledger.reserve("a-different-operation", ["extract", "review"])
        for operation in ("a-different-operation", "missing-operation"):
            with self.subTest(operation=operation), self.assertRaises(CaptureError):
                self.resume(paths, reservation, BudgetedModelCall(self.ledger, operation,
                    transport=lambda role, payload, **options: self.model(role, payload)))
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, calls))
        self.no_formal()

    def test_private_resume_switch_without_explicit_revision_or_run_is_rejected(self):
        head, count = PipelineGit(self.repo).head(), self.number
        for kwargs in ({}, {"run_id": "run-" + "c" * 32}):
            with self.subTest(kwargs=kwargs):
                self.assert_error("invalid_revision_resume", lambda: process_sources(self.repo, [self.saved["path"]],
                    model_call=self.model, _resume_unstarted={}, **kwargs))
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, count))

    def test_wrong_expected_receipt_hash_and_absent_budget_adapter_cannot_resume(self):
        paths, _, reservation, _ = self.paused()
        head, count = PipelineGit(self.repo).head(), self.number
        with self.assertRaises(CaptureError):
            resume_unstarted_revision(self.repo, self.run_id, allowed_source_paths=paths,
                model_call=self.model, reservation=reservation, expected_reservation_sha256="f" * 64,
                review_strategy="auto")
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, count))
