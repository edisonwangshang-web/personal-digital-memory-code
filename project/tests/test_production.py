"""Durable batch queues with real Git/audit accounting and fake transports."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_pipeline
from pdm.batch_groups import verify_group_publication
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.model_calls import ModelCallError
from pdm.production import (_read, prepare_production, production_status,
                            run_production)
from pdm.transaction import PipelineGit


class ProductionTests(unittest.TestCase):
    def setUp(self):
        test_pipeline.PipelineTests.setUp(self)
        temporary = tempfile.TemporaryDirectory(prefix="pdm-production-audit-")
        self.addCleanup(temporary.cleanup)
        self.ledger = BudgetLedger.create(Path(temporary.name) / "audit.json", None, 24,
                                         "artificial-ongoing-production-grant", mode="audit")
        self.adapter = BudgetedModelCall(self.ledger, "worker", self.transport)
        self.payloads = []

    def sources(self, count):
        return [capture(self.repo, {"source_key": f"fixture:production:{index}", "source_type": "note",
                    "acquisition_status": "full_text", "body": f"Artificial fact {index}; not yet independently verified.\n"})["path"]
                for index in range(count)]

    def transport(self, role, payload, timeout=120):
        self.number += 1
        self.roles.append(role)
        self.payloads.append((role, deepcopy(payload)))
        if role == "extract":
            objects, wikis = [], []
            for source in payload["sources"]:
                line = source["fragments"][0]["lines"][0]
                suffix = source["metadata"]["source_id"][-10:]
                ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                       "locator": f"L{line['line']}-L{line['line']}", "quote": line["text"]}
                identifier = "record-" + suffix
                objects.append({"id": identifier, "type": "knowledge", "title": "Artificial fact",
                    "source_kind": "user_statement", "sources": [ref], "domains": ["learning"],
                    "valid_from": None, "valid_to": None, "verification_status": "unverified",
                    "lifecycle_status": "undetermined", "body": line["text"]})
                wikis.append({"id": "topic-" + suffix, "title": "Artificial topic", "body": line["text"],
                              "sources": [ref], "record_ids": [identifier]})
            output = {"objects": objects, "wikis": wikis}
        else:
            output = {"verdict": self.verdict, "reasons": ["Artificial independent review of the supplied batch."]}
        return {"output": output, "model": "artificial", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def prepare(self, name="queue", count=2):
        paths = self.sources(count)
        result = prepare_production(self.repo, name, paths)
        self.assertEqual(result["status"], "ready", result)
        return paths, result

    def state(self, name="queue"):
        return _read(PipelineGit(self.repo), f"memory/runs/processes/queues/{name}/state.md", "production_queue")

    def attempts(self):
        git = PipelineGit(self.repo)
        paths = git.text("ls-tree", "-r", "--name-only", "HEAD", "--", "memory/runs/processes/queue-attempts").splitlines()
        return [_read(git, path, "production_attempts") for path in paths]

    def assert_error(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def assert_no_formal(self):
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                                   "memory/records", "memory/topics"), "")

    def test_single_batch_publishes_once_and_repeating_published_queue_is_read_only(self):
        paths, prepared = self.prepare()
        self.assertEqual(prepared["coverage"]["batch_count"], 1)
        result = run_production(self.repo, "queue", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 2)
        publication = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=paths)
        self.assertEqual(len(publication["files"]), 4)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
        again = run_production(self.repo, "queue", model_call=self.adapter)
        self.assertEqual(again["publication"]["commit_sha"], publication["commit_sha"])
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes()), before)
        self.assertEqual(self.number, 2)

    def test_two_batches_need_real_final_extract_review_and_one_atomic_group(self):
        paths, prepared = self.prepare(count=9)
        self.assertEqual(prepared["coverage"]["batch_count"], 2)
        result = run_production(self.repo, "queue", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "review"] * 3)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 6)
        extracts = [payload for role, payload in self.payloads if role == "extract"]
        self.assertEqual(sorted(len(payload["sources"]) for payload in extracts[:2]), [1, 8])
        self.assertEqual(len(extracts[-1]["sources"]), 9)
        self.assertEqual(len(extracts[-1]["prior_candidates"]), 2)
        self.assertEqual(len(extracts[-1]["batch_context"]["parent_batch_ids"]), 2)
        publication = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=paths)
        self.assertEqual(len(publication["files"]), 18)
        git = PipelineGit(self.repo)
        changed = git.text("diff-tree", "--no-commit-id", "--name-only", "-r", publication["commit_sha"]).splitlines()
        self.assertEqual(set(changed), set(publication["files"]) | {publication["manifest_path"],
                         f"memory/runs/processes/grouped/{prepared['plan_id']}.md"})
        self.assertEqual(len(self.attempts()), 3)

    def test_step_slices_resume_without_repeating_completed_drafts_or_final_review(self):
        _, prepared = self.prepare(count=9)
        observed = []
        for _ in range(4):
            result = run_production(self.repo, "queue", model_call=self.adapter, max_steps=1)
            observed.append((result["status"], self.number))
        self.assertEqual(observed, [("ready", 2), ("ready", 4), ("ready", 6), ("published", 6)])
        self.assertEqual(len(result["completed_batches"]), prepared["coverage"]["batch_count"])
        self.assertEqual(self.ledger.snapshot()["reserved_calls"], 6)

    def test_four_attempts_total_survive_queue_rename_without_more_calls(self):
        paths, _ = self.prepare()
        self.verdict = "needs_revision"
        first = run_production(self.repo, "queue", model_call=self.adapter, max_steps=8)
        self.assertEqual(first["status"], "attempt_limit", first)
        self.assertEqual(self.number, 8)
        self.assertEqual(self.roles, ["extract", "review"] * 4)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 8)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 4)
        prepared = prepare_production(self.repo, "renamed", paths)
        self.assertEqual(prepared["status"], "ready")
        again = run_production(self.repo, "renamed", model_call=self.adapter, max_steps=8)
        self.assertEqual(again["status"], "attempt_limit")
        self.assertEqual(self.number, 8)
        self.assertEqual(len(self.attempts()), 1)
        self.assert_no_formal()

    def test_unknown_launch_is_not_replayed_after_restart_or_queue_rename(self):
        paths, _ = self.prepare()
        launched = []
        def interrupted(role, payload, timeout=120):
            launched.append(role)
            raise KeyboardInterrupt()
        adapter = BudgetedModelCall(self.ledger, "interrupted-worker", interrupted)
        with self.assertRaises(KeyboardInterrupt):
            run_production(self.repo, "queue", model_call=adapter)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)
        self.assertIsNotNone(self.state()["audit_pool"])
        first = run_production(self.repo, "queue", model_call=self.adapter)
        self.assertEqual(first["status"], "unknown", first)
        prepare_production(self.repo, "renamed", paths)
        second = run_production(self.repo, "renamed", model_call=self.adapter)
        self.assertEqual(second["status"], "unknown", second)
        self.assertEqual(launched, ["extract"])
        self.assertEqual(self.number, 0)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 1)
        self.assert_no_formal()

    def test_known_passed_review_after_state_write_crash_resumes_without_model_replay(self):
        self.prepare()
        from pdm import production
        original = production._save
        def interrupted(git, path, value, kind, head):
            if kind == "production_attempts" and value["attempts"][-1]["status"] == "passed":
                raise CaptureError("artificial_state_write_interruption", "Artificial interruption")
            return original(git, path, value, kind, head)
        with patch("pdm.production._save", side_effect=interrupted):
            self.assert_error("artificial_state_write_interruption", lambda: run_production(
                self.repo, "queue", model_call=self.adapter))
        self.assertEqual(self.number, 2)
        self.assertEqual(self.attempts()[0]["attempts"][0]["status"], "reserved")
        result = run_production(self.repo, "queue", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.number, 2)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 2)

    def test_known_needs_revision_after_state_write_crash_uses_next_bounded_attempt(self):
        self.prepare()
        self.verdict = "needs_revision"
        from pdm import production
        original = production._save
        def interrupted(git, path, value, kind, head):
            if kind == "production_attempts" and value["attempts"][-1]["status"] == "needs_revision":
                raise CaptureError("artificial_state_write_interruption", "Artificial interruption")
            return original(git, path, value, kind, head)
        with patch("pdm.production._save", side_effect=interrupted):
            self.assert_error("artificial_state_write_interruption", lambda: run_production(
                self.repo, "queue", model_call=self.adapter))
        self.assertEqual(self.number, 2)
        self.verdict = "passed"
        result = run_production(self.repo, "queue", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.number, 4)
        self.assertEqual([item["status"] for item in self.attempts()[0]["attempts"]], ["needs_revision", "passed"])

    def test_changed_plan_cannot_reuse_old_passed_context_or_reset_attempt_count(self):
        paths, _ = self.prepare()
        first = run_production(self.repo, "queue", model_call=self.adapter, max_steps=1)
        self.assertEqual(first["status"], "ready")
        original_run = first["final_run"]
        original_bytes = (self.repo / f"memory/staging/{original_run}.md").read_bytes()
        prepare_production(self.repo, "new-plan", paths)
        second = run_production(self.repo, "new-plan", model_call=self.adapter, max_steps=1)
        self.assertEqual(second["status"], "ready", second)
        self.assertNotEqual(second["final_run"], original_run)
        self.assertEqual(self.number, 4)
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual([item["attempt"] for item in self.attempts()[0]["attempts"]], [1, 2])
        self.assertEqual((self.repo / f"memory/staging/{original_run}.md").read_bytes(), original_bytes)

    def test_audit_identity_is_bound_before_model_and_cannot_switch_after_interruption(self):
        self.prepare()
        def interrupted(role, payload, timeout=120):
            self.assertIsNotNone(self.state()["audit_pool"])
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run_production(self.repo, "queue", model_call=BudgetedModelCall(self.ledger, "worker", interrupted))
        other = BudgetLedger.create(self.ledger.path.parent / "other.json", None, 0,
                                    "different-artificial-grant", mode="audit")
        self.assert_error("queue_audit_identity_changed", lambda: run_production(
            self.repo, "queue", model_call=BudgetedModelCall(other, "worker", self.transport)))
        self.assertEqual(self.number, 0)
        self.assertEqual(other.snapshot()["started_calls"], 0)

    def test_reentrant_worker_cannot_claim_same_queue_or_overwrite_audit_identity(self):
        self.prepare()
        other = BudgetLedger.create(self.ledger.path.parent / "competing.json", None, 0,
                                    "competing-artificial-grant", mode="audit")
        competitor = BudgetedModelCall(other, "competitor", self.transport)
        attempted = []
        def owner(role, payload, timeout=120):
            if role == "extract":
                git = PipelineGit(self.repo)
                before = git.head(), (self.repo / "memory/runs/processes/queues/queue/state.md").read_bytes()
                for adapter in (self.adapter, competitor):
                    self.assert_error("production_locked", lambda adapter=adapter: run_production(
                        self.repo, "queue", model_call=adapter))
                    attempted.append(adapter.ledger.snapshot()["pool_id"])
                self.assertEqual((git.head(), (self.repo / "memory/runs/processes/queues/queue/state.md").read_bytes()), before)
                self.assertEqual(other.snapshot()["started_calls"], 0)
            return self.transport(role, payload, timeout=timeout)
        result = run_production(self.repo, "queue", model_call=BudgetedModelCall(self.ledger, "owner", owner))
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(len(attempted), 2)
        self.assertEqual(self.number, 2)
        self.assertEqual(self.state()["audit_pool"]["pool_id"], self.ledger.snapshot()["pool_id"])

    def test_review_prelaunch_failure_reuses_extraction_and_only_starts_review_on_next_step(self):
        self.prepare()
        prelaunch = []
        def transport(role, payload, timeout=120, on_start=None):
            if role == "review" and not prelaunch:
                prelaunch.append(role)
                raise ModelCallError("codex_not_found", "model-" + "f" * 32)
            on_start("model-" + f"{self.number + 1:032x}")
            return self.transport(role, payload, timeout=timeout)
        adapter = BudgetedModelCall(self.ledger, "prelaunch-worker")
        with patch("pdm.model_calls._call_transport", side_effect=transport):
            first = run_production(self.repo, "queue", model_call=adapter, max_steps=1)
            self.assertEqual(first["status"], "failed", first)
            self.assertEqual(self.roles, ["extract"])
            previous = self.attempts()[0]["attempts"][0]
            self.assertNotIn("review", self.ledger.operation(previous["operation_id"])["calls"])
            old_stage = self.repo / f"memory/staging/{previous['run_id']}.md"
            original = old_stage.read_bytes()
            result = run_production(self.repo, "queue", model_call=adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 2)
        self.assertEqual(old_stage.read_bytes(), original)
        attempts = self.attempts()[0]["attempts"]
        self.assertEqual([item["attempt"] for item in attempts], [1, 2])
        self.assertEqual([item["status"] for item in attempts], ["failed", "passed"])
        self.assertEqual(set(self.ledger.operation(attempts[1]["operation_id"])["calls"]), {"review"})
        publication = verify_group_publication(self.repo, result["publication"]["run_id"])
        proof_paths = {item["path"] for item in publication["authority_snapshots"]}
        self.assertIn(f"memory/staging/{previous['run_id']}.md", proof_paths)
        self.assertIn(f"memory/runs/processes/{previous['run_id']}/failure.md", proof_paths)
        # Keep the new candidate/Review unchanged: changing the referenced old
        # bytes alone must invalidate its saved extraction-reuse pointer.
        old_stage.write_bytes(original + b"\n")
        git = PipelineGit(self.repo)
        git.run("add", "--", str(old_stage.relative_to(self.repo)))
        git.run("commit", "-m", "artificial reused extraction bytes changed")
        self.assert_error("extraction_not_reusable", lambda: verify_group_publication(
            self.repo, result["publication"]["run_id"]))


if __name__ == "__main__":
    unittest.main()
