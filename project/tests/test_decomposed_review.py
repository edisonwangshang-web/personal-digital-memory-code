"""Frozen Review obligations, native audit identities and recovery; artificial data."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_decomposed_pipeline as fixtures
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.contracts import CaptureError, timestamp
from pdm.decomposed_review import (COUNTERS, _hash, _layout, _read, run_decomposed_review,
                                   verify_decomposed_review)
from pdm.pipeline import (_candidate_binding, _call_metadata, _document, _load, _load_sources,
                          _objects, _source_fields, execution_versions)
from pdm.production import _guarded
from pdm.transaction import PipelineGit


class DecomposedReviewTests(unittest.TestCase):
    setUp = fixtures.DecomposedPipelineTests.setUp
    sources = fixtures.DecomposedPipelineTests.sources
    prepare = fixtures.DecomposedPipelineTests.prepare
    model = fixtures.DecomposedPipelineTests.model

    def freeze(self):
        paths, _, projection = self.prepare()
        self.git = PipelineGit(self.repo)
        self.inputs = _load_sources(self.repo, paths, max_sources=256)
        versions = execution_versions(self.git)
        self.payload = {"sources": projection["source_views"], "batch_context": projection["batch_context"],
                        "domain_policy": {}, "rules_version": versions["rules_version"]}
        response = self.model("extract", self.payload)
        run, created = "run-" + "a" * 32, timestamp()
        self.candidate = {"run_id": run, "base_head": self.git.head(), **_source_fields(self.inputs),
            "versions": versions, "objects": _objects(response["output"], self.inputs, run,
                response, versions, created, batch=True), "related": [],
            "source_views": projection["source_views"], "batch_context": projection["batch_context"],
            "extraction": _call_metadata(response), "extraction_output": response["output"], "created_at": created}
        self.candidate["binding"] = _candidate_binding(self.candidate)
        self.stage_path = "memory/staging/" + run + ".md"
        with self.git.lock():
            self.stage = self.git.write_files({self.stage_path: _document("candidate", self.candidate, "0.8")},
                                              self.git.check(), "fixture: freeze artificial candidate")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.ledger = BudgetLedger.create(Path(temporary.name) / "audit.json", None, 0,
                                         "synthetic-decomposed-review", repo=self.repo, mode="audit")
        self.adapter = BudgetedModelCall(self.ledger, "fixture", lambda role, payload, timeout=120: self.model(role, payload))

    def run_review(self, adapter=None):
        with self.git.lock():
            return run_decomposed_review(self.git, self.candidate, self.stage, self.payload, adapter or self.adapter)

    def verify(self, result):
        return verify_decomposed_review(self.git, self.candidate,
            {"decomposed_proof": result["proof"], "call": result["call"], "output": result["output"]},
            self.payload, self.inputs)

    def counters(self):
        paths = self.git.text("ls-tree", "-r", "--name-only", "HEAD", "--", COUNTERS).splitlines()
        return [_load((self.repo / path).read_bytes(), "decomposed_review_attempts", "0.8") for path in paths]

    def test_complete_real_scopes_native_review_only_and_zero_call_reopen(self):
        self.freeze()
        result = self.run_review()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["attempted_calls"], 4)
        requests = self.contexts[1:]
        self.assertEqual([request["review_scope"]["kind"] for request in requests],
                         ["object", "object", "coverage", "global"])
        wiki = next(request for request in requests if any(obj["path"].startswith("memory/topics/")
                    for obj in request["candidate"]["objects"]) and request["review_scope"]["kind"] == "object")
        self.assertEqual(len(wiki["candidate"]["objects"]), 2)
        self.assertEqual(requests[-1]["candidate"]["objects"], self.candidate["objects"])
        self.assertEqual(len(requests[-1]["review_certificates"]), 3)
        self.assertGreater(len(self.verify(result)), 8)
        self.assertTrue(all(operation["reserved_roles"] == ["review"] for operation in self.ledger.snapshot()["operations"].values()))
        before = self.git.head(), self.ledger.path.read_bytes(), self.number
        reopened = self.run_review()
        self.assertEqual(reopened["proof"], result["proof"])
        self.assertEqual(reopened["attempted_calls"], 0)
        self.assertEqual((self.git.head(), self.ledger.path.read_bytes(), self.number), before)

    def test_real_nonpassed_node_cannot_be_promoted_to_complete(self):
        self.freeze()
        self.verdict = "needs_confirmation"
        result = self.run_review()
        self.assertEqual(result["status"], "needs_confirmation")
        self.assertEqual(result["attempted_calls"], 1)
        self.assertEqual(result["output"]["verdict"], "needs_confirmation")
        self.assertTrue(self.verify(result))
        forged = deepcopy(result)
        forged["proof"]["status"] = "passed"
        forged["output"]["verdict"] = "passed"
        with self.assertRaises(CaptureError):
            self.verify(forged)

    def test_returned_malformed_outputs_are_known_failures_bounded_at_four(self):
        self.freeze()
        def invalid(role, payload, timeout=120):
            response = self.model(role, payload)
            response["output"]["verdict"] = "invalid"
            return response
        adapter = BudgetedModelCall(self.ledger, "fixture", invalid)
        for number in range(4):
            result = self.run_review(adapter)
            self.assertEqual(result["status"], "partial", result)
            self.assertEqual(result["attempted_calls"], 1)
            counter = self.counters()[0]
            self.assertEqual(len(counter["attempts"]), number + 1)
            failure = counter["attempts"][-1]["failure"]
            receipt, _ = _read(self.git, failure["path"], "decomposed_review_failure", commit=failure["commit_sha"])
            self.assertTrue(receipt["received_response"])
            self.assertEqual(receipt["attempted_calls"], 1)
            self.assertNotIn("output", receipt)
        before = self.number, self.ledger.path.read_bytes()
        self.assertEqual(self.run_review(adapter)["status"], "attempt_limit")
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 4)
        self.assertTrue(all(call["status"] == "completed" for operation in self.ledger.snapshot()["operations"].values()
                            for call in operation["calls"].values()))

    def test_returned_invalid_metadata_and_repeated_invocation_are_known(self):
        for change in ("metadata", "invocation"):
            with self.subTest(change=change):
                # Separate fixtures avoid sharing the deliberate failure identity.
                if hasattr(self, "candidate"):
                    self.doCleanups()
                    self.setUp()
                self.freeze()
                def invalid(role, payload, timeout=120):
                    response = self.model(role, payload)
                    if change == "metadata":
                        response["elapsed_seconds"] = -1
                    else:
                        response["invocation_id"] = self.candidate["extraction"]["invocation_id"]
                    return response
                result = self.run_review(BudgetedModelCall(self.ledger, "fixture", invalid))
                self.assertEqual(result["status"], "partial", result)
                self.assertEqual(self.counters()[0]["attempts"][0]["status"], "failed")

    def test_zero_start_remote_or_pause_gate_keeps_one_reservation(self):
        self.freeze()
        gates = []
        def guard(repo, phase, details):
            gates.append(details)
            raise CaptureError("publication_paused", "Artificial pause")
        paused = _guarded(self.adapter, guard, self.repo, "queue", self.candidate["run_id"])
        for _ in range(5):
            result = self.run_review(paused)
            self.assertEqual(result["status"], "partial", result)
            self.assertEqual(result["attempted_calls"], 0)
            self.assertEqual(len(self.counters()[0]["attempts"]), 1)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)
        self.assertEqual(len({gate["operation_id"] for gate in gates}), 1)
        completed = self.run_review()
        self.assertEqual(completed["status"], "passed", completed)
        self.assertEqual(completed["attempted_calls"], 4)
        self.assertTrue(self.verify(completed))

    def test_started_unknown_is_not_replayed(self):
        self.freeze()
        def interrupted(role, payload, timeout=120):
            raise KeyboardInterrupt()
        adapter = BudgetedModelCall(self.ledger, "fixture", interrupted)
        with self.assertRaises(KeyboardInterrupt):
            self.run_review(adapter)
        before = self.ledger.path.read_bytes()
        for _ in range(2):
            result = self.run_review()
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["attempted_calls"], 0)
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 1)

    def test_incomplete_or_wrong_node_cannot_authorize_publication(self):
        self.freeze()
        result = self.run_review()
        for change in ("missing_coverage", "missing_global", "wrong_hash"):
            forged = deepcopy(result)
            if change == "missing_coverage":
                forged["proof"]["nodes"].pop(-2)
            elif change == "missing_global":
                forged["proof"]["nodes"].pop()
            else:
                forged["proof"]["nodes"][0]["sha256"] = "sha256-" + "0" * 64
            with self.subTest(change=change), self.assertRaises(CaptureError):
                self.verify(forged)

    def test_unauthorized_scope_rejects_before_any_git_read(self):
        self.freeze()
        with patch.object(self.git, "read", side_effect=AssertionError("Scope must be checked first")):
            with self.assertRaises(CaptureError) as raised:
                verify_decomposed_review(self.git, self.candidate, {}, self.payload, [])
        self.assertEqual(raised.exception.code, "decomposed_scope_mismatch")

    def test_pool_change_rejected_before_any_new_call(self):
        self.freeze()
        self.verdict = "needs_confirmation"
        self.run_review()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        other = BudgetLedger.create(Path(temporary.name) / "audit.json", None, 0,
                                   "different-synthetic-authorization", repo=self.repo, mode="audit")
        with self.assertRaises(CaptureError) as raised:
            self.run_review(BudgetedModelCall(other, "different", lambda *args, **kwargs: self.fail("Must not call")))
        self.assertEqual(raised.exception.code, "decomposed_audit_identity_changed")
        self.assertEqual(other.snapshot()["started_calls"], 0)

    def test_draft_semantic_closure_is_transitive_independent_of_input_order(self):
        record = {"path": "memory/records/record.md", "metadata": {"id": "record", "content_version": "version",
                    "sources": [{"source_id": "c", "source_version": "v"}]}}
        ranges = [{"fragment_id": value, "source_id": value, "source_version": "v", "path": value} for value in "abc"]
        drafts = [{"run_id": "draft-" + pair, "batch_context": {"source_ranges": [item for item in ranges if item["fragment_id"] in pair]}}
                  for pair in ("ab", "bc")]
        payload = {"batch_context": {"source_ranges": ranges}, "prior_candidates": drafts}
        units, _ = _layout({"objects": [record]}, payload)
        self.assertEqual(units[0]["fragment_ids"], list("abc"))
        payload["prior_candidates"].reverse()
        reversed_units, _ = _layout({"objects": [record]}, payload)
        self.assertEqual(units[0], reversed_units[0])

    def test_oversized_complete_scope_blocks_without_launch_or_partial_publish(self):
        self.freeze()
        # The frozen final body is itself necessary semantic input. Enlarging
        # it cannot justify clipping it or pretending local passes suffice.
        self.candidate["objects"][0]["body"] = "Artificial final body. " * 20_000
        self.candidate["binding"] = _candidate_binding(self.candidate)
        old = (self.repo / self.stage_path).read_bytes()
        with self.git.lock():
            self.stage = self.git.write_changes({self.stage_path: _document("candidate", self.candidate, "0.8")},
                        {self.stage_path: old}, self.git.check(), "fixture: freeze oversized final body")
        result = self.run_review()
        self.assertEqual(result["status"], "blocked_capacity", result)
        self.assertEqual(result["attempted_calls"], 0)
        self.assertEqual(result["proof"]["nodes"], [])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)

    def test_later_commit_with_same_stage_blob_is_not_its_frozen_root(self):
        self.freeze()
        self.git.run("commit", "--allow-empty", "-m", "fixture unrelated later commit")
        self.stage = self.git.head()
        with self.assertRaises(CaptureError) as raised:
            self.run_review()
        self.assertEqual(raised.exception.code, "decomposed_stage_changed")
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)


if __name__ == "__main__":
    unittest.main()

