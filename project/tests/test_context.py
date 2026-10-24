"""Context and answered jobs use disposable Git and injected model fixtures."""

import copy
import unittest
from unittest.mock import patch

import test_pending
from pdm.context import discover_context, execute_continuation, plan_answered_pending
from pdm.contracts import CaptureError
from pdm.controls import record_control
from pdm.pending import answer_pending, read_registry, resolve_pending
from pdm.pipeline import process_sources, publish
from pdm.transaction import PipelineGit


class ContextTests(unittest.TestCase):
    setUp = test_pending.PendingTests.setUp
    capture = test_pending.PendingTests.capture
    assert_error = test_pending.PendingTests.assert_error
    ref = staticmethod(test_pending.PendingTests.ref)

    def model(self, role, payload):
        self.invocations += 1
        if role == "extract":
            selected = payload["sources"] if getattr(self, "use_reply", True) else [
                source for source in payload["sources"] if source["metadata"]["source_id"] == self.source["source_id"]]
            refs = [{"source_id": source["metadata"]["source_id"],
                     "source_version": source["metadata"]["source_version"],
                     "locator": "L1-L1", "quote": source["body"].splitlines()[0]} for source in selected]
            output = {"objects": [{"id": "clarified-date", "type": "knowledge", "title": "同一个标题",
                "source_kind": "user_statement", "sources": refs, "domains": [],
                "valid_from": None, "valid_to": None, "verification_status": "unverified",
                "lifecycle_status": "undetermined", "body": "来源表达及限定如实保存。"}],
                "wiki": {"id": "clarified-topic", "title": "隔离主题", "body": "按来源保留范围。", "sources": refs}}
        else:
            output = {"verdict": getattr(self, "verdict", "passed"), "reasons": ["具体从哪一天开始？"]}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.invocations:032x}"}

    def process(self, paths, related=()):
        return process_sources(self.repo, paths, related_runs=list(related), model_call=self.model)

    def initial(self):
        processed = self.process([self.source["path"]])
        return processed, publish(self.repo, processed["run_id"])

    def answered(self):
        self.verdict = "needs_confirmation"
        original = self.process([self.source["path"]])
        item = read_registry(self.repo)["items"][0]
        reply = self.capture("reply", "用户：明确为2030年1月1日。\n")
        answer_pending(self.repo, item["item_id"], reply["path"])
        self.verdict = "passed"
        return original, item, reply

    def plan(self, original, reply, **kwargs):
        return plan_answered_pending(self.repo, allowed_source_paths=[self.source["path"], reply["path"]],
                                     allowed_run_ids=[original["run_id"]], **kwargs)

    def snapshot(self):
        git = PipelineGit(self.repo)
        paths = git.text("ls-files").splitlines()
        return git.head(), git.run("status", "--porcelain"), {path: (self.repo / path).read_bytes() for path in paths}

    def test_discovery_is_scoped_metadata_only_and_does_not_assert_semantic_identity(self):
        original, _ = self.initial()
        fresh = self.capture("fresh", "用户：这里是一项后续补充。\n")
        before = self.snapshot()
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("source body opened")):
            result = discover_context(self.repo, [fresh["path"]],
                allowed_source_paths=[self.source["path"], fresh["path"]], allowed_run_ids=[original["run_id"]])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["related_runs"], [original["run_id"]])
        self.assertEqual(len(result["candidate_objects"]), 2)
        self.assertFalse(result["semantic_identity_verified"])
        self.assertTrue(result["publication_verification_required"])
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(self.snapshot(), before)

    def test_run_source_authorization_precedes_formal_frontmatter(self):
        original, _ = self.initial()
        fresh = self.capture("fresh")
        with patch("pdm.context._metadata_only", side_effect=AssertionError("unauthorized metadata opened")):
            result = discover_context(self.repo, [fresh["path"]], allowed_source_paths=[fresh["path"]],
                                      allowed_run_ids=[original["run_id"]])
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["related_runs"], [])
        self.assertEqual(result["coverage"]["gaps"][0]["reason"], "unauthorized_context_sources")

    def test_exact_ids_and_capacity_report_gaps_without_truncating_context(self):
        original, _ = self.initial()
        fresh = self.capture("fresh")
        scope = {"allowed_source_paths": [self.source["path"], fresh["path"]],
                 "allowed_run_ids": [original["run_id"]]}
        missing = discover_context(self.repo, [fresh["path"]], object_ids=["different-identity"], **scope)
        self.assertEqual(missing["related_runs"], [])
        self.assertEqual(missing["coverage"]["gaps"][0]["reason"], "requested_object_not_available")
        full = discover_context(self.repo, [fresh["path"]], object_ids=["clarified-date"], max_sources=1, **scope)
        self.assertEqual(len(full["source_paths"]), 2)
        self.assertFalse(full["coverage"]["complete"])
        self.assertEqual(full["coverage"]["gaps"][0]["reason"], "context_source_capacity")

    def test_updated_publication_replaces_historical_context_and_keeps_exact_ids(self):
        original, _ = self.initial()
        fresh = self.capture("fresh")
        updated = self.process([self.source["path"], fresh["path"]], [original["run_id"]])
        publish(self.repo, updated["run_id"])
        result = discover_context(self.repo, [fresh["path"]],
            allowed_source_paths=[self.source["path"], fresh["path"]],
            allowed_run_ids=[original["run_id"], updated["run_id"]])
        self.assertEqual(result["related_runs"], [updated["run_id"]])
        self.assertEqual(result["skipped_runs"], [{"run_id": original["run_id"], "reason": "historical_publication"}])

    def test_deleted_context_is_not_proposed(self):
        original, publication = self.initial()
        fresh = self.capture("fresh")
        record_control(self.repo, {"request_key": "context:delete", "action": "delete",
            "source_paths": [self.source["path"]], "object_paths": sorted(publication["files"])})
        result = discover_context(self.repo, [fresh["path"]],
            allowed_source_paths=[self.source["path"], fresh["path"]], allowed_run_ids=[original["run_id"]])
        self.assertEqual(result["related_runs"], [])
        self.assertTrue(result["skipped_runs"])

    def test_answered_job_is_stable_zero_write_and_not_an_open_item(self):
        self.verdict = "needs_confirmation"
        original = self.process([self.source["path"]])
        open_plan = plan_answered_pending(self.repo, allowed_source_paths=[self.source["path"]],
                                         allowed_run_ids=[original["run_id"]])
        self.assertEqual(open_plan["jobs"], [])
        reply = self.capture("reply")
        item = read_registry(self.repo)["items"][0]
        answer_pending(self.repo, item["item_id"], reply["path"])
        before = self.snapshot()
        first, second = self.plan(original, reply), self.plan(original, reply)
        self.assertEqual(first, second)
        self.assertEqual(len(first["jobs"]), 1)
        self.assertEqual(first["jobs"][0]["parent_item_id"], item["item_id"])
        self.assertEqual(first["jobs"][0]["parent_run_ids"], [original["run_id"]])
        self.assertEqual(first["jobs"][0]["reply_sources"][0]["commit_sha"], reply["commit_sha"])
        self.assertEqual(first["model_calls"], 0)
        self.assertEqual(self.snapshot(), before)

    def test_scope_gaps_do_not_disclose_question_or_read_parent_candidate(self):
        original, _, reply = self.answered()
        with patch("pdm.context._pending_origin", side_effect=AssertionError("unauthorized run opened")):
            result = plan_answered_pending(self.repo, allowed_source_paths=[reply["path"]], allowed_run_ids=[])
        self.assertEqual(result["jobs"], [])
        self.assertFalse(result["coverage"]["complete"])
        self.assertNotIn("question", str(result))

    def test_answered_followup_preserves_original_published_context_and_requires_authorization(self):
        published, _ = self.initial()
        extra = self.capture("followup", "用户：后续安排需要补充日期。\n")
        self.verdict = "needs_confirmation"
        original = self.process([self.source["path"], extra["path"]], [published["run_id"]])
        reply = self.capture("reply")
        item = read_registry(self.repo)["items"][0]
        answer_pending(self.repo, item["item_id"], reply["path"])
        paths = [self.source["path"], extra["path"], reply["path"]]
        blocked = plan_answered_pending(self.repo, allowed_source_paths=paths,
                                       allowed_run_ids=[original["run_id"]])
        self.assertEqual(blocked["jobs"], [])
        self.assertEqual(blocked["coverage"]["gaps"][0]["reason"], "unauthorized_parent_context")
        ready = plan_answered_pending(self.repo, allowed_source_paths=paths,
                                      allowed_run_ids=[original["run_id"], published["run_id"]])
        self.assertEqual(ready["jobs"][0]["related_runs"], [published["run_id"]])
        self.assertEqual(ready["jobs"][0]["source_paths"], sorted(paths))

    def test_answered_job_runs_real_processing_review_publication_and_resolve(self):
        original, item, reply = self.answered()
        job = self.plan(original, reply)["jobs"][0]
        calls = self.invocations
        result = execute_continuation(self.repo, job,
            process_call=lambda repo, work: self.process(work["source_paths"], work["related_runs"]))
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(self.invocations - calls, 2)
        self.assertNotEqual(result["publication_run_id"], original["run_id"])
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "resolved")
        before, calls = self.snapshot(), self.invocations
        self.assertEqual(self.plan(original, reply)["jobs"], [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.invocations, calls)

    def test_review_failure_does_not_publish_or_resolve(self):
        original, _, reply = self.answered()
        job = self.plan(original, reply)["jobs"][0]
        self.verdict = "needs_revision"
        with patch("pdm.pipeline.publish", side_effect=AssertionError("unapproved publication")):
            result = execute_continuation(self.repo, job,
                process_call=lambda repo, work: self.process(work["source_paths"], work["related_runs"]))
        self.assertFalse(result["resolved"])
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")

    def test_published_before_resolution_interruption_resumes_with_zero_new_model_calls(self):
        original, _, reply = self.answered()
        job = self.plan(original, reply)["jobs"][0]

        def interrupt(*_):
            raise CaptureError("isolated_interruption", "isolated")

        self.assert_error("isolated_interruption", lambda: execute_continuation(self.repo, job,
            process_call=lambda repo, work: self.process(work["source_paths"], work["related_runs"]),
            resolve_call=interrupt))
        next_job = self.plan(original, reply)["jobs"][0]
        self.assertEqual(next_job["job_id"], job["job_id"])
        self.assertNotEqual(next_job["head"], job["head"])
        calls = self.invocations
        result = execute_continuation(self.repo, next_job,
            process_call=lambda repo, work: self.process(work["source_paths"], work["related_runs"]))
        self.assertTrue(result["resolved"])
        self.assertEqual(self.invocations, calls)

    def test_reply_only_in_input_without_record_reference_cannot_resolve(self):
        original, _, reply = self.answered()
        job = self.plan(original, reply)["jobs"][0]
        self.use_reply = False
        self.assert_error("pending_reply_not_applied", lambda: execute_continuation(self.repo, job,
            process_call=lambda repo, work: self.process(work["source_paths"], work["related_runs"])))
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")

    def test_changed_job_and_fake_publication_callbacks_fail_closed(self):
        original, _, reply = self.answered()
        job = self.plan(original, reply)["jobs"][0]
        forged = copy.deepcopy(job)
        forged["question"] = "替换事项"
        self.assert_error("continuation_job_changed", lambda: execute_continuation(self.repo, forged,
            process_call=lambda *_: self.fail("changed job executed")))
        with self.assertRaises(CaptureError):
            execute_continuation(self.repo, job,
                process_call=lambda *_: {"status": "passed", "run_id": "run-" + "f" * 32},
                publish_call=lambda *_, **kwargs: {"status": "published_local"})
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")

    def test_fake_resolve_callback_is_not_accepted(self):
        original, _, reply = self.answered()
        job = self.plan(original, reply)["jobs"][0]
        self.assert_error("continuation_not_resolved", lambda: execute_continuation(self.repo, job,
            process_call=lambda repo, work: self.process(work["source_paths"], work["related_runs"]),
            resolve_call=lambda *_: {"pending_status": "resolved"}))
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")


if __name__ == "__main__":
    unittest.main()
