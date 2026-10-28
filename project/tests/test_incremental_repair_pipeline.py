"""Exact old statistics cross the real artificial-Git pipeline under 12/10 only."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_reuse_quality as legacy_fixture
from pdm.batching import plan_batches, project_batch_sources
from pdm.contracts import markdown, parse_markdown
from pdm.model_calls import ModelCallError
from pdm.pipeline import (_candidate_binding, _hash, _load, _read_fixed_candidate,
                          _read_reviewed, process_sources, publish, verify_publication)
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class IncrementalRepairPipelineTests(unittest.TestCase):
    # Share artificial setup methods without inheriting or importing another
    # TestCase class into this module's unittest discovery namespace.
    setUp = legacy_fixture.ReuseQualityTests.setUp
    model = legacy_fixture.ReuseQualityTests.model
    refresh = legacy_fixture.ReuseQualityTests.refresh
    assert_error = legacy_fixture.ReuseQualityTests.assert_error

    def run_incremental(self, *, omit=True, refs=True, bundle=None, model=None):
        self.reuse, self.omit_claims = refs, omit
        self.roles.clear()
        self.payloads = []

        def observed(role, payload):
            self.payloads.append((role, deepcopy(payload)))
            return self.model(role, payload) if model is None else model(role, payload)

        return process_sources(self.repo, [self.saved["path"]],
            related_runs=[self.old["run_id"]], model_call=observed,
            skill_bundle=bundle or resolve_skill_refs("knowledge-extract@12", "knowledge-review@10"))

    def candidate(self, result):
        self.assertEqual(result["status"], "passed", result)
        return _load((self.repo / result["stage_path"]).read_bytes(), "candidate")

    def committed_candidate_change(self, result, mutate):
        path = result["stage_path"]
        metadata, body = parse_markdown((self.repo / path).read_bytes())
        candidate = json.loads(body)
        mutate(candidate)
        # Rebind the edited candidate so this tests inherited-proof rebuilding,
        # rather than merely exercising the ordinary document hash check.
        candidate["binding"] = _candidate_binding(candidate)
        (self.repo / path).write_bytes(markdown(metadata, json.dumps(candidate, ensure_ascii=False)))
        legacy_fixture.git(self.repo, "add", "--", path)
        legacy_fixture.git(self.repo, "commit", "-m", "artificial candidate mutation")

    def assert_inheritance_rejected(self, result, mutate):
        self.committed_candidate_change(result, mutate)
        self.assert_error("reuse_inheritance_changed", lambda: _read_fixed_candidate(
            PipelineGit(self.repo), result["run_id"], source_paths=[self.saved["path"]], historical=True))

    def test_omitted_exact_reuse_claims_reach_independent_review_publish_and_history(self):
        formal = {path: (self.repo / path).read_bytes() for path in self.publication["files"]}
        result = self.run_incremental()
        candidate = self.candidate(result)
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(candidate["raw_extraction_output"], self.outputs[-1])
        self.assertEqual(candidate["raw_extraction_output"]["quality"]["statistic_claims"], [])
        self.assertEqual(len(candidate["extraction_output"]["quality"]["statistic_claims"]), 2)
        inherited = candidate["reuse_statistic_inheritance"]
        self.assertEqual(inherited["contract"], "exact-statistic-inheritance-v1")
        self.assertEqual(inherited["controls_digest"], candidate["controls_digest"])
        self.assertEqual(len(inherited["claims"]), 2)
        self.assertEqual({item["origin"]["stage_path"] for item in inherited["claims"]}, {self.old["stage_path"]})
        self.assertEqual(candidate["extraction"]["output_sha256"], _hash(candidate["raw_extraction_output"]))
        self.assertNotEqual(candidate["extraction"]["output_sha256"], _hash(candidate["extraction_output"]))
        self.assertTrue(all(operation["action"] == "reuse" for operation in candidate["operations"].values()))
        review_request = next(payload for role, payload in self.payloads if role == "review")
        self.assertEqual(review_request["candidate"]["extraction_output"]["quality"],
                         candidate["extraction_output"]["quality"])
        self.assertNotIn("raw_extraction_output", review_request["candidate"])
        self.assertNotIn("reuse_statistic_inheritance", review_request["candidate"])
        self.assertTrue(review_request["required_checks"])
        saved = publish(self.repo, result["run_id"])
        before = PipelineGit(self.repo).head(), self.number
        with patch("pdm.model_calls.call_model", side_effect=AssertionError("history must not call a model")):
            proof = verify_publication(self.repo, result["run_id"], source_paths=[self.saved["path"]])
            historical = verify_publication(self.repo, result["run_id"], source_paths=[self.saved["path"]], historical=True)
            rebuilt, _, _ = _read_reviewed(PipelineGit(self.repo), result["run_id"], historical=True)
        self.assertEqual(proof["commit_sha"], saved["commit_sha"])
        self.assertEqual(historical["commit_sha"], saved["commit_sha"])
        self.assertEqual(rebuilt, candidate)
        self.assertEqual((PipelineGit(self.repo).head(), self.number), before)
        self.assertEqual({path: (self.repo / path).read_bytes() for path in formal}, formal)

    def test_history_rebuilds_original_inheritance_after_later_valid_content_update(self):
        result = self.run_incremental()
        original = self.candidate(result)
        publish(self.repo, result["run_id"])
        self.reuse, self.omit_claims, self.replace = False, False, True
        updated = process_sources(self.repo, [self.saved["path"]],
            related_runs=[result["run_id"]], model_call=self.model,
            # A different supported rule snapshot gives the later artificial
            # update a distinct legitimate processing key without altering or
            # deleting the original processed-input marker.
            skill_bundle=resolve_skill_refs("knowledge-extract@11", "knowledge-review@9"))
        self.assertEqual(updated["status"], "passed", updated)
        publish(self.repo, updated["run_id"])
        historical = verify_publication(self.repo, result["run_id"], historical=True)
        restored, _, _ = _read_reviewed(PipelineGit(self.repo), result["run_id"], historical=True)
        self.assertTrue(historical["historical"])
        self.assertEqual(restored["reuse_statistic_inheritance"], original["reuse_statistic_inheritance"])

    def test_raw_claim_tampering_cannot_borrow_the_materialized_proof(self):
        result = self.run_incremental()
        candidate = self.candidate(result)

        def mutate(value):
            value["raw_extraction_output"]["quality"]["statistic_claims"] = [
                deepcopy(candidate["extraction_output"]["quality"]["statistic_claims"][0])]

        self.assert_inheritance_rejected(result, mutate)

    def test_unstarted_review_reuses_actual_raw_and_rebuilds_inherited_publication_proof(self):
        plan = plan_batches(self.repo, [self.saved["path"]], max_input_bytes=20000,
                            payload_overhead_bytes=128, purpose="history")
        projected = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"],
                                          source_paths=[self.saved["path"]])
        self.reuse, self.omit_claims = True, True
        options = {"related_runs": [self.old["run_id"]],
                   "skill_bundle": resolve_skill_refs("knowledge-extract@12", "knowledge-review@10"),
                   "batch_context": projected["batch_context"], "source_views": projected["source_views"]}

        def review_never_launched(role, payload):
            if role == "review":
                # Invalid timeout is rejected before any external model start.
                raise ModelCallError("invalid_timeout", "model-" + "f" * 32)
            return self.model(role, payload)

        first = process_sources(self.repo, [self.saved["path"]], model_call=review_never_launched, **options)
        self.assertEqual((first["status"], first["error"]), ("failed", "invalid_timeout"))
        old = _load((self.repo / first["stage_path"]).read_bytes(), "candidate")
        self.assertEqual(old["raw_extraction_output"]["quality"]["statistic_claims"], [])
        self.assertEqual(len(old["extraction_output"]["quality"]["statistic_claims"]), 2)
        self.assertFalse((self.repo / ("memory/runs/processes/" + first["run_id"] + "/review.md")).exists())
        calls = []

        def only_review(role, payload):
            calls.append(role)
            self.assertEqual(role, "review", "a completed extraction must not be regenerated")
            return self.model(role, payload)

        second = process_sources(self.repo, [self.saved["path"]], model_call=only_review,
                                 reuse_extraction_run=first["run_id"], **options)
        restored = self.candidate(second)
        self.assertEqual(calls, ["review"])
        self.assertEqual(restored["raw_extraction_output"], old["raw_extraction_output"])
        self.assertEqual(restored["extraction_output"], old["extraction_output"])
        self.assertEqual(restored["extraction"], old["extraction"])
        self.assertEqual(restored["extraction_reused_from"]["run_id"], first["run_id"])
        self.assertEqual(restored["reuse_statistic_inheritance"], old["reuse_statistic_inheritance"])
        saved = publish(self.repo, second["run_id"])
        verified = verify_publication(self.repo, second["run_id"], historical=True)
        self.assertEqual(verified["commit_sha"], saved["commit_sha"])

    def test_materialized_claim_tampering_is_rejected_after_rebinding(self):
        result = self.run_incremental()
        self.candidate(result)

        def mutate(value):
            value["extraction_output"]["quality"]["statistic_claims"][0]["value"] = "999"

        self.assert_inheritance_rejected(result, mutate)

    def test_inheritance_origin_tampering_is_rejected_after_rebinding(self):
        result = self.run_incremental()
        self.candidate(result)

        def mutate(value):
            value["reuse_statistic_inheritance"]["claims"][0]["origin"]["claim_index"] += 1

        self.assert_inheritance_rejected(result, mutate)

    def test_fully_declared_new_contract_keeps_actual_output_without_inheritance_marker(self):
        candidate = self.candidate(self.run_incremental(omit=False))
        self.assertEqual(candidate["extraction_output"], self.outputs[-1])
        self.assertNotIn("raw_extraction_output", candidate)
        self.assertNotIn("reuse_statistic_inheritance", candidate)

    def test_wrong_model_supplied_value_is_preserved_and_rejected_before_review(self):
        def wrong_model(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                response["output"]["quality"]["statistic_claims"][0]["value"] = "999"
            return response

        result = self.run_incremental(omit=False, model=wrong_model)
        self.assertEqual((result["status"], result["error"]), ("failed", "quality_statistic_mismatch"))
        self.assertEqual(self.roles, ["extract"])
        failure = _load((self.repo / result["failure_path"]).read_bytes(), "failure")
        self.assertEqual(failure["extraction_diagnostic"]["response"]["output"]["quality"]["statistic_claims"][0]["value"], "999")

    def test_legacy_eleven_still_rejects_omitted_old_claims_without_review_or_writes(self):
        before = {path: (self.repo / path).read_bytes() for path in self.publication["files"]}
        result = self.run_incremental(bundle=resolve_skill_refs("knowledge-extract@11", "knowledge-review@9"))
        self.assertEqual((result["status"], result["error"]), ("failed", "reuse_statistic_claims_missing"))
        self.assertEqual(self.roles, ["extract"])
        self.assertFalse((self.repo / ("memory/staging/" + result["run_id"] + ".md")).exists())
        failure = _load((self.repo / result["failure_path"]).read_bytes(), "failure")
        self.assertEqual(failure["extraction_diagnostic"]["response"]["output"]["quality"]["statistic_claims"], [])
        self.assertEqual({path: (self.repo / path).read_bytes() for path in before}, before)


if __name__ == "__main__":
    unittest.main()
