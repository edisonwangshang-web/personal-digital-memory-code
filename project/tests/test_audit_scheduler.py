"""Synthetic continuous scheduling; no real models or platform timers."""
import json
from datetime import timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall
from pdm.contracts import CaptureError, parse_markdown
from pdm.model_calls import ModelCallError
from pdm.scheduler import (_append, _require, configure_schedule, drain_schedule, normalize_config,
                           plan_daily, run_daily, schedule_status)
from pdm.transaction import PipelineGit
import test_scheduler as fixtures


class AuditSchedulerTests(unittest.TestCase):
    setUp = fixtures.SchedulerTests.setUp
    capture = fixtures.SchedulerTests.capture
    config = fixtures.SchedulerTests.config
    trigger = fixtures.SchedulerTests.trigger
    model = fixtures.SchedulerTests.model
    fresh_source = fixtures.SchedulerTests.fresh_source

    def configure(self, **updates):
        # Exercise the preserved schema-0.6 engine, rather than silently
        # treating old event/attempt contracts as the new production bridge.
        value = self.config(execution_policy="continuous-v1", processing_engine="legacy-v1", **updates)
        result = configure_schedule(self.repo, value, now=self.now)
        self.ledger = BudgetLedger.create(Path(self.temp.name) / "audit.json", None, 24,
                                         "synthetic-new-ongoing-authorization", mode="audit")
        self.adapter = BudgetedModelCall(self.ledger, "stable-runner", self.model)
        return result

    def state(self):
        return _require(PipelineGit(self.repo), "isolated")

    def drain(self, name="discovery", **kwargs):
        return drain_schedule(self.repo, "isolated", self.trigger(name),
                              model_call=kwargs.pop("model_call", self.adapter),
                              clock=kwargs.pop("clock", lambda: self.now), **kwargs)

    def blocked(self, code, action, kind=CaptureError):
        with self.assertRaises(kind) as raised:
            action()
        self.assertEqual(code, raised.exception.code)

    def test_policy_is_opt_in_and_rejects_hidden_finite_caps_or_ambiguous_retry_count(self):
        original = normalize_config(self.config())
        self.assertNotIn("execution_policy", original)
        self.assertEqual(16, original["max_calls"])
        self.assertEqual(3, original["max_attempts"])
        current = normalize_config(self.config(execution_policy="continuous-v1"))
        self.assertIsNone(current["max_calls"])
        self.assertEqual(4, current["max_attempts"])
        for fields in ({"max_calls": 1_000_000}, {"max_calls": 16}, {"max_attempts": 3}, {"max_attempts": 5}):
            self.blocked("invalid_schedule_budget", lambda: normalize_config(
                self.config(execution_policy="continuous-v1", **fields)))
        self.blocked("invalid_schedule_policy", lambda: normalize_config(
            self.config(execution_policy="unknown")))

    def test_continuous_requires_audit_ledger_before_any_tick_or_model_call(self):
        self.configure()
        head = PipelineGit(self.repo).head()
        finite = BudgetLedger.create(Path(self.temp.name) / "finite.json", 100, 0, "synthetic-finite")
        self.blocked("audit_ledger_required", lambda: self.drain(model_call=BudgetedModelCall(
            finite, "finite", lambda *args, **kwargs: self.fail("sent"))), BudgetBlocked)
        self.blocked("budget_binding_required", lambda: self.drain(model_call=self.model), BudgetBlocked)
        self.assertEqual(head, PipelineGit(self.repo).head())
        self.assertEqual([], self.roles)

    def test_fourth_attempt_can_publish_fifth_does_not_execute_across_day_and_operation_name(self):
        self.configure(max_items=1)
        self.verdict = "needs_revision"
        for index in range(3):
            if index == 1:
                self.now += timedelta(days=1)
                self.adapter = BudgetedModelCall(BudgetLedger(self.ledger.path), "renamed-runner", self.model)
            result = self.drain(max_slices=1)
            self.assertEqual("continue", result["next_action"])
        self.verdict = "passed"
        result = self.drain(max_slices=1)
        self.assertEqual("idle", result["next_action"])
        self.assertEqual(8, len(self.roles))
        plan = plan_daily(self.repo, "isolated", self.now)
        self.assertEqual(4, plan["items"][0]["attempts"])
        self.assertEqual("completed", plan["items"][0]["status"])
        before = PipelineGit(self.repo).head()
        self.assertEqual("idle", self.drain()["next_action"])
        self.assertEqual(before, PipelineGit(self.repo).head())
        self.assertEqual(8, len(self.roles))

    def test_retry_exhaustion_is_durable_and_does_not_block_other_items_or_restart(self):
        other = self.capture("two")
        self.configure(max_items=1, allowed_source_ids=[self.source["source_id"], other["source_id"]])
        self.verdict = "needs_revision"
        result = self.drain(max_slices=8)
        self.assertEqual("attention_required", result["next_action"])
        self.assertEqual(16, len(self.roles))
        self.assertEqual({"attempt_limit"}, {item["status"] for item in result["remaining"]})
        self.now += timedelta(days=2)
        self.adapter = BudgetedModelCall(BudgetLedger(self.ledger.path), "other-operation", self.model)
        self.drain("new-daily-discovery")
        self.assertEqual(16, len(self.roles))
        self.assertEqual({4}, {item["attempts"] for item in plan_daily(self.repo, "isolated", self.now)["items"]})

    def test_same_day_continuations_exceed_old_daily_and_historical_counts(self):
        other = [self.capture("two"), self.capture("three")]
        self.configure(max_items=3, allowed_source_ids=[self.source["source_id"], *[item["source_id"] for item in other]])
        self.verdict = "needs_revision"
        result = self.drain(max_slices=4)
        self.assertEqual(4, len(result["slices"]))
        self.assertEqual(24, len(self.roles))
        self.assertEqual(48, self.ledger.snapshot()["used"])
        self.assertIsNone(self.ledger.snapshot()["remaining"])
        self.assertEqual("attention_required", result["next_action"])
        events = self.state()["events"]
        ticks = [event["data"] for event in events if event["kind"] == "tick_started"]
        self.assertEqual([0, 1, 2, 3], [tick["queue"]["slice_index"] for tick in ticks])
        for previous, current in zip(ticks, ticks[1:]):
            self.assertEqual(previous["tick_key"], current["queue"]["parent_tick_key"])
        for event in events:
            header, _ = parse_markdown((self.repo / event["path"]).read_bytes())
            self.assertEqual("0.6", header["schema_version"])

    def test_continuation_token_reopens_same_snapshot_and_does_not_rediscover_new_version(self):
        self.configure(max_items=1)
        self.verdict = "needs_revision"
        first = self.drain(max_slices=1)
        self.assertEqual("continue", first["next_action"])
        changed = self.capture("one", body="用户：这是新版本。\n尚未完成。\n")
        self.assertNotEqual(self.source["path"], changed["path"])
        reopened = BudgetedModelCall(BudgetLedger(self.ledger.path), "runner-reopened", self.model)
        second = self.drain(max_slices=1, continuation=first["continuation"], model_call=reopened)
        self.assertEqual(4, len(self.roles))
        self.assertNotIn(changed["path"], json.dumps(second["slices"]))
        self.assertEqual([self.source["path"]], [item["input_path"] for item in second["remaining"]])
        queue = [event["data"]["queue"] for event in self.state()["events"] if event["kind"] == "tick_started"][-1]
        self.assertEqual([self.source["path"]], queue["source_paths"])

    def test_duplicate_root_discovery_and_replayed_token_never_replay_model_role(self):
        self.configure(max_items=1)
        self.verdict = "needs_revision"
        first = self.drain(max_slices=1)
        token = first["continuation"]
        second = self.drain(max_slices=1, continuation=token)
        before = len(self.roles)
        replay = self.drain(max_slices=1, continuation=token)
        self.assertEqual(before, len(self.roles))
        self.assertEqual("skipped_duplicate", replay["slices"][0]["status"])
        self.assertEqual(second["continuation"], replay["continuation"])

    def test_unknown_call_and_expired_lease_are_not_blindly_retried(self):
        self.configure()
        sent = []
        def unknown(*args, **kwargs):
            sent.append(1)
            raise ModelCallError("timeout", "model-" + "1" * 32)
        adapter = BudgetedModelCall(self.ledger, "unknown", unknown)
        first = self.drain(model_call=adapter)
        self.assertEqual("reconcile", first["next_action"])
        self.assertEqual(1, len(sent))
        self.now += timedelta(days=1)
        again = self.drain(model_call=BudgetedModelCall(BudgetLedger(self.ledger.path), "new-name", unknown))
        self.assertEqual("reconcile", again["next_action"])
        self.assertEqual(1, len(sent))
        self.assertEqual(1, plan_daily(self.repo, "isolated", self.now)["items"][0]["attempts"])

    def test_window_end_returns_continuation_without_claiming_platform_wakeup(self):
        self.configure(max_items=1)
        self.verdict = "needs_revision"
        readings = iter([self.now, self.now, *[self.now + timedelta(seconds=20)] * 100])
        result = self.drain(clock=lambda: next(readings), window_seconds=10, max_slices=8)
        self.assertEqual(1, len(result["slices"]))
        self.assertEqual("continue", result["next_action"])
        self.assertIsNotNone(result["continuation"])
        self.assertEqual("not_provisioned", result["external_wakeup"])
        self.assertFalse(result["real_daily_verified"])

    def test_window_end_before_first_slice_keeps_unstarted_work_visible(self):
        self.configure()
        before = PipelineGit(self.repo).head()
        readings = iter([self.now, *[self.now + timedelta(seconds=20)] * 100])
        result = self.drain(clock=lambda: next(readings), window_seconds=10)
        self.assertEqual([], result["slices"])
        self.assertEqual("window_ended", result["status"])
        self.assertEqual("continue", result["next_action"])
        self.assertEqual([self.source["path"]], [item["input_path"] for item in result["remaining"]])
        self.assertIsNone(result["continuation"])
        self.assertEqual(before, PipelineGit(self.repo).head())
        self.assertEqual([], self.roles)

    def test_forged_or_cross_schedule_continuation_is_refused_without_mutation(self):
        self.configure(max_items=1)
        self.verdict = "needs_revision"
        token = self.drain(max_slices=1)["continuation"]
        before = PipelineGit(self.repo).head()
        for fields in ({"slice_index": 10}, {"parent_tick_key": "forged"}, {"schedule_id": "other"}):
            self.blocked("invalid_schedule_continuation", lambda: self.drain(continuation={**token, **fields}))
        self.assertEqual(before, PipelineGit(self.repo).head())
        self.assertEqual(2, len(self.roles))

    def test_another_audit_pool_cannot_reset_existing_schedule_authorization(self):
        self.configure(max_items=1)
        self.verdict = "needs_revision"
        self.drain(max_slices=1)
        other = BudgetLedger.create(Path(self.temp.name) / "other.json", None, 0,
                                    "another-synthetic-authorization", mode="audit")
        before = PipelineGit(self.repo).head()
        self.blocked("budget_pool_mismatch", lambda: self.drain(model_call=BudgetedModelCall(
            other, "another-pool", self.model)), BudgetBlocked)
        self.assertEqual(before, PipelineGit(self.repo).head())

    def test_stale_duplicate_slice_claim_is_rejected_before_append(self):
        self.configure(max_items=1)
        self.verdict = "needs_revision"
        self.drain(max_slices=1)
        state = self.state()
        tick = next(event for event in state["events"] if event["kind"] == "tick_started")
        adapter = PipelineGit(self.repo)
        before = adapter.head()
        self.blocked("unknown_schedule_state", lambda: _append(
            adapter, "isolated", "tick_started", tick["data"], self.now, before))
        self.assertEqual(before, adapter.head())
        self.assertEqual(2, len(self.roles))

    def test_daily_status_reports_finished_continuation_instead_of_first_partial_slice(self):
        other = self.capture("two")
        self.configure(max_items=1, allowed_source_ids=[self.source["source_id"], other["source_id"]])
        trigger = self.trigger("platform-discovery", kind="scheduled")
        self.now += timedelta(hours=1)
        result = drain_schedule(self.repo, "isolated", trigger, model_call=self.adapter,
                                clock=lambda: self.now, max_slices=2)
        self.assertEqual(["partial", "completed"], [item["status"] for item in result["slices"]])
        status = schedule_status(self.repo, "isolated", self.now)
        self.assertEqual("completed", status["status"])
        self.assertEqual("completed", status["expected_windows"][-1]["receipt_status"])
        self.assertEqual([], status["remaining"])


if __name__ == "__main__":
    unittest.main()
