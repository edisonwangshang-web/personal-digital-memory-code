"""Actual schedule-to-production orchestration with disposable Git and fake models."""

from datetime import datetime, timedelta, timezone
import hashlib
import subprocess
import unittest
from unittest.mock import patch

import test_production
import test_scheduler
from git_fixtures import configure_test_repository
from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.batching import plan_batches, project_batch_sources
from pdm.contracts import CaptureError, parse_markdown
from pdm.pipeline import process_sources, publish
from pdm.pending import answer_pending, read_registry
from pdm.scheduler import (_append, _require, configure_schedule, drain_schedule, normalize_config,
                           pause_schedule, plan_daily, run_daily)
from pdm.runtime import bind_environment
from pdm.transaction import PipelineGit


class ScheduleProductionTests(unittest.TestCase):
    setUp = test_production.ProductionTests.setUp
    sources = test_production.ProductionTests.sources
    transport = test_production.ProductionTests.transport

    def configure(self, count=2, **updates):
        self.paths = self.sources(count)
        self.now = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)
        config = {"schedule_id": "production", "daily_time": "22:00", "allowed_source_ids": [],
                  "allowed_source_paths": self.paths, "execution_policy": "continuous-v1", "remote_mode": "local_only", **updates}
        configure_schedule(self.repo, config, now=self.now)
        return config

    def trigger(self, name="first"):
        return {"kind": "test", "trigger_id": name, "scheduled_for": self.now.isoformat()}

    def run_slice(self, name="first", **updates):
        return run_daily(self.repo, "production", self.trigger(name), model_call=self.adapter,
                         clock=lambda: self.now, **updates)

    def state(self):
        return _require(PipelineGit(self.repo), "production")

    def formal(self):
        return PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--", "memory/records", "memory/topics")

    def install_remote(self, *, bind=True):
        remote = self.repo.parent / (self.repo.name + "-schedule-remote.git")
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
        configure_test_repository(remote)
        git = PipelineGit(self.repo)
        git.run("branch", "-m", "dev")
        git.run("remote", "add", "origin", str(remote))
        if bind:
            bind_environment(self.repo, {"schema_version": "1", "environment": "dev", "repo_root": str(self.repo),
                "remote_name": "origin", "remote_identity": str(remote), "branch": "dev", "ref": "refs/heads/dev",
                "state_root": str(self.ledger.path.parent), "credential_refs": {"git": "artificial-git", "model": "artificial-model"}})
        self.addCleanup(lambda: __import__("shutil").rmtree(remote, ignore_errors=True))
        return remote

    def test_default_continuous_really_runs_nine_source_batches_and_atomic_group(self):
        config = self.configure(count=9)
        self.assertEqual(self.state()["config"]["processing_engine"], "production-v1")
        header, _ = parse_markdown((self.repo / self.state()["events"][0]["path"]).read_bytes())
        self.assertEqual(header["schema_version"], "0.7")
        result = self.run_slice()
        self.assertEqual((result["status"], result["next_action"]), ("completed", "idle"), result)
        self.assertEqual(self.roles, ["extract", "review"] * 3)
        publication = result["processing"]["publication"]
        self.assertEqual(len(publication["files"]), 18)
        changed = PipelineGit(self.repo).text("diff-tree", "--no-commit-id", "--name-only", "-r", publication["commit_sha"]).splitlines()
        self.assertTrue(set(publication["files"]) <= set(changed))
        self.assertEqual({item["status"] for item in plan_daily(self.repo, "production")["items"]}, {"completed"})
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number
        self.assertEqual(configure_schedule(self.repo, config)["status"], "already_configured")
        self.run_slice()
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number), before)
        self.assertFalse(result["real_daily_verified"])

    def test_slices_keep_one_queue_and_do_not_replay_drafts(self):
        self.configure(count=9, max_items=1)
        result, queues, counts = None, set(), []
        for _ in range(4):
            result = self.run_slice(continuation=result.get("continuation") if result else None)
            queues.add(result["queue_id"])
            counts.append(self.number)
        self.assertEqual(counts, [2, 4, 6, 6])
        self.assertEqual(len(queues), 1)
        self.assertEqual(result["next_action"], "idle")

    def test_existing_continuous_configuration_without_engine_stays_legacy_and_idempotent(self):
        self.now = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)
        paths = self.sources(1)
        config = normalize_config({"schedule_id": "production", "daily_time": "22:00", "allowed_source_ids": [],
                  "allowed_source_paths": paths, "execution_policy": "continuous-v1", "remote_mode": "local_only"})
        _append(PipelineGit(self.repo), "production", "configured", config, self.now)
        before = PipelineGit(self.repo).head()
        result = configure_schedule(self.repo, config)
        self.assertEqual(result["status"], "already_configured")
        self.assertNotIn("processing_engine", self.state()["config"])
        self.assertEqual(PipelineGit(self.repo).head(), before)

    def test_unknown_work_cannot_replay_under_another_trigger_or_day(self):
        self.configure(count=1)
        def interrupted(role, payload, timeout=120):
            raise KeyboardInterrupt()
        self.adapter = BudgetedModelCall(self.ledger, "interrupted", interrupted)
        with self.assertRaises(KeyboardInterrupt):
            self.run_slice()
        self.adapter = BudgetedModelCall(self.ledger, "another-worker", self.transport)
        self.now += timedelta(days=1)
        result = self.run_slice("next-day")
        self.assertEqual(result["next_action"], "reconcile", result)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)
        self.assertEqual(self.number, 0)
        self.assertEqual(self.formal(), "")

    def test_pool_remains_bound_after_completed_work(self):
        self.configure(count=1)
        self.run_slice()
        other = BudgetLedger.create(self.ledger.path.parent / "other-pool.json", None, 0,
                                    "other-artificial-authorization", mode="audit")
        self.adapter = BudgetedModelCall(other, "other-worker", self.transport)
        before = PipelineGit(self.repo).head(), self.number
        with self.assertRaises(BudgetBlocked) as caught:
            self.run_slice("next-root")
        self.assertEqual(caught.exception.code, "budget_pool_mismatch")
        self.assertEqual((PipelineGit(self.repo).head(), self.number), before)
        self.assertEqual(other.snapshot()["started_calls"], 0)

    def test_required_noop_remote_callback_cannot_authorize_a_model_call(self):
        self.install_remote()
        self.configure(count=1, remote_mode="required")
        with self.assertRaises(CaptureError):
            self.run_slice(remote_confirm=lambda *args: None)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)
        self.assertEqual(self.formal(), "")

    def test_required_mode_confirms_group_receipt_and_queue_from_actual_remote(self):
        self.install_remote()
        self.configure(count=2, remote_mode="required")
        result = self.run_slice()
        self.assertEqual(result["next_action"], "idle", result)
        git = PipelineGit(self.repo)
        git.run("fetch", "--no-tags", "origin", "refs/heads/dev")
        remote = git.text("rev-parse", "FETCH_HEAD")
        for path in [result["processing"]["publication"]["manifest_path"],
                     result["processing"]["publication"]["receipt_path"],
                     f"memory/runs/processes/queues/{result['queue_id']}/state.md"]:
            self.assertEqual(git.read(remote, path), git.read(git.head(), path))

    def test_ack_loss_after_group_publish_reopens_with_zero_new_calls(self):
        self.configure(count=2)
        from pdm import scheduler
        original = scheduler._confirm_publication
        def missing_ack(*args, **kwargs):
            raise CaptureError("artificial_ack_loss", "artificial")
        with patch("pdm.scheduler._confirm_publication", side_effect=missing_ack):
            with self.assertRaises(CaptureError) as caught:
                self.run_slice()
        self.assertEqual(caught.exception.code, "artificial_ack_loss")
        calls = self.number
        result = self.run_slice()
        self.assertEqual(result["next_action"], "idle", result)
        self.assertEqual(self.number, calls)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 2)

    def test_paused_schedule_blocks_before_model_start(self):
        self.configure(count=1)
        pause_schedule(self.repo, "production", "artificial pause", now=self.now)
        self.assertEqual(self.run_slice()["status"], "skipped_paused")
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)

    def test_eight_already_published_runs_and_ninth_source_reuse_old_objects(self):
        paths = self.sources(9)
        old_runs, old_bytes = [], {}
        for index, path in enumerate(paths[:8]):
            plan = plan_batches(self.repo, [path], max_input_bytes=100000)
            projected = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=[path])
            result = process_sources(self.repo, [path], related_runs=[], model_call=self.adapter.for_operation(f"initial-{index}"),
                                     batch_context=projected["batch_context"], source_views=projected["source_views"])
            self.assertEqual(result["status"], "passed", result)
            publication = publish(self.repo, result["run_id"])
            old_runs.append(publication["run_id"])
            old_bytes.update({name: (self.repo / name).read_bytes() for name in publication["files"]})
        self.configure(count=9, related_run_ids=old_runs)
        before = self.number
        result = self.run_slice()
        self.assertEqual(result["next_action"], "idle", result)
        self.assertEqual(self.number - before, 6)
        publication = result["processing"]["publication"]
        self.assertEqual(len(publication["files"]), 18)
        self.assertEqual(sum(value["action"] == "reuse" for value in publication["operations"].values()), 16)
        self.assertEqual(sum(value["action"] == "create" for value in publication["operations"].values()), 2)
        self.assertEqual(old_bytes, {name: (self.repo / name).read_bytes() for name in old_bytes})

    def test_answered_nine_source_pending_runs_batches_with_reply_then_resolves(self):
        reply_key = "fixture:schedule-production:reply"
        reply_id = "src-" + hashlib.sha256(reply_key.encode()).hexdigest()
        self.configure(count=9, max_items=1, allowed_source_ids=[reply_id])
        self.verdict = "needs_confirmation"
        first = self.run_slice()
        self.assertEqual(first["status"], "needs_confirmation", first)
        item = read_registry(self.repo)["items"][0]
        reply = capture(self.repo, {"source_key": reply_key, "source_type": "note", "acquisition_status": "full_text",
                                   "body": "Artificial user answer: preserve uncertainty for every supplied fact.\n"})
        answer_pending(self.repo, item["item_id"], reply["path"])
        self.verdict = "passed"
        self.now += timedelta(days=1)
        result = drain_schedule(self.repo, "production", self.trigger("answered-day"), model_call=self.adapter,
                                clock=lambda: self.now, max_slices=8)
        self.assertEqual(result["next_action"], "idle", result)
        saved = read_registry(self.repo)["items"][0]
        self.assertEqual(saved["pending_status"], "resolved")
        extractions = [payload for role, payload in self.payloads if role == "extract" and payload.get("continuation_context")]
        self.assertEqual(len(extractions), 3)
        self.assertTrue(all(any(source["metadata"]["source_id"] == reply_id for source in payload["sources"]) for payload in extractions))
        self.assertEqual(self.number, 8)
        self.assertTrue(all(work["finished"] for work in plan_daily(self.repo, "production")["works"]))
        before = self.number, self.ledger.path.read_bytes()
        self.run_slice("after-resolution")
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before)

    def test_new_engine_cannot_bypass_an_old_unfinished_reservation(self):
        paths = self.sources(1)
        self.now = datetime(2026, 10, 19, 0, tzinfo=timezone.utc)
        config = {"schedule_id": "old-legacy", "daily_time": "22:00", "allowed_source_ids": [],
                  "allowed_source_paths": paths, "remote_mode": "local_only"}
        configure_schedule(self.repo, config, now=self.now)
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run_daily(self.repo, "old-legacy", self.trigger(), model_call=interrupted, clock=lambda: self.now)
        self.configure(count=1)
        with self.assertRaises(CaptureError) as caught:
            self.run_slice()
        self.assertEqual(caught.exception.code, "legacy_schedule_reconciliation_required")
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)

    def test_new_required_configuration_rejects_missing_binding(self):
        self.install_remote(bind=False)
        with self.assertRaises(CaptureError) as caught:
            self.configure(count=1, remote_mode="required")
        self.assertEqual(caught.exception.code, "environment_binding_required")
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)

    def test_completed_work_does_not_hide_new_source_from_another_trigger(self):
        future_key = "fixture:schedule-production:next"
        future_id = "src-" + hashlib.sha256(future_key.encode()).hexdigest()
        self.configure(count=1, allowed_source_ids=[future_id])
        self.run_slice()
        capture(self.repo, {"source_key": future_key, "source_type": "note", "acquisition_status": "full_text",
                            "body": "Artificial subsequent independent fact.\n"})
        result = self.run_slice("second-discovery")
        self.assertEqual(result["next_action"], "idle", result)
        self.assertEqual(len(plan_daily(self.repo, "production")["items"]), 2)
        self.assertEqual({item["status"] for item in plan_daily(self.repo, "production")["items"]}, {"completed"})


class LegacyRegistrationRecoveryTests(unittest.TestCase):
    setUp = test_scheduler.SchedulerTests.setUp
    capture = test_scheduler.SchedulerTests.capture
    config = test_scheduler.SchedulerTests.config
    model = test_scheduler.SchedulerTests.model
    trigger = test_scheduler.SchedulerTests.trigger
    fresh_source = test_scheduler.SchedulerTests.fresh_source

    def test_known_review_registry_failure_repairs_old_attempt_without_replay(self):
        configure_schedule(self.repo, self.config(), now=self.now)
        self.verdict = "needs_confirmation"
        with patch("pdm.pending.pending_update", side_effect=CaptureError("pending_registry_limit", "artificial full registry")):
            first = run_daily(self.repo, "isolated", self.trigger(), model_call=self.model, clock=lambda: self.now)
        self.assertEqual(first["outcomes"][0]["status"], "reconciliation_required")
        self.assertEqual(first["outcomes"][0]["reason"], "registration_required")
        self.assertEqual(len(self.roles), 2)
        second = run_daily(self.repo, "isolated", self.trigger(), model_call=self.model, clock=lambda: self.now)
        self.assertEqual(second["status"], "skipped_duplicate")
        self.assertEqual(len(self.roles), 2)
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "open")
        self.assertEqual(plan_daily(self.repo, "isolated", self.now)["items"][0]["status"], "awaiting_confirmation")
        state = _require(PipelineGit(self.repo), "isolated")
        self.assertEqual(sum(event["kind"] == "attempt_registration_repaired" for event in state["events"]), 1)


if __name__ == "__main__":
    unittest.main()
