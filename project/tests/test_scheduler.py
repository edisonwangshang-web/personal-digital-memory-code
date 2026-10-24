"""Artificial Git repositories only; no test provisions a real daily runner."""

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError, markdown, parse_markdown, source_contract
from pdm.model_calls import ModelCallError
from pdm.pending import answer_pending, list_pending, register_pending, resolve_pending
from pdm.pipeline import process_sources, publish
from pdm.scheduler import (
    _append, _attempts, _require, abandon_attempt, assert_publication_allowed, configure_schedule,
    normalize_config, pause_schedule, plan_daily, resume_schedule, run_daily,
    schedule_status,
)
from pdm.transaction import PipelineGit


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repository"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Isolated Schedule")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "isolated baseline")
        self.now = datetime(2026, 10, 15, 0, tzinfo=timezone.utc)
        self.source = self.capture("one")
        self.roles, self.timeouts = [], []
        self.number = 0
        self.verdict = "passed"

    def capture(self, key, original_time=None, body=None):
        return capture(self.repo, {"source_key": "schedule-fixture:" + key, "source_type": "note",
            "platform": "isolated", "original_url": None, "acquisition_status": "full_text",
            "original_time": original_time, "body": body or "用户：隔离资料 " + key + "。\n尚未完成。\n"})

    def config(self, **updates):
        return {"schedule_id": "isolated", "daily_time": "09:00", "allowed_source_ids": [self.source["source_id"]],
                "allowed_source_paths": [], "remote_mode": "local_only", **updates}

    def configure(self, **updates):
        if updates.get("remote_mode") == "required":
            self.install_remote()
        return configure_schedule(self.repo, self.config(**updates), now=self.now)

    def install_remote(self):
        remote = Path(self.temp.name) / "remote.git"
        if not remote.exists():
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            configure_test_repository(remote)
            git(self.repo, "remote", "add", "origin", str(remote))
        return remote

    def trigger(self, name="first", kind="test"):
        return {"kind": kind, "trigger_id": name, "scheduled_for": (self.now + timedelta(hours=1)).isoformat()}

    def model(self, role, payload, timeout=120):
        self.roles.append(role)
        self.timeouts.append(timeout)
        self.number += 1
        source = self.fresh_source(payload)
        suffix = source["metadata"]["source_id"][-10:]
        ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
               "locator": "L1-L2", "quote": source["body"].splitlines()[0]}
        if role == "extract":
            output = {"objects": [{"id": "goal-" + suffix, "type": "goal", "title": "隔离目标",
                "source_kind": "user_statement", "sources": [ref], "domains": [], "valid_from": None, "valid_to": None,
                "verification_status": "unverified", "lifecycle_status": "undetermined", "body": "用户提交隔离资料，尚未完成。"}],
                "wiki": {"id": "topic-" + suffix, "title": "隔离主题", "body": "目标尚未完成。", "sources": [ref]}}
            fields = set(output["objects"][0])
            for run in payload["related"]:
                for document in run["documents"]:
                    if document["path"].startswith("memory/records/") and document["metadata"]["id"] != "goal-" + suffix:
                        output["objects"].append({**{field: document["metadata"][field] for field in fields if field != "body"}, "body": document["body"]})
        else:
            output = {"verdict": self.verdict, "reasons": ["隔离审核结果"]}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def fresh_source(self, payload):
        old = {(ref["source_id"], ref["source_version"]) for run in payload["related"] for doc in run["documents"] for ref in doc["metadata"]["sources"]}
        return next((source for source in payload["sources"] if (source["metadata"]["source_id"], source["metadata"]["source_version"]) not in old), payload["sources"][0])

    def run_tick(self, name="first", **kwargs):
        return run_daily(self.repo, "isolated", self.trigger(name), model_call=self.model, clock=lambda: self.now, **kwargs)

    def error(self, code, action):
        with self.assertRaises(CaptureError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def state(self):
        return _require(PipelineGit(self.repo), "isolated")

    def test_config_requires_scope_real_timezone_and_enforceable_budget(self):
        self.error("empty_schedule_scope", lambda: normalize_config(self.config(allowed_source_ids=[])))
        self.error("invalid_schedule_config", lambda: normalize_config(self.config(timezone="UTC")))
        self.error("invalid_schedule_config", lambda: normalize_config(self.config(daily_time="25:00")))
        self.error("invalid_schedule_budget", lambda: normalize_config(self.config(max_calls=1)))
        self.error("invalid_schedule_budget", lambda: normalize_config(self.config(lease_seconds=10)))
        self.error("invalid_schedule_config", lambda: normalize_config(self.config(api_key="forbidden")))
        value = self.config()
        del value["remote_mode"]
        self.assertEqual(normalize_config(value)["remote_mode"], "required")

    def test_plan_only_enumerates_explicit_scope_without_source_body_reads(self):
        other = self.capture("not-authorized")
        self.configure()
        original = PipelineGit.read
        def guarded(adapter, commit, path):
            if path.startswith("memory/sources/"):
                self.fail("planning must not read personal source bodies")
            return original(adapter, commit, path)
        with patch.object(PipelineGit, "read", guarded):
            plan = plan_daily(self.repo, "isolated", self.now)
        self.assertEqual([item["source_paths"] for item in plan["items"]], [[self.source["path"]]])
        self.assertNotIn(other["path"], json.dumps(plan))

    def test_success_persists_reservation_calls_timeout_and_deduplicates_ticks(self):
        self.configure(timeout_seconds=7)
        result = self.run_tick()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reserved_calls"], 2)
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(self.timeouts, [7, 7])
        self.assertFalse(result["real_daily_verified"])
        self.assertEqual(result["scheduled_environment"], "unverified")
        self.assertEqual(result["remote_status"], "not_requested")
        self.assertEqual(self.run_tick()["status"], "skipped_duplicate")
        self.assertEqual(self.run_tick("second")["reserved_calls"], 0)
        self.assertEqual(len(self.roles), 2)
        self.assertEqual(schedule_status(self.repo, "isolated", self.now)["remaining"], [])
        events = self.state()["events"]
        reserved = next(event for event in events if event["kind"] == "attempt_reserved")
        self.assertEqual(reserved["data"]["reserved_calls"], 2)
        result_event = next(event for event in events if event["kind"] == "attempt_result")
        self.assertIsNone(result_event["data"]["usage"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_late_historical_input_and_changed_source_version_are_not_date_filtered(self):
        self.configure()
        self.run_tick()
        changed = self.capture("one", original_time="2001-01")
        self.assertNotEqual(changed["path"], self.source["path"])
        items = plan_daily(self.repo, "isolated", self.now + timedelta(days=1))["items"]
        by_path = {item["input_path"]: item["status"] for item in items}
        self.assertEqual(by_path[self.source["path"]], "completed")
        self.assertEqual(by_path[changed["path"]], "eligible")

    def test_budget_leaves_explicit_remaining_without_starting_extra_calls(self):
        other = self.capture("two")
        self.configure(allowed_source_ids=[self.source["source_id"], other["source_id"]], max_calls=2)
        result = self.run_tick()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["reserved_calls"], 2)
        self.assertEqual(len(result["remaining"]), 1)
        self.assertEqual(result["remaining"][0]["status"], "eligible")
        self.assertEqual(len(self.roles), 2)

    def test_failed_unknown_usage_consumes_reservation_and_stops_at_attempt_limit(self):
        self.configure(max_attempts=2)
        def failing(role, payload, timeout=120):
            self.roles.append(role)
            raise ModelCallError("timeout", "model-" + "1" * 32)
        for name in ("one", "two"):
            run_daily(self.repo, "isolated", self.trigger(name), model_call=failing, clock=lambda: self.now)
        plan = plan_daily(self.repo, "isolated")
        self.assertEqual(plan["items"][0]["status"], "attempt_limit")
        self.assertEqual(plan["items"][0]["attempts"], 2)
        self.run_tick("three")
        self.assertEqual(self.roles, ["extract", "extract"])
        results = [event["data"] for event in self.state()["events"] if event["kind"] == "attempt_result"]
        self.assertTrue(all(result["reserved_calls"] == 2 and result["usage"] is None for result in results))

    def test_pending_does_not_block_unrelated_passing_item(self):
        other = self.capture("two")
        self.configure(allowed_source_ids=[self.source["source_id"], other["source_id"]])
        original = self.model
        def mixed(role, payload, timeout=120):
            self.verdict = "needs_confirmation" if self.fresh_source(payload)["metadata"]["source_id"] == self.source["source_id"] else "passed"
            return original(role, payload, timeout)
        result = run_daily(self.repo, "isolated", self.trigger(), model_call=mixed, clock=lambda: self.now)
        self.assertEqual({outcome["status"] for outcome in result["outcomes"]}, {"awaiting_confirmation", "published"})
        self.assertEqual(len(self.roles), 4)
        self.run_tick("later")
        self.assertEqual(len(self.roles), 4)

    def pending_followup(self, *, authorized=True):
        reply_payload = {"source_key": "schedule-fixture:reply", "source_type": "note", "platform": "isolated",
                         "original_url": None, "acquisition_status": "full_text", "original_time": None,
                         "body": "用户：隔离问题已有答复。\n保留尚未执行的限定。\n"}
        _, reply_id, _ = source_contract(reply_payload)
        allowed = [self.source["source_id"], reply_id] if authorized else [self.source["source_id"]]
        self.configure(allowed_source_ids=allowed)
        self.verdict = "needs_confirmation"
        first = self.run_tick("pending")
        self.assertEqual(first["outcomes"][0]["status"], "awaiting_confirmation")
        item = list_pending(self.repo)[0]
        reply = capture(self.repo, reply_payload)
        answer_pending(self.repo, item["item_id"], reply["path"])
        self.verdict = "passed"
        processed = process_sources(self.repo, [reply["path"]], related_runs=[], model_call=self.model)
        self.assertEqual(processed["status"], "passed")
        publication = publish(self.repo, processed["run_id"], processed["head"])
        resolve_pending(self.repo, item["item_id"], processed["run_id"])
        return first, item, reply, processed, publication

    def edit_registry(self, change):
        path = self.repo / "memory/pending.md"
        header, body = parse_markdown(path.read_bytes())
        registry = json.loads(body)
        change(registry["items"])
        path.write_bytes(markdown(header, json.dumps(registry, ensure_ascii=False)))
        git(self.repo, "add", "memory/pending.md")
        git(self.repo, "commit", "-m", "isolated registry alteration")

    def test_resolved_confirmation_finishes_next_tick_without_republishing_original_run(self):
        first, item, reply, processed, publication = self.pending_followup()
        original = first["outcomes"][0]["run_id"]
        old_events = {event["path"]: (self.repo / event["path"]).read_bytes() for event in self.state()["events"]}
        head = git(self.repo, "rev-parse", "HEAD")
        calls = len(self.roles)
        plan = plan_daily(self.repo, "isolated", self.now)
        selected = next(entry for entry in plan["items"] if entry["input_path"] == self.source["path"])
        self.assertEqual(selected["status"], "resolved")
        self.assertEqual(selected["confirmation_resolutions"], [{"pending_item_id": item["item_id"],
            "original_run_id": original, "publication_run_id": processed["run_id"],
            "commit_sha": publication["commit_sha"], "manifest_path": publication["manifest_path"]}])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        result = self.run_tick("after-resolved")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["remaining"], [])
        self.assertEqual([entry["status"] for entry in result["outcomes"]], ["already_processed"])
        self.assertEqual(len(self.roles), calls)
        self.assertEqual(schedule_status(self.repo, "isolated", self.now)["remaining"], [])
        self.assertFalse((self.repo / "memory/runs/processes" / original / "manifest.md").exists())
        self.assertEqual(list_pending(self.repo)[0]["pending_status"], "resolved")
        for path, data in old_events.items():
            self.assertEqual((self.repo / path).read_bytes(), data)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_open_and_answered_confirmations_still_wait_without_model_calls(self):
        self.configure()
        self.verdict = "needs_confirmation"
        self.run_tick("pending")
        item = list_pending(self.repo)[0]
        calls = len(self.roles)
        for trigger in ("unanswered", "answered"):
            if trigger == "answered":
                reply = self.capture("reply")
                answer_pending(self.repo, item["item_id"], reply["path"])
            plan = plan_daily(self.repo, "isolated", self.now)
            self.assertEqual(plan["items"][0]["status"], "awaiting_confirmation")
            self.assertNotIn("confirmation_resolutions", plan["items"][0])
            result = self.run_tick(trigger)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["outcomes"], [])
            self.assertEqual(len(self.roles), calls)

    def test_unrelated_resolved_item_does_not_complete_waiting_schedule_input(self):
        self.configure()
        self.verdict = "needs_confirmation"
        self.run_tick("pending")
        question = self.capture("unrelated-question")
        registered = register_pending(self.repo, {"question": "另一个隔离问题", "source_refs": [
            {key: question[key] for key in ("source_id", "source_version", "path")} ]})
        reply = self.capture("unrelated-reply")
        answer_pending(self.repo, registered["item_id"], reply["path"])
        self.verdict = "passed"
        processed = process_sources(self.repo, [reply["path"]], related_runs=[], model_call=self.model)
        publish(self.repo, processed["run_id"], processed["head"])
        resolve_pending(self.repo, registered["item_id"], processed["run_id"])
        calls = len(self.roles)
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("unrelated original body read")):
            plan = plan_daily(self.repo, "isolated", self.now)
        self.assertEqual(plan["items"][0]["status"], "awaiting_confirmation")
        self.assertEqual(len(self.roles), calls)

    def test_resolved_confirmation_rejects_reply_outside_schedule_scope_before_body_reads(self):
        self.pending_followup(authorized=False)
        head = git(self.repo, "rev-parse", "HEAD")
        calls = len(self.roles)
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("out-of-scope original body read")):
            self.error("invalid_schedule_scope", lambda: plan_daily(self.repo, "isolated", self.now))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(len(self.roles), calls)

    def test_resolved_status_alone_cannot_hide_a_forged_publication_commit(self):
        self.pending_followup()
        self.edit_registry(lambda items: items[0]["resolution"].update(commit_sha="f" * 40))
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("unverified original body read")):
            self.error("pending_publication_mismatch", lambda: plan_daily(self.repo, "isolated", self.now))

    def test_resolved_item_must_match_original_question_and_object_references(self):
        self.pending_followup()
        self.edit_registry(lambda items: items[0].update(question="另一个问题，不能冒充原待确认事项"))
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("mismatched original body read")):
            self.error("schedule_pending_mismatch", lambda: plan_daily(self.repo, "isolated", self.now))

    def test_resolved_confirmation_rejects_changed_original_object_reference(self):
        self.pending_followup()
        self.edit_registry(lambda items: items[0]["object_refs"][0].update(content_version="sha256-" + "f" * 64))
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("mismatched original body read")):
            self.error("schedule_pending_mismatch", lambda: plan_daily(self.repo, "isolated", self.now))

    def test_resolution_manifest_cannot_hide_a_candidate_source_outside_its_scope(self):
        from pdm.pipeline import _candidate_binding, _source_binding, load_source
        _, _, _, processed, _ = self.pending_followup()
        outside = self.capture("outside-resolution")
        path = self.repo / processed["stage_path"]
        header, body = parse_markdown(path.read_bytes())
        candidate = json.loads(body)
        candidate["additional_sources"] = [_source_binding(load_source(self.repo, outside["path"]))]
        candidate["binding"] = _candidate_binding(candidate)
        path.write_bytes(markdown(header, json.dumps(candidate, ensure_ascii=False)))
        git(self.repo, "add", processed["stage_path"])
        git(self.repo, "commit", "-m", "isolated hidden candidate source")
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("hidden original body read")):
            self.error("evidence_source_mismatch", lambda: plan_daily(self.repo, "isolated", self.now))

    def test_later_reviewed_update_preserves_historical_confirmation_resolution(self):
        import pdm.pipeline as pipeline
        first, _, reply, processed, publication = self.pending_followup()
        record_path = next(path for path in publication["files"] if path.startswith("memory/records/"))
        topic_path = next(path for path in publication["files"] if path.startswith("memory/topics/"))
        old_bytes = (self.repo / record_path).read_bytes()
        old_record, _ = parse_markdown(old_bytes)
        old_topic, _ = parse_markdown((self.repo / topic_path).read_bytes())
        extra = self.capture("later-update")
        def updated_model(role, payload, timeout=120):
            response = self.model(role, payload, timeout)
            if role == "extract":
                source = next(source for source in payload["sources"] if source["path"] == extra["path"])
                ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                       "locator": "L1-L2", "quote": source["body"].splitlines()[0]}
                fields = response["output"]["objects"][0].keys() - {"body"}
                response["output"]["objects"] = [{**{field: old_record[field] for field in fields},
                    "sources": [ref], "body": "后续独立审核已更新隔离记录。"}]
                response["output"]["wiki"] = {"id": old_topic["id"], "title": old_topic["title"],
                    "sources": [ref], "body": "主题已随隔离记录更新。"}
            return response
        updated = process_sources(self.repo, [reply["path"], extra["path"]], related_runs=[processed["run_id"]], model_call=updated_model)
        self.assertEqual(updated["status"], "passed")
        publish(self.repo, updated["run_id"], updated["head"])
        self.assertNotEqual((self.repo / record_path).read_bytes(), old_bytes)
        original_load = pipeline.load_source
        def scoped_load(repo, path, *args, **kwargs):
            self.assertNotEqual(path, extra["path"], "new unrelated source must not enter old resolution proof")
            return original_load(repo, path, *args, **kwargs)
        calls, head = len(self.roles), git(self.repo, "rev-parse", "HEAD")
        with patch("pdm.pipeline.load_source", side_effect=scoped_load):
            selected = next(item for item in plan_daily(self.repo, "isolated", self.now)["items"]
                            if item["input_path"] == self.source["path"])
        self.assertEqual(selected["status"], "resolved")
        self.assertEqual(selected["confirmation_resolutions"][0]["publication_run_id"], processed["run_id"])
        self.assertEqual(selected["confirmation_resolutions"][0]["original_run_id"], first["outcomes"][0]["run_id"])
        self.assertEqual(len(self.roles), calls)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_unknown_inflight_never_repeats_call_even_after_lease_expiry(self):
        self.configure()
        with patch("pdm.pipeline.process_sources", side_effect=RuntimeError("simulated process disappearance")):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        self.now += timedelta(days=1)
        result = self.run_tick("later")
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertTrue(result["lease_expired"])
        self.assertEqual(self.roles, [])
        self.error("schedule_reconciliation_required", lambda: resume_schedule(self.repo, "isolated", "isolation-test"))

    def test_crash_after_review_recovers_without_new_model_calls(self):
        self.configure()
        with patch("pdm.pipeline.publish", side_effect=RuntimeError("simulated before publication")):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        self.assertEqual(self.roles, ["extract", "review"])
        result = self.run_tick()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.roles), 2)
        self.assertTrue(result["outcomes"][0]["recovered"])

    def test_required_remote_reservation_ack_precedes_any_model_call(self):
        self.configure(remote_mode="required")
        confirmations = []
        def confirm(adapter, commit, files):
            adapter.sync("origin", files, commit)
            for value in files.values():
                metadata, body = parse_markdown(value)
                if metadata.get("kind") == "schedule_event":
                    event = json.loads(body)
                    if event["kind"] == "attempt_reserved" and not confirmations:
                        confirmations.append(("reserved", len(self.roles)))
        result = self.run_tick(remote_confirm=confirm)
        self.assertEqual(confirmations, [("reserved", 0)])
        self.assertEqual(result["remote_status"], "confirmed")
        self.assertFalse(result["real_daily_verified"])

    def test_required_remote_failure_prevents_model_execution(self):
        self.configure(remote_mode="required")
        def reject(adapter, commit, files):
            raise CaptureError("remote_unavailable", "isolated")
        self.error("remote_unavailable", lambda: self.run_tick(remote_confirm=reject))
        self.assertEqual(self.roles, [])

    def test_remote_ack_loss_after_publication_reconciles_existing_commit(self):
        self.configure(remote_mode="required")
        def lose_ack(adapter, commit, files):
            adapter.sync("origin", files, commit)
            if any(path.startswith("memory/records/") for path in files):
                raise CaptureError("remote_unconfirmed", "isolated loss of acknowledgement")
        self.error("remote_unconfirmed", lambda: self.run_tick(remote_confirm=lose_ack))
        self.assertEqual(len(self.roles), 2)
        result = self.run_tick()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.roles), 2)

    def test_real_git_remote_default_sync_and_fresh_clone_preserve_progress(self):
        remote = self.install_remote()
        self.configure(remote_mode="required")
        self.assertEqual(self.run_tick()["remote_status"], "confirmed")
        restored = Path(self.temp.name) / "restored"
        subprocess.run(["git", "clone", "--branch", "main", str(remote), str(restored)], check=True, capture_output=True)
        configure_test_repository(restored)
        plan = plan_daily(restored, "isolated")
        self.assertEqual(plan["items"][0]["status"], "completed")

    def test_pause_blocks_manual_process_and_publish_and_resume_requires_validation(self):
        self.configure()
        pending = process_sources(self.repo, [self.source["path"]], related_runs=[], model_call=self.model)
        pause_schedule(self.repo, "isolated", "isolated gate check", now=self.now)
        self.error("publication_paused", lambda: process_sources(self.repo, [self.source["path"]], related_runs=[], model_call=self.model))
        self.error("publication_paused", lambda: publish(self.repo, pending["run_id"], git(self.repo, "rev-parse", "HEAD")))
        self.assertEqual(self.run_tick()["status"], "skipped_paused")
        self.error("schedule_validation_required", lambda: resume_schedule(self.repo, "isolated", ""))
        resumed = resume_schedule(self.repo, "isolated", "project/tests/test_scheduler.py", now=self.now)
        self.assertEqual(resumed["status"], "resumed")
        self.assertTrue(assert_publication_allowed(self.repo)["allowed"])

    def test_unknown_schema_and_uncommitted_schedule_state_fail_closed(self):
        self.configure()
        path = self.repo / self.state()["events"][0]["path"]
        original = path.read_bytes()
        path.write_bytes(original.replace(b'"0.4"', b'"9.9"'))
        self.error("schedule_state_changed", lambda: assert_publication_allowed(self.repo))
        git(self.repo, "add", str(path.relative_to(self.repo)))
        git(self.repo, "commit", "-m", "isolated unknown scheduler schema")
        self.error("unknown_schedule_state", lambda: assert_publication_allowed(self.repo))

    def test_status_distinguishes_unknown_no_trigger_incomplete_and_empty(self):
        self.configure(allowed_source_ids=[], allowed_source_paths=[self.source["path"]])
        self.now += timedelta(hours=2)
        status = schedule_status(self.repo, "isolated", self.now)
        self.assertEqual(status["status"], "trigger_unknown")
        observation = {"status": "available", "window_start": (self.now - timedelta(hours=2)).isoformat(),
                       "window_end": self.now.isoformat(), "trigger_ids": []}
        self.assertEqual(schedule_status(self.repo, "isolated", self.now, observation)["status"], "no_trigger")
        observation["trigger_ids"] = ["platform-fixture"]
        self.assertEqual(schedule_status(self.repo, "isolated", self.now, observation)["status"], "incomplete")
        # An explicitly authorized ID with no source yet gives a real empty tick,
        # distinct from a timer which never triggered.
        configure_schedule(self.repo, self.config(schedule_id="empty", allowed_source_ids=["src-" + "f" * 64]), now=self.now - timedelta(hours=2))
        trigger = {"kind": "scheduled", "trigger_id": "fixture-empty", "scheduled_for": (self.now - timedelta(hours=1)).isoformat()}
        result = run_daily(self.repo, "empty", trigger, model_call=self.model, clock=lambda: self.now)
        self.assertEqual(result["status"], "completed_empty")
        self.assertEqual(schedule_status(self.repo, "empty", self.now)["status"], "completed_empty")
        self.assertEqual(self.roles, [])
        self.assertFalse(result["real_daily_verified"])

    def test_context_digest_and_reservation_scope_are_enforced(self):
        self.configure()
        with patch("pdm.pipeline.process_sources", side_effect=RuntimeError("isolated stop after reserve")):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        state = self.state()
        reservation = next(event for event in state["events"] if event["kind"] == "attempt_reserved")
        data = reservation["data"]
        context = {"schedule_id": "isolated", "tick_key": data["tick_key"], "attempt_id": data["attempt_id"],
                   "run_id": data["run_id"], "reservation_path": reservation["path"], "reservation_sha256": "0" * 64}
        self.error("invalid_execution_context", lambda: assert_publication_allowed(self.repo, context))
        import hashlib
        context["reservation_sha256"] = hashlib.sha256((self.repo / reservation["path"]).read_bytes()).hexdigest()
        allowed = assert_publication_allowed(self.repo, context)
        self.assertEqual(allowed["source_paths"], [self.source["path"]])
        self.assertEqual(allowed["reserved_calls"], 2)

    def test_cross_day_recovery_does_not_insert_tick_between_review_and_publish(self):
        self.configure()
        with patch("pdm.pipeline.publish", side_effect=RuntimeError("isolated interruption")):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        self.now += timedelta(days=1)
        result = self.run_tick("next-day")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(result["reserved_calls"], 0)
        self.assertTrue(result["outcomes"][0]["recovered"])

    def test_manifest_without_receipt_is_reconciled_without_republication(self):
        self.configure()
        original = PipelineGit.write_files
        failed = []
        def interrupted(adapter, files, expected_head, message):
            if not failed and any(path.endswith("/receipt.md") for path in files):
                failed.append(True)
                raise CaptureError("receipt_interrupted", "isolated interruption")
            return original(adapter, files, expected_head, message)
        with patch.object(PipelineGit, "write_files", interrupted):
            self.error("receipt_interrupted", lambda: self.run_tick())
        manifest = next((self.repo / "memory/runs/processes").glob("run-*/manifest.md"))
        publication_commit = git(self.repo, "log", "-1", "--format=%H", "--", manifest.relative_to(self.repo).as_posix())
        result = self.run_tick()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.roles), 2)
        self.assertEqual(git(self.repo, "log", "-1", "--format=%H", "--", manifest.relative_to(self.repo).as_posix()), publication_commit)

    def test_processing_reuse_marker_survives_crash_before_schedule_result(self):
        existing = process_sources(self.repo, [self.source["path"]], related_runs=[], model_call=self.model)
        publish(self.repo, existing["run_id"])
        self.configure()
        from pdm.scheduler import _event
        def interrupted(adapter, state, kind, data, at, callback=None):
            if kind == "attempt_result":
                raise RuntimeError("isolated interruption after reuse marker")
            return _event(adapter, state, kind, data, at, callback)
        with patch("pdm.scheduler._event", interrupted):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        self.assertEqual(len(self.roles), 2)
        result = self.run_tick()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["outcomes"][0]["status"], "already_processed")
        self.assertEqual(len(self.roles), 2)

    def test_deleting_or_editing_committed_pause_cannot_unpause_publication(self):
        self.configure()
        pause_schedule(self.repo, "isolated", "isolated check", now=self.now)
        last = self.repo / self.state()["events"][-1]["path"]
        git(self.repo, "rm", last.relative_to(self.repo).as_posix())
        git(self.repo, "commit", "-m", "isolated malicious pause removal")
        self.error("unknown_schedule_state", lambda: assert_publication_allowed(self.repo))

    def test_future_scheduled_claim_does_not_run_early_or_prove_real_runner(self):
        self.configure()
        result = run_daily(self.repo, "isolated", self.trigger(kind="scheduled"), model_call=self.model, clock=lambda: self.now)
        self.assertEqual(result["status"], "not_due")
        self.assertEqual(result["scheduled_environment"], "unverified")
        self.assertEqual(self.roles, [])

    def test_forged_reservation_cannot_expand_the_configured_source_scope(self):
        other = self.capture("not-authorized")
        self.configure()
        with patch("pdm.pipeline.process_sources", side_effect=RuntimeError("isolated stop")):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        state = self.state()
        value = next(event["data"] for event in state["events"] if event["kind"] == "attempt_reserved")
        forged = {**value, "attempt_id": "attempt-" + "f" * 32, "run_id": "run-" + "e" * 32,
                  "source_paths": [other["path"]], "input_path": other["path"]}
        _append(PipelineGit(self.repo), "isolated", "attempt_reserved", forged, self.now)
        self.error("unknown_schedule_state", lambda: assert_publication_allowed(self.repo))

    def test_deleted_source_is_excluded_before_budget_reservation(self):
        from pdm.controls import record_control
        other = self.capture("active")
        record_control(self.repo, {"request_key": "isolated-delete", "action": "delete", "source_paths": [self.source["path"]],
                       "object_paths": [], "replacement_source_paths": []})
        self.configure(allowed_source_ids=[self.source["source_id"], other["source_id"]], max_calls=2)
        planned = plan_daily(self.repo, "isolated")
        self.assertEqual(next(item["status"] for item in planned["items"] if item["input_path"] == self.source["path"]), "excluded")
        result = self.run_tick()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reserved_calls"], 2)
        self.assertEqual(len(self.roles), 2)

    def test_two_isolated_days_update_one_topic_and_resume_only_unfinished_versions(self):
        """Simulated clock and fake calls are never evidence of real GPT triggers."""
        import hashlib
        failed_source = self.capture("flaky")
        pending_source = self.capture("pending")
        historical_id = "src-" + hashlib.sha256(b"schedule-fixture:historical").hexdigest()
        self.configure(allowed_source_ids=[self.source["source_id"], failed_source["source_id"], pending_source["source_id"], historical_id], max_calls=8)
        stage = {"day": 1, "fresh": None}
        extracted = []
        def joint_model(role, payload, timeout=120):
            self.roles.append(role)
            self.number += 1
            if role == "extract":
                old = {(ref["source_id"], ref["source_version"]) for run in payload["related"] for doc in run["documents"] for ref in doc["metadata"]["sources"]}
                fresh = [source for source in payload["sources"] if (source["metadata"]["source_id"], source["metadata"]["source_version"]) not in old]
                self.assertEqual(len(fresh), 1)
                stage["fresh"] = fresh[0]
                extracted.append(fresh[0]["path"])
                if stage["day"] == 1 and fresh[0]["metadata"]["source_id"] == failed_source["source_id"]:
                    raise ModelCallError("timeout", "model-" + f"{self.number:032x}")
                refs = [{"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                         "locator": "L1-L2", "quote": source["body"].splitlines()[0]} for source in payload["sources"]]
                body = "\n".join(source["body"] for source in payload["sources"])
                output = {"objects": [{"id": "goal-shared", "type": "goal", "title": "共同隔离主题",
                    "source_kind": "user_statement", "sources": refs, "domains": [], "valid_from": None, "valid_to": None,
                    "verification_status": "unverified", "lifecycle_status": "undetermined", "body": body}],
                    "wiki": {"id": "topic-shared", "title": "共同隔离主题", "body": body, "sources": refs}}
            else:
                verdict = "needs_confirmation" if stage["fresh"]["metadata"]["source_id"] == pending_source["source_id"] else "passed"
                output = {"verdict": verdict, "reasons": ["隔离日增量验证"]}
            return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                    "invocation_id": "model-" + f"{self.number:032x}"}
        first = run_daily(self.repo, "isolated", self.trigger("isolated-day-one"), model_call=joint_model, clock=lambda: self.now)
        self.assertEqual(first["status"], "partial")
        self.assertEqual({item["status"] for item in first["outcomes"]}, {"published", "failed", "awaiting_confirmation"})
        self.assertEqual(first["reserved_calls"], 6)
        updated = self.capture("one", original_time="2001-01", body="用户：隔离目标已经调整。\n这是新版本。\n")
        historical = self.capture("historical", original_time="1999-12")
        stage["day"] = 2
        self.now += timedelta(days=1)
        before = len(extracted)
        second = run_daily(self.repo, "isolated", self.trigger("isolated-day-two"), model_call=joint_model, clock=lambda: self.now)
        self.assertEqual(second["reserved_calls"], 6)
        self.assertEqual(set(extracted[before:]), {updated["path"], historical["path"], failed_source["path"]})
        self.assertNotIn(self.source["path"], extracted[before:])
        self.assertNotIn(pending_source["path"], extracted[before:])
        self.assertEqual({item["status"] for item in second["remaining"]}, {"awaiting_confirmation"})
        self.assertEqual(len(list((self.repo / "memory/records").glob("*.md"))), 1)
        self.assertEqual(len(list((self.repo / "memory/topics").glob("*.md"))), 1)
        self.assertIn("隔离目标已经调整", (self.repo / "memory/topics/topic-shared.md").read_text())
        self.assertFalse(first["real_daily_verified"])
        self.assertFalse(second["real_daily_verified"])

    def test_second_clone_remote_pause_is_seen_before_local_formal_commit(self):
        self.configure(remote_mode="required")
        original = publish
        def pause_from_other_clone(repo, run_id, expected_head=None):
            controller = Path(self.temp.name) / "controller"
            subprocess.run(["git", "clone", "--branch", "main", str(self.install_remote()), str(controller)], check=True, capture_output=True)
            configure_test_repository(controller)
            git(controller, "config", "user.name", "Isolated Controller")
            git(controller, "config", "user.email", "controller@example.invalid")
            pause_schedule(controller, "isolated", "隔离远端暂停", now=self.now)
            return original(repo, run_id, expected_head)
        with patch("pdm.pipeline.publish", pause_from_other_clone):
            self.error("publication_paused", lambda: self.run_tick())
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertFalse((self.repo / "memory/records").exists())
        self.assertFalse((self.repo / "memory/topics").exists())
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_fake_confirmation_callback_cannot_bypass_required_remote_read_gate(self):
        self.configure(remote_mode="required")
        self.error("git_failed", lambda: self.run_tick(remote_confirm=lambda *args: None))
        self.assertEqual(self.roles, [])

    def test_future_and_effective_supersedes_are_planned_with_aware_time(self):
        from pdm.controls import record_control
        replacement = self.capture("replacement")
        record_control(self.repo, {"request_key": "future-change", "action": "supersedes",
            "source_paths": [self.source["path"]], "replacement_source_paths": [replacement["path"]],
            "effective_from": "2027-01-01"})
        self.configure(allowed_source_ids=[self.source["source_id"], replacement["source_id"]])
        future = plan_daily(self.repo, "isolated", self.now)
        self.assertEqual(next(item["status"] for item in future["items"] if item["input_path"] == self.source["path"]), "eligible")
        effective = plan_daily(self.repo, "isolated", datetime(2027, 2, 1, tzinfo=timezone.utc))
        self.assertEqual(next(item["status"] for item in effective["items"] if item["input_path"] == self.source["path"]), "excluded")

    def test_distinct_manual_trigger_ids_cannot_reset_actual_daily_call_budget(self):
        other = self.capture("two")
        self.configure(allowed_source_ids=[self.source["source_id"], other["source_id"]], max_calls=2)
        first = self.run_tick("manual-first")
        second = self.run_tick("manual-second")
        self.assertEqual(first["daily_reserved_calls"], 2)
        self.assertEqual(second["daily_reserved_calls"], 2)
        self.assertEqual(second["reserved_calls"], 0)
        self.assertEqual(len(self.roles), 2)
        self.now += timedelta(days=1)
        third = self.run_tick("manual-next-day")
        self.assertEqual(third["daily_reserved_calls"], 2)
        self.assertEqual(third["reserved_calls"], 2)
        self.assertEqual(len(self.roles), 4)

    def test_callback_which_only_confirms_claim_cannot_claim_final_publication(self):
        self.configure(remote_mode="required")
        def partial(adapter, commit, files):
            if not self.roles:
                adapter.sync("origin", files, commit)
        self.error("remote_unconfirmed", lambda: self.run_tick(remote_confirm=partial))
        remote = self.install_remote()
        files = git(remote, "ls-tree", "-r", "--name-only", "refs/heads/main", "--", "memory/records/")
        self.assertEqual(files, "")
        self.assertEqual(self.roles, ["extract", "review"])

    def test_pause_abandon_resume_requires_new_run_review_and_keeps_budget(self):
        self.configure()
        with patch("pdm.pipeline.publish", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_tick()
        reservation = next(event for event in self.state()["events"] if event["kind"] == "attempt_reserved")
        old = reservation["data"]
        pause_schedule(self.repo, "isolated", "隔离暂停", now=self.now)
        self.error("schedule_reconciliation_required", lambda: resume_schedule(self.repo, "isolated", "isolated-validation"))
        closed = abandon_attempt(self.repo, "isolated", old["attempt_id"], "已停止隔离进程并复验", validation_ref="isolated-stop-and-validation")
        self.assertEqual(closed["status"], "abandoned")
        self.assertEqual(closed["reserved_calls"], 2)
        self.assertTrue((self.repo / ("memory/staging/" + old["run_id"] + ".md")).exists())
        resume_schedule(self.repo, "isolated", "isolated-validation", now=self.now)
        self.error("invalid_execution_context", lambda: publish(self.repo, old["run_id"], git(self.repo, "rev-parse", "HEAD")))
        result = self.run_tick("after-resume")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["daily_reserved_calls"], 4)
        self.assertEqual(len(self.roles), 4)
        self.assertNotEqual(result["outcomes"][0]["run_id"], old["run_id"])
        self.assertEqual(plan_daily(self.repo, "isolated")["items"][0]["attempts"], 2)

    def test_abandon_reconciles_existing_publication_instead_of_claiming_cancelled(self):
        self.configure()
        from pdm.scheduler import _event
        def interrupted(adapter, state, kind, data, at, callback=None):
            if kind == "attempt_result":
                raise RuntimeError("isolated interruption after publication")
            return _event(adapter, state, kind, data, at, callback)
        with patch("pdm.scheduler._event", interrupted):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        reservation = next(event for event in self.state()["events"] if event["kind"] == "attempt_reserved")
        pause_schedule(self.repo, "isolated", "隔离暂停", now=self.now)
        result = abandon_attempt(self.repo, "isolated", reservation["data"]["attempt_id"], "核对隔离已完成发布", validation_ref="isolated-remote-check")
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["publication_run_id"], reservation["data"]["run_id"])
        self.assertEqual(len(self.roles), 2)
        self.assertTrue((self.repo / "memory/records").exists())
        resume_schedule(self.repo, "isolated", "isolated-validation", now=self.now)

    def test_abandon_does_not_guess_that_unknown_inflight_call_stopped(self):
        self.configure()
        with patch("pdm.pipeline.process_sources", side_effect=RuntimeError("isolated disappearance")):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        reservation = next(event for event in self.state()["events"] if event["kind"] == "attempt_reserved")
        self.error("schedule_validation_required", lambda: abandon_attempt(self.repo, "isolated", reservation["data"]["attempt_id"], "", validation_ref=""))
        self.error("schedule_reconciliation_required", lambda: abandon_attempt(self.repo, "isolated", reservation["data"]["attempt_id"], "缺少停止证据", validation_ref="unverified"))

    def test_abandon_refuses_a_manifest_deleted_after_publication(self):
        self.configure()
        from pdm.scheduler import _event
        def interrupted(adapter, state, kind, data, at, callback=None):
            if kind == "attempt_result":
                raise RuntimeError("isolated interruption")
            return _event(adapter, state, kind, data, at, callback)
        with patch("pdm.scheduler._event", interrupted):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        reservation = next(event for event in self.state()["events"] if event["kind"] == "attempt_reserved")
        manifest = "memory/runs/processes/" + reservation["data"]["run_id"] + "/manifest.md"
        git(self.repo, "rm", manifest)
        git(self.repo, "commit", "-m", "isolated deleted publication manifest")
        self.error("publication_reconciliation_required", lambda: abandon_attempt(self.repo, "isolated", reservation["data"]["attempt_id"], "必须先核对既有发布", validation_ref="isolated-validation"))

    def test_abandon_repairs_missing_publication_receipt_before_recording_success(self):
        self.configure()
        original = PipelineGit.write_files
        interrupted = []
        def fail_receipt(adapter, files, expected_head, message):
            if not interrupted and any(path.endswith("/receipt.md") for path in files):
                interrupted.append(True)
                raise CaptureError("receipt_interrupted", "isolated")
            return original(adapter, files, expected_head, message)
        with patch.object(PipelineGit, "write_files", fail_receipt):
            self.error("receipt_interrupted", lambda: self.run_tick())
        reservation = next(event for event in self.state()["events"] if event["kind"] == "attempt_reserved")
        pause_schedule(self.repo, "isolated", "隔离回执恢复", now=self.now)
        result = abandon_attempt(self.repo, "isolated", reservation["data"]["attempt_id"], "核对已提交内容并补回执", validation_ref="isolated-receipt-recovery")
        self.assertEqual(result["status"], "published")
        self.assertTrue((self.repo / ("memory/runs/processes/" + reservation["data"]["run_id"] + "/receipt.md")).exists())
        self.assertEqual(len(self.roles), 2)

    def test_known_link_only_waits_for_text_without_reserving_or_blocking_text(self):
        import hashlib
        key = next("link-fixture:" + str(number) for number in range(1000)
                   if hashlib.sha256(("link-fixture:" + str(number)).encode()).hexdigest() < self.source["source_id"][4:])
        linked = capture(self.repo, {"source_key": key, "source_type": "attachment_link", "platform": "isolated",
            "original_url": "https://example.invalid/link", "acquisition_status": "link_only", "body": "", "original_time": None})
        self.configure(allowed_source_ids=[linked["source_id"], self.source["source_id"]], max_calls=2)
        plan = plan_daily(self.repo, "isolated")
        self.assertEqual(next(item["status"] for item in plan["items"] if item["input_path"] == linked["path"]), "waiting_text")
        result = self.run_tick()
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(result["reserved_calls"], 2)
        self.assertEqual(result["outcomes"][0]["status"], "published")
        self.assertEqual([item["status"] for item in result["remaining"]], ["waiting_text"])

    def test_uncertain_abandon_requires_explicit_stop_pause_and_expired_lease(self):
        self.now = datetime.now(timezone.utc) - timedelta(hours=1)
        self.configure()
        with patch("pdm.pipeline.process_sources", side_effect=RuntimeError("isolated disappearance")):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        reservation = next(event for event in self.state()["events"] if event["kind"] == "attempt_reserved")
        attempt = reservation["data"]["attempt_id"]
        self.error("schedule_pause_required", lambda: abandon_attempt(self.repo, "isolated", attempt, "明确停止旧执行", validation_ref="isolated-stop-record", execution_stopped=True))
        pause_schedule(self.repo, "isolated", "隔离停止核对", now=self.now)
        self.error("schedule_reconciliation_required", lambda: abandon_attempt(self.repo, "isolated", attempt, "未明确声明停止", validation_ref="isolated-stop-record"))
        result = abandon_attempt(self.repo, "isolated", attempt, "已人工确认旧执行停止；保留未知用量", validation_ref="isolated-stop-record", execution_stopped=True)
        self.assertEqual(result["status"], "abandoned_uncertain")
        self.assertEqual(result["reserved_calls"], 2)
        self.assertIsNone(result["usage"])
        self.assertTrue(result["execution_stopped_statement"])
        resume_schedule(self.repo, "isolated", "isolated-revalidation", now=self.now)
        self.assertEqual(plan_daily(self.repo, "isolated")["items"][0]["attempts"], 1)
        self.assertEqual(plan_daily(self.repo, "isolated")["items"][0]["status"], "eligible")

    def test_explicit_stop_cannot_abandon_an_active_unknown_lease(self):
        self.now = datetime.now(timezone.utc)
        self.configure()
        with patch("pdm.pipeline.process_sources", side_effect=RuntimeError("isolated disappearance")):
            with self.assertRaises(RuntimeError):
                self.run_tick()
        reservation = next(event for event in self.state()["events"] if event["kind"] == "attempt_reserved")
        pause_schedule(self.repo, "isolated", "等待租约到期", now=self.now)
        self.error("schedule_lease_active", lambda: abandon_attempt(self.repo, "isolated", reservation["data"]["attempt_id"], "不能以声明跳过活跃租约", validation_ref="isolated-stop-record", execution_stopped=True))


if __name__ == "__main__":
    unittest.main()
