"""Durable trigger discovery is evidence of unfinished local processing.

Disposable repositories and fake transports only; these tests never claim an
actual platform timer ran or transfer personal material to a model.
"""

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import test_schedule_production as fixtures
from pdm.scheduler import run_daily, schedule_status
from pdm.transaction import PipelineGit


class ScheduleDiscoveryStatusTests(unittest.TestCase):
    setUp = fixtures.ScheduleProductionTests.setUp
    sources = fixtures.ScheduleProductionTests.sources
    transport = fixtures.ScheduleProductionTests.transport
    configure = fixtures.ScheduleProductionTests.configure
    state = fixtures.ScheduleProductionTests.state

    def prepared(self, *, kind="scheduled"):
        self.configure(count=1)
        self.now = datetime(2026, 10, 19, 14, 1, tzinfo=timezone.utc)
        return {"kind": kind, "trigger_id": "artificial-platform-receipt",
                "scheduled_for": "2026-10-19T14:00:00+00:00"}

    def interrupt_after_discovery(self, trigger):
        with patch("pdm.schedule_production._resolved_waits", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run_daily(self.repo, "production", trigger, model_call=self.adapter,
                          clock=lambda: self.now)
        self.assertEqual([event["kind"] for event in self.state()["events"]],
                         ["configured", "production_discovered"])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)
        self.assertEqual(self.number, 0)

    def test_discovered_without_work_is_incomplete_even_after_grace_or_empty_platform_query(self):
        trigger = self.prepared()
        self.interrupt_after_discovery(trigger)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
        self.now = datetime(2026, 10, 19, 15, tzinfo=timezone.utc)
        observation = {"status": "available", "window_start": trigger["scheduled_for"],
                       "window_end": self.now.isoformat(), "trigger_ids": []}
        for platform in (None, observation):
            with self.subTest(platform_available=platform is not None):
                result = schedule_status(self.repo, "production", now=self.now,
                                         platform_observation=platform)
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["expected_windows"], [{
                    "scheduled_for": trigger["scheduled_for"], "receipt_status": "incomplete"}])
                self.assertIsNone(result["latest_success_at"])
                self.assertEqual(result["scheduled_environment"], "unverified")
                self.assertEqual(result["external_trigger_status"], "unverified")
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes()), before)

    def test_same_root_resume_finishes_original_discovery_and_does_not_replay_calls(self):
        trigger = self.prepared()
        self.interrupt_after_discovery(trigger)
        resumed = run_daily(self.repo, "production", trigger, model_call=self.adapter,
                            clock=lambda: self.now)
        self.assertEqual((resumed["status"], resumed["next_action"]), ("completed", "idle"))
        self.assertEqual(self.roles, ["extract", "review"])
        events = self.state()["events"]
        self.assertEqual(sum(event["kind"] == "production_discovered" for event in events), 1)
        self.assertEqual(sum(event["kind"] == "production_tick_finished" for event in events), 1)
        status = schedule_status(self.repo, "production", now=self.now)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["external_trigger_status"], "unverified")
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number
        run_daily(self.repo, "production", trigger, model_call=self.adapter,
                  clock=lambda: self.now)
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number), before)

    def test_manual_discovery_does_not_prove_scheduled_trigger_or_success(self):
        trigger = self.prepared(kind="manual")
        self.interrupt_after_discovery(trigger)
        result = schedule_status(self.repo, "production",
                                 now=datetime(2026, 10, 19, 15, tzinfo=timezone.utc))
        self.assertEqual(result["status"], "trigger_unknown")
        self.assertEqual(result["expected_windows"][0]["receipt_status"], "missing_receipt")
        self.assertIsNone(result["latest_success_at"])
        self.assertEqual(result["external_trigger_status"], "unverified")


if __name__ == "__main__":
    unittest.main()
