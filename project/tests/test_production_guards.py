"""Production execution gates use real audit ledgers and artificial models."""

import json
from pathlib import Path
import tempfile
import unittest

import test_production
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.contracts import CaptureError, canonical, markdown, parse_markdown
from pdm.production import production_status, run_production
from pdm.transaction import PipelineGit


class ProductionGuardTests(unittest.TestCase):
    setUp = test_production.ProductionTests.setUp
    sources = test_production.ProductionTests.sources
    prepare = test_production.ProductionTests.prepare
    transport = test_production.ProductionTests.transport
    state = test_production.ProductionTests.state
    attempts = test_production.ProductionTests.attempts
    assert_error = test_production.ProductionTests.assert_error
    assert_no_formal = test_production.ProductionTests.assert_no_formal

    def test_before_call_guard_precedes_first_audit_start(self):
        self.prepare()
        seen = []
        def guard(repo, phase, details):
            if phase == "before_call":
                seen.append(details)
                self.assertEqual(self.ledger.snapshot()["started_calls"], 0)
                self.assertEqual(details["role"], "extract")
                self.assertEqual(self.ledger.operation(details["operation_id"])["calls"], {})
                raise CaptureError("remote_sync_required", "Artificial missing remote acknowledgement")
        result = run_production(self.repo, "queue", model_call=self.adapter, max_steps=1, execution_guard=guard)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.roles, [])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)
        self.assert_no_formal()

    def test_review_guard_failure_preserves_extraction_and_resumes_only_review(self):
        self.prepare()
        rejected = []
        def guard(repo, phase, details):
            if phase == "before_call" and details["role"] == "review" and not rejected:
                operation = self.ledger.operation(details["operation_id"])
                self.assertEqual(set(operation["calls"]), {"extract"})
                rejected.append(details["run_id"])
                raise CaptureError("publication_paused", "Artificial pause before Review starts")
        first = run_production(self.repo, "queue", model_call=self.adapter, max_steps=1, execution_guard=guard)
        self.assertEqual(first["status"], "failed")
        self.assertEqual(self.roles, ["extract"])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)
        original = (self.repo / f"memory/staging/{rejected[0]}.md").read_bytes()
        result = run_production(self.repo, "queue", model_call=self.adapter, execution_guard=guard)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 2)
        self.assertEqual((self.repo / f"memory/staging/{rejected[0]}.md").read_bytes(), original)
        self.assertEqual([attempt["status"] for attempt in self.attempts()[0]["attempts"]], ["failed", "passed"])

    def test_before_publish_guard_blocks_formal_commit_without_repeating_review(self):
        self.prepare()
        blocked = []
        def guard(repo, phase, details):
            if phase == "before_publish" and not blocked:
                blocked.append(details["run_id"])
                raise CaptureError("publication_paused", "Artificial pause after independent Review")
        self.assert_error("publication_paused", lambda: run_production(
            self.repo, "queue", model_call=self.adapter, execution_guard=guard))
        self.assertEqual(self.roles, ["extract", "review"])
        self.assert_no_formal()
        self.assertEqual(self.state()["final_run"], blocked[0])
        result = run_production(self.repo, "queue", model_call=self.adapter, execution_guard=guard)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "review"])

    def test_checkpoint_acknowledgement_loss_recovers_without_model_replay(self):
        self.prepare()
        lost = []
        def guard(repo, phase, details):
            if phase == "after_checkpoint" and details["run_id"] and not lost:
                lost.append(details["run_id"])
                raise CaptureError("remote_unconfirmed", "Artificial acknowledgement lost after durable state")
        self.assert_error("remote_unconfirmed", lambda: run_production(
            self.repo, "queue", model_call=self.adapter, execution_guard=guard))
        self.assertEqual(self.state()["final_run"], lost[0])
        self.assertEqual(self.roles, ["extract", "review"])
        result = run_production(self.repo, "queue", model_call=self.adapter, execution_guard=guard)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "review"])

    def test_published_queue_only_verifies_and_confirms_existing_state(self):
        self.prepare()
        published = run_production(self.repo, "queue", model_call=self.adapter)
        path = self.repo / "memory/runs/processes/queues/queue/state.md"
        before = PipelineGit(self.repo).head(), path.read_bytes(), self.ledger.path.read_bytes()
        phases = []
        def guard(repo, phase, details):
            phases.append((phase, details["status"]))
        result = run_production(self.repo, "queue", model_call=self.adapter, execution_guard=guard)
        self.assertEqual(result["publication"], published["publication"])
        self.assertEqual(phases, [("after_checkpoint", "published")])
        self.assertEqual((PipelineGit(self.repo).head(), path.read_bytes(), self.ledger.path.read_bytes()), before)
        self.assertEqual(self.roles, ["extract", "review"])

    def test_published_queue_rejects_replacement_audit_pool_before_confirmation(self):
        self.prepare()
        run_production(self.repo, "queue", model_call=self.adapter)
        temporary = tempfile.TemporaryDirectory(prefix="pdm-other-guard-audit-")
        self.addCleanup(temporary.cleanup)
        other = BudgetLedger.create(Path(temporary.name) / "audit.json", None, 0,
                                    "artificial replacement authorization", mode="audit")
        confirmations = []
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), other.path.read_bytes()
        self.assert_error("queue_audit_identity_changed", lambda: run_production(
            self.repo, "queue", model_call=BudgetedModelCall(other, "replacement", self.transport),
            execution_guard=lambda *args: confirmations.append(args)))
        self.assertEqual(confirmations, [])
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), other.path.read_bytes()), before)
        self.assertEqual(self.roles, ["extract", "review"])

    def legacy_queue(self, *, old_program=False):
        git = PipelineGit(self.repo)
        path = "memory/runs/processes/queues/queue/state.md"
        before = git._existing(path)
        _, body = parse_markdown(before)
        value = json.loads(body)
        value.pop("last_result", None)
        value.pop("active_run_id", None)
        if old_program:
            value["plan"]["snapshot"]["versions"]["program_sha256"] = "sha256-" + "0" * 64
        with git.lock():
            git.write_changes({path: markdown({"schema_version": "0.6", "kind": "production_queue"},
                                               canonical(value).decode() + "\n")},
                              {path: before}, git.head(), "Artificial historical queue format")
        return path

    def test_legacy_published_queue_read_and_confirmation_do_not_rewrite_schema(self):
        self.prepare()
        run_production(self.repo, "queue", model_call=self.adapter)
        path = self.legacy_queue()
        before = PipelineGit(self.repo).head(), (self.repo / path).read_bytes(), self.ledger.path.read_bytes()
        self.assertEqual(production_status(self.repo, "queue")["status"], "published")
        self.assertEqual(run_production(self.repo, "queue", model_call=self.adapter)["status"], "published")
        self.assertEqual((PipelineGit(self.repo).head(), (self.repo / path).read_bytes(), self.ledger.path.read_bytes()), before)
        self.assertEqual(parse_markdown((self.repo / path).read_bytes())[0]["schema_version"], "0.6")

    def test_old_program_legacy_queue_cannot_silently_restart_or_upgrade(self):
        self.prepare()
        path = self.legacy_queue(old_program=True)
        before = PipelineGit(self.repo).head(), (self.repo / path).read_bytes(), self.ledger.path.read_bytes()
        self.assertEqual(production_status(self.repo, "queue")["status"], "ready")
        self.assert_error("batch_snapshot_changed", lambda: run_production(self.repo, "queue", model_call=self.adapter))
        self.assertEqual((PipelineGit(self.repo).head(), (self.repo / path).read_bytes(), self.ledger.path.read_bytes()), before)
        self.assertEqual(self.roles, [])


if __name__ == "__main__":
    unittest.main()
