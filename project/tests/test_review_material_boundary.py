"""Current-draft Review inputs and frozen legacy requests; artificial data only."""

from copy import deepcopy
import unittest

import test_production_revision_feedback as feedback_fixtures
from engine_fixtures import isolated_engine
from pdm.batch_groups import verify_group_publication
from pdm.contracts import CaptureError
from pdm.knowledge_quality import prepare_quality
from pdm.model_calls import render_model_request, request_binding
from pdm.pipeline import (
    CURRENT_DRAFT_REVIEW_CONTRACT, _candidate_binding, _document, _load,
    _read_fixed_candidate, _read_reviewed, _review_payload,
)
from pdm.production import run_production
from pdm.revision_context import _model_projection
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit
from test_pipeline import git


def artificial_legacy_material():
    source = {"path": "memory/sources/inbox/src-" + "a" * 64 + "/sha256-" + "b" * 64 + ".md",
              "metadata": {"source_id": "src-" + "a" * 64, "source_version": "sha256-" + "b" * 64,
                           "source_type": "note", "original_time": None},
              "body": "Artificial learning observation, not a completed action.\n"}
    ref = {key: source["metadata"][key] for key in ("source_id", "source_version")}
    ref.update(locator="L1-L1", quote=source["body"].strip())
    quality = prepare_quality([source])
    bundle = resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")
    current = {"path": "memory/records/artificial-learning.md",
               "metadata": {"id": "artificial-learning", "type": "knowledge", "sources": [ref]},
               "body": "Artificial learning observation remains unperformed."}
    candidate = {"run_id": "run-" + "1" * 32, "binding": "sha256-" + "2" * 64,
                 "quality_contract": "skill-quality-v1", "skill_bundle": bundle,
                 "quality_context": quality, "objects": [current],
                 "extraction_output": {"quality": {"coverage_disposition": [],
                     "statistic_claims": [], "temporal_claims": []}},
                 "revision_context": {"candidate": {"skill_bundle": bundle,
                     "extraction_output": {"artificial_previous_output": "retained in legacy request"}}}}
    payload = {"sources": [source], "quality_contract": "skill-quality-v1",
               "skill_bundle": bundle, "quality_context": quality,
               "revision_context": {"artificial_latest_feedback": "repair the supplied-source scope"}}
    return candidate, payload


class ReviewMaterialContractTests(unittest.TestCase):
    def test_unmarked_legacy_review_keeps_its_frozen_request_bytes(self):
        candidate, payload = artificial_legacy_material()
        frozen = deepcopy((candidate, payload))
        request = _review_payload(candidate, payload)
        # Pinned with the pre-marker dev25 request builder and unchanged 10/8
        # snapshots; compatibility must not depend on the current builder.
        self.assertEqual(request_binding("review", request), {
            "sha256": "sha256-66dacd3e61a01c697be69d3111b641772a00312f03e05c1e061460e22e2cdb1b",
            "bytes": 109288})
        self.assertIn("revision_context", request["candidate"])
        self.assertIn("revision_context", request)
        self.assertEqual((candidate, payload), frozen)

    def test_marked_review_excludes_history_without_changing_saved_material(self):
        candidate, payload = artificial_legacy_material()
        candidate["review_request_contract"] = CURRENT_DRAFT_REVIEW_CONTRACT
        candidate.update(revision_revalidation={"artificial_raw_response": "private-history-only"},
                         revision_resume={"artificial_reservation": "program-proof-only"},
                         quality_revalidation_guard_ref={"artificial_ref": "program-proof-only"})
        frozen = deepcopy((candidate, payload))
        request = _review_payload(candidate, payload)
        self.assertNotIn("revision_context", request)
        for field in ("revision_context", "revision_revalidation", "revision_resume", "quality_revalidation_guard_ref"):
            self.assertNotIn(field, request["candidate"])
        self.assertEqual(request["candidate"]["objects"], candidate["objects"])
        self.assertEqual(request["sources"], payload["sources"])
        self.assertTrue(request["required_checks"])
        wire = render_model_request("review", request).decode()
        self.assertNotIn("private-history-only", wire)
        self.assertNotIn("program-proof-only", wire)
        self.assertNotIn("retained in legacy request", wire)
        self.assertEqual((candidate, payload), frozen)

    def test_unknown_marker_and_nonquality_opt_in_are_rejected(self):
        candidate, payload = artificial_legacy_material()
        for value in (None, False, "current-draft-v99"):
            with self.subTest(value=value):
                candidate["review_request_contract"] = value
                with self.assertRaises(CaptureError) as raised:
                    _review_payload(candidate, payload)
                self.assertEqual(raised.exception.code, "invalid_review_request_contract")
        with self.assertRaises(CaptureError) as raised:
            _review_payload({"review_request_contract": CURRENT_DRAFT_REVIEW_CONTRACT}, {})
        self.assertEqual(raised.exception.code, "invalid_review_request_contract")

    def test_same_marker_limits_feedback_to_unresolved_checks_and_keeps_full_history(self):
        candidate, _ = artificial_legacy_material()
        candidate["versions"] = {"program_commit": "c" * 40}
        record = {"run_id": candidate["run_id"], "review_id": "rev-" + "3" * 32,
                  "binding": candidate["binding"], "stage_path": "memory/staging/" + candidate["run_id"] + ".md",
                  "stage_commit": "d" * 40, "stage_sha256": "sha256-" + "4" * 64,
                  "call": {"artificial_accounting": "program-only"},
                  "request_binding": {"artificial_binding": "program-only"},
                  "decomposed_proof": {"artificial_certificate": "program-only"},
                  "output": {"verdict": "needs_revision", "reasons": ["Repair the actual supplied-source scope."],
                             "checks": [{"check_id": "passed-item", "result": "passed", "reason": "Already verified."},
                                        {"check_id": "open-item", "result": "needs_revision", "reason": "Clarify the scope."}]}}
        context = {"schema_version": "0.12", "contract": "review-feedback-v1",
                   "source_run_id": candidate["run_id"], "trust": "unpublished_feedback",
                   "candidate_binding": candidate["binding"], "stage": {}, "review": {},
                   "source_bindings": [], "related_runs": [], "controls_digest": "sha256-" + "5" * 64,
                   "binding": "sha256-" + "6" * 64, "candidate": candidate, "review_record": record}
        frozen = deepcopy(context)
        legacy = _model_projection(context)
        self.assertEqual(legacy["review_record"], record)
        for verdict in ("needs_revision", "needs_confirmation"):
            updated = deepcopy(context)
            updated["candidate"]["review_request_contract"] = CURRENT_DRAFT_REVIEW_CONTRACT
            updated["review_record"]["output"]["verdict"] = verdict
            updated["review_record"]["output"]["checks"][1]["result"] = verdict
            view = _model_projection(updated)
            feedback = view["review_record"]
            self.assertEqual(feedback["output"]["checks"], [updated["review_record"]["output"]["checks"][1]])
            self.assertEqual(feedback["output"]["verdict"], verdict)
            self.assertEqual(feedback["output"]["reasons"], record["output"]["reasons"])
            for field in ("call", "request_binding", "decomposed_proof"):
                self.assertNotIn(field, feedback)
                self.assertIn(field, updated["review_record"])
            self.assertEqual(view["candidate"]["quality_provenance"]["quality_context"], candidate["quality_context"])
        self.assertEqual(context, frozen)
        self.assertEqual(_model_projection(context), legacy)


class ReviewMaterialPipelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = feedback_fixtures.ProductionRevisionFeedbackTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        isolated_engine(self)
        self.repo = self.fixture.repo

    def test_three_attempts_keep_full_proof_and_only_review_current_draft(self):
        fixture = self.fixture
        fixture.reject_reviews = 2
        paths, _ = fixture.prepare("material-boundary")
        frozen = {}
        results = []
        for expected in ("needs_revision", "needs_revision", "ready"):
            result = run_production(self.repo, "material-boundary", model_call=fixture.adapter, max_steps=1)
            self.assertEqual(result["status"], expected, result)
            results.append(result)
            for field in ("stage_path", "review_path"):
                path = result["last_result"][field]
                frozen[path] = (self.repo / path).read_bytes()
        extracts = [payload for role, payload in fixture.payloads if role == "extract"]
        reviews = [payload for role, payload in fixture.payloads if role == "review"]
        self.assertEqual((len(extracts), len(reviews)), (3, 3))
        self.assertNotIn("revision_context", extracts[0])
        for extract in extracts[1:]:
            self.assertEqual(extract["revision_context"]["review_record"]["output"]["verdict"], "needs_revision")
            self.assertNotIn("skill_bundle", extract["revision_context"]["candidate"])
            feedback = extract["revision_context"]["review_record"]
            self.assertTrue(feedback["output"]["checks"])
            self.assertTrue(all(check["result"] != "passed" for check in feedback["output"]["checks"]))
            self.assertNotIn("call", feedback)
            self.assertNotIn("decomposed_proof", feedback)
        for review in reviews:
            self.assertNotIn("revision_context", review)
            self.assertNotIn("revision_context", review["candidate"])
            self.assertEqual(review["candidate"]["review_request_contract"], CURRENT_DRAFT_REVIEW_CONTRACT)
            self.assertTrue(review["candidate"]["objects"])
            self.assertTrue(review["sources"])
            self.assertTrue(review["required_checks"])
        engine = PipelineGit(self.repo)
        candidate, _, _ = _read_reviewed(engine, results[-1]["final_run"])
        self.assertEqual(candidate["extraction_request_binding"], request_binding("extract", extracts[-1]))
        previous = candidate["revision_context"]["candidate"]
        self.assertTrue(any(check["result"] == "passed"
                            for check in candidate["revision_context"]["review_record"]["output"]["checks"]))
        self.assertIn("revision_context", previous)
        self.assertIn("skill_bundle", previous["revision_context"]["candidate"])
        before_calls = fixture.number
        published = run_production(self.repo, "material-boundary", model_call=fixture.adapter, max_steps=1)
        self.assertEqual(published["status"], "published", published)
        proof = verify_group_publication(self.repo, published["publication"]["run_id"], source_paths=paths)
        authority = {item["path"] for item in proof["authority_snapshots"]}
        self.assertTrue(set(frozen) <= authority)
        self.assertEqual(fixture.number, before_calls)
        self.assertEqual(frozen, {path: (self.repo / path).read_bytes() for path in frozen})

    def test_rehashed_unknown_saved_marker_is_rejected_without_new_model_call(self):
        fixture = self.fixture
        fixture.reject_reviews = 0
        fixture.prepare("bad-marker")
        result = run_production(self.repo, "bad-marker", model_call=fixture.adapter, max_steps=1)
        path = result["last_result"]["stage_path"]
        candidate = _load((self.repo / path).read_bytes(), "candidate")
        candidate["review_request_contract"] = "current-draft-v99"
        candidate["binding"] = _candidate_binding(candidate)
        (self.repo / path).write_bytes(_document("candidate", candidate, "0.12"))
        git(self.repo, "add", path)
        git(self.repo, "commit", "-m", "artificial unknown Review material contract")
        before = fixture.number
        with self.assertRaises(CaptureError) as raised:
            _read_fixed_candidate(PipelineGit(self.repo), result["final_run"])
        self.assertEqual(raised.exception.code, "invalid_review_request_contract")
        self.assertEqual(fixture.number, before)


if __name__ == "__main__":
    unittest.main()
