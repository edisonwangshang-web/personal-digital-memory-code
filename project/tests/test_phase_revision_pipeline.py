"""Real Git phase-preserving repair, using artificial sources and model calls."""

from copy import deepcopy
import unittest

import test_production_revision_feedback
from pdm.batching import plan_batches, project_batch_sources
from pdm.budget import BudgetedModelCall
from pdm.capture import capture
from pdm.context import plan_answered_pending
from pdm.contracts import CaptureError
from pdm.pending import answer_pending, read_registry, resolve_pending
from pdm.pipeline import _read_reviewed, process_sources, publish, verify_publication
from pdm.production import prepare_production, run_production
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class PhaseRevisionPipelineTests(unittest.TestCase):
    setUp = test_production_revision_feedback.ProductionRevisionFeedbackTests.setUp
    sources = test_production_revision_feedback.ProductionRevisionFeedbackTests.sources
    transport = test_production_revision_feedback.ProductionRevisionFeedbackTests.transport
    attempts = test_production_revision_feedback.ProductionRevisionFeedbackTests.attempts

    def prepare_phase(self, paths, *, draft=False, continuation=None, bundle=None):
        plan = plan_batches(self.repo, paths, max_input_bytes=128000)
        projected = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"],
                                          source_paths=paths)
        selected = sorted(source["path"] for source in projected["source_views"])
        options = {"related_runs": [], "draft_only": draft,
                   "batch_context": projected["batch_context"], "source_views": projected["source_views"],
                   "skill_bundle": bundle or resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")}
        if continuation is not None:
            options["continuation_context"] = deepcopy(continuation)
        return selected, options

    def expect_error(self, code, callback):
        with self.assertRaises(CaptureError) as error:
            callback()
        self.assertEqual(error.exception.code, code)

    def test_rejected_quality_draft_is_repaired_but_remains_unpublishable(self):
        selected, options = self.prepare_phase(self.sources(9), draft=True)
        self.assertEqual(len(selected), 8)
        rejected = process_sources(self.repo, selected, model_call=self.transport, **options)
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        frozen = {path: (self.repo / path).read_bytes()
                  for path in (rejected["stage_path"], rejected["review_path"], *selected)}
        fixed = process_sources(self.repo, selected, revision_from_run=rejected["run_id"],
                                model_call=self.transport, **options)
        self.assertEqual(fixed["status"], "passed", fixed)
        candidate, review, _ = _read_reviewed(PipelineGit(self.repo), fixed["run_id"])
        self.assertTrue(candidate["batch_material"]["draft_only"])
        self.assertEqual(review["output"]["verdict"], "passed")
        feedback = [payload for role, payload in self.payloads if role == "extract"][-1]["revision_context"]
        self.assertEqual(feedback["source_run_id"], rejected["run_id"])
        self.assertEqual(feedback["review_record"]["output"]["verdict"], "needs_revision")
        self.expect_error("batch_requires_final_review", lambda: publish(self.repo, fixed["run_id"]))
        self.assertEqual(self.roles, ["extract", "review"] * 2)
        self.assertEqual(frozen, {path: (self.repo / path).read_bytes() for path in frozen})
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                                    "memory/records", "memory/topics"), "")

    def test_revision_cannot_turn_draft_into_final_or_change_its_fragment_scope(self):
        selected, options = self.prepare_phase(self.sources(9), draft=True)
        rejected = process_sources(self.repo, selected, model_call=self.transport, **options)
        before = PipelineGit(self.repo).head(), self.number
        self.expect_error("invalid_revision_context", lambda: process_sources(
            self.repo, selected, revision_from_run=rejected["run_id"], model_call=self.transport,
            **{**options, "draft_only": False}))
        changed = deepcopy(options)
        changed["source_views"][0]["fragments"][0]["lines"] = []
        self.expect_error("revision_batch_context_changed", lambda: process_sources(
            self.repo, selected, revision_from_run=rejected["run_id"], model_call=self.transport, **changed))
        self.assertEqual((PipelineGit(self.repo).head(), self.number), before)

    def answered_context(self):
        paths = self.sources(1)
        selected, options = self.prepare_phase(paths)
        self.reject_reviews = 0
        def needs_answer(role, payload):
            response = self.transport(role, payload)
            if role == "review":
                response["output"].update(verdict="needs_confirmation", reasons=["Which source scope should be retained?"])
                response["output"]["checks"][0].update(result="needs_confirmation",
                    reason="An artificial user clarification is required.")
            return response
        origin = process_sources(self.repo, selected, model_call=needs_answer, **options)
        self.assertEqual(origin["status"], "needs_confirmation", origin)
        item = read_registry(self.repo)["items"][0]
        reply = capture(self.repo, {"source_key": "artificial:phase-reply", "source_type": "note",
            "acquisition_status": "full_text", "body": "Artificial user clarification: retain the supplied source scope.\n"})
        answer_pending(self.repo, item["item_id"], reply["path"])
        paths.append(reply["path"])
        jobs = plan_answered_pending(self.repo, allowed_source_paths=paths,
                                    allowed_run_ids=[origin["run_id"]], max_sources=256, max_runs=256)
        self.assertEqual(len(jobs["jobs"]), 1, jobs)
        job = jobs["jobs"][0]
        context = {key: job[key] for key in ("job_id", "parent_item_id", "question", "parent_run_ids",
                                            "source_refs", "object_refs", "reply_sources")}
        return paths, context, item

    def test_answered_context_survives_rejection_repair_publication_and_resolution(self):
        paths, context, item = self.answered_context()
        selected, options = self.prepare_phase(paths, continuation=context)
        self.reject_reviews = 1
        rejected = process_sources(self.repo, selected, model_call=self.transport, **options)
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        fixed = process_sources(self.repo, selected, revision_from_run=rejected["run_id"],
                                model_call=self.transport, **options)
        self.assertEqual(fixed["status"], "passed", fixed)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), fixed["run_id"])
        self.assertEqual(candidate["continuation_context"], context)
        self.assertEqual(candidate["revision_context"]["candidate"]["continuation_context"], context)
        last = [payload for role, payload in self.payloads if role == "extract"][-1]
        self.assertEqual(last["continuation_context"], context)
        self.assertEqual(last["revision_context"]["source_run_id"], rejected["run_id"])
        publication = publish(self.repo, fixed["run_id"])
        self.assertEqual(verify_publication(self.repo, fixed["run_id"])["commit_sha"], publication["commit_sha"])
        resolved = resolve_pending(self.repo, item["item_id"], fixed["run_id"])
        self.assertEqual(resolved["pending_status"], "resolved")

    def test_answered_revision_cannot_drop_or_forge_the_user_context(self):
        paths, context, _ = self.answered_context()
        selected, options = self.prepare_phase(paths, continuation=context)
        self.reject_reviews = 1
        rejected = process_sources(self.repo, selected, model_call=self.transport, **options)
        before = PipelineGit(self.repo).head(), self.number
        for replacement in (None, {**context, "question": "A different artificial question"}):
            with self.subTest(replacement=replacement):
                self.expect_error("invalid_revision_context", lambda: process_sources(self.repo, selected,
                    revision_from_run=rejected["run_id"], model_call=self.transport,
                    **{**options, "continuation_context": replacement}))
        self.assertEqual((PipelineGit(self.repo).head(), self.number), before)

    def test_legacy_quality_draft_still_cannot_use_new_phase_revision(self):
        selected, options = self.prepare_phase(self.sources(9), draft=True,
            bundle=resolve_skill_refs("knowledge-extract@9", "knowledge-review@7"))
        rejected = process_sources(self.repo, selected, model_call=self.transport, **options)
        before = PipelineGit(self.repo).head(), self.number
        self.expect_error("invalid_revision_context", lambda: process_sources(
            self.repo, selected, revision_from_run=rejected["run_id"], model_call=self.transport, **options))
        self.assertEqual((PipelineGit(self.repo).head(), self.number), before)

    def test_production_repairs_rejected_draft_within_the_same_attempt_counter(self):
        paths = self.sources(9)
        prepare_production(self.repo, "phase-draft", paths,
            skill_bundle=resolve_skill_refs("knowledge-extract@10", "knowledge-review@8"))
        rejected = run_production(self.repo, "phase-draft", model_call=self.adapter, max_steps=1)
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        frozen = (self.repo / rejected["last_result"]["stage_path"]).read_bytes()
        fixed = run_production(self.repo, "phase-draft", model_call=self.adapter, max_steps=1)
        self.assertEqual(fixed["status"], "ready", fixed)
        self.assertEqual(len(fixed["completed_batches"]), 1)
        self.assertEqual(len(self.attempts()), 1)
        attempts = self.attempts()[0]["attempts"]
        self.assertEqual([attempt["status"] for attempt in attempts], ["needs_revision", "passed"])
        self.assertEqual(attempts[1]["revision_from_run"], rejected["active_run_id"])
        self.assertEqual(self.roles, ["extract", "review"] * 2)
        self.assertEqual((self.repo / rejected["last_result"]["stage_path"]).read_bytes(), frozen)

    def test_unknown_draft_revision_is_not_replayed_by_a_new_queue(self):
        paths = self.sources(9)
        bundle = resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")
        prepare_production(self.repo, "phase-old", paths, skill_bundle=bundle)
        rejected = run_production(self.repo, "phase-old", model_call=self.adapter, max_steps=1)
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run_production(self.repo, "phase-old",
                model_call=BudgetedModelCall(self.ledger, "worker", interrupted), max_steps=1)
        before = self.ledger.path.read_bytes(), self.number
        prepare_production(self.repo, "phase-new", paths, skill_bundle=bundle)
        for queue in ("phase-old", "phase-new"):
            result = run_production(self.repo, queue, model_call=self.adapter, max_steps=1)
            self.assertEqual(result["status"], "unknown", result)
        self.assertEqual((self.ledger.path.read_bytes(), self.number), before)
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 2)


if __name__ == "__main__":
    unittest.main()
