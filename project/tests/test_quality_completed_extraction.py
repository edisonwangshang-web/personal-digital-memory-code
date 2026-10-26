"""Known completed native calls recover from reviewed synthetic cache, never models."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from engine_fixtures import isolated_engine, advance_engine
from quality_fixtures import add_quality, passed_review
from test_pipeline import git
from pdm import pipeline
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.batching import plan_batches, project_batch_sources
from pdm.capture import capture
from pdm.contracts import CaptureError, canonical
from pdm.model_calls import render_model_request, request_binding
from pdm.quality_completed_extraction import (CONTRACT, RESPONSE_PROVENANCE, RESERVATION_CONTRACT,
    prepare_attestation, verify_completed_extraction, verify_revalidated_extraction)
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class QualityCompletedExtractionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-quality-completed-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "data"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Completed Extraction")
        git(self.repo, "config", "user.email", "completed@example.invalid")
        self.code = isolated_engine(self)
        legacy = self._testMethodName.startswith("test_legacy_")
        legacy_patches = []
        if legacy:
            shutil.copytree(self.code / "project", self.repo / "project")
            shutil.copyfile(self.code / "pyproject.toml", self.repo / "pyproject.toml")
            git(self.repo, "add", ".")
            git(self.repo, "commit", "-m", "artificial legacy embedded engine")
            legacy_patches = [patch("pdm.engine_provenance.program_commit", return_value=git(self.repo, "rev-parse", "HEAD")),
                              patch("pdm.engine_provenance.reservation_file", return_value={})]
            for fixture in legacy_patches:
                fixture.start()
                self.addCleanup(fixture.stop)
        else:
            git(self.repo, "commit", "--allow-empty", "-m", "artificial data-only baseline")
        self.saved = capture(self.repo, {"source_key": "artificial:completed", "source_type": "note",
            "acquisition_status": "full_text", "body": "date,load_kg\n2025-01-01,10\n2025-01-02,20\n"})
        self.bundle = resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")
        self.rejected_bundle = resolve_skill_refs("knowledge-extract@9", "knowledge-review@7")
        self.assertNotEqual(self.rejected_bundle, self.bundle)
        self.ledger_path = self.root / "audit" / "calls.json"
        self.ledger_path.parent.mkdir(mode=0o700)
        BudgetLedger.create(self.ledger_path, None, 0, "artificial completed recovery", repo=self.repo, mode="audit")
        self.ledger = BudgetLedger(self.ledger_path, repo=self.repo)
        self.operation = "artificial:completed:original"
        self.adapter = BudgetedModelCall(self.ledger, self.operation)
        self.cache = self.root / "model-cache" / hashlib.sha256(self.operation.encode()).hexdigest() / "extract"
        self.cache.mkdir(mode=0o700, parents=True)
        self.cache.parent.chmod(0o700)
        self.number, self.phase, self.roles = 0, "reject", []
        self.review_requests = []
        self.patch_native = patch("pdm.model_calls._call_transport", side_effect=self.native)
        self.patch_native.start()
        self.addCleanup(self.patch_native.stop)
        self.batch = self._testMethodName.startswith("test_batch_")
        batch_options = {}
        if self.batch:
            plan = plan_batches(self.repo, [self.saved["path"]], max_input_bytes=240000, purpose="history")
            projected = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=[self.saved["path"]])
            batch_options = {"batch_context": projected["batch_context"], "source_views": projected["source_views"],
                             "review_strategy": "auto"}
        rejected = pipeline.process_sources(self.repo, [self.saved["path"]], model_call=self.adapter.for_operation("artificial:rejected"),
                                            skill_bundle=self.rejected_bundle, **batch_options)
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        self.rejected = rejected
        self.phase = "complete"
        original_document = pipeline._document

        def storage_limit(kind, value, schema_version="0.2"):
            raw = original_document(kind, value, schema_version)
            # Force the existing storage boundary after the actual completed
            # call and all validators, without changing its payload or response.
            return raw + b" " * (5 * 1024 * 1024) if kind == "candidate" else raw

        with patch("pdm.pipeline._document", side_effect=storage_limit):
            result = pipeline.process_sources(self.repo, [self.saved["path"]], related_runs=[],
                revision_from_run=rejected["run_id"], model_call=self.adapter, skill_bundle=self.bundle, **batch_options)
        self.assertEqual((result["status"], result["error"]), ("failed", "candidate_too_large"), result)
        self.failed = result
        self.git = PipelineGit(self.repo)
        self.receipts = {name: hashlib.sha256((self.cache / name).read_bytes()).hexdigest()
                         for name in ("payload.json", "response.json", "request-binding.json", "request.bin")}
        self.payload = json.loads((self.cache / "payload.json").read_text())
        self.assertEqual(self.payload["skill_bundle"], self.bundle)
        self.assertEqual(self.payload["revision_context"]["candidate"]["quality_provenance"]["skill_bundle_sha256"],
                         self.rejected_bundle["sha256"])
        self.response = json.loads((self.cache / "response.json").read_text())
        self.binding = json.loads((self.cache / "request-binding.json").read_text())
        for fixture in legacy_patches:
            fixture.stop()
        if legacy:
            git(self.repo, "rm", "-r", "project", "pyproject.toml")
            git(self.repo, "commit", "-m", "remove active legacy engine; preserve original Git history")
        else:
            git(self.repo, "commit", "--allow-empty", "-m", "artificial later data checkpoint")
        # The public engine evolves independently of immutable private proofs.
        advance_engine(self.code)

    def native(self, role, payload, timeout=120, *, on_start):
        self.number += 1
        invocation = "model-" + f"{self.number:032x}"
        on_start(invocation)
        self.roles.append((role, self.phase))
        if role == "extract":
            source = payload["sources"][0]
            identity = {key: source["metadata"][key] for key in ("source_id", "source_version")}
            lines = (source["body"].splitlines() if "body" in source else
                     [line["text"] for fragment in source["fragments"] for line in fragment["lines"]])
            ref = {**identity, "locator": "L1-L3", "quote": lines[0]}
            text = "人工所选样本mean=15。" + ("仅限该人工表的两行样本。" if self.phase == "complete" else "样本范围需补充说明。")
            record = {"id": "artificial-completed", "type": "knowledge", "title": "人工样本",
                "source_kind": "user_statement", "sources": [ref], "domains": ["career"],
                "valid_from": None, "valid_to": None, "verification_status": "unverified",
                "lifecycle_status": "undetermined", "body": text}
            output = add_quality({"objects": [record], "wiki": {"id": "artificial-completed-topic", "title": "人工主题",
                "body": text, "sources": [ref]}, "reuse_refs": []}, payload["quality_context"])
            mean = next(stat for stat in payload["quality_context"]["statistics"] if stat["operation"] == "mean")
            output["quality"]["statistic_claims"] = [{"stat_id": mean["stat_id"], "record_id": record["id"],
                "value": "15", "body_evidence": "mean=15", "sources": [{**identity, "locator": "L2-L2", "quote": lines[1]}]}]
            if payload.get("output_contract") == "batch-v1":
                output["wikis"] = [{**output.pop("wiki"), "record_ids": [record["id"]]}]
        else:
            self.review_requests.append(deepcopy(payload))
            output = passed_review(payload["required_checks"], payload.get("skill_bundle"))
            if self.phase == "reject":
                output["verdict"] = "needs_revision"
                output["checks"][0]["result"] = "needs_revision"
        response = {"output": output, "model": "artificial-native", "invocation_id": invocation,
                    "elapsed_seconds": 0.01, "usage": {"input_tokens": 10, "output_tokens": 10}}
        if role == "extract" and self.phase == "complete":
            raw_request = render_model_request(role, payload)
            values = {"payload.json": json.dumps(payload, ensure_ascii=False).encode(),
                      "response.json": json.dumps(response, ensure_ascii=False).encode(),
                      "request-binding.json": json.dumps({**request_binding(role, payload), "role": role,
                                                          "operation_id": self.operation}).encode(),
                      "request.bin": raw_request}
            for name, raw in values.items():
                descriptor = os.open(self.cache / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
        return response

    def prepare(self):
        return prepare_attestation(self.git, self.failed["run_id"], model_call=self.adapter,
            source_paths=[self.saved["path"]], related_runs=[], original_payload=self.payload, response=self.response,
            original_native_request_binding=self.binding, external_receipts_sha256=self.receipts, cache_dir=self.cache)

    def verify(self, prepared=None, **changes):
        args = {"git": self.git, "run_id": self.failed["run_id"], "model_call": self.adapter,
                "source_paths": [self.saved["path"]], "related_runs": [], **(self.prepare() if prepared is None else prepared)}
        args.update(changes)
        return verify_completed_extraction(**args)

    def test_preparation_and_runtime_validation_are_zero_call_and_preserve_original_proof(self):
        before = self.git.head(), self.ledger_path.read_bytes(), self.number
        prepared = self.prepare()
        context = self.verify(prepared)
        self.assertEqual(context["response_provenance"], RESPONSE_PROVENANCE)
        self.assertEqual(context["original_resume_context"]["contract"], RESERVATION_CONTRACT)
        self.assertNotIn("unstarted", context["original_resume_context"]["contract"])
        self.assertEqual(context["original_payload"], self.payload)
        self.assertEqual(context["original_response"], self.response)
        self.assertNotEqual(prepared["attestation"]["validation_head"], prepared["attestation"]["head"])
        self.assertEqual((self.git.head(), self.ledger_path.read_bytes(), self.number), before)

    def test_changed_payload_response_scope_and_native_binding_are_rejected(self):
        prepared = self.prepare()
        for field in ("original_payload", "response", "source_paths"):
            changed = deepcopy(prepared)
            changes = {}
            if field == "original_payload":
                changed[field]["rules_version"] = "sha256-" + "0" * 64
            elif field == "response":
                changed[field]["output"]["wiki"]["title"] += "Changed"
            else:
                changes[field] = []
            with self.subTest(field=field), self.assertRaises(CaptureError):
                self.verify(changed, **changes)
        binding = deepcopy(self.binding)
        binding["bytes"] += 1
        with self.assertRaises(CaptureError):
            prepare_attestation(self.git, self.failed["run_id"], model_call=self.adapter,
                source_paths=[self.saved["path"]], original_payload=self.payload, response=self.response,
                original_native_request_binding=binding, external_receipts_sha256=self.receipts, cache_dir=self.cache)

    def test_changed_cache_and_symlink_are_rejected(self):
        prepared = self.prepare()
        path = self.cache / "response.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaises(CaptureError):
            self.verify(prepared)
        path.unlink()
        elsewhere = self.root / "elsewhere.json"
        elsewhere.write_bytes(original)
        path.symlink_to(elsewhere)
        with self.assertRaises(CaptureError):
            self.verify(prepared)

    def test_unknown_or_review_started_cannot_be_reused(self):
        prepared = self.prepare()
        with self.ledger._locked() as directory:
            state = self.ledger._read(directory)
            state["operations"][self.operation]["calls"]["extract"]["status"] = "unknown"
            state["operations"][self.operation]["calls"]["extract"]["error_code"] = "artificial_unknown"
            self.ledger._write(directory, state)
        with self.assertRaises(CaptureError):
            self.verify(prepared)

    def test_review_already_started_prevents_a_new_attestation(self):
        self.ledger.start(self.operation, "review", "0" * 64, "model-" + "e" * 32)
        with self.assertRaises(CaptureError):
            self.prepare()

    def test_another_budget_or_custom_service_is_rejected(self):
        prepared = self.prepare()
        other = self.root / "other-audit" / "calls.json"
        other.parent.mkdir(mode=0o700)
        BudgetLedger.create(other, None, 0, "other artificial authorization", repo=self.repo, mode="audit")
        for adapter in (BudgetedModelCall(BudgetLedger(other, repo=self.repo), self.operation),
                        BudgetedModelCall(self.ledger, self.operation, transport=lambda *args, **kwargs: None)):
            with self.subTest(adapter=type(adapter).__name__), self.assertRaises(CaptureError):
                self.verify(prepared, model_call=adapter)

    def test_validation_head_drift_and_falsely_stronger_provenance_are_rejected(self):
        prepared = self.prepare()
        changed = deepcopy(prepared)
        changed["attestation"]["prior_git_output_sha256"] = "0" * 64
        changed["expected_attestation_sha256"] = hashlib.sha256(canonical(changed["attestation"])).hexdigest()
        with self.assertRaises(CaptureError):
            self.verify(changed)
        (self.repo / "memory/queue-changed.md").write_text("Artificial concurrent state.\n")
        git(self.repo, "add", "memory/queue-changed.md")
        git(self.repo, "commit", "-m", "artificial concurrent memory mutation")
        with self.assertRaises(CaptureError):
            self.verify(prepared)

    def test_original_successful_response_recovers_with_review_only_and_survives_cache_deletion(self):
        prepared = self.prepare()
        roles = len(self.roles)
        protected = {path: self.git.read(self.git.head(), path) for path in (
            prepared["attestation"]["started"]["path"], prepared["attestation"]["revision_attempt"]["path"],
            prepared["attestation"]["failure"]["path"])}
        result = pipeline.revalidate_completed_extraction(self.repo, self.failed["run_id"],
            allowed_source_paths=[self.saved["path"]], model_call=self.adapter, related_runs=[],
            review_strategy="legacy-v1", **prepared)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["extract_calls"], 0)
        self.assertEqual(self.roles[roles:], [("review", "complete")])
        candidate = pipeline._load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        self.assertEqual(candidate["extraction_output"], self.response["output"])
        self.assertEqual(candidate["revision_revalidation"]["contract"], CONTRACT)
        request = self.review_requests[-1]
        self.assertFalse({"revision_context", "revision_revalidation", "revision_resume", "quality_revalidation_guard_ref"}
                         .intersection(request["candidate"]))
        self.assertEqual(candidate["review_request_contract"], pipeline.CURRENT_DRAFT_REVIEW_CONTRACT)
        self.assertNotIn("revision_context", request)
        self.assertEqual(candidate["revision_context"]["model_context"], self.payload["revision_context"])
        self.assertEqual(candidate["revision_revalidation"]["original_payload"], self.payload)
        self.assertEqual(request["candidate"]["binding"], candidate["binding"])
        self.assertEqual(request["candidate"]["extraction_output"]["quality"], self.response["output"]["quality"])
        self.assertEqual(json.loads((self.cache / "response.json").read_text()), self.response)
        self.assertEqual((self.cache / "request.bin").read_bytes(), render_model_request("extract", self.payload))
        self.assertTrue(verify_revalidated_extraction(self.git, candidate, historical=True))
        published = pipeline.publish(self.repo, result["run_id"])
        shutil.rmtree(self.cache.parent.parent)
        self.ledger_path.unlink()
        self.assertTrue(verify_revalidated_extraction(self.git, candidate, historical=True))
        proof = pipeline.verify_publication(self.repo, result["run_id"], historical=True)
        self.assertEqual(proof["files"], published["files"])
        required_history = set(protected) | {candidate["revision_context"]["stage"]["path"],
                                              candidate["revision_context"]["review"]["path"]}
        self.assertTrue(required_history <= {item["path"] for item in proof["authority_snapshots"]})
        for path, raw in protected.items():
            self.assertEqual((self.repo / path).read_bytes(), raw)

    def test_legacy_original_response_recovers_from_history_after_active_engine_removed(self):
        self.assertFalse((self.repo / "project").exists())
        self.assertFalse((self.repo / pipeline._run_path(self.failed["run_id"], "engine")).exists())
        self.test_original_successful_response_recovers_with_review_only_and_survives_cache_deletion()

    def test_deleted_engine_proof_cannot_fall_back_to_legacy_or_current_code(self):
        path = pipeline._run_path(self.failed["run_id"], "engine")
        git(self.repo, "rm", path)
        git(self.repo, "commit", "-m", "artificial removed provenance")
        before = self.number, self.ledger_path.read_bytes()
        with self.assertRaises(CaptureError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.code, "engine_provenance_changed")
        self.assertEqual((self.number, self.ledger_path.read_bytes()), before)

    def test_batch_original_views_survive_new_program_plan_and_historical_review(self):
        plan = plan_batches(self.repo, [self.saved["path"]], max_input_bytes=240000, purpose="history")
        projected = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=[self.saved["path"]])
        self.assertEqual(projected["source_views"], self.payload["sources"])
        self.assertEqual(projected["batch_context"]["source_ranges"], self.payload["batch_context"]["source_ranges"])
        self.assertNotEqual(projected["batch_context"], self.payload["batch_context"])
        prepared = self.prepare()
        before = len(self.roles)
        result = pipeline.revalidate_completed_extraction(self.repo, self.failed["run_id"],
            allowed_source_paths=[self.saved["path"]], model_call=self.adapter, related_runs=[],
            batch_context=projected["batch_context"], source_views=projected["source_views"],
            review_strategy="auto", **prepared)
        self.assertEqual((result["status"], result["extract_calls"]), ("passed", 0), result)
        self.assertEqual(self.roles[before:], [("review", "complete")])
        candidate = pipeline._load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        self.assertEqual(candidate["revision_revalidation"]["original_payload"], self.payload)
        self.assertEqual(candidate["batch_context"], projected["batch_context"])
        self.assertEqual(candidate["revision_revalidation"]["original_native_request_binding"], request_binding("extract", self.payload))
        self.assertTrue(verify_revalidated_extraction(self.git, candidate, historical=True))


if __name__ == "__main__":
    unittest.main()
