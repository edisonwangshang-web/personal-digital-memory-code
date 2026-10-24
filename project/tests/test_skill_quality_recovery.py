"""Quality feedback survives continuation; old operator proofs cannot invent it."""

from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

import test_pipeline
from pdm.context import _pending_origin_data
from pdm.contracts import CaptureError
from pdm.known_extraction import verify_completed_extraction, verify_revalidated_extraction
from pdm.pipeline import _hash
from pdm.revision_context import (
    build_revision_context, revision_model_context,
)
from pdm.revision_resume import (
    attach_revision_resume_extraction, verify_revision_resume, verify_unstarted_revision,
)
from pdm.transaction import PipelineGit


class QualityFeedbackTests(unittest.TestCase):
    def fixture(self, quality=True):
        run = "run-" + "a" * 32
        source = {"path": "memory/sources/artificial.md", "source_id": "src-artificial",
                  "source_version": "sha256-" + "1" * 64}
        candidate = {"run_id": run, "binding": "sha256-" + "2" * 64,
                     "versions": {"rules_version": "sha256-" + "3" * 64},
                     "source": source, "objects": [{"body": "人工样例期间概览。"}],
                     "operations": {"record": "create"}, "related_runs": []}
        review = {"output": {"verdict": "needs_revision", "reasons": ["需要说明未记录日期。"]}}
        if quality:
            candidate.update(
                quality_contract="skill-quality-v1",
                skill_bundle={"sha256": "sha256-" + "4" * 64},
                quality_context={"targets": [{"target_id": "sleep-score"}]},
                extraction_output={"quality": {"coverage_disposition": [{"target_id": "sleep-score"}],
                                                "statistic_claims": [], "temporal_claims": []}})
            review["output"]["checks"] = [{"check_id": "coverage:sleep-score", "status": "failed"}]
        rejected = (candidate, [], b"candidate", "5" * 40, review, b"review", "6" * 40,
                    "sha256-" + "7" * 64)
        git = Mock()
        git.head.return_value = "8" * 40
        with patch("pdm.revision_context._read_rejected", return_value=rejected):
            context = build_revision_context(git, run, source_paths=[source["path"]])
        return context

    def test_revision_projection_preserves_prior_quality_decisions_and_review_checks(self):
        context = self.fixture()
        projected = revision_model_context(context)
        self.assertEqual(context["schema_version"], "0.10")
        self.assertEqual(projected["review_record"]["output"]["checks"],
                         context["review_output"]["checks"])
        prior = projected["candidate"]["quality_provenance"]
        self.assertEqual(prior["quality"], context["candidate"]["extraction_output"]["quality"])
        self.assertEqual(prior["quality_context"], context["candidate"]["quality_context"])
        self.assertEqual(prior["skill_bundle_sha256"], context["candidate"]["skill_bundle"]["sha256"])
        # The prior rules are identified as feedback, never installed as the new
        # role's instructions, and callers cannot mutate the frozen decisions.
        self.assertNotIn("skill_bundle", projected["candidate"])
        prior["quality"]["coverage_disposition"].clear()
        self.assertTrue(context["candidate"]["extraction_output"]["quality"]["coverage_disposition"])

    def test_legacy_feedback_keeps_original_schema_and_projection(self):
        context = self.fixture(quality=False)
        self.assertEqual(context["schema_version"], "0.9")
        projected = revision_model_context(context)
        self.assertNotIn("quality_provenance", projected["candidate"])
        self.assertEqual(set(projected["candidate"]), {"run_id", "binding", "versions", "objects", "operations"})

    def test_quality_feedback_cannot_be_downgraded_or_omit_coverage(self):
        original = self.fixture()
        for mode in ("schema", "projection"):
            changed = deepcopy(original)
            if mode == "schema":
                changed["schema_version"] = "0.9"
                material = {key: value for key, value in changed.items() if key not in {"binding", "model_context"}}
                changed["binding"] = _hash(material)
            else:
                del changed["model_context"]["candidate"]["quality_provenance"]
            with self.assertRaises(CaptureError):
                revision_model_context(changed)

    def test_quality_confirmation_requires_complete_proof_even_for_ordinary_review(self):
        context = self.fixture()
        candidate = deepcopy(context["candidate"])
        candidate["objects"] = []
        candidate["review_strategy"] = "legacy-v1"
        review = deepcopy(context["review_record"])
        review.update(call={"invocation_id": "model-" + "9" * 32})
        review["output"]["verdict"] = "needs_confirmation"
        # A root "checks" list alone is not proof: full candidate/request/node
        # validation must happen before this question can drive a continuation.
        with (patch("pdm.pipeline._committed", return_value=b"fixed"),
              patch("pdm.pipeline._load", side_effect=[candidate, review]),
              patch("pdm.pipeline._call_metadata", return_value=review["call"]),
              patch("pdm.pipeline._last_commit", return_value="1" * 40),
              patch("pdm.pipeline._read_reviewed",
                    side_effect=CaptureError("invalid_quality_proof", "人工验证拒绝")) as verifier):
            with self.assertRaises(CaptureError) as raised:
                _pending_origin_data(Mock(), {}, candidate["run_id"])
            self.assertEqual(raised.exception.code, "invalid_quality_proof")
            verifier.assert_called_once()


class QualityOperatorRecoveryBoundaryTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp

    def test_old_zero_start_proof_cannot_infer_skill_and_has_zero_calls_or_writes(self):
        git = PipelineGit(self.repo)
        before = git.head(), git.run("status", "--porcelain"), self.number
        transport = Mock(side_effect=AssertionError("Recovery must not call a model"))
        candidate = {"quality_contract": "skill-quality-v1", "skill_bundle": {"sha256": "not-proof"}}
        with patch("pdm.revision_resume._adapter", side_effect=AssertionError("Do not even open old ledger")):
            with self.assertRaises(CaptureError) as raised:
                verify_unstarted_revision(git, "run-" + "a" * 32, {"candidate": candidate},
                    model_call=transport, source_paths=[self.saved["path"]], related_runs=[],
                    reservation={}, expected_reservation_sha256="0" * 64)
        self.assertEqual(raised.exception.code, "skill_quality_recovery_attestation_required")
        transport.assert_not_called()
        self.assertEqual((git.head(), git.run("status", "--porcelain"), self.number), before)

    def test_old_completed_response_proof_cannot_promote_quality_and_is_read_only(self):
        git = PipelineGit(self.repo)
        before = git.head(), git.run("status", "--porcelain"), self.number
        transport = Mock(side_effect=AssertionError("Recovery must not call a model"))
        with patch("pdm.pipeline._budget_adapter", side_effect=AssertionError("Do not open old ledger")):
            with self.assertRaises(CaptureError) as raised:
                verify_completed_extraction(git, "run-" + "a" * 32,
                    model_call=transport, source_paths=[self.saved["path"]], related_runs=[],
                    attestation={}, expected_attestation_sha256="0" * 64,
                    original_payload={"quality_contract": "skill-quality-v1"}, response={},
                    original_resume_context={})
        self.assertEqual(raised.exception.code, "skill_quality_recovery_attestation_required")
        transport.assert_not_called()
        self.assertEqual((git.head(), git.run("status", "--porcelain"), self.number), before)

    def test_partial_quality_fields_cannot_enter_legacy_historical_recovery(self):
        git = PipelineGit(self.repo)
        for field in ("quality_contract", "skill_bundle", "quality_context", "extraction_request_binding"):
            candidate = {field: None}
            for verifier in (verify_revision_resume, verify_revalidated_extraction):
                with self.subTest(field=field, verifier=verifier.__name__):
                    with self.assertRaises(CaptureError) as raised:
                        verifier(git, candidate, historical=True)
                    self.assertEqual(raised.exception.code, "skill_quality_recovery_attestation_required")
            with self.assertRaises(CaptureError) as raised:
                attach_revision_resume_extraction(None, {}, {}, candidate)
            self.assertEqual(raised.exception.code, "skill_quality_recovery_attestation_required")


if __name__ == "__main__":
    unittest.main()
