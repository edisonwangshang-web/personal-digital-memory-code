"""A recovered synthetic Review is selected without rewriting its failed row."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_quality_completed_rebuild as fixtures
from test_pipeline import git
from pdm import pipeline
from pdm.batching import project_batch_sources
from pdm.contracts import CaptureError
from pdm.production import (
    _hash, _read, _revision_feedback, _save, prepare_production, run_production,
)


class RecoveredReviewFeedbackTests(unittest.TestCase):
    setUp = fixtures.CompletedRebuildTests.setUp
    native = fixtures.CompletedRebuildTests.native
    projection = fixtures.CompletedRebuildTests.projection
    prepare = fixtures.CompletedRebuildTests.prepare
    recover = fixtures.CompletedRebuildTests.recover

    def rejected_recovery(self):
        self.phase = "reject"
        result = self.recover()
        self.assertEqual(result["status"], "needs_revision", result)
        return result

    def queue_and_counter(self):
        prepare_production(self.repo, "recovered-feedback", [self.saved["path"]],
            allowed_run_ids=[], rebuild_context=self.rebuild, skill_bundle=self.bundle,
            review_strategy="auto", max_input_bytes=240000)
        state = _read(self.git, "memory/runs/processes/queues/recovered-feedback/state.md", "production_queue")
        projection = project_batch_sources(self.repo, state["plan"], state["plan"]["batches"][0]["batch_id"],
                                           source_paths=[self.saved["path"]])
        identity = {"fragments": sorted(item["fragment_id"] for item in projection["batch_context"]["source_ranges"]),
                    "phase": "final", "related_runs": [], "rebuild_context": self.rebuild}
        counter = {"identity": identity, "quality_contract": "skill-quality-v1", "schema_version": "0.12",
            "attempts": [
                {"attempt": 1, "run_id": self.rejected["run_id"], "operation_id": "artificial:rebuild:first",
                 "status": "needs_revision", "error": None},
                {"attempt": 2, "run_id": self.failed["run_id"], "operation_id": self.operation,
                 "status": "failed", "error": "quality_temporal_mismatch"}]}
        key = _hash(identity)
        path = "memory/runs/processes/queue-attempts/" + key + ".md"
        with self.git.lock():
            _save(self.git, path, counter, "production_attempts", self.git.check())
        return state, projection, path, deepcopy(counter), key

    def feedback(self, state, projection, counter):
        return _revision_feedback(self.git, counter["attempts"], state, projection,
                                  [self.saved["path"]], draft=False, merge_runs=())

    def test_worker_third_attempt_uses_latest_real_v2_review_and_preserves_both_rows(self):
        self.rejected_recovery()
        state, projection, path, original, key = self.queue_and_counter()
        original_process = pipeline.process_sources
        observed = []
        def stop_before_new_model(*args, **kwargs):
            if kwargs.get("_preflight_only"):
                return original_process(*args, **kwargs)
            observed.append(deepcopy({name: value for name, value in kwargs.items() if name != "model_call"}))
            return {"run_id": kwargs["run_id"], "status": "failed", "error": "artificial_stop_before_model",
                    "attempted_calls": 0, "model_calls": 0}
        before_calls, audit = self.number, self.ledger_path.read_bytes()
        with patch("pdm.pipeline.process_sources", side_effect=stop_before_new_model):
            result = run_production(self.repo, "recovered-feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["revision_from_run"], self.failed["run_id"])
        self.assertEqual(observed[0]["run_id"], "run-" + _hash({"work": key, "attempt": 3})[:32])
        updated = _read(self.git, path, "production_attempts")
        self.assertEqual(updated["attempts"][:2], original["attempts"])
        self.assertEqual(len(updated["attempts"]), 3)
        self.assertEqual(updated["attempts"][2]["revision_from_run"], self.failed["run_id"])
        self.assertEqual((self.number, self.ledger_path.read_bytes()), (before_calls, audit))

    def test_v2_stage_without_actual_review_blocks_instead_of_using_older_feedback(self):
        recovered = self.rejected_recovery()
        state, projection, _, counter, _ = self.queue_and_counter()
        git(self.repo, "rm", recovered["review_path"])
        git(self.repo, "commit", "-m", "artificial missing recovered Review")
        before = self.git.head(), self.number, self.ledger_path.read_bytes()
        with self.assertRaises(CaptureError) as raised:
            self.feedback(state, projection, counter)
        self.assertEqual(raised.exception.code, "queue_revision_context_mismatch")
        self.assertEqual((self.git.head(), self.number, self.ledger_path.read_bytes()), before)

    def test_non_v2_failed_candidate_cannot_displace_verified_older_feedback(self):
        recovered = self.rejected_recovery()
        state, projection, _, counter, _ = self.queue_and_counter()
        path = self.repo / recovered["stage_path"]
        candidate = pipeline._load(path.read_bytes(), "candidate")
        candidate["revision_revalidation"]["contract"] = "quality-completed-extraction-revalidation-v1"
        path.write_bytes(pipeline._document("candidate", candidate, "0.12"))
        git(self.repo, "add", recovered["stage_path"])
        git(self.repo, "commit", "-m", "artificial non-V2 marker cannot claim recovered feedback")
        before = self.git.head(), self.number, self.ledger_path.read_bytes()
        feedback = self.feedback(state, projection, counter)
        self.assertEqual(feedback["run_id"], self.rejected["run_id"])
        self.assertEqual((self.git.head(), self.number, self.ledger_path.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
