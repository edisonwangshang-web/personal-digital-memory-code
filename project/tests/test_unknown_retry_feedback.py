"""Artificial regressions for a validator-failed draft followed by an extraction timeout."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_unknown_retry
from pdm.batch_groups import verify_group_publication
from pdm.contracts import CaptureError
from pdm.pipeline import process_sources
from pdm.transaction import PipelineGit


class FailedDraftTimeoutRetryTests(unittest.TestCase):
    setUp = test_unknown_retry.ExtractTimeoutRetryTests.setUp
    state = test_unknown_retry.ExtractTimeoutRetryTests.state
    attempts = test_unknown_retry.ExtractTimeoutRetryTests.attempts
    sources = test_unknown_retry.ExtractTimeoutRetryTests.sources
    transport = test_unknown_retry.ExtractTimeoutRetryTests.transport
    prepare = test_unknown_retry.ExtractTimeoutRetryTests.prepare
    run_slice = test_unknown_retry.ExtractTimeoutRetryTests.run_slice
    accounting = test_unknown_retry.ExtractTimeoutRetryTests.accounting
    one_work = test_unknown_retry.ExtractTimeoutRetryTests.one_work
    assert_no_formal = test_unknown_retry.ExtractTimeoutRetryTests.assert_no_formal

    def failed_then_timeout(self, failed_count=1):
        self.prepare()
        self.failure_mode = "known_failure"
        for _ in range(failed_count):
            result = self.run_slice()
            self.assertEqual(result["status"], "failed", result)
        self.failure_mode = "timeout"
        result = self.run_slice()
        self.assertEqual(result["status"], "failed", result)
        rows = self.one_work()["attempts"]
        self.assertEqual(rows[-1]["status"], "unknown")
        self.assertIn("extraction_feedback_ref", rows[-1])
        self.assertEqual(rows[-1]["extraction_feedback_ref"]["source_run_id"], rows[-2]["run_id"])
        return deepcopy(rows)

    def test_second_attempt_timeout_keeps_the_first_failed_draft_for_the_third_attempt(self):
        before_rows = self.failed_then_timeout()
        original_call = deepcopy(self.ledger.operation(before_rows[-1]["operation_id"])["calls"]["extract"])
        self.failure_mode = None
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        rows = self.one_work()["attempts"]
        self.assertEqual(rows[:2], before_rows)
        self.assertEqual([row["status"] for row in rows], ["failed", "unknown", "passed"])
        self.assertEqual(rows[2]["extraction_feedback_ref"], rows[1]["extraction_feedback_ref"])
        extracts = [payload for role, payload in self.payloads if role == "extract"]
        self.assertNotIn("extraction_feedback", extracts[0])
        self.assertEqual(extracts[2]["extraction_feedback"], extracts[1]["extraction_feedback"])
        self.assertEqual(self.ledger.operation(rows[1]["operation_id"])["calls"]["extract"], original_call)
        self.assertEqual(original_call["status"], "unknown")
        self.assertEqual(self.roles, ["extract"] * 3 + ["review"])
        proof = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=self.paths)
        self.assertEqual(len(proof["files"]), 2)

    def test_third_attempt_timeout_keeps_the_latest_failed_draft_in_the_last_slot(self):
        before_rows = self.failed_then_timeout(failed_count=2)
        self.failure_mode = None
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        rows = self.one_work()["attempts"]
        self.assertEqual(rows[:3], before_rows)
        self.assertEqual([row["attempt"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(rows[3]["extraction_feedback_ref"], rows[2]["extraction_feedback_ref"])
        self.assertEqual(rows[3]["extraction_feedback_ref"]["source_run_id"], rows[1]["run_id"])
        extracts = [payload for role, payload in self.payloads if role == "extract"]
        self.assertEqual(extracts[3]["extraction_feedback"], extracts[2]["extraction_feedback"])
        self.assertEqual(self.roles, ["extract"] * 4 + ["review"])
        before = self.accounting()
        self.assertEqual(self.run_slice(steps=8)["status"], "published")
        self.assertEqual(self.accounting(), before)

    def test_reserved_successor_cannot_drop_or_replace_its_failed_draft_feedback(self):
        self.failed_then_timeout()
        self.failure_mode = None
        invocations = []

        def stop_before_started(repo, paths, **options):
            if options.get("_preflight_only"):
                return process_sources(repo, paths, **options)
            invocations.append((list(paths), dict(options)))
            raise KeyboardInterrupt("artificial feedback reservation before model launch")

        with patch("pdm.pipeline.process_sources", side_effect=stop_before_started):
            with self.assertRaises(KeyboardInterrupt):
                self.run_slice()
        self.assertEqual(len(invocations), 1)
        paths, options = invocations[0]
        self.assertIn("extraction_feedback_ref", options)
        changed_ref = deepcopy(options["extraction_feedback_ref"])
        changed_ref["source_run_id"] = "run-" + "f" * 32
        for feedback in (None, changed_ref):
            with self.subTest(feedback=feedback):
                changed = {**options, "extraction_feedback_ref": feedback, "expected_head": PipelineGit(self.repo).head()}
                before = PipelineGit(self.repo).head(), self.accounting()
                with self.assertRaises(CaptureError) as caught:
                    process_sources(self.repo, paths, **changed)
                self.assertEqual(caught.exception.code, "timeout_retry_scope_changed")
                self.assertEqual((PipelineGit(self.repo).head(), self.accounting()), before)
        self.assertEqual(self.roles, ["extract"] * 2)
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(len(self.one_work()["attempts"]), 3)

    def test_one_known_failure_and_three_timeouts_end_without_a_fifth_attempt(self):
        initial = self.failed_then_timeout()
        for expected in ("failed", "attempt_limit"):
            result = self.run_slice()
            self.assertEqual(result["status"], expected, result)
        rows = self.one_work()["attempts"]
        self.assertEqual(rows[:2], initial)
        self.assertEqual([row["status"] for row in rows], ["failed"] + ["unknown"] * 3)
        self.assertTrue(all(row["extraction_feedback_ref"] == initial[-1]["extraction_feedback_ref"] for row in rows[1:]))
        before = self.accounting()
        self.assertEqual(self.run_slice(steps=8)["status"], "attempt_limit")
        self.assertEqual(self.accounting(), before)
        self.assertEqual(self.roles, ["extract"] * 4)
        self.assert_no_formal()

    def test_resuming_timeout_does_not_skip_original_completed_call_authority(self):
        initial = self.failed_then_timeout()
        self.failure_mode = None
        original_operation = self.ledger.operation

        def original_call_became_unknown(operation_id):
            result = original_operation(operation_id)
            if operation_id == initial[0]["operation_id"]:
                result["calls"]["extract"]["status"] = "unknown"
            return result

        before = self.accounting()
        with patch.object(self.ledger, "operation", side_effect=original_call_became_unknown):
            result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "blocked_context", result)
        self.assertEqual(result["last_result"]["error"], "extraction_feedback_audit_mismatch")
        self.assertEqual(self.accounting(), before)
        self.assertEqual(self.roles, ["extract"] * 2)
        self.assert_no_formal()


if __name__ == "__main__":
    unittest.main()
