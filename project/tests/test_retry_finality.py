"""Immediate retry exhaustion with real temporary Git/audit and artificial output."""

from copy import deepcopy
import unittest

import test_production as fixtures
import pdm.production as production
from pdm.batching import project_batch_sources
from pdm.budget import BudgetedModelCall
from pdm.model_calls import ModelCallError
from pdm.pipeline import process_sources
from pdm.production import prepare_production, production_status, run_production
from pdm.transaction import PipelineGit


class RetryFinalityTests(unittest.TestCase):
    setUp = fixtures.ProductionTests.setUp
    sources = fixtures.ProductionTests.sources
    attempts = fixtures.ProductionTests.attempts
    assert_no_formal = fixtures.ProductionTests.assert_no_formal

    def transport(self, role, payload, timeout=120):
        response = fixtures.ProductionTests.transport(self, role, payload, timeout)
        if role == "extract" and getattr(self, "later_object_ids", False):
            for record in response["output"]["objects"]:
                record["id"] += "-later"
            for topic in response["output"]["wikis"]:
                topic["id"] += "-later"
                topic["record_ids"] = [value + "-later" for value in topic["record_ids"]]
        mode = getattr(self, "failure_mode", None)
        if mode == "unknown" and role == "extract":
            raise ModelCallError("timeout", response["invocation_id"])
        if mode == "extract" and role == "extract":
            response["output"]["objects"] = []
        if mode == "review" and role == "review":
            response["output"]["verdict"] = "invalid"
        return response

    def prepare(self, *, strategy=None):
        self.paths = self.sources(1)
        self.strategy = strategy
        self.queue = "four-attempts"
        result = prepare_production(self.repo, self.queue, self.paths,
                                    review_strategy=strategy)
        self.assertEqual(result["status"], "ready", result)

    def run_slice(self, queue=None):
        return run_production(self.repo, queue or self.queue,
                              model_call=self.adapter, max_steps=1)

    def frozen_accounting(self):
        return self.number, self.ledger.path.read_bytes(), deepcopy(self.attempts())

    def assert_stopped(self, result, *, source_attempts):
        self.assertEqual(result["status"], "attempt_limit", result)
        self.assertNotEqual(result["next_action"], "continue", result)
        self.assertEqual(production_status(self.repo, self.queue)["status"], "attempt_limit")
        self.assertEqual(len(self.attempts()), 1)
        self.assertEqual(len(self.attempts()[0]["attempts"]), source_attempts)
        self.assert_no_formal()
        before = self.frozen_accounting()
        self.assertEqual(self.run_slice()["status"], "attempt_limit")
        self.assertEqual(self.frozen_accounting(), before)
        # Queue/worker labels do not provide another attempt allowance.
        prepare_production(self.repo, "renamed", self.paths,
                           review_strategy=self.strategy)
        self.adapter = BudgetedModelCall(self.ledger, "other-worker", self.transport)
        self.assertEqual(self.run_slice("renamed")["status"], "attempt_limit")
        self.assertEqual(self.frozen_accounting(), before)

    def test_four_known_extraction_failures_stop_in_the_fourth_single_step(self):
        self.prepare()
        self.failure_mode = "extract"
        for number in range(4):
            result = self.run_slice()
            self.assertEqual(result["status"], "failed" if number < 3 else "attempt_limit", result)
            self.assertEqual(len(self.attempts()[0]["attempts"]), number + 1)
        self.assertEqual(self.roles, ["extract"] * 4)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 4)
        self.assertTrue(all(attempt["status"] == "failed"
                            for attempt in self.attempts()[0]["attempts"]))
        self.assert_stopped(result, source_attempts=4)

    def test_four_rejected_reviews_stop_without_rewriting_the_last_review(self):
        self.prepare()
        self.verdict = "needs_revision"
        for number in range(4):
            result = self.run_slice()
            self.assertEqual(result["status"], "needs_revision" if number < 3 else "attempt_limit", result)
        self.assertEqual(self.roles, ["extract", "review"] * 4)
        self.assertTrue(all(attempt["status"] == "needs_revision"
                            for attempt in self.attempts()[0]["attempts"]))
        from pdm.pipeline import _committed, _load
        run = self.attempts()[0]["attempts"][-1]["run_id"]
        review = _load(_committed(PipelineGit(self.repo),
                                 "memory/runs/processes/" + run + "/review.md"), "review")
        self.assertEqual(review["output"]["verdict"], "needs_revision")
        self.assert_stopped(result, source_attempts=4)

    def test_fourth_attempt_can_pass_and_publish_without_another_model_call(self):
        self.prepare()
        self.verdict = "needs_revision"
        for _ in range(3):
            self.assertEqual(self.run_slice()["status"], "needs_revision")
        self.verdict = "passed"
        self.assertEqual(self.run_slice()["status"], "ready")
        before = self.number, self.ledger.path.read_bytes()
        self.assertEqual(self.run_slice()["status"], "published")
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 4)

    def test_fourth_confirmation_request_is_not_changed_into_failure(self):
        self.prepare()
        self.verdict = "needs_revision"
        for _ in range(3):
            self.assertEqual(self.run_slice()["status"], "needs_revision")
        self.verdict = "needs_confirmation"
        result = self.run_slice()
        self.assertEqual(result["status"], "needs_confirmation", result)
        self.assertEqual(result["next_action"], "await_reply")
        before = self.frozen_accounting()
        self.assertEqual(self.run_slice()["status"], "needs_confirmation")
        self.assertEqual(self.frozen_accounting(), before)

    def test_first_unknown_stops_without_consuming_the_remaining_three_attempts(self):
        self.prepare()
        self.failure_mode = "unknown"
        result = self.run_slice()
        self.assertEqual((result["status"], result["next_action"]), ("unknown", "reconcile"))
        self.assertEqual(self.roles, ["extract"])
        self.assertEqual(len(self.attempts()[0]["attempts"]), 1)
        before = self.frozen_accounting()
        for _ in range(2):
            self.assertEqual(self.run_slice()["status"], "unknown")
        prepare_production(self.repo, "renamed", self.paths)
        self.failure_mode = None
        self.assertEqual(self.run_slice("renamed")["status"], "unknown")
        self.assertEqual(self.frozen_accounting(), before)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)
        self.assert_no_formal()

    def test_decomposed_exhaustion_cannot_restart_the_parent_extraction(self):
        self.prepare(strategy="decomposed-v1")
        self.failure_mode = "review"
        for number in range(4):
            result = self.run_slice()
            self.assertEqual(result["status"], "partial" if number < 3 else "attempt_limit", result)
        self.assertEqual(self.roles, ["extract"] + ["review"] * 4)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 5)
        self.assert_stopped(result, source_attempts=1)

    def test_historical_limit_precedes_any_later_partial_or_reserved_review_resume(self):
        self.prepare(strategy="decomposed-v1")
        self.failure_mode = "review"
        for _ in range(4):
            stopped = self.run_slice()
        self.assertEqual(stopped["status"], "attempt_limit")
        git = PipelineGit(self.repo)
        counter_path = git.text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                production.ATTEMPTS).splitlines()[0]
        work_key = counter_path.rsplit("/", 1)[1].removesuffix(".md")
        operation = "batch:" + work_key + ":2"
        later_run = "run-" + production._hash({"work": work_key, "attempt": 2})[:32]
        state_path = production._path(self.queue)
        state = production._read(git, state_path, "production_queue")
        projection = project_batch_sources(self.repo, state["plan"],
            state["plan"]["batches"][0]["batch_id"], source_paths=self.paths)
        # Recreate an old reachable history in this artificial checkout only:
        # the old parent gate admitted a second extraction after child exhaustion.
        # Both stages, child failures and calls below come from the real pipeline.
        self.later_object_ids = True
        later = process_sources(self.repo, self.paths, model_call=self.adapter.for_operation(operation),
            related_runs=[], run_id=later_run, review_strategy="decomposed-v1",
            batch_context=projection["batch_context"], source_views=projection["source_views"])
        self.assertEqual(later["status"], "partial", later)
        counter = production._read(git, counter_path, "production_attempts")
        counter["attempts"].append({"attempt": 2, "operation_id": operation,
            "run_id": later_run, "status": "partial", "error": later.get("error")})
        with git.lock():
            head = production._save(git, counter_path, counter, "production_attempts", git.check())
            state.update(status="partial", active_run_id=later_run, last_result=later,
                         revision=state["revision"] + 1)
            production._save(git, state_path, state, "production_queue", head)
        self.assertEqual(self.roles, ["extract"] + ["review"] * 4 + ["extract", "review"])
        stages = {"memory/staging/" + item["run_id"] + ".md":
                  (self.repo / ("memory/staging/" + item["run_id"] + ".md")).read_bytes()
                  for item in counter["attempts"]}
        for status in ("partial", "reserved"):
            with self.subTest(later_attempt_status=status):
                if status == "reserved":
                    # A crash could leave the parent's reservation behind while
                    # its immutable stage/known child failure were already saved.
                    counter["attempts"][-1]["status"] = status
                    with git.lock():
                        production._save(git, counter_path, counter, "production_attempts", git.check())
                before = self.frozen_accounting()
                result = self.run_slice()
                self.assertEqual(result["status"], "attempt_limit", result)
                self.assertNotEqual(result["next_action"], "continue", result)
                self.assertEqual(self.frozen_accounting(), before)
                self.assertEqual({path: (self.repo / path).read_bytes() for path in stages}, stages)
                self.assert_no_formal()


if __name__ == "__main__":
    unittest.main()
