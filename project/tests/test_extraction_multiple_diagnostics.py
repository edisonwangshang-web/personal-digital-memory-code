"""Artificial exact references and independent pre-candidate error collection."""

from copy import deepcopy
import unittest

from pdm.contracts import CaptureError
from pdm.domains import DOMAIN_POLICY_VERSION, domain_policy
from pdm.extraction_validation import collect_issues
from pdm.pipeline import _hash, _refs
from pdm.skill_support import resolve_skill_refs
import test_related_failure_diagnostics as related


class ExtractionMultipleDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.source = {"source_id": "src-" + "a" * 64, "source_version": "sha256-" + "b" * 64,
                       "path": "artificial-selected-source", "body": "Artificial first line.\n    Second line.\n"}
        self.ref = {key: self.source[key] for key in ("source_id", "source_version")}
        self.ref.update(locator="L1-L1", quote="Artificial first line.")
        self.output = {"objects": [{"id": "artificial-record", "type": "knowledge", "body": "An artificial fact.",
                                   "title": "Artificial record", "source_kind": "external_source",
                                   "valid_from": None, "valid_to": None, "verification_status": "unverified",
                                   "lifecycle_status": "undetermined",
                                   "domains": ["learning"], "sources": [deepcopy(self.ref)]}],
                       "wiki": {"id": "artificial-topic", "title": "Artificial topic", "body": "An artificial summary.",
                                "sources": [deepcopy(self.ref)]}, "reuse_refs": []}
        self.versions = {"domain_policy_version": DOMAIN_POLICY_VERSION,
                         "domain_policy_sha256": _hash(domain_policy())}

    def collect(self, **kwargs):
        return collect_issues(self.output, [self.source], versions=self.versions, **kwargs)

    def test_multiple_exact_refs_domains_and_omitted_records_are_independent(self):
        record = self.output["objects"][0]
        record["sources"] = [{**self.ref, "quote": "Wrong quote."}, {**self.ref, "locator": "wrong"}]
        record["domains"] = ["invalid-domain"]
        self.output["wiki"]["sources"][0]["quote"] = "Wrong topic quote."
        old = {"path": "memory/records/missing-old.md", "metadata": {"id": "missing-old"}}
        before = deepcopy(self.output)
        found = self.collect(related=[{"documents": [old]}], check_current=True,
                             first_error=CaptureError("invalid_quote", "First strict rejection"))
        self.assertFalse(found["issues_truncated"])
        self.assertEqual(["invalid_quote", "invalid_quote", "invalid_locator", "invalid_domains",
                          "related_records_omitted"], [issue["code"] for issue in found["issues"]])
        refs = [item for item in found["issues"] if item["code"] == "invalid_quote"]
        self.assertEqual(["/objects/0/sources/0", "/wiki/sources/0"], [item["field_path"] for item in refs])
        self.assertEqual(self.source["source_id"], refs[0]["source_id"])
        self.assertEqual(self.source["source_version"], refs[0]["source_version"])
        self.assertEqual("artificial-record", refs[0]["record_id"])
        self.assertEqual(0, refs[0]["ref_index"])
        self.assertEqual(before, self.output)

    def test_selected_fragment_boundary_is_not_replaced_by_full_source_access(self):
        view = {"metadata": {key: self.source[key] for key in ("source_id", "source_version")},
                "fragments": [{"lines": [{"line": 2, "text": "    Second line."}]}]}
        found = self.collect(source_views=[view])
        self.assertEqual(["fragment_reference_outside_view"] * 2,
                         [item["code"] for item in found["issues"]])
        self.assertEqual("/objects/0/sources/0", found["issues"][0]["field_path"])

    def test_unselected_source_stops_that_reference_without_inspecting_external_data(self):
        self.output["objects"][0]["sources"] = [
            {**self.ref, "source_id": "src-" + "c" * 64}, {**self.ref, "quote": "Wrong."}]
        found = self.collect()
        self.assertEqual(["source_reference_mismatch", "invalid_quote"],
                         [item["code"] for item in found["issues"]])
        self.assertEqual("/objects/0/sources/1", found["issues"][1]["field_path"])

    def test_issue_limit_is_explicit_and_never_looks_like_complete_repair_scope(self):
        self.output["objects"][0]["sources"] = [{**self.ref, "quote": "Wrong."} for _ in range(35)]
        found = self.collect()
        self.assertEqual(32, len(found["issues"]))
        self.assertTrue(found["issues_truncated"])

    def test_bad_quote_does_not_hide_invalid_wiki_structure_or_empty_record_set(self):
        self.output["objects"][0]["sources"][0]["quote"] = "Wrong."
        self.output["wiki"]["title"] = ""
        found = self.collect()
        self.assertEqual({"invalid_quote", "invalid_model_output"}, {item["code"] for item in found["issues"]})
        self.assertIn("/wiki/title", [item.get("field_path") for item in found["issues"]])
        self.output["objects"] = []
        self.assertIn("invalid_model_output", {item["code"] for item in self.collect()["issues"]})

    def test_batch_wiki_dependencies_are_checked_alongside_bad_references(self):
        self.output["wikis"] = [{**self.output.pop("wiki"), "record_ids": ["missing-record"]}]
        self.output["objects"][0]["sources"][0]["quote"] = "Wrong."
        found = self.collect()
        self.assertIn("invalid_wiki_dependency", {item["code"] for item in found["issues"]})
        self.assertIn("/wikis/0/record_ids", [item.get("field_path") for item in found["issues"]])

    def test_body_uses_the_normal_validator_65536_character_bound(self):
        self.output["objects"][0]["body"] = "x" * 40000
        self.assertEqual([], self.collect()["issues"])

    def test_quality_reference_is_precise_but_does_not_claim_remaining_quality_checks_completed(self):
        from pdm.knowledge_quality import prepare_quality
        from quality_fixtures import add_quality
        bundle = resolve_skill_refs("knowledge-extract@16", "knowledge-review@14")
        context = prepare_quality([self.source], skill_bundle=bundle)
        self.output["objects"][0]["body"] = "导出时尚未导入。"
        self.output = add_quality(self.output, context)
        self.output["quality"]["temporal_claims"] = [{"record_id": "artificial-record",
            "statement": "导出时尚未导入。", "basis": "source_time", "system_fact_id": None,
            "source_refs": [{**self.ref, "quote": "Artificial"}]}]
        found = self.collect(quality_context=context)
        exact = [item for item in found["issues"]
                 if item.get("field_path") == "/quality/temporal_claims/0/source_refs/0"]
        self.assertEqual("quality_source_reference_mismatch", exact[0]["code"])
        self.assertIn("quality_validation_incomplete", {item["code"] for item in found["issues"]})

    def test_reference_precision_is_opt_in_for_legacy_failure_bytes(self):
        ref = {**self.ref, "quote": "Wrong."}
        with self.assertRaises(CaptureError) as old:
            _refs([ref], [self.source])
        self.assertEqual("invalid_quote", old.exception.code)
        self.assertIsNone(old.exception.details)
        with self.assertRaises(CaptureError) as current:
            _refs([ref], [self.source], field_path="/objects/1/sources", record_id="artificial-record", ref_offset=2)
        self.assertEqual("/objects/1/sources/2", current.exception.details["field_path"])
        self.assertEqual(2, current.exception.details["ref_index"])
        # An out-of-bounds locator still wins over malformed quote, as before.
        with self.assertRaises(CaptureError) as bounds:
            _refs([{**ref, "locator": "L99-L99", "quote": None}], [self.source])
        self.assertEqual("invalid_quote", bounds.exception.code)


class StableFailureDiagnosticIntegrationTests(unittest.TestCase):
    setUp = related.RelatedFailureDiagnosticTests.setUp
    run_failure = related.RelatedFailureDiagnosticTests.run_failure
    MARKER = related.RelatedFailureDiagnosticTests.MARKER
    DEBUG = related.RelatedFailureDiagnosticTests.DEBUG

    def transport(self, role, payload, timeout=120):
        response = related.RelatedFailureDiagnosticTests.transport(self, role, payload, timeout)
        if role == "extract" and hasattr(self, "initial"):
            output = response["output"]
            record = output["objects"][0]
            record["sources"][0]["quote"] = "Artificial wrong quote."
            output["wikis"][0]["sources"][0]["locator"] = "wrong"
            output["wikis"][0]["sources"].append({**record["sources"][0], "quote": "Artificial wrong topic quote."})
            record["body"] += " 当前已经全部完成。"
            record["domains"] = ["invalid-domain"]
            self.responses[-1] = deepcopy(response)
        return response

    def test_stable_single_rejection_preserves_all_independent_findings_without_review(self):
        self.bundle = resolve_skill_refs("knowledge-extract@16", "knowledge-review@14")
        result, failure = self.run_failure("multiple-stable-errors")
        self.assertEqual("invalid_quote", failure["error"])
        diagnostic = failure["extraction_diagnostic"]
        details = diagnostic["validation_error"]["details"]
        self.assertFalse(details["issues_truncated"])
        codes = {item["code"] for item in details["issues"]}
        self.assertTrue({"invalid_quote", "invalid_locator", "invalid_domains", "quality_temporal_missing",
                         "related_records_omitted"}.issubset(codes), details)
        quotes = [item for item in details["issues"] if item["code"] == "invalid_quote"]
        self.assertEqual(["/objects/0/sources/0", "/wikis/0/sources/1"],
                         [item["field_path"] for item in quotes])
        self.assertEqual(self.responses[-1]["output"], diagnostic["response"]["output"])
        self.assertEqual(self.formal, {path: (self.repo / path).read_bytes() for path in self.formal})

    def test_legacy_single_error_diagnostic_has_no_new_issues_fields(self):
        result, failure = self.run_failure("multiple-legacy-errors")
        self.assertEqual("invalid_quote", failure["error"])
        self.assertIsNone(failure["extraction_diagnostic"]["validation_error"]["details"])


class StableReferenceRepairIntegrationTests(unittest.TestCase):
    setUp = related.RelatedFailureDiagnosticTests.setUp
    MARKER = related.RelatedFailureDiagnosticTests.MARKER
    DEBUG = related.RelatedFailureDiagnosticTests.DEBUG

    def transport(self, role, payload, timeout=120):
        feedback = payload.get("extraction_feedback", {})
        if role == "extract" and "repair_context" in feedback:
            self.calls.append((role, deepcopy(payload)))
            self.repair_count = getattr(self, "repair_count", 0) + 1
            source = payload["sources"][0]
            lines = [line["text"] for fragment in source["fragments"] for line in fragment["lines"]]
            ref = {"source_id": source["metadata"]["source_id"],
                   "source_version": source["metadata"]["source_version"], "locator": "L1-L3", "quote": lines[0]}
            if getattr(self, "bad_first_repair", False) and self.repair_count == 1:
                ref["quote"] = "Artificial still wrong quote."
            output = {"reference_repairs": [{"field_path": path, "reference": deepcopy(ref)}
                      for path in feedback["repair_context"]["field_paths"]]}
            response = {"output": output, "model": "artificial-related-model", "usage": None,
                        "elapsed_seconds": .01, "invocation_id": "model-" + f"{len(self.calls):032x}"}
            self.responses.append(deepcopy(response))
            return response
        response = related.RelatedFailureDiagnosticTests.transport(self, role, payload, timeout)
        if role == "extract" and hasattr(self, "initial"):
            response["output"]["objects"][0]["sources"][0]["quote"] = "Artificial wrong first quote."
            self.responses[-1] = deepcopy(response)
        elif role == "review" and payload.get("skill_bundle", {}).get("skills", {}).get("extract", {}).get("ref") == "knowledge-extract@16":
            response["output"]["repair_scope"] = {"kind": "none", "missing_bindings": []}
            if hasattr(self, "forbidden_review_invocation"):
                response["invocation_id"] = self.forbidden_review_invocation
            self.responses[-1] = deepcopy(response)
        return response

    def test_narrow_reference_repair_publishes_and_rebuilds_current_and_historical_proof(self):
        from pdm.budget import BudgetedModelCall
        from pdm.production import prepare_production, run_production
        from pdm.pipeline import _load, verify_publication
        from pdm.assembly_final import extraction_invocations
        self.bundle = resolve_skill_refs("knowledge-extract@16", "knowledge-review@14")
        self.omit = False
        queue = "artificial-reference-repair"
        prepare_production(self.repo, queue, [self.path], allowed_run_ids=[self.initial["run_id"]],
                           skill_bundle=self.bundle)
        adapter = BudgetedModelCall(self.ledger, queue, self.transport)
        first = run_production(self.repo, queue, model_call=adapter, max_steps=1)
        self.assertEqual("failed", first["status"], first)
        first_invocation = self.responses[-1]["invocation_id"]
        failure = _load((self.repo / first["last_result"]["failure_path"]).read_bytes(), "failure")
        frozen = failure["extraction_diagnostic"]["validated_output"]
        result = run_production(self.repo, queue, model_call=adapter)
        self.assertEqual("published", result["status"], result)
        run = result["publication"]["run_id"]
        candidates = [_load(path.read_bytes(), "candidate") for path in self.repo.glob("memory/staging/*.md")]
        candidate = next(row for row in candidates if "reference_repair_context" in row)
        self.assertIn("reference_repair_context", candidate)
        self.assertNotIn("statistic_claims_materialization", candidate)
        self.assertEqual({"reference_repairs"}, set(candidate["raw_extraction_output"]))
        self.assertEqual(frozen["quality"], candidate["extraction_output"]["quality"])
        self.assertEqual([row["body"] for row in frozen["objects"]],
                         [row["body"] for row in candidate["extraction_output"]["objects"]])
        self.assertIn(first_invocation, extraction_invocations(candidate))
        self.assertEqual(result["publication"]["commit_sha"], verify_publication(self.repo, run)["commit_sha"])
        self.assertEqual(result["publication"]["commit_sha"], verify_publication(self.repo, run, historical=True)["commit_sha"])

    def test_continuous_reference_repairs_keep_original_generator_independent_from_review(self):
        from pdm.budget import BudgetedModelCall
        from pdm.production import prepare_production, run_production
        from pdm.pipeline import _load
        from pdm.assembly_final import extraction_invocations
        self.bundle = resolve_skill_refs("knowledge-extract@16", "knowledge-review@14")
        self.omit, self.bad_first_repair = False, True
        queue = "artificial-continuous-reference-repair"
        prepare_production(self.repo, queue, [self.path], allowed_run_ids=[self.initial["run_id"]],
                           skill_bundle=self.bundle)
        adapter = BudgetedModelCall(self.ledger, queue, self.transport)
        first = run_production(self.repo, queue, model_call=adapter, max_steps=1)
        self.assertEqual("invalid_quote", first["last_result"]["error"])
        original = self.responses[-1]["invocation_id"]
        second = run_production(self.repo, queue, model_call=adapter, max_steps=1)
        self.assertEqual("invalid_quote", second["last_result"]["error"])
        previous_patch = self.responses[-1]["invocation_id"]
        failed_patch = _load((self.repo / second["last_result"]["failure_path"]).read_bytes(), "failure")
        self.assertEqual({original, previous_patch},
                         set(failed_patch["extraction_diagnostic"]["validation_context"]["generation_invocations"]))
        self.assertIn("objects", failed_patch["extraction_diagnostic"]["validated_output"])
        self.forbidden_review_invocation = original
        third = run_production(self.repo, queue, model_call=adapter, max_steps=1)
        self.assertEqual("review_not_independent", third["last_result"]["error"], third)
        candidate = _load((self.repo / third["last_result"]["stage_path"]).read_bytes(), "candidate")
        self.assertTrue({original, previous_patch}.issubset(extraction_invocations(candidate)))
        self.assertEqual(self.formal, {path: (self.repo / path).read_bytes() for path in self.formal})


if __name__ == "__main__":
    unittest.main()
