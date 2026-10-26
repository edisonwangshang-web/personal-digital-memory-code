"""Real empty budget reservations, including refusals after a call starts."""

from copy import deepcopy
from pathlib import Path
import hashlib
import tempfile
import unittest
from unittest.mock import patch

import test_pipeline
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.contracts import CaptureError, canonical
from pdm.pipeline import _call_metadata, _hash, _revision_attempt_path, process_sources
from pdm.revision_context import build_revision_context
from pdm.revision_resume import CONTRACT, attach_revision_resume_extraction, verify_unstarted_revision
from pdm.transaction import PipelineGit


class RevisionResumeTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    model = test_pipeline.PipelineTests.model
    commit_change = test_pipeline.PipelineTests.commit_change

    def prepare(self):
        self.verdict = "needs_revision"
        paths = [self.saved["path"]]
        rejected = process_sources(self.repo, paths, related_runs=[], model_call=self.model)
        git = PipelineGit(self.repo)
        full = build_revision_context(git, rejected["run_id"], source_paths=paths, related_runs=[])
        private = tempfile.TemporaryDirectory(prefix="pdm-revision-resume-audit-")
        self.addCleanup(private.cleanup)
        ledger = BudgetLedger.create(Path(private.name) / "audit.json", None, 0,
            "Artificial explicit zero-start revision recovery only", mode="audit")
        class Paused(BudgetedModelCall):
            def __call__(self, role, payload, timeout=120):
                raise RuntimeError("Artificial pause before ledger.start and transport")
        operation = "revision-resume:artificial:extract:1"
        adapter = Paused(ledger, operation)
        run = "run-" + "7" * 32
        with self.assertRaises(RuntimeError):
            process_sources(self.repo, paths, related_runs=[], revision_from_run=rejected["run_id"],
                            run_id=run, model_call=adapter)
        head = git.head()
        started = f"memory/runs/processes/{run}/started.md"
        pointer = _revision_attempt_path(full)
        import json
        start = json.loads(git.read(head, started).split(b"\n---\n", 1)[1])
        state = ledger.snapshot()
        reservation = {"schema_version": "0.9", "contract": CONTRACT, "run_id": run,
            "revision_from_run": rejected["run_id"], "head": head,
            "started": {"path": started, "commit_sha": head, "sha256": _hash(git.read(head, started))},
            "revision_attempt": {"path": pointer, "commit_sha": head, "sha256": _hash(git.read(head, pointer))},
            "processing_key": start["processing_key"], "revision_context_sha256": _hash(full),
            "source_paths": paths, "related_runs": [], "budget": {
                **{key: state[key] for key in ("schema_version", "pool_id", "authorization_sha256", "limit", "initial_used")},
                "mode": "audit", "operation_id": operation, "reserved_roles": ["extract", "review"],
                "ledger_sha256": hashlib.sha256(ledger.path.read_bytes()).hexdigest()},
            "external_receipts_sha256": {"prior-operator-attestation.json": hashlib.sha256(b"Artificial reviewed attestation").hexdigest()}}
        return git, run, full, paths, adapter, reservation

    def verify(self, prepared, reservation=None):
        git, run, full, paths, adapter, original = prepared
        value = original if reservation is None else reservation
        return verify_unstarted_revision(git, run, full, model_call=adapter, source_paths=paths,
            related_runs=[], reservation=value, expected_reservation_sha256=hashlib.sha256(canonical(value)).hexdigest())

    def test_exact_unstarted_reservation_is_read_only_and_keeps_original_identity(self):
        fixture = self.prepare()
        git, run, _, _, adapter, reservation = fixture
        before = git.head(), adapter.ledger.path.read_bytes(), self.number
        context = self.verify(fixture)
        self.assertEqual(context["reservation"], reservation)
        self.assertEqual(context["reservation"]["run_id"], run)
        self.assertEqual(context["reservation"]["budget"]["operation_id"], adapter.operation_id)
        self.assertEqual(adapter.operation_state()["calls"], {})
        self.assertEqual((git.head(), adapter.ledger.path.read_bytes(), self.number), before)

    def test_operation_with_any_started_call_is_not_zero_start_even_with_fresh_hash_attestation(self):
        fixture = self.prepare()
        adapter, reservation = fixture[4], deepcopy(fixture[5])
        adapter.ledger.start(adapter.operation_id, "extract", "0" * 64)
        reservation["budget"]["ledger_sha256"] = hashlib.sha256(adapter.ledger.path.read_bytes()).hexdigest()
        before = adapter.ledger.path.read_bytes()
        with self.assertRaises(CaptureError):
            self.verify(fixture, reservation)
        self.assertEqual(adapter.ledger.path.read_bytes(), before)
        self.assertEqual(adapter.operation_state()["started_calls"], 1)

    def test_wrong_operation_or_unbudgeted_adapter_cannot_claim_original_empty_reservation(self):
        fixture = self.prepare()
        git, run, full, paths, adapter, reservation = fixture
        expected = hashlib.sha256(canonical(reservation)).hexdigest()
        for model in (adapter.for_operation("revision-resume:different:operation"), self.model):
            with self.assertRaises(CaptureError):
                verify_unstarted_revision(git, run, full, model_call=model, source_paths=paths,
                    related_runs=[], reservation=reservation, expected_reservation_sha256=expected)
        self.assertEqual(adapter.operation_state()["calls"], {})

    def test_scope_or_pointer_tampering_is_rejected_before_source_bodies_are_loaded(self):
        fixture = self.prepare()
        for field in ("revision_context_sha256", "source_paths", "started"):
            value = deepcopy(fixture[5])
            if field == "revision_context_sha256":
                value[field] = "sha256-" + "0" * 64
            elif field == "source_paths":
                value[field] = []
            else:
                value[field]["sha256"] = "sha256-" + "0" * 64
            with patch("pdm.pipeline._load_sources", side_effect=AssertionError("Unauthorized original opened")):
                with self.assertRaises(CaptureError):
                    self.verify(fixture, value)
        self.assertEqual(fixture[4].operation_state()["calls"], {})

    def test_head_advance_prevents_zero_start_automatic_reentry(self):
        fixture = self.prepare()
        self.commit_change("unrelated.md", b"Artificial concurrent change.\n")
        with self.assertRaises(CaptureError):
            self.verify(fixture)
        self.assertEqual(fixture[4].operation_state()["calls"], {})

    def test_missing_engine_proof_is_rejected_before_resuming_an_unstarted_call(self):
        fixture = self.prepare()
        git, run, _, _, adapter, _ = fixture
        (self.repo / f"memory/runs/processes/{run}/engine.md").unlink()
        before = git.head(), adapter.ledger.path.read_bytes(), self.number
        with self.assertRaises(CaptureError) as raised:
            self.verify(fixture)
        self.assertEqual(raised.exception.code, "engine_provenance_changed")
        self.assertEqual((git.head(), adapter.ledger.path.read_bytes(), self.number), before)
        self.assertEqual(adapter.operation_state()["calls"], {})

    def test_completed_extraction_attaches_real_record_and_rejects_payload_or_invocation_substitution(self):
        fixture = self.prepare()
        context = self.verify(fixture)
        base = fixture[4]
        def transport(role, payload, timeout=120):
            return {"output": {"artificial": True}, "model": "artificial", "usage": None,
                    "elapsed_seconds": 0.01, "invocation_id": "model-" + "8" * 32}
        adapter = BudgetedModelCall(base.ledger, base.operation_id, transport)
        payload = {"artificial_complete_input": ["one", "two"]}
        response = adapter("extract", payload)
        extraction = _call_metadata(response)
        attached = attach_revision_resume_extraction(adapter, context, extraction, payload)
        self.assertEqual(attached["extraction_audit"]["call"], adapter.operation_state()["calls"]["extract"])
        self.assertEqual(attached["extraction_audit"]["call"]["status"], "completed")
        with self.assertRaises(CaptureError):
            attach_revision_resume_extraction(adapter, context, extraction, {"different_input": True})
        changed = {**extraction, "invocation_id": "model-" + "9" * 32}
        with self.assertRaises(CaptureError):
            attach_revision_resume_extraction(adapter, context, changed, payload)
        self.assertEqual(adapter.operation_state()["started_calls"], 1)


if __name__ == "__main__":
    unittest.main()
