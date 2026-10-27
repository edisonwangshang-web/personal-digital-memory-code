"""Real CLI-to-worker confirmation flow using synthetic native transport only."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine_fixtures import isolated_engine
from git_fixtures import configure_test_repository
from quality_fixtures import add_quality, passed_review
from test_pipeline import git
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.cli import main
from pdm.context import _continuation_queue_id, plan_answered_pending
from pdm.contracts import CaptureError
from pdm.controls import record_control
from pdm.model_calls import ModelCallError
from pdm.pending import answer_pending, read_registry
from pdm.pipeline import process_sources, verify_publication
from pdm.production import _read
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class CliContinuationFlowTests(unittest.TestCase):
    QUESTION = "是否计划从2030年1月1日开始？"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-cli-pending-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "data"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Pending CLI")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial data baseline")
        isolated_engine(self)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.ledger = BudgetLedger.create(self.private / "audit.json", None, 0,
                                         "artificial-pending-cli", repo=self.repo, mode="audit")
        self.calls = []
        self.origin_mode, self.rejections, self.unknown = True, 0, False
        self.source = self.capture("question", "用户：学习任务的开始日期需要确认。\n")
        origin = process_sources(self.repo, [self.source["path"]], related_runs=[],
            skill_bundle=resolve_skill_refs(),
            model_call=BudgetedModelCall(self.ledger, "artificial-origin", self.transport))
        self.assertEqual(origin["status"], "needs_confirmation", origin)
        self.origin = origin["run_id"]
        self.item = read_registry(self.repo)["items"][0]
        self.reply = self.capture("reply", "用户：对。\n")
        answer_pending(self.repo, self.item["item_id"], self.reply["path"])
        self.origin_mode = False
        self.paths = sorted([self.source["path"], self.reply["path"]])
        self.job = self.plan()

    def capture(self, key, body):
        return capture(self.repo, {"source_key": "artificial:pending:" + key,
            "source_type": "note", "acquisition_status": "full_text", "body": body})

    def plan(self):
        result = plan_answered_pending(self.repo, allowed_source_paths=self.paths,
                                      allowed_run_ids=[self.origin])
        self.assertTrue(result["coverage"]["complete"], result)
        return result["jobs"][0]

    def transport(self, role, payload, timeout=120):
        self.calls.append((role, deepcopy(payload)))
        invocation = "model-" + f"{len(self.calls):032x}"
        if not self.origin_mode:
            context = payload.get("continuation_context")
            self.assertIsNotNone(context, "CLI omitted the actual question behind the short reply")
            self.assertEqual(context["question"], self.QUESTION)
            self.assertEqual(context["reply_sources"][0]["path"], self.reply["path"])
        if self.unknown and role == "extract":
            raise ModelCallError("timeout", invocation)
        if role == "extract":
            refs = []
            for source in payload["sources"]:
                line = (source["fragments"][0]["lines"][0] if "fragments" in source
                        else {"line": 1, "text": source["body"].splitlines()[0]})
                refs.append({"source_id": source["metadata"]["source_id"],
                    "source_version": source["metadata"]["source_version"],
                    "locator": f"L{line['line']}-L{line['line']}", "quote": line["text"]})
            revision = (" Synthetic wording revision " + str(len(self.calls)) + "."
                        if "revision_context" in payload else "")
            record = {"id": "artificial-confirmed-date", "type": "knowledge", "title": "人工计划",
                "source_kind": "user_statement", "sources": refs, "domains": ["learning"],
                "valid_from": None, "valid_to": None, "verification_status": "unverified",
                "lifecycle_status": "undetermined", "body": "Synthetic plan and reply; no execution claim." + revision}
            wiki = {"id": "artificial-confirmed-topic", "title": "人工主题", "sources": refs,
                    "body": record["body"]}
            if "fragments" in payload["sources"][0]:
                wiki["record_ids"] = [record["id"]]
                output = {"objects": [record], "wikis": [wiki], "reuse_refs": []}
            else:
                output = {"objects": [record], "wiki": wiki, "reuse_refs": []}
            output = add_quality(output, payload["quality_context"])
        else:
            output = passed_review(payload["required_checks"], payload["skill_bundle"])
            if self.origin_mode:
                output.update(verdict="needs_confirmation", reasons=[self.QUESTION])
                output["checks"][0].update(result="needs_confirmation", reason=self.QUESTION)
            elif self.rejections:
                self.rejections -= 1
                output.update(verdict="needs_revision", reasons=["Revise only the synthetic draft wording."])
                output["checks"][0].update(result="needs_revision", reason=output["reasons"][0])
                output["repair_scope"] = {"kind": "content", "missing_bindings": []}
        return {"output": output, "model": "synthetic-cli-only", "invocation_id": invocation,
                "elapsed_seconds": 0.01, "usage": None}

    def native(self, role, payload, timeout, *, on_start):
        on_start()
        return self.transport(role, payload, timeout)

    def execute(self, job=None, expected=0):
        self.job_path = self.private / "job.json"
        self.job_path.write_text(json.dumps(job or self.job))
        output, errors = io.StringIO(), io.StringIO()
        with patch("pdm.model_calls._call_transport", side_effect=self.native), \
                redirect_stdout(output), redirect_stderr(errors):
            code = main(["context", "continuations", "execute", "--repo", str(self.repo),
                         "--input", str(self.job_path), "--budget-ledger", str(self.ledger.path)])
        result = json.loads(output.getvalue() or errors.getvalue())
        self.assertEqual(code, expected, result)
        return result

    def snapshot(self):
        return (git(self.repo, "rev-parse", "HEAD"), self.ledger.path.read_bytes(),
                {str(f.relative_to(self.repo)): f.read_bytes() for f in (self.repo / "memory").rglob("*") if f.is_file()})

    def queue(self):
        return _read(PipelineGit(self.repo),
                     "memory/runs/processes/queues/" + _continuation_queue_id(self.job) + "/state.md", "production_queue")

    def test_default_cli_binds_original_question_and_short_reply_then_replays_without_calls(self):
        before = len(self.calls)
        result = self.execute()
        self.assertTrue(result["resolved"])
        self.assertEqual(len(self.calls) - before, 2)
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "resolved")
        verify_publication(self.repo, result["publication_run_id"], source_paths=self.paths)
        snapshot = self.snapshot()
        repeated = self.execute()
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["model_calls"], 0)
        self.assertEqual(self.snapshot(), snapshot)

    def test_content_rejection_retains_question_through_existing_bounded_feedback(self):
        self.rejections = 1
        before = len(self.calls)
        result = self.execute()
        self.assertTrue(result["resolved"])
        self.assertEqual(len(self.calls) - before, 4)
        revised = [p for role, p in self.calls[before:] if role == "extract" and "revision_context" in p]
        self.assertEqual(len(revised), 1)
        self.assertEqual(revised[0]["continuation_context"]["question"], self.QUESTION)

    def test_four_attempts_exhausted_and_replanning_does_not_reset_counter(self):
        self.rejections = 10
        before = len(self.calls)
        first = self.execute(expected=4)
        self.assertEqual(first["status"], "attempt_limit", first)
        self.assertEqual(len(self.calls) - before, 8)
        old_plan = self.queue()["plan"]["plan_id"]
        refreshed = self.plan()
        self.assertEqual(refreshed["job_id"], self.job["job_id"])
        self.assertNotEqual(refreshed["head"], self.job["head"])
        calls, snapshot = len(self.calls), self.snapshot()
        self.assertEqual(self.execute(refreshed, expected=4)["status"], "attempt_limit")
        self.assertEqual(self.queue()["plan"]["plan_id"], old_plan)
        self.assertEqual(len(self.calls), calls)
        after = self.snapshot()
        self.assertEqual(after[1], snapshot[1])
        queue_path = "memory/runs/processes/queues/" + _continuation_queue_id(self.job) + "/state.md"
        self.assertEqual({p: v for p, v in after[2].items() if p != queue_path},
                         {p: v for p, v in snapshot[2].items() if p != queue_path})

    def test_uncertain_continuation_call_is_not_replayed_on_restart(self):
        self.unknown = True
        before = len(self.calls)
        self.assertEqual(self.execute(expected=4)["status"], "unknown")
        self.assertEqual(len(self.calls) - before, 1)
        calls = len(self.calls)
        self.assertEqual(self.execute(self.plan(), expected=4)["status"], "unknown")
        self.assertEqual(len(self.calls), calls)

    def test_published_before_resolution_interruption_resumes_original_queue_without_calls(self):
        with patch("pdm.pending.resolve_pending", side_effect=CaptureError("artificial_stop", "fixture")):
            self.assertEqual(self.execute(expected=2)["error"], "artificial_stop")
        self.assertEqual(self.queue()["status"], "published")
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")
        plan_id, calls = self.queue()["plan"]["plan_id"], len(self.calls)
        self.assertTrue(self.execute(self.plan())["resolved"])
        self.assertEqual(self.queue()["plan"]["plan_id"], plan_id)
        self.assertEqual(len(self.calls), calls)

    def legacy(self, status):
        self.ledger.reserve(self.job["job_id"], ["extract", "review"])
        if status is not None:
            self.ledger.start(self.job["job_id"], "extract", "a" * 64)
            self.ledger.finish(self.job["job_id"], "extract", status)
        before, calls = self.snapshot(), len(self.calls)
        result = self.execute(expected=2)
        self.assertEqual(result["error"], "continuation_legacy_reconciliation_required")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.calls), calls)
        self.assertIsNone(self.queue())

    def test_legacy_completed_call_cannot_be_reissued_by_new_engine(self):
        self.legacy("completed")

    def test_legacy_unknown_call_cannot_be_reissued_by_new_engine(self):
        self.legacy("unknown")

    def test_legacy_unstarted_reservation_also_requires_reconciliation(self):
        self.legacy(None)

    def test_oversized_required_reply_stops_before_any_model_call(self):
        large = self.capture("large-reply", "x" * 300000 + "\n")
        answer_pending(self.repo, self.item["item_id"], large["path"])
        self.paths.append(large["path"])
        job, calls, ledger = self.plan(), len(self.calls), self.ledger.path.read_bytes()
        result = self.execute(job, expected=4)
        self.assertEqual(result["status"], "blocked_capacity")
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.ledger.path.read_bytes(), ledger)

    def test_forged_question_is_rejected_before_worker_or_model(self):
        changed = deepcopy(self.job)
        changed["question"] = "Invented replacement question."
        before, calls = self.snapshot(), len(self.calls)
        self.assertEqual(self.execute(changed, expected=2)["error"], "continuation_job_changed")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.calls), calls)

    def test_completed_replay_honors_deletion_before_historical_body_reads(self):
        completed = self.execute()
        record_control(self.repo, {"request_key": "artificial:delete-completed-scope", "action": "delete",
            "source_paths": self.paths, "object_paths": sorted(completed["publication"]["files"])})
        before, calls = self.snapshot(), len(self.calls)
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("deleted body opened")):
            self.assertEqual(self.execute(expected=2)["error"], "pending_scope_deleted")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.calls), calls)
