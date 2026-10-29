"""Frozen Skill schedules, with disposable Git and no external model calls."""

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

import test_schedule_production
from pdm.budget import BudgetedModelCall
from pdm.contracts import CaptureError, parse_markdown
from pdm.scheduler import (configure_schedule, normalize_config, pause_schedule,
                           resume_schedule)
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class SkillQualityScheduleTests(unittest.TestCase):
    setUp = test_schedule_production.ScheduleProductionTests.setUp
    sources = test_schedule_production.ScheduleProductionTests.sources
    transport = test_schedule_production.ScheduleProductionTests.transport
    configure = test_schedule_production.ScheduleProductionTests.configure
    trigger = test_schedule_production.ScheduleProductionTests.trigger
    run_slice = test_schedule_production.ScheduleProductionTests.run_slice
    state = test_schedule_production.ScheduleProductionTests.state
    formal = test_schedule_production.ScheduleProductionTests.formal

    def test_frozen_rules_survive_reopen_and_pause_without_local_rule_loading(self):
        bundle = resolve_skill_refs()
        config = self.configure(count=1, skill_bundle=bundle)
        self.assertEqual(self.state()["config"]["skill_bundle"], bundle)
        # A restart need not read current local Skill files. The committed
        # snapshot is the authoritative request material for this schedule.
        config.pop("skill_bundle")
        before = PipelineGit(self.repo).head()
        with patch("pdm.skill_support.resolve_skill_refs",
                   side_effect=AssertionError("must not load a replacement")):
            result = configure_schedule(self.repo, config)
            self.assertEqual(result["status"], "already_configured")
            self.assertEqual(PipelineGit(self.repo).head(), before)
            pause_schedule(self.repo, "production", "人工暂停", now=self.now)
            resume_schedule(self.repo, "production", "人工核对通过", now=self.now)
            self.assertEqual(self.state()["config"]["skill_bundle"], bundle)
        for event in self.state()["events"]:
            header, _ = parse_markdown((self.repo / event["path"]).read_bytes())
            self.assertEqual(header["schema_version"], "0.20")
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)

    def test_invalid_bundle_blocks_before_configuration_is_written(self):
        config = self.configure(count=1)
        config["schedule_id"] = "invalid-quality"
        bundle = resolve_skill_refs()
        bundle["sha256"] = "sha256-" + "0" * 64
        config["skill_bundle"] = bundle
        before = PipelineGit(self.repo).head()
        with self.assertRaises(CaptureError) as caught:
            configure_schedule(self.repo, config)
        self.assertEqual(caught.exception.code, "invalid_schedule_skill_bundle")
        self.assertEqual(PipelineGit(self.repo).head(), before)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)

    def test_legacy_engines_cannot_silently_ignore_explicit_skill_rules(self):
        config = self.configure(count=1)
        bundle = resolve_skill_refs()
        for changes in ({"processing_engine": "legacy-v1"},
                        {"execution_policy": None, "processing_engine": "legacy-v1"}):
            with self.subTest(changes=changes):
                candidate = {**config, **changes, "skill_bundle": bundle}
                with self.assertRaises(CaptureError):
                    normalize_config(candidate)
        plain = normalize_config({**config, "processing_engine": "production-v1"})
        self.assertNotIn("skill_bundle", plain)

    def test_existing_schedule_cannot_be_upgraded_or_disabled_in_place(self):
        config = self.configure(count=1)
        before = PipelineGit(self.repo).head()
        result = configure_schedule(self.repo, config, use_default_skills=True)
        self.assertEqual(result["status"], "already_configured")
        self.assertNotIn("skill_bundle", self.state()["config"])
        with self.assertRaises(CaptureError) as caught:
            configure_schedule(self.repo, {**config, "skill_bundle": resolve_skill_refs()})
        self.assertEqual(caught.exception.code, "schedule_already_configured")
        self.assertEqual(PipelineGit(self.repo).head(), before)
        config = {**config, "schedule_id": "quality", "skill_bundle": resolve_skill_refs()}
        configure_schedule(self.repo, config, now=self.now)
        before = PipelineGit(self.repo).head()
        with self.assertRaises(CaptureError) as caught:
            configure_schedule(self.repo, {**config, "skill_bundle": None})
        self.assertEqual(caught.exception.code, "invalid_schedule_skill_bundle")
        self.assertEqual(PipelineGit(self.repo).head(), before)

    def test_cli_defaults_enable_rules_only_for_a_new_production_schedule(self):
        legacy = self.configure(count=1)
        new = {**legacy, "schedule_id": "new-quality"}
        result = configure_schedule(self.repo, new, now=self.now, use_default_skills=True)
        from pdm.scheduler import _require
        state = _require(PipelineGit(self.repo), "new-quality")
        self.assertEqual(state["config"]["skill_bundle"], resolve_skill_refs())
        header, _ = parse_markdown((self.repo / result["config_path"]).read_bytes())
        self.assertEqual(header["schema_version"], "0.20")
        self.assertNotIn("skill_bundle", self.state()["config"])

    def test_actual_schedule_work_passes_exact_snapshot_to_production(self):
        bundle = resolve_skill_refs()
        self.configure(count=1, skill_bundle=bundle)
        observed = []

        def stop_at_prepare(repo, queue_id, paths, **options):
            observed.append((queue_id, list(paths), deepcopy(options)))
            raise CaptureError("observed_prepare", "isolated prepare boundary")

        with patch("pdm.production.prepare_production", side_effect=stop_at_prepare):
            for name in ("first", "reconnected"):
                with self.assertRaises(CaptureError) as caught:
                    self.run_slice(name)
                self.assertEqual(caught.exception.code, "observed_prepare")
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0][2]["skill_bundle"], bundle)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)
        self.assertEqual(self.formal(), "")

    def test_quality_unknown_call_is_not_replayed_after_reconnect_or_next_day(self):
        self.configure(count=1, skill_bundle=resolve_skill_refs())
        seen = []

        def interrupted(role, payload, timeout=120):
            seen.append(deepcopy(payload))
            raise KeyboardInterrupt()

        self.adapter = BudgetedModelCall(self.ledger, "interrupted-quality", interrupted)
        with self.assertRaises(KeyboardInterrupt):
            self.run_slice()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["skill_bundle"], self.state()["config"]["skill_bundle"])
        self.adapter = BudgetedModelCall(self.ledger, "reconnected-quality", self.transport)
        self.now += timedelta(days=1)
        result = self.run_slice("next-day")
        self.assertEqual(result["next_action"], "reconcile", result)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)
        self.assertEqual(self.number, 0)
        self.assertEqual(self.formal(), "")


if __name__ == "__main__":
    unittest.main()
