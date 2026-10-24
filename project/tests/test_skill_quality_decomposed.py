"""Skill-bound decomposed Review uses artificial data in disposable Git repos."""

from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch

import test_decomposed_review as legacy
from pdm.budget import BudgetedModelCall
from pdm.contracts import CaptureError, parse_markdown
from pdm.decomposed_review import (_hash, _input_bytes, _node_identity, _plan, _quality_declarations, _read,
                                   _request, _result_node)
from pdm.pipeline import _candidate_binding, _document


class SkillQualityDecomposedTests(TestCase):
    setUp = legacy.DecomposedReviewTests.setUp
    sources = legacy.DecomposedReviewTests.sources
    prepare = legacy.DecomposedReviewTests.prepare
    run_review = legacy.DecomposedReviewTests.run_review
    verify = legacy.DecomposedReviewTests.verify
    counters = legacy.DecomposedReviewTests.counters

    def model(self, role, payload):
        response = legacy.DecomposedReviewTests.model(self, role, payload)
        if role == "extract" and "quality_context" in payload:
            records = response["output"]["objects"]
            for record in records:
                record["body"] = "At source time, " + record["body"]
            response["output"]["quality"] = {
                "coverage_disposition": [
                    {"target_id": target["target_id"], "status": "summarized",
                     "record_ids": [record["id"]],
                     "body_evidence": [{"record_id": record["id"], "text": record["body"]}],
                     "reason": "Preserves this artificial source's stated learning plan and its original time."}
                    for target in payload["quality_context"]["targets"] for record in records
                    if record["sources"][0]["source_id"] == target["source_id"]],
                "statistic_claims": [],
                "temporal_claims": [
                    {"record_id": record["id"], "statement": record["body"], "basis": "source_time",
                     "source_refs": record["sources"], "system_fact_id": None} for record in records]}
        if role == "review" and "required_checks" in payload:
            response["output"]["checks"] = [
                {"check_id": check["check_id"], "scope_sha256": check["scope_sha256"],
                 "result": self.verdict, "reason": "Checked the supplied artificial evidence.",
                 "evidence_ids": check["evidence_ids"]}
                for check in payload["required_checks"]]
        return response

    def freeze(self):
        from pdm.knowledge_quality import prepare_quality
        from pdm.skill_support import resolve_skill_refs
        legacy.DecomposedReviewTests.freeze(self)
        quality = {"quality_contract": "skill-quality-v1", "skill_bundle": resolve_skill_refs(),
                   "quality_context": prepare_quality(self.inputs, source_views=self.payload["sources"])}
        self.payload.update(deepcopy(quality))
        self.candidate.update(deepcopy(quality))
        # These tests exercise the independent proof boundary. Extraction's
        # declared dispositions are untrusted material, never a Review pass.
        self.candidate["extraction_output"]["quality"] = {
            "coverage_disposition": [], "statistic_claims": [], "temporal_claims": []}
        self.candidate["binding"] = _candidate_binding(self.candidate)
        with self.git.lock():
            old = (self.repo / self.stage_path).read_bytes()
            self.stage = self.git.write_changes(
                {self.stage_path: _document("candidate", self.candidate, "0.10")},
                {self.stage_path: old}, self.git.check(), "fixture: bind frozen Review Skill")

    def test_every_unit_has_real_skill_checks_and_rendered_binding_and_zero_call_reopen(self):
        from pdm.model_calls import request_binding
        self.freeze()
        result = self.run_review()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["proof"]["schema_version"], "0.10")
        requests = self.contexts[1:]
        self.assertEqual([request["review_scope"]["kind"] for request in requests],
                         ["object", "object", "coverage", "global"])
        for request, node in zip(requests, result["proof"]["nodes"]):
            self.assertEqual(request["skill_bundle"], self.candidate["skill_bundle"])
            self.assertTrue(request["required_checks"])
            value, raw = _read(self.git, node["path"], "decomposed_review_result")
            self.assertEqual(parse_markdown(raw)[0]["schema_version"], "0.10")
            self.assertEqual(value["request_binding"], request_binding("review", request))
            self.assertEqual({check["check_id"] for check in value["output"]["checks"]},
                             {check["check_id"] for check in request["required_checks"]})
        self.assertEqual(requests[-1]["sources"], [])
        self.assertEqual(requests[-1]["quality_context"]["sources"], [])
        self.assertEqual(requests[-1]["quality_context"]["statistics"], [])
        self.assertEqual(requests[-1]["quality_context"]["scope_mode"], "certificate_only")
        self.assertEqual(len(requests[-1]["review_certificates"]), 3)
        for certificate, node in zip(requests[-1]["review_certificates"], result["proof"]["nodes"]):
            complete, _ = _read(self.git, node["path"], "decomposed_review_result")
            self.assertEqual(certificate["certificate_contract"], "verified-quality-conclusions-v1")
            self.assertEqual(certificate["complete_output_sha256"], complete["call"]["output_sha256"])
            self.assertEqual(certificate["complete_checks_sha256"], _hash(complete["output"]["checks"]))
            self.assertEqual(certificate["output"]["reasons"], complete["output"]["reasons"])
            for projected, original in zip(certificate["output"]["checks"], complete["output"]["checks"]):
                self.assertNotIn("evidence_ids", projected)
                self.assertEqual(projected["reason"], original["reason"])
                self.assertEqual(projected["evidence_ids_sha256"], _hash(original["evidence_ids"]))
        self.assertTrue(self.verify(result))
        before = self.git.head(), self.ledger.path.read_bytes(), self.number
        with patch("pdm.decomposed_review._prompt_metadata", side_effect=AssertionError("Frozen Skill required")):
            reopened = self.run_review()
            self.assertTrue(self.verify(reopened))
        self.assertEqual(reopened["attempted_calls"], 0)
        self.assertEqual(reopened["proof"], result["proof"])
        self.assertEqual((self.git.head(), self.ledger.path.read_bytes(), self.number), before)

    def test_full_quality_pipeline_publishes_only_complete_proof_and_verifies_history(self):
        from pdm.pipeline import _load, process_sources, publish, verify_publication
        from pdm.skill_support import resolve_skill_refs
        from pdm.transaction import PipelineGit
        paths, _, projection = self.prepare(2)
        result = process_sources(self.repo, paths, related_runs=[], model_call=self.model,
            batch_context=projection["batch_context"], source_views=projection["source_views"],
            review_strategy="decomposed-v1", skill_bundle=resolve_skill_refs())
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(self.roles, ["extract"] + ["review"] * 6)
        for request in self.contexts[1:]:
            declared = request["candidate"]["quality"]
            selected_ids = {obj["metadata"]["id"] for obj in request["candidate"]["objects"]
                            if obj["path"].startswith("memory/records/")}
            self.assertTrue(all(claim["record_id"] in selected_ids
                                for claim in declared["temporal_claims"]))
            self.assertTrue(all("quote" not in ref for claim in declared["temporal_claims"]
                                for ref in claim["source_refs"]))
            if request["review_scope"]["kind"] == "object":
                provided = {view["metadata"]["source_id"] for view in request["sources"]}
                self.assertEqual(len(provided), 1)
                self.assertEqual({profile["source_id"] for profile in request["quality_context"]["sources"]},
                                 provided)
                self.assertTrue(all(target["source_id"] in provided
                                    for target in request["quality_context"]["targets"]))
        review = _load((self.repo / result["review_path"]).read_bytes(), "review")
        self.assertEqual(review["decomposed_proof"]["schema_version"], "0.10")
        self.assertFalse((self.repo / "memory/records").exists())
        published = publish(self.repo, result["run_id"], result["head"])
        verified = verify_publication(self.repo, result["run_id"], source_paths=paths)
        self.assertEqual(verified["commit_sha"], published["commit_sha"])
        self.assertEqual(len(verified["files"]), 4)
        head, number = PipelineGit(self.repo).head(), self.number
        with patch("pdm.decomposed_review._prompt_metadata", side_effect=AssertionError("Frozen Skill required")):
            self.assertEqual(verify_publication(self.repo, result["run_id"], source_paths=paths)["commit_sha"],
                             published["commit_sha"])
        self.assertEqual((PipelineGit(self.repo).head(), self.number), (head, number))

    def test_declaration_projection_cannot_reintroduce_other_object_quotes_or_whole_statistics(self):
        paths = ["memory/records/first.md", "memory/records/second.md"]
        quality = {"coverage_disposition": [], "statistic_claims": [], "temporal_claims": []}
        objects = []
        for number, path in enumerate(paths):
            record_id, target_id, stat_id = f"record-{number}", f"target-{number}", f"stat-{number}"
            ref = {"source_id": f"source-{number}", "source_version": "fixed", "locator": "L1-L1",
                   "quote": f"UNLISTED_PRIVATE_SOURCE_QUOTE_{number}"}
            objects.append({"path": path, "metadata": {"id": record_id}})
            quality["coverage_disposition"].append({
                "target_id": target_id, "status": "summarized", "record_ids": [record_id],
                "body_evidence": [{"record_id": record_id, "text": f"Summary {number}"}],
                "reason": f"Mapped summary {number}"})
            quality["statistic_claims"].append({
                "stat_id": stat_id, "record_id": record_id, "value": str(number),
                "body_evidence": f"Summary {number}", "sources": [deepcopy(ref)]})
            quality["temporal_claims"].append({
                "record_id": record_id, "statement": f"At source time summary {number}",
                "basis": "source_time", "source_refs": [deepcopy(ref)], "system_fact_id": None})
        candidate = {"objects": objects, "extraction_output": {"quality": quality}}
        material = {"targets": [{"target_id": "target-0"}], "statistics": [{"stat_id": "stat-0"}]}
        unit = {"kind": "object", "object_paths": paths[:1], "fragment_ids": ["fragment-0"]}
        projected, scope = _quality_declarations(candidate, unit, material)
        encoded = json.dumps(projected)
        self.assertNotIn("record-1", encoded)
        self.assertNotIn("Summary 1", encoded)
        self.assertNotIn("stat-1", encoded)
        self.assertNotIn("UNLISTED_PRIVATE_SOURCE_QUOTE", encoded)
        self.assertEqual(projected["statistic_claims"][0]["stat_id"], "stat-0")
        self.assertEqual(scope["basis"], "actual_supplied_source_fragments")
        partial, partial_scope = _quality_declarations(candidate, unit, {**material, "statistics": []})
        self.assertEqual(partial["statistic_claims"], [])
        self.assertEqual(partial_scope["omitted_statistic_claim_ids"], ["stat-0"])
        global_result, global_scope = _quality_declarations(candidate,
            {"kind": "global", "object_paths": paths, "fragment_ids": []},
            {"targets": [{"target_id": f"target-{n}"} for n in range(2)], "statistics": []})
        self.assertEqual(len(global_result["statistic_claims"]), 2)
        self.assertNotIn("UNLISTED_PRIVATE_SOURCE_QUOTE", json.dumps(global_result))
        self.assertEqual(global_scope["basis"], "preceding_review_certificates")
        self.assertEqual(global_scope["fragment_ids"], [])

    def test_missing_check_is_failed_before_certificate_or_publication(self):
        self.freeze()
        def invalid(role, payload, timeout=120):
            response = self.model(role, payload)
            response["output"]["checks"].pop()
            return response
        result = self.run_review(BudgetedModelCall(self.ledger, "fixture", invalid))
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(result["attempted_calls"], 1)
        self.assertEqual(result["proof"]["nodes"], [])
        self.assertEqual(self.counters()[0]["attempts"][0]["status"], "failed")
        self.assertFalse((self.repo / "memory/records").exists())

    def test_check_scope_cannot_be_borrowed_from_another_unit(self):
        self.freeze()
        def invalid(role, payload, timeout=120):
            response = self.model(role, payload)
            response["output"]["checks"][0]["scope_sha256"] = "sha256-" + "0" * 64
            return response
        result = self.run_review(BudgetedModelCall(self.ledger, "fixture", invalid))
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(result["proof"]["nodes"], [])

    def test_frozen_skill_tampering_and_partial_contract_fail_before_call(self):
        self.freeze()
        before = self.git.head(), self.ledger.path.read_bytes(), self.number
        original_candidate, original_payload = deepcopy(self.candidate), deepcopy(self.payload)
        for change in ("hash", "missing_bundle", "missing_contract", "missing_quality_output"):
            with self.subTest(change=change):
                self.candidate, self.payload = deepcopy(original_candidate), deepcopy(original_payload)
                if change == "hash":
                    for value in (self.candidate, self.payload):
                        value["skill_bundle"]["sha256"] = "sha256-" + "0" * 64
                elif change == "missing_quality_output":
                    self.candidate["extraction_output"].pop("quality")
                else:
                    key = "skill_bundle" if change == "missing_bundle" else "quality_contract"
                    self.candidate.pop(key)
                    self.payload.pop(key)
                self.candidate["binding"] = _candidate_binding(self.candidate)
                with self.assertRaises(CaptureError):
                    self.run_review()
                self.assertEqual((self.git.head(), self.ledger.path.read_bytes(), self.number), before)

    def test_capacity_counts_frozen_skill_not_only_candidate_json(self):
        from pdm.model_calls import request_binding
        self.freeze()
        plan = _plan(self.git, self.candidate, self.stage, self.payload)
        request = _request(plan, plan["units"][0], self.candidate, self.payload)
        size = request_binding("review", request)["bytes"]
        self.assertEqual(_input_bytes(request, plan["prompt"]), size)
        with patch("pdm.model_calls.MAX_INPUT_BYTES", size - 1):
            result = self.run_review()
        self.assertEqual(result["status"], "blocked_capacity", result)
        self.assertEqual(result["attempted_calls"], 0)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)

    def _assert_later_unit_blocks_before_first_review(self, kind):
        from pdm.model_calls import request_binding
        self.freeze()
        if kind == "coverage":
            # Coverage must retain the complete earlier draft, which object
            # units do not consume. Make that genuine extra obligation large
            # enough for coverage alone to exceed the test's byte limit.
            draft_objects = deepcopy(self.candidate["objects"])
            draft_objects[0]["body"] += "\n" + "Earlier synthetic learning draft context. " * 200
            draft = {"run_id": "run-" + "b" * 32, "binding": _hash(draft_objects),
                     "objects": draft_objects, "review_id": "review-" + "c" * 32,
                     "batch_context": deepcopy(self.candidate["batch_context"])}
            self.payload["prior_candidates"] = [draft]
            self.candidate["merge_inputs"] = [deepcopy(draft)]
            self.candidate["binding"] = _candidate_binding(self.candidate)
            with self.git.lock():
                old = (self.repo / self.stage_path).read_bytes()
                self.stage = self.git.write_changes(
                    {self.stage_path: _document("candidate", self.candidate, "0.10")},
                    {self.stage_path: old}, self.git.check(), "fixture: complete earlier draft context")
        plan = _plan(self.git, self.candidate, self.stage, self.payload)
        requests = [_request(plan, unit, self.candidate, self.payload)
                    for unit in plan["units"] if unit["kind"] != "global"]
        index = next(index for index, request in enumerate(requests)
                     if (request["review_scope"]["kind"] == "coverage" if kind == "coverage" else
                         request["review_scope"]["kind"] == "object"
                         and any(path.startswith("memory/topics/")
                                 for path in request["review_scope"]["object_paths"])))
        sizes = [request_binding("review", request)["bytes"] for request in requests]
        limit = sizes[index] - 1
        self.assertGreater(index, 0)
        self.assertTrue(all(size <= limit for size in sizes[:index]), sizes)
        before = self.ledger.path.read_bytes(), self.number, (self.repo / self.stage_path).read_bytes()
        with patch("pdm.model_calls.MAX_INPUT_BYTES", limit):
            result = self.run_review()
        self.assertEqual(result["status"], "blocked_capacity", result)
        self.assertEqual(result["error"], "decomposed_input_limit")
        self.assertEqual(result["attempted_calls"], 0)
        self.assertEqual(result["proof"]["nodes"], [])
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)
        self.assertEqual(self.counters(), [])
        self.assertEqual((self.ledger.path.read_bytes(), self.number,
                          (self.repo / self.stage_path).read_bytes()), before)
        saved, _ = _read(self.git, result["proof"]["plan_path"], "decomposed_review_plan")
        self.assertEqual(saved, plan)
        untouched = (self.git.head(), self.ledger.path.read_bytes(), self.number,
                     (self.repo / self.stage_path).read_bytes(),
                     (self.repo / result["proof"]["plan_path"]).read_bytes())
        with patch("pdm.model_calls.MAX_INPUT_BYTES", limit):
            reopened = self.run_review()
        self.assertEqual(reopened, result)
        self.assertEqual(self.counters(), [])
        self.assertEqual((self.git.head(), self.ledger.path.read_bytes(), self.number,
                          (self.repo / self.stage_path).read_bytes(),
                          (self.repo / result["proof"]["plan_path"]).read_bytes()), untouched)

    def test_fresh_plan_checks_later_wiki_capacity_before_first_review(self):
        self._assert_later_unit_blocks_before_first_review("wiki")

    def test_fresh_plan_checks_later_coverage_capacity_before_first_review(self):
        self._assert_later_unit_blocks_before_first_review("coverage")

    def test_result_rebuild_rejects_valid_looking_but_wrong_checks(self):
        self.freeze()
        result = self.run_review()
        node = result["proof"]["nodes"][0]
        plan, _ = _read(self.git, result["proof"]["plan_path"], "decomposed_review_plan")
        unit = plan["units"][0]
        request = _request(plan, unit, self.candidate, self.payload)
        saved, _ = _read(self.git, node["path"], "decomposed_review_result")
        reservation, _ = _read(self.git, saved["reservation"]["path"], "decomposed_review_reservation")
        # Simulate a consistently rehashed forged result, so the gate must
        # reject the checklist itself, not merely a stale byte digest.
        from pdm.pipeline import _call_metadata
        saved["output"]["checks"][0]["scope_sha256"] = "sha256-" + "0" * 64
        saved["call"] = _call_metadata({**saved["call"], "output": saved["output"]})
        actual_read = _read
        def forged_read(git, path, kind, **kwargs):
            if path == node["path"]:
                return saved, b"forged artificial result"
            return actual_read(git, path, kind, **kwargs)
        with patch("pdm.decomposed_review._read", side_effect=forged_read):
            with self.assertRaises(CaptureError):
                _result_node(self.git, plan, unit, reservation, node["path"], request)

    def test_unknown_call_is_not_replayed_and_skill_does_not_clear_identity(self):
        self.freeze()
        plan = _plan(self.git, self.candidate, self.stage, self.payload)
        identity = _node_identity(plan, plan["units"][0])
        changed_plan = deepcopy(plan)
        changed_plan["skill_bundle_sha256"] = "sha256-" + "0" * 64
        changed_plan["quality_context_sha256"] = "sha256-" + "1" * 64
        self.assertEqual(_node_identity(changed_plan, changed_plan["units"][0]), identity)
        def interrupted(role, payload, timeout=120):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_review(BudgetedModelCall(self.ledger, "fixture", interrupted))
        before = self.ledger.path.read_bytes(), self.number
        result = self.run_review()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["attempted_calls"], 0)
        self.assertEqual((self.ledger.path.read_bytes(), self.number), before)

    def test_legacy_proof_stays_readable_without_loading_any_skill(self):
        legacy.DecomposedReviewTests.freeze(self)
        with patch("pdm.skill_support.validate_skill_bundle", side_effect=AssertionError("Legacy proof")):
            result = self.run_review()
            self.assertEqual(result["proof"]["schema_version"], "0.8")
            self.assertTrue(self.verify(result))
            self.assertEqual(self.run_review()["attempted_calls"], 0)
