"""Completed artificial extractions keep one consistent private diagnostic."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from pdm import pipeline
from pdm.budget import BudgetedModelCall
from pdm.contracts import CaptureError
from pdm.production import prepare_production, run_production
from test_pipeline import git
import test_related_failure_diagnostics as related


class PrecandidateFailureDiagnosticTests(unittest.TestCase):
    MARKER = related.RelatedFailureDiagnosticTests.MARKER
    DEBUG = related.RelatedFailureDiagnosticTests.DEBUG
    setUp = related.RelatedFailureDiagnosticTests.setUp
    run_failure = related.RelatedFailureDiagnosticTests.run_failure
    assert_private_diagnostic = related.RelatedFailureDiagnosticTests.assert_private_diagnostic

    def transport(self, role, payload, timeout=120):
        response = related.RelatedFailureDiagnosticTests.transport(self, role, payload, timeout)
        if role == "extract" and hasattr(self, "initial"):
            if getattr(self, "invalid_labels", False):
                response["output"]["objects"][0]["domains"] = ["learning", "invalid-domain"]
            self.responses[-1] = deepcopy(response)
            if getattr(self, "advance_head", False):
                git(self.repo, "commit", "--allow-empty", "-m", "artificial concurrent data checkpoint")
        return response

    def test_returned_domain_error_keeps_raw_materialized_output_and_exact_field(self):
        self.omit = False
        self.invalid_labels = True
        result, failure = self.run_failure("invalid-classification")
        diagnostic = self.assert_private_diagnostic(result, failure, "invalid_domains")
        self.assertEqual({"record_id": "old-z", "object_path": "memory/records/old-z.md",
                          "field_path": "/domains", "domains": ["learning", "invalid-domain"]},
                         diagnostic["validation_error"]["details"])

    def test_candidate_storage_error_keeps_return_and_precise_capacity_location(self):
        self.omit = False
        document = pipeline._document

        def over_storage_bound(kind, value, schema_version="0.2"):
            result = document(kind, value, schema_version)
            return result + b" " * (5 * 1024 * 1024) if kind == "candidate" else result

        queue = "candidate-capacity"
        prepare_production(self.repo, queue, [self.path], allowed_run_ids=[self.initial["run_id"]],
                           skill_bundle=self.bundle)
        with patch("pdm.pipeline._document", side_effect=over_storage_bound):
            result = run_production(self.repo, queue,
                model_call=BudgetedModelCall(self.ledger, queue, self.transport), max_steps=1)
        self.assertEqual("blocked_capacity", result["status"], result)
        last = result["last_result"]
        failure = pipeline._load((self.repo / last["failure_path"]).read_bytes(), "failure")
        self.assertFalse((self.repo / pipeline._stage_path(last["run_id"])).exists())
        self.assertFalse((self.repo / f"memory/runs/processes/{last['run_id']}/review.md").exists())
        self.assertEqual(["extract"], [role for role, _ in self.calls[self.initial_calls:]])
        self.assertEqual(self.formal, {path: (self.repo / path).read_bytes() for path in self.formal})
        diagnostic = self.assert_private_diagnostic(result, failure, "candidate_too_large")
        details = diagnostic["validation_error"]["details"]
        self.assertEqual("/candidate", details["field_path"])
        self.assertEqual(pipeline._stage_path(result["last_result"]["run_id"]), details["candidate_path"])
        self.assertGreater(details["candidate_bytes"], 5 * 1024 * 1024)
        self.assertEqual("candidate_too_large", details["storage_error"])
        # Historical completed-extraction recovery binds this original summary
        # shape; adding diagnostics must not silently change that contract.
        self.assertEqual({"invocation_id", "model", "elapsed_seconds"}, set(failure["call"]))

    def test_classification_diagnostic_respects_the_engineering_root_boundary(self):
        self.omit = False
        self.invalid_labels = True
        directory = self.repo / "project/src/pdm"
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("# Artificial combined checkout.\n")
        git(self.repo, "add", "project/src/pdm/__init__.py")
        git(self.repo, "commit", "-m", "artificial engineering boundary")
        result, failure = self.run_failure("combined-classification")
        self.assertEqual("invalid_domains", failure["error"])
        self.assertEqual({"status": "not_saved_engineering_root"}, failure["extraction_diagnostic"])
        text = (self.repo / result["last_result"]["failure_path"]).read_text()
        self.assertNotIn(self.MARKER, text)
        self.assertNotIn(self.DEBUG, text)

    def test_changed_head_still_propagates_without_writing_a_stale_failure(self):
        self.omit = False
        self.invalid_labels = True
        self.advance_head = True
        queue = "concurrent-classification"
        prepare_production(self.repo, queue, [self.path], allowed_run_ids=[self.initial["run_id"]],
                           skill_bundle=self.bundle)
        with self.assertRaises(CaptureError) as raised:
            run_production(self.repo, queue,
                model_call=BudgetedModelCall(self.ledger, queue, self.transport), max_steps=1)
        self.assertEqual("head_conflict", raised.exception.code)
        self.assertEqual(["extract"], [role for role, _ in self.calls[self.initial_calls:]])
        self.assertEqual([], list(self.repo.glob("memory/runs/processes/*/failure.md")))
        self.assertEqual(self.formal, {path: (self.repo / path).read_bytes() for path in self.formal})
        for operation, old in self.old_audit.items():
            self.assertEqual(old, self.ledger.snapshot()["operations"][operation])
        new_calls = [call for operation_id, operation in self.ledger.snapshot()["operations"].items()
                     if operation_id not in self.old_audit for call in operation["calls"].values()]
        self.assertEqual(["completed"], [call["status"] for call in new_calls])


if __name__ == "__main__":
    unittest.main()
