"""A known completed synthetic rebuild recovers with Review, never extraction."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from engine_fixtures import advance_engine, isolated_engine
from git_fixtures import configure_test_repository
from test_pipeline import git
import test_quality_completed_extraction as legacy_fixtures
from pdm import pipeline
from pdm.batching import plan_batches, project_batch_sources
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.quality_completed_extraction import (
    CONTRACT, CONTRACT_V2, RESERVATION_CONTRACT_V2, RESPONSE_PROVENANCE,
    prepare_attestation, verify_completed_extraction, verify_revalidated_extraction,
)
from pdm.rebuild import prepare_rebuild
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class CompletedRebuildTests(unittest.TestCase):
    native = legacy_fixtures.QualityCompletedExtractionTests.native

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-completed-rebuild-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "data"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Rebuild Recovery")
        git(self.repo, "config", "user.email", "rebuild-recovery@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial data baseline")
        self.code = isolated_engine(self)
        self.saved = capture(self.repo, {"source_key": "artificial:rebuild-recovery", "source_type": "note",
            "acquisition_status": "full_text", "body": "date,load_kg\n2025-01-01,10\n2025-01-02,20\n"})
        self.bundle = resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")
        self.ledger_path = self.root / "audit" / "calls.json"
        self.ledger_path.parent.mkdir(mode=0o700)
        BudgetLedger.create(self.ledger_path, None, 0, "artificial rebuild recovery", repo=self.repo, mode="audit")
        self.ledger = BudgetLedger(self.ledger_path, repo=self.repo)
        self.operation = "artificial:rebuild:second"
        self.adapter = BudgetedModelCall(self.ledger, self.operation)
        self.cache = self.root / "model-cache" / hashlib.sha256(self.operation.encode()).hexdigest() / "extract"
        self.cache.mkdir(mode=0o700, parents=True)
        self.cache.parent.chmod(0o700)
        self.number, self.phase, self.roles, self.review_requests = 0, "initial", [], []
        native = patch("pdm.model_calls._call_transport", side_effect=self.native)
        native.start()
        self.addCleanup(native.stop)
        old = pipeline.process_sources(self.repo, [self.saved["path"]], related_runs=[],
            skill_bundle=self.bundle, model_call=self.adapter.for_operation("artificial:previous-publication"))
        published = pipeline.publish(self.repo, old["run_id"])
        self.rebuild = prepare_rebuild(self.repo, "artificial:clear-then-rebuild", [self.saved["path"]],
                                      sorted(published["files"]))["rebuild_context"]
        projection = self.projection()
        self.batch_options = {"batch_context": projection["batch_context"],
                              "source_views": projection["source_views"], "review_strategy": "auto"}
        self.phase = "reject"
        self.rejected = pipeline.process_sources(self.repo, [self.saved["path"]], related_runs=[],
            skill_bundle=self.bundle, rebuild_context=self.rebuild,
            model_call=self.adapter.for_operation("artificial:rebuild:first"), **self.batch_options)
        self.assertEqual(self.rejected["status"], "needs_revision")
        self.phase = "complete"
        failed_run = "run-" + "d" * 32
        original_objects = pipeline._objects
        failure_error = ("candidate_too_large" if self._testMethodName == "test_other_failure_kind_is_not_v2_eligible"
                         else "quality_temporal_mismatch")

        def old_validation(*args, **kwargs):
            result = original_objects(*args, **kwargs)
            if args[2] == failed_run:
                raise CaptureError(failure_error, "Artificial old validator rejected a valid output")
            return result

        with patch("pdm.pipeline._objects", side_effect=old_validation):
            self.failed = pipeline.process_sources(self.repo, [self.saved["path"]], related_runs=[],
                run_id=failed_run, revision_from_run=self.rejected["run_id"],
                model_call=self.adapter, skill_bundle=self.bundle, rebuild_context=self.rebuild,
                **self.batch_options)
        self.assertEqual((self.failed["status"], self.failed["error"]), ("failed", failure_error))
        self.git = PipelineGit(self.repo)
        self.receipts = {name: hashlib.sha256((self.cache / name).read_bytes()).hexdigest()
                         for name in ("payload.json", "response.json", "request-binding.json", "request.bin")}
        self.payload = json.loads((self.cache / "payload.json").read_text())
        self.response = json.loads((self.cache / "response.json").read_text())
        self.binding = json.loads((self.cache / "request-binding.json").read_text())
        failure = pipeline._load((self.repo / self.failed["failure_path"]).read_bytes(), "failure")
        self.assertEqual(set(failure["call"]), {"model", "invocation_id", "elapsed_seconds"})
        advance_engine(self.code)

    def projection(self):
        plan = plan_batches(self.repo, [self.saved["path"]], max_input_bytes=240000,
                            purpose="history", rebuild_context=self.rebuild)
        return project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"],
                                     source_paths=[self.saved["path"]])

    def prepare(self, contract=CONTRACT_V2):
        return prepare_attestation(self.git, self.failed["run_id"], model_call=self.adapter,
            source_paths=[self.saved["path"]], original_payload=self.payload, response=self.response,
            original_native_request_binding=self.binding, external_receipts_sha256=self.receipts,
            cache_dir=self.cache, contract=contract)

    def recover(self, prepared=None):
        prepared = self.prepare() if prepared is None else prepared
        projection = self.projection()
        return pipeline.revalidate_completed_extraction(self.repo, self.failed["run_id"],
            allowed_source_paths=[self.saved["path"]], model_call=self.adapter,
            rebuild_context=self.rebuild, batch_context=projection["batch_context"],
            source_views=projection["source_views"], review_strategy="auto", **prepared)

    def test_zero_extract_review_atomic_guard_publish_and_cache_independent_history(self):
        prepared = self.prepare()
        self.assertEqual(prepared["attestation"]["contract"], CONTRACT_V2)
        self.assertEqual(prepared["attestation"]["response_provenance"], RESPONSE_PROVENANCE)
        self.assertEqual(prepared["original_resume_context"]["contract"], RESERVATION_CONTRACT_V2)
        self.assertEqual(prepared["attestation"]["rebuild_context"], self.rebuild)
        protected = {prepared["attestation"][key]["path"]:
                     (self.repo / prepared["attestation"][key]["path"]).read_bytes()
                     for key in ("started", "revision_attempt", "failure")}
        before_roles = len(self.roles)
        result = self.recover(prepared)
        self.assertEqual((result["status"], result["extract_calls"]), ("passed", 0), result)
        self.assertEqual(self.roles[before_roles:], [("review", "complete")])
        candidate = pipeline._load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        self.assertEqual(candidate["extraction_output"], self.response["output"])
        self.assertEqual(candidate["rebuild_context"], self.rebuild)
        guard = candidate["quality_revalidation_guard_ref"]["path"]
        self.assertEqual(git(self.repo, "log", "-1", "--format=%H", "--", guard),
                         git(self.repo, "log", "-1", "--format=%H", "--", result["stage_path"]))
        publication = pipeline.publish(self.repo, result["run_id"])
        shutil.rmtree(self.cache.parent.parent)
        self.ledger_path.unlink()
        self.assertTrue(verify_revalidated_extraction(self.git, candidate, historical=True))
        self.assertEqual(pipeline.verify_publication(self.repo, result["run_id"])["files"], publication["files"])
        for path, raw in protected.items():
            self.assertEqual((self.repo / path).read_bytes(), raw)

    def test_v1_and_unknown_contract_cannot_accept_temporal_rebuild_failure(self):
        for contract in (CONTRACT, "quality-completed-extraction-revalidation-v3"):
            with self.subTest(contract=contract), self.assertRaises(CaptureError):
                self.prepare(contract)

    def test_other_failure_kind_is_not_v2_eligible(self):
        with self.assertRaises(CaptureError):
            self.prepare()

    def test_started_review_cannot_prepare_recovery(self):
        self.ledger.start(self.operation, "review", "0" * 64, "model-" + "e" * 32)
        with self.assertRaises(CaptureError):
            self.prepare()

    def test_unknown_extract_cannot_prepare_recovery(self):
        with self.ledger._locked() as directory:
            state = self.ledger._read(directory)
            state["operations"][self.operation]["calls"]["extract"]["status"] = "unknown"
            state["operations"][self.operation]["calls"]["extract"]["error_code"] = "artificial_unknown"
            self.ledger._write(directory, state)
        with self.assertRaises(CaptureError):
            self.prepare()

    def test_changed_cache_response_cannot_be_attested_as_original(self):
        raw = self.cache / "response.json"
        raw.write_bytes(raw.read_bytes() + b" ")
        with self.assertRaises(CaptureError):
            self.prepare()

    def test_wrong_rebuild_reference_is_rejected_before_any_review(self):
        prepared = self.prepare()
        before = self.number, self.ledger_path.read_bytes(), self.git.head()
        wrong = deepcopy(self.rebuild)
        wrong["sha256"] = "sha256-" + "f" * 64
        with self.assertRaises(CaptureError):
            pipeline.revalidate_completed_extraction(self.repo, self.failed["run_id"],
                allowed_source_paths=[self.saved["path"]], model_call=self.adapter,
                rebuild_context=wrong, **prepared)
        self.assertEqual((self.number, self.ledger_path.read_bytes(), self.git.head()), before)

    def test_persisted_context_and_attestation_contracts_must_match(self):
        result = self.recover()
        candidate = pipeline._load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        candidate["revision_revalidation"]["contract"] = CONTRACT
        with self.assertRaises(CaptureError):
            verify_revalidated_extraction(self.git, candidate, historical=True)

    def test_persisted_guard_hash_cannot_be_exchanged(self):
        result = self.recover()
        candidate = pipeline._load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        candidate["quality_revalidation_guard_ref"]["sha256"] = "sha256-" + "e" * 64
        with self.assertRaises(CaptureError):
            verify_revalidated_extraction(self.git, candidate, historical=True)

    def test_guard_restored_in_a_later_commit_is_not_an_atomic_pair(self):
        result = self.recover()
        candidate = pipeline._load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        path = candidate["quality_revalidation_guard_ref"]["path"]
        original = (self.repo / path).read_bytes()
        git(self.repo, "rm", path)
        git(self.repo, "commit", "-m", "artificial guard removal")
        (self.repo / path).write_bytes(original)
        git(self.repo, "add", path)
        git(self.repo, "commit", "-m", "artificial later guard restoration")
        with self.assertRaises(CaptureError):
            verify_revalidated_extraction(self.git, candidate, historical=True)

    def test_later_revalidation_failure_preserves_original_failure(self):
        prepared = self.prepare()
        original_path = self.repo / self.failed["failure_path"]
        original_failure = original_path.read_bytes()
        before_roles, before_audit = len(self.roles), self.ledger_path.read_bytes()
        current_program = pipeline.execution_versions(self.git)["program_commit"]
        original_objects = pipeline._objects
        def invalid_under_new_program(*args, **kwargs):
            result = original_objects(*args, **kwargs)
            if args[2] == self.failed["run_id"] and args[4]["program_commit"] == current_program:
                raise CaptureError("quality_statistic_mismatch", "Artificial subsequent validation failure")
            return result
        with patch("pdm.pipeline._objects", side_effect=invalid_under_new_program):
            result = self.recover(prepared)
        self.assertEqual((result["status"], result["error"]), ("failed", "quality_statistic_mismatch"))
        self.assertTrue(result["failure_path"].endswith("/revalidation-failure.md"))
        self.assertEqual(original_path.read_bytes(), original_failure)
        self.assertEqual(self.roles[before_roles:], [])
        self.assertEqual(self.ledger_path.read_bytes(), before_audit)


if __name__ == "__main__":
    unittest.main()
