"""Production source reference capacity keeps every original quote and bound."""

from copy import deepcopy
import unittest

import test_pipeline
from pdm.capture import capture
from pdm.pipeline import process_sources, publish, verify_publication
from pdm.transaction import PipelineGit


class SourceReferenceCapacityTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp

    def prepare_source(self):
        return capture(self.repo, {"source_key": "fixture:source-reference-capacity",
            "source_type": "note", "acquisition_status": "full_text",
            "body": "\n".join(f"Artificial row {number}." for number in range(1, 258)) + "\n"})["path"]

    def model_for(self, count, *, wrong_quote=False):
        def model(role, payload):
            self.number += 1
            self.roles.append(role)
            if role == "extract":
                source = payload["sources"][0]
                refs = [{"source_id": source["metadata"]["source_id"],
                         "source_version": source["metadata"]["source_version"],
                         "locator": f"L{number}-L{number}", "quote": f"Artificial row {number}."}
                        for number in range(1, count + 1)]
                if wrong_quote:
                    refs[127]["quote"] = "A fabricated quote absent from the original."
                obj = {"id": "all-artificial-rows", "type": "knowledge", "title": "Artificial rows",
                    "source_kind": "external_source", "sources": refs, "domains": ["learning"],
                    "valid_from": None, "valid_to": None, "verification_status": "unverified",
                    "lifecycle_status": "undetermined", "body": "The original supplies the listed artificial rows."}
                output = {"objects": [obj], "wiki": {"id": "artificial-row-topic", "title": "Artificial row topic",
                          "body": "The supplied rows remain artificial fixture evidence.", "sources": deepcopy(refs)}}
            else:
                output = {"verdict": "passed", "reasons": ["Artificial fixture quotes checked independently."]}
            return {"output": output, "model": "artificial", "usage": None,
                    "elapsed_seconds": 0.01, "invocation_id": f"model-{self.number:032x}"}
        return model

    def test_256_distinct_exact_refs_survive_publication_and_current_historical_read(self):
        path = self.prepare_source()
        result = process_sources(self.repo, [path], related_runs=[], model_call=self.model_for(256))
        self.assertEqual(result["status"], "passed", result)
        publication = publish(self.repo, result["run_id"])
        for historical in (False, True):
            verified = verify_publication(self.repo, result["run_id"], source_paths=[path], historical=historical)
            self.assertEqual(verified["files"], publication["files"])
        from pdm.evidence import build_current_evidence
        from pdm.domain_catalog import build_domain_catalog
        evidence = build_current_evidence(self.repo, [path], [result["run_id"]])
        documents = evidence["publications"][0]["documents"]
        self.assertEqual(len(documents), 2)
        self.assertEqual([len(doc["metadata"]["sources"]) for doc in documents], [256, 256])
        self.assertTrue(build_domain_catalog(self.repo, [path], [result["run_id"]])["catalog_complete_within_selected_scope"])
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(PipelineGit(self.repo).text("status", "--porcelain"), "")

    def test_257_refs_reject_before_review_without_formal_objects(self):
        path = self.prepare_source()
        result = process_sources(self.repo, [path], related_runs=[], model_call=self.model_for(257))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["error"], "invalid_model_output")
        self.assertEqual(self.roles, ["extract"])
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--", "memory/records", "memory/topics"), "")

    def test_quote_in_middle_of_256_refs_is_still_exactly_validated_before_review(self):
        path = self.prepare_source()
        result = process_sources(self.repo, [path], related_runs=[], model_call=self.model_for(256, wrong_quote=True))
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["error"], "invalid_quote")
        self.assertEqual(self.roles, ["extract"])
