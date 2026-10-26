"""Complete final Review after source-capacity fallback, artificial inputs only."""

from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_skill_quality_production
from engine_fixtures import isolated_engine
from pdm.capture import capture
from pdm.contracts import CaptureError, parse_markdown
from pdm.evidence import build_current_evidence
from pdm.context import plan_answered_pending
from pdm.pending import answer_pending, read_registry
from pdm.pipeline import _committed, _load, _read_reviewed, publish, verify_publication
from pdm.portable import prepare_reading_package, verify_reading_package
from pdm.production import prepare_production, run_production
from pdm.budget import BudgetedModelCall
from pdm.transaction import PipelineGit


class AssemblyProductionTests(unittest.TestCase):
    def setUp(self):
        test_skill_quality_production.SkillQualityProductionTests.setUp(self)
        isolated_engine(self)
    state = test_skill_quality_production.SkillQualityProductionTests.state
    attempts = test_skill_quality_production.SkillQualityProductionTests.attempts

    def transport(self, role, payload, timeout=120):
        response = test_skill_quality_production.SkillQualityProductionTests.transport(self, role, payload, timeout)
        if role == "review" and payload.get("review_scope", {}).get("kind") == "global" and getattr(self, "reject_global", False):
            response["output"]["verdict"] = "needs_revision"
            response["output"]["reasons"] = ["Artificial cross-source condition requires substantive repair."]
            response["output"]["checks"][0].update(result="needs_revision", reason="Artificial cross-source condition requires substantive repair.")
        if role == "review" and payload.get("review_scope", {}).get("kind") == "global" and getattr(self, "confirm_global", False):
            response["output"]["verdict"] = "needs_confirmation"
            response["output"]["reasons"] = ["Should both supplied artificial source scopes be retained?"]
            response["output"]["checks"][0].update(result="needs_confirmation", reason="Artificial user clarification is required.")
        return response

    def prepare(self):
        paths = [capture(self.repo, {"source_key": f"artificial:assembly:{i}", "source_type": "note",
                 "acquisition_status": "full_text", "body": f"Artificial supplied source {i}: " + "x" * 6000 + ".\n"})["path"]
                 for i in range(2)]
        prepared = prepare_production(self.repo, "assembly", paths, use_default_skills=True,
                                      max_input_bytes=10000, payload_overhead_bytes=0)
        self.assertEqual(prepared["coverage"]["batch_count"], 2)
        return paths

    def test_full_final_review_atomic_publication_reading_and_fresh_clone(self):
        paths = self.prepare()
        result = run_production(self.repo, "assembly", model_call=self.adapter, max_steps=4)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles.count("extract"), 2)
        final, review, _ = _read_reviewed(PipelineGit(self.repo), result["final_run"])
        self.assertNotIn("extraction", final)
        self.assertEqual(final["assembly"]["mode"], "verbatim_assembly_no_model_extract")
        self.assertEqual(parse_markdown((self.repo / f"memory/staging/{result['final_run']}.md").read_bytes())[0]["schema_version"], "0.14")
        final_requests = [payload for role, payload in self.payloads if "review_scope" in payload]
        self.assertEqual([payload["review_scope"]["kind"] for payload in final_requests], ["object"] * 4 + ["coverage"] * 2 + ["global"])
        self.assertEqual(review["output"]["verdict"], "passed")
        proof = verify_publication(self.repo, result["publication"]["run_id"], source_paths=paths)
        self.assertEqual(len(proof["files"]), 4)
        record = next(path for path in proof["files"] if path.startswith("memory/records/"))
        metadata, _ = parse_markdown((self.repo / record).read_bytes())
        self.assertIn(metadata["generated_by"]["run_id"], result["completed_batches"].values())
        self.assertEqual(metadata["review_ref"], f"memory/runs/processes/{result['final_run']}/review.md")
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
        selected = build_current_evidence(self.repo, paths, [proof["run_id"]], object_paths=[record], compact_sources=True)
        self.assertTrue(selected["coverage"]["object_selection_complete"])
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "reading.json"
            prepared = prepare_reading_package(self.repo, paths, ["What do the supplied artificial records say?"], [proof["run_id"]], output=package)
            self.assertEqual(verify_reading_package(package, expected_sha256=prepared["sha256"])["status"], "integrity_verified")
            clone = Path(directory) / "restore"
            subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(self.repo), str(clone)], check=True, capture_output=True)
            restored = verify_publication(clone, proof["run_id"], source_paths=paths)
            self.assertEqual(restored["files"], proof["files"])
            self.assertEqual(PipelineGit(clone).text("rev-list", "--parents", "HEAD"), PipelineGit(self.repo).text("rev-list", "--parents", "HEAD"))
        again = run_production(self.repo, "assembly", model_call=self.adapter)
        self.assertEqual(again["status"], "published")
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes()), before)
        with self.assertRaises(CaptureError) as caught:
            publish(self.repo, result["final_run"])
        self.assertEqual(caught.exception.code, "assembly_requires_group_publication")

    def test_global_rejection_is_preserved_and_does_not_repeat_verbatim_review(self):
        self.prepare()
        self.reject_global = True
        first = run_production(self.repo, "assembly", model_call=self.adapter, max_steps=3)
        self.assertEqual(first["status"], "needs_revision", first)
        before = self.number, self.ledger.path.read_bytes()
        second = run_production(self.repo, "assembly", model_call=self.adapter)
        self.assertEqual(second["status"], "blocked_context", second)
        self.assertEqual(second["last_result"]["error"], "assembly_revision_required")
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before)
        self.assertEqual(self.roles.count("extract"), 2)
        self.assertFalse(list((self.repo / "memory/records").glob("*.md")) if (self.repo / "memory/records").exists() else [])

    def test_unknown_final_review_resumes_without_replaying_any_call(self):
        self.prepare()
        ready = run_production(self.repo, "assembly", model_call=self.adapter, max_steps=2)
        self.assertEqual(ready["status"], "ready")
        def interrupted(role, payload, timeout=120):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run_production(self.repo, "assembly", model_call=BudgetedModelCall(self.ledger, "worker", interrupted), max_steps=1)
        before = self.ledger.path.read_bytes(), self.number
        resumed = run_production(self.repo, "assembly", model_call=self.adapter)
        self.assertEqual(resumed["status"], "unknown", resumed)
        self.assertEqual((self.ledger.path.read_bytes(), self.number), before)
        self.assertEqual(self.roles.count("extract"), 2)

    def test_non_global_capacity_is_checked_before_final_attempt_or_call(self):
        self.prepare()
        run_production(self.repo, "assembly", model_call=self.adapter, max_steps=2)
        before = self.ledger.path.read_bytes(), self.number, deepcopy(self.attempts())
        with patch("pdm.decomposed_review._input_bytes", return_value=256001):
            result = run_production(self.repo, "assembly", model_call=self.adapter)
        self.assertEqual(result["status"], "blocked_capacity", result)
        self.assertEqual(result["last_result"]["error"], "decomposed_input_limit")
        self.assertEqual((self.ledger.path.read_bytes(), self.number, self.attempts()), before)

    def test_final_review_cannot_reuse_any_input_extraction_or_review_invocation(self):
        self.prepare()
        ready = run_production(self.repo, "assembly", model_call=self.adapter, max_steps=2)
        drafts = list(ready["completed_batches"].values())
        last, last_review, _ = _read_reviewed(PipelineGit(self.repo), drafts[-1])
        calls = [last["extraction"]["invocation_id"], last_review["call"]["invocation_id"]]
        original = self.transport
        for identifier in calls:
            def reused(role, payload, timeout=120, identifier=identifier):
                response = original(role, payload, timeout)
                if "review_scope" in payload:
                    response["invocation_id"] = identifier
                return response
            result = run_production(self.repo, "assembly", model_call=BudgetedModelCall(self.ledger, "worker", reused), max_steps=1)
            self.assertEqual(result["status"], "partial", result)
            self.assertEqual(result["last_result"]["error"], "review_not_independent")
        self.assertEqual(self.roles.count("extract"), 2)
        self.assertFalse((self.repo / "memory/records").exists())

    @staticmethod
    def rename_record(output, new_id):
        old_id = output["objects"][0]["id"]
        output["objects"][0]["id"] = new_id
        for topic in output["wikis"]:
            topic["record_ids"] = [new_id if identifier == old_id else identifier for identifier in topic["record_ids"]]
        for disposition in output["quality"]["coverage_disposition"]:
            disposition["record_ids"] = [new_id if identifier == old_id else identifier for identifier in disposition["record_ids"]]
            for evidence in disposition["body_evidence"]:
                if evidence["record_id"] == old_id:
                    evidence["record_id"] = new_id

    def test_conflicting_drafts_stop_before_final_attempt_and_do_not_overwrite(self):
        self.prepare()
        original = self.transport
        def conflict(role, payload, timeout=120):
            response = original(role, payload, timeout)
            if role == "extract":
                self.rename_record(response["output"], "artificial-shared-object")
            return response
        adapter = BudgetedModelCall(self.ledger, "worker", conflict)
        run_production(self.repo, "assembly", model_call=adapter, max_steps=2)
        before = self.ledger.path.read_bytes(), self.number, deepcopy(self.attempts())
        result = run_production(self.repo, "assembly", model_call=adapter)
        self.assertEqual(result["status"], "blocked_context", result)
        self.assertEqual(result["last_result"]["error"], "assembly_object_conflict")
        self.assertEqual((self.ledger.path.read_bytes(), self.number, self.attempts()), before)

    def test_fragment_statistics_scope_is_not_fabricated_into_whole_source_quality(self):
        paths = [capture(self.repo, {"source_key": "artificial:assembly:fragment-scope", "source_type": "note",
                  "acquisition_status": "full_text", "body": "# Alpha\n" + "a" * 6000 + "\n# Beta\n" + "b" * 6000 + "\n"})["path"]]
        prepared = prepare_production(self.repo, "assembly", paths, use_default_skills=True,
                                      max_input_bytes=10000, payload_overhead_bytes=0)
        self.assertEqual(prepared["coverage"]["batch_count"], 2)
        original = self.transport
        def local_objects(role, payload, timeout=120):
            response = original(role, payload, timeout)
            if role == "extract":
                line = payload["sources"][0]["fragments"][0]["lines"][0]["line"]
                self.rename_record(response["output"], "artificial-section-" + str(line))
                response["output"]["wikis"][0]["id"] += "-" + str(line)
            return response
        adapter = BudgetedModelCall(self.ledger, "worker", local_objects)
        ready = run_production(self.repo, "assembly", model_call=adapter, max_steps=2)
        self.assertEqual(ready["status"], "ready", ready)
        before = self.ledger.path.read_bytes(), self.number, deepcopy(self.attempts())
        result = run_production(self.repo, "assembly", model_call=adapter)
        self.assertEqual(result["status"], "blocked_context", result)
        self.assertEqual(result["last_result"]["error"], "assembly_quality_scope_mismatch")
        self.assertEqual((self.ledger.path.read_bytes(), self.number, self.attempts()), before)

    def test_final_confirmation_is_registered_readable_and_usable_as_pending_origin(self):
        paths = self.prepare()
        self.confirm_global = True
        result = run_production(self.repo, "assembly", model_call=self.adapter, max_steps=3)
        self.assertEqual(result["status"], "needs_confirmation", result)
        candidate, review, _ = _read_reviewed(PipelineGit(self.repo), result["active_run_id"], allow_nonpassed=True)
        self.assertIn("assembly", candidate)
        self.assertNotIn("extraction", candidate)
        self.assertEqual(review["output"]["verdict"], "needs_confirmation")
        items = read_registry(self.repo)["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["pending_status"], "open")
        self.assertEqual(item["run_ids"], [result["active_run_id"]])
        before = self.number, self.ledger.path.read_bytes()
        evidence = build_current_evidence(self.repo, paths, [])
        self.assertEqual(evidence["pending"]["items"][0]["item_id"], item["item_id"])
        reply = capture(self.repo, {"source_key": "artificial:assembly:confirmation-reply", "source_type": "note",
                        "acquisition_status": "full_text", "body": "Artificial user reply: retain both supplied scopes.\n"})
        answer_pending(self.repo, item["item_id"], reply["path"])
        jobs = plan_answered_pending(self.repo, allowed_source_paths=paths + [reply["path"]],
                                    allowed_run_ids=[result["active_run_id"]], max_sources=256, max_runs=256)
        self.assertEqual(len(jobs["jobs"]), 1, jobs)
        self.assertEqual(jobs["jobs"][0]["parent_run_ids"], [result["active_run_id"]])
        self.assertEqual(jobs["jobs"][0]["question"], item["question"])
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
