"""Original Git/Review evidence, not synthetic feedback, authorizes revision."""

from copy import deepcopy
from unittest.mock import patch
import unittest

import test_pipeline
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.pipeline import process_sources
from pdm.revision_context import build_revision_context, verify_revision_context, revision_model_context
from pdm.transaction import PipelineGit


class RevisionContextTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    model = test_pipeline.PipelineTests.model
    assert_error = test_pipeline.PipelineTests.assert_error
    commit_change = test_pipeline.PipelineTests.commit_change

    def rejected(self, verdict="needs_revision", **kwargs):
        self.verdict = verdict
        result = process_sources(self.repo, [self.saved["path"]], model_call=self.model,
                                 related_runs=[], **kwargs)
        self.assertEqual(result["status"], verdict, result)
        return result

    def build(self, result):
        return build_revision_context(PipelineGit(self.repo), result["run_id"],
                                      source_paths=[self.saved["path"]], related_runs=[])

    def test_real_rejected_computation_yields_complete_feedback_and_authority_without_calls(self):
        result = self.rejected()
        before = PipelineGit(self.repo).head(), self.number
        context = self.build(result)
        self.assertEqual(context["candidate"]["run_id"], result["run_id"])
        self.assertEqual(context["review_record"]["output"], context["review_output"])
        self.assertEqual(context["review_output"]["verdict"], "needs_revision")
        projection = revision_model_context(context)
        self.assertEqual(projection["candidate"]["objects"], context["candidate"]["objects"])
        self.assertEqual(projection["review_record"], context["review_record"])
        self.assertEqual(projection["context_binding"], context["binding"])
        self.assertNotIn("source_views", projection["candidate"])
        self.assertNotIn("extraction_output", projection["candidate"])
        snapshots = verify_revision_context(PipelineGit(self.repo), context,
            source_paths=[self.saved["path"]], related_runs=[])
        self.assertTrue({context["stage"]["path"], context["review"]["path"],
                         self.saved["path"], context["source_bindings"][0]["receipt_path"]}
                        <= {entry["path"] for entry in snapshots})
        self.assertEqual((PipelineGit(self.repo).head(), self.number), before)
        self.assertEqual(list(self.repo.glob("memory/records/**/*.md")), [])

    def test_source_scope_is_rejected_before_opening_any_original_body(self):
        result = self.rejected()
        other = capture(self.repo, {"source_key": "fixture:unselected-revision", "source_type": "note",
            "acquisition_status": "full_text", "body": "Unselected artificial material.\n"})["path"]
        with patch("pdm.pipeline._load_sources", side_effect=AssertionError("unauthorized original opened")):
            self.assert_error("revision_source_mismatch", lambda: build_revision_context(
                PipelineGit(self.repo), result["run_id"], source_paths=[other], related_runs=[]))

    def test_modified_feedback_or_pointer_cannot_be_verified(self):
        context = self.build(self.rejected())
        for key in ("review_output", "review_record", "stage", "source_bindings"):
            changed = deepcopy(context)
            if key == "review_output":
                changed[key]["reasons"] = ["Invented feedback."]
            elif key == "review_record":
                changed[key]["call"]["model"] = "invented"
            elif key == "stage":
                changed[key]["sha256"] = "sha256-" + "0" * 64
            else:
                changed[key][0]["source_version"] = "sha256-" + "0" * 64
            self.assert_error("revision_context_mismatch", lambda: verify_revision_context(
                PipelineGit(self.repo), changed, source_paths=[self.saved["path"]], related_runs=[]))

    def test_actual_passed_and_confirmation_review_do_not_authorize_revision(self):
        for verdict in ("passed", "needs_confirmation"):
            result = self.rejected(verdict)
            self.assert_error("revision_requires_needs_revision", lambda: self.build(result))

    def test_changed_stored_review_is_not_accepted_as_operator_feedback(self):
        result = self.rejected()
        path = result["review_path"]
        self.commit_change(path, (self.repo / path).read_bytes().replace(b'"needs_revision"', b'"passed"'))
        with self.assertRaises(CaptureError):
            self.build(result)

    def test_related_scope_cannot_be_changed_to_introduce_unverified_context(self):
        result = self.rejected()
        with patch("pdm.pipeline._load_sources", side_effect=AssertionError("body read before related authorization")):
            self.assert_error("revision_related_mismatch", lambda: build_revision_context(
                PipelineGit(self.repo), result["run_id"], source_paths=[self.saved["path"]],
                related_runs=["run-" + "f" * 32]))


if __name__ == "__main__":
    unittest.main()
