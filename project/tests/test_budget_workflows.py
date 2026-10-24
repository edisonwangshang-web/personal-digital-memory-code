"""Shared-budget publication and restart checks use disposable Git and fake calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_multisource_pipeline
import test_pipeline
import test_scheduler
from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall
from pdm.compatibility import inspect_memory_schema
from pdm.contracts import parse_markdown
from pdm.controls import record_control
from pdm.model_calls import ModelCallError
from pdm.pipeline import lookup_processed, process_sources, publish, verify_publication
from pdm.scheduler import _attempts, _require, run_daily
from pdm.transaction import PipelineGit


class BudgetFixtures:
    def adapter(self, limit=8, operation="isolated", transport=None):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-budget-workflow-")
        self.addCleanup(temporary.cleanup)
        ledger = BudgetLedger.create(Path(temporary.name) / "budget.json", limit, 0,
                                     "isolated-test-authorization", repo=self.repo)
        return ledger, BudgetedModelCall(ledger, operation, transport or self.transport)

    def blocked(self, code, action):
        with self.assertRaises(BudgetBlocked) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class BudgetPipelineTests(BudgetFixtures, unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    model = test_multisource_pipeline.MultisourcePipelineTests.model
    read = test_multisource_pipeline.MultisourcePipelineTests.read
    source = test_multisource_pipeline.MultisourcePipelineTests.source

    def transport(self, role, payload, timeout=120):
        return self.model(role, payload)

    def test_pair_budget_refusal_precedes_any_git_attempt_or_model_call(self):
        ledger, adapter = self.adapter(limit=1)
        head = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        error = self.blocked("budget_exhausted", lambda: process_sources(self.repo,
            [self.saved["path"]], related_runs=[], model_call=adapter))
        self.assertEqual(error.attempted_calls, 0)
        self.assertEqual(self.roles, [])
        self.assertEqual((ledger.snapshot()["used"], adapter.started_count), (0, 0))
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertFalse((self.repo / "memory/runs/processes").exists())

    def test_pair_publication_and_verified_reuse_keep_review_parent_and_old_bytes(self):
        ledger, adapter = self.adapter(limit=2)
        result = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=adapter)
        publication = publish(self.repo, result["run_id"])
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", publication["commit_sha"] + "^"), result["head"])
        self.assertEqual((ledger.snapshot()["used"], adapter.started_count), (2, 2))
        self.assertEqual(self.roles, ["extract", "review"])
        before = {path: (self.repo / path).read_bytes() for path in publication["files"]}
        head = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        checked = lookup_processed(self.repo, [self.saved["path"]])
        repeated = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=adapter)
        self.assertEqual(checked["run_id"], repeated["run_id"])
        self.assertEqual(repeated["status"], "already_processed")
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(before, {path: (self.repo / path).read_bytes() for path in before})
        self.assertEqual(ledger.snapshot()["used"], 2)

    def test_unknown_started_operation_cannot_be_replayed_with_a_new_random_run(self):
        def interrupted(role, payload, timeout=120):
            self.roles.append(role)
            raise KeyboardInterrupt("isolated unknown transport result")
        ledger, adapter = self.adapter(limit=2, transport=interrupted)
        with self.assertRaises(KeyboardInterrupt):
            process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=adapter)
        head = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        reopened = BudgetedModelCall(BudgetLedger(ledger.path, repo=self.repo), "isolated", interrupted)
        error = self.blocked("budget_call_already_started", lambda: process_sources(self.repo,
            [self.saved["path"]], related_runs=[], model_call=reopened))
        self.assertEqual((error.attempted_calls, error.started_count), (0, 1))
        self.assertEqual(self.roles, ["extract"])
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual((ledger.snapshot()["used"], ledger.snapshot()["started_calls"]), (2, 1))

    def test_local_transport_validation_failure_records_real_zero_or_one_start(self):
        for failed_role, count in (("extract", 0), ("review", 1)):
            with self.subTest(role=failed_role):
                ledger, adapter = self.adapter(limit=2, operation=failed_role)
                # Use the actual adapter's pre-Popen callback boundary; this
                # artificial transport validates before invoking on_start.
                adapter.transport = None
                def preflight(role, payload, timeout=120, on_start=None):
                    if role == failed_role:
                        raise ModelCallError("input_limit", "model-" + "e" * 32)
                    on_start("model-" + f"{self.number + 1:032x}")
                    return self.transport(role, payload, timeout)
                with patch("pdm.model_calls._call_transport", side_effect=preflight):
                    result = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=adapter)
                self.assertEqual(result["status"], "failed")
                failure = self.read(result["failure_path"])[1]
                self.assertEqual((failure["phase"], failure["attempted_calls"]), (failed_role, count))
                self.assertEqual((ledger.snapshot()["used"], adapter.started_count), (2, count))
                self.assertFalse((self.repo / "memory/records").exists())


class BudgetScheduleTests(BudgetFixtures, unittest.TestCase):
    setUp = test_scheduler.SchedulerTests.setUp
    capture = test_scheduler.SchedulerTests.capture
    config = test_scheduler.SchedulerTests.config
    configure = test_scheduler.SchedulerTests.configure
    trigger = test_scheduler.SchedulerTests.trigger
    model = test_scheduler.SchedulerTests.model
    fresh_source = test_scheduler.SchedulerTests.fresh_source
    state = test_scheduler.SchedulerTests.state

    def transport(self, role, payload, timeout=120):
        return self.model(role, payload, timeout)

    def tick(self, adapter, name="first"):
        return run_daily(self.repo, "isolated", self.trigger(name), model_call=adapter, clock=lambda: self.now)

    def test_exhausted_pool_is_bound_without_creating_an_unresolved_attempt(self):
        self.configure()
        ledger, adapter = self.adapter(limit=1)
        result = self.tick(adapter)
        self.assertEqual(result["status"], "budget_exhausted")
        state = self.state()
        self.assertEqual([event["kind"] for event in state["events"]], ["configured", "tick_started", "tick_finished"])
        self.assertEqual(_attempts(state), ({}, {}))
        self.assertEqual(self.roles, [])
        self.assertEqual(ledger.snapshot()["used"], 0)
        start = state["events"][1]
        self.assertEqual(parse_markdown((self.repo / start["path"]).read_bytes())[0]["schema_version"], "0.5")
        other, replacement = self.adapter(limit=8)
        head = test_scheduler.git(self.repo, "rev-parse", "HEAD")
        self.blocked("budget_pool_mismatch", lambda: self.tick(replacement, "other"))
        self.assertEqual(test_scheduler.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(other.snapshot()["used"], 0)
        reopened = BudgetedModelCall(BudgetLedger(ledger.path, repo=self.repo), "isolated", self.transport)
        self.assertEqual(self.tick(reopened)["status"], "skipped_duplicate")

    def test_budget_events_and_actual_call_counts_reject_older_schema_support(self):
        self.configure()
        ledger, adapter = self.adapter(limit=2)
        result = self.tick(adapter)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.roles, ["extract", "review"])
        reservations, results = _attempts(self.state())
        event = next(iter(reservations.values()))
        outcome = next(iter(results.values()))
        binding = event["data"]["budget_binding"]
        self.assertEqual(binding["pool_id"], ledger.snapshot()["pool_id"])
        self.assertEqual(binding["authorization_sha256"], ledger.snapshot()["authorization_sha256"])
        self.assertEqual((binding["reserved_calls"], binding["reuse_run_id"]), (2, None))
        self.assertEqual((outcome["data"]["observed_calls"], outcome["data"]["budget_started_calls"]), (2, 2))
        for saved in (event, outcome):
            self.assertEqual(parse_markdown((self.repo / saved["path"]).read_bytes())[0]["schema_version"], "0.5")
        self.assertEqual(ledger.snapshot()["used"], 2)
        with patch("pdm.compatibility.SUPPORTED_SCHEMA_VERSIONS", ("0.1", "0.2", "0.3", "0.4")):
            self.assertFalse(inspect_memory_schema(self.repo)["writable"])

    def test_manually_processed_input_uses_no_global_slots_when_scheduled(self):
        ledger, adapter = self.adapter(limit=2)
        initial = process_sources(self.repo, [self.source["path"]], related_runs=[], model_call=adapter)
        publish(self.repo, initial["run_id"])
        self.configure()
        result = self.tick(adapter.for_operation("daily"))
        outcome = result["outcomes"][0]
        self.assertEqual(outcome["status"], "already_processed")
        self.assertEqual((outcome["observed_calls"], outcome["budget_started_calls"]), (0, 0))
        self.assertEqual(outcome["budget_binding"]["reserved_calls"], 0)
        self.assertEqual(outcome["budget_binding"]["reuse_run_id"], initial["run_id"])
        self.assertEqual((ledger.snapshot()["used"], ledger.snapshot()["started_calls"]), (2, 2))
        self.assertEqual(len(self.roles), 2)
        self.assertEqual(result["reserved_calls"], 2)  # Legacy daily slots are separate.

    def test_reviewed_attempt_recovers_under_original_operation_without_new_calls(self):
        self.configure()
        ledger, adapter = self.adapter(limit=2)
        with patch("pdm.pipeline.publish", side_effect=RuntimeError("isolated interruption before publication")):
            with self.assertRaises(RuntimeError):
                self.tick(adapter)
        reopened = BudgetedModelCall(BudgetLedger(ledger.path, repo=self.repo), "different-resume-label", self.transport)
        result = self.tick(reopened)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["outcomes"][0]["recovered"])
        self.assertEqual(result["outcomes"][0]["budget_started_calls"], 2)
        self.assertEqual(len(self.roles), 2)
        self.assertEqual(ledger.snapshot()["used"], 2)
        verify_publication(self.repo, result["outcomes"][0]["run_id"])

    def test_reserved_unknown_attempt_never_automatically_starts_on_restart(self):
        self.configure()
        ledger, adapter = self.adapter(limit=2)
        with patch("pdm.pipeline.process_sources", side_effect=KeyboardInterrupt("isolated interruption after reservation")):
            with self.assertRaises(KeyboardInterrupt):
                self.tick(adapter)
        result = self.tick(BudgetedModelCall(BudgetLedger(ledger.path, repo=self.repo), "resume", self.transport))
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertEqual(self.roles, [])
        self.assertEqual((ledger.snapshot()["used"], ledger.snapshot()["started_calls"]), (2, 0))
        _, replacement = self.adapter(limit=8)
        self.blocked("budget_pool_mismatch", lambda: self.tick(replacement))

    def test_zero_call_reuse_recovers_after_reservation_without_spending_any_slot(self):
        ledger, adapter = self.adapter(limit=2)
        initial = process_sources(self.repo, [self.source["path"]], related_runs=[], model_call=adapter)
        publish(self.repo, initial["run_id"])
        self.configure()
        with patch("pdm.pipeline.process_sources", side_effect=KeyboardInterrupt("isolated interruption before reuse receipt")):
            with self.assertRaises(KeyboardInterrupt):
                self.tick(adapter.for_operation("daily"))
        recovered = self.tick(adapter.for_operation("resume"))
        self.assertEqual(recovered["outcomes"][0]["status"], "already_processed")
        self.assertEqual(recovered["outcomes"][0]["budget_started_calls"], 0)
        self.assertEqual((ledger.snapshot()["used"], len(self.roles)), (2, 2))

    def test_reuse_invalidated_after_preflight_cannot_upgrade_itself_to_live_calls(self):
        unrelated = self.capture("unrelated")
        ledger, adapter = self.adapter(limit=8)
        initial = process_sources(self.repo, [self.source["path"]], related_runs=[], model_call=adapter)
        original = publish(self.repo, initial["run_id"])
        before = {path: (self.repo / path).read_bytes() for path in original["files"]}
        self.configure()
        from pdm.scheduler import _event
        def changed(git, state, kind, data, at, callback=None):
            saved = _event(git, state, kind, data, at, callback)
            if kind == "attempt_reserved":
                record_control(self.repo, {"request_key": "isolated:preflight-race", "action": "delete",
                                           "source_paths": [unrelated["path"]]})
            return saved
        with patch("pdm.scheduler._event", side_effect=changed):
            self.blocked("budget_processing_mismatch", lambda: self.tick(adapter.for_operation("daily")))
        self.assertEqual((ledger.snapshot()["used"], len(self.roles)), (2, 2))
        self.assertEqual(before, {path: (self.repo / path).read_bytes() for path in before})
        reservation = next(iter(_attempts(self.state())[0].values()))
        self.assertFalse((self.repo / f"memory/runs/processes/{reservation['data']['run_id']}").exists())

    def test_removing_a_bound_operation_does_not_reset_schedule_budget_history(self):
        self.configure()
        ledger, adapter = self.adapter(limit=2)
        self.tick(adapter)
        raw = json.loads(ledger.path.read_text())
        raw["operations"] = {}
        ledger.path.write_text(json.dumps(raw))
        reopened = BudgetedModelCall(BudgetLedger(ledger.path, repo=self.repo), "new-label", self.transport)
        self.blocked("budget_operation_conflict", lambda: self.tick(reopened, "next"))
        self.assertEqual(len(self.roles), 2)

    def test_reopening_same_pool_with_changed_limit_cannot_expand_its_grant(self):
        self.configure()
        ledger, adapter = self.adapter(limit=2)
        self.tick(adapter)
        raw = json.loads(ledger.path.read_text())
        raw["limit"] = 8
        ledger.path.write_text(json.dumps(raw))
        reopened = BudgetedModelCall(BudgetLedger(ledger.path, repo=self.repo), "new-label", self.transport)
        self.blocked("budget_grant_mismatch", lambda: self.tick(reopened, "next"))
        self.assertEqual(len(self.roles), 2)
