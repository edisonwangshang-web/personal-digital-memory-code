"""Repair 26 omitted bindings without regenerating correct artificial prose."""

from copy import deepcopy
import unittest

import test_production
from engine_fixtures import isolated_engine
from pdm.budget import BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError, parse_markdown
from pdm.evidence import build_current_evidence
from pdm.knowledge_quality import resolve_statistic
from pdm.pipeline import _hash, _read_reviewed, verify_publication
from pdm.production import prepare_production, run_production
from pdm.transaction import PipelineGit
from quality_fixtures import add_quality, passed_review


class StatisticRepairPipelineTests(unittest.TestCase):
    def setUp(self):
        test_production.ProductionTests.setUp(self)
        isolated_engine(self)
        self.reject_first = True
        self.content_failure = False
        self.invalid_patch = False
        self.interrupt_patch = False
        columns = ["metric-" + str(i) + "_bpm" for i in range(13)]
        body = "date," + ",".join(columns) + "\n2028-01-01," + ",".join(["6"] * 13) + "\n2028-01-02," + ",".join(["8"] * 13) + "\n"
        self.path = capture(self.repo, {"source_key": "artificial:twenty-six-bindings", "source_type": "note",
                            "acquisition_status": "full_text", "body": body})["path"]
        self.paths = [self.path]
        prepare_production(self.repo, "statistics", self.paths, use_default_skills=True)

    def bindings(self, context):
        return [{"stat_id": stat["stat_id"], "record_id": "artificial-period"}
                for stat in context["statistics"] if stat["operation"] in {"present_count", "blank_count"}
                and next(target for target in context["targets"] if target["target_id"] == stat["target_id"]).get("column", "").startswith("metric-")]

    def excerpt(self, context, stat_id):
        stat = next(item for item in context["statistics"] if item["stat_id"] == stat_id)
        column = next(target for target in context["targets"] if target["target_id"] == stat["target_id"])["column"]
        return f"{column}: present 2, blank 0."

    def full_output(self, payload):
        source = payload["sources"][0]
        lines = {line["line"]: line["text"] for fragment in source["fragments"] for line in fragment["lines"]}
        ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
               "locator": "L1-L3", "quote": lines[1]}
        body = "Artificial supplied table for 2028-01-01 to 2028-01-02.\n" + "\n".join(
            f"metric-{i}_bpm: present 2, blank 0." for i in range(13))
        if "revision_context" in payload:
            body += "\nThis wording describes only the explicitly supplied artificial table."
        output = {"objects": [{"id": "artificial-period", "type": "knowledge", "title": "Artificial period",
                   "source_kind": "external_source", "domains": ["learning"], "sources": [ref],
                   "valid_from": None, "valid_to": None, "verification_status": "unverified",
                   "lifecycle_status": "undetermined", "body": body}],
                  "wikis": [{"id": "artificial-period-topic", "title": "Artificial topic", "body": "Only the supplied artificial table is summarized.",
                             "sources": [ref], "record_ids": ["artificial-period"]}], "reuse_refs": []}
        output = add_quality(output, payload["quality_context"])
        if "revision_context" in payload:
            for pair in self.bindings(payload["quality_context"]):
                stat = resolve_statistic(payload["quality_context"], next(stat for stat in payload["quality_context"]["statistics"]
                                         if stat["stat_id"] == pair["stat_id"]))
                output["quality"]["statistic_claims"].append({**pair, "value": stat["value"],
                    "body_evidence": self.excerpt(payload["quality_context"], pair["stat_id"]),
                    "sources": [{**ref, "locator": "L2-L2", "quote": lines[2]}]})
        return output

    def transport(self, role, payload, timeout=120):
        self.roles.append(role)
        self.payloads.append((role, deepcopy(payload)))
        self.number += 1
        if role == "extract":
            if "quality_repair_context" in payload:
                if self.interrupt_patch:
                    raise KeyboardInterrupt()
                output = {"statistic_bindings": [{**pair, "body_evidence": self.excerpt(payload["quality_context"], pair["stat_id"])}
                          for pair in payload["quality_repair_context"]["missing_bindings"]]}
                if self.invalid_patch:
                    output["objects"] = []
            else:
                output = self.full_output(payload)
        else:
            output = passed_review(payload["required_checks"], payload["skill_bundle"])
            if self.reject_first:
                self.reject_first = False
                kind = "object_fidelity" if self.content_failure else "object_statistics_time"
                bad = next(check for check in payload["required_checks"] if check["kind"] == kind
                           and "memory/records/artificial-period.md" in check["object_paths"])
                next(check for check in output["checks"] if check["check_id"] == bad["check_id"]).update(
                    result="needs_revision", reason="Artificial missing bindings or explicit content repair.")
                output.update(verdict="needs_revision", reasons=["Bind the explicitly identified statistics to the existing period prose."],
                    repair_scope={"kind": "content" if self.content_failure else "statistic_bindings",
                                  "missing_bindings": [] if self.content_failure else self.bindings(payload["quality_context"])})
        return {"output": output, "model": "artificial", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def test_twenty_six_missing_bindings_keep_text_and_actual_generation_provenance(self):
        first = run_production(self.repo, "statistics", model_call=self.adapter, max_steps=1)
        self.assertEqual(first["status"], "needs_revision", first)
        old, old_review, _ = _read_reviewed(PipelineGit(self.repo), first["active_run_id"], allow_nonpassed=True)
        self.assertEqual(len(old_review["output"]["repair_scope"]["missing_bindings"]), 26)
        second = run_production(self.repo, "statistics", model_call=self.adapter, max_steps=1)
        self.assertEqual(second["status"], "ready", second)
        new, _, _ = _read_reviewed(PipelineGit(self.repo), second["final_run"])
        self.assertEqual(new["objects"], old["objects"])
        self.assertEqual(new["operations"], old["operations"])
        self.assertEqual(new["extraction_output"]["objects"], old["extraction_output"]["objects"])
        self.assertEqual(len(new["raw_extraction_output"]["statistic_bindings"]), 26)
        self.assertEqual(new["extraction"]["output_sha256"], _hash(new["raw_extraction_output"]))
        self.assertNotEqual(new["extraction"]["output_sha256"], _hash(new["extraction_output"]))
        self.assertEqual(len(new["extraction_output"]["quality"]["statistic_claims"]), 26)
        self.assertEqual(self.roles, ["extract", "review"] * 2)
        patch_request = [payload for role, payload in self.payloads if role == "extract"][-1]
        final_review = [payload for role, payload in self.payloads if role == "review"][-1]
        self.assertIn("quality_repair_context", patch_request)
        self.assertNotIn("quality_repair_context", final_review)
        self.assertNotIn("raw_extraction_output", final_review["candidate"])
        published = run_production(self.repo, "statistics", model_call=self.adapter)
        self.assertEqual(published["status"], "published", published)
        proof = verify_publication(self.repo, published["publication"]["run_id"], source_paths=self.paths)
        metadata, _ = parse_markdown((self.repo / "memory/records/artificial-period.md").read_bytes())
        self.assertEqual(metadata["generated_by"]["run_id"], old["run_id"])
        self.assertEqual(metadata["review_ref"], f"memory/runs/processes/{new['run_id']}/review.md")
        self.assertTrue(build_current_evidence(self.repo, self.paths, [proof["run_id"]])["coverage"]["current_complete"])

    def test_content_rejection_uses_full_output_instead_of_metadata_patch(self):
        self.content_failure = True
        first = run_production(self.repo, "statistics", model_call=self.adapter, max_steps=1)
        self.assertEqual(first["status"], "needs_revision", first)
        result = run_production(self.repo, "statistics", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "ready", result)
        repaired, _, _ = _read_reviewed(PipelineGit(self.repo), result["final_run"])
        self.assertNotIn("quality_repair_context", repaired)
        self.assertNotIn("raw_extraction_output", repaired)
        self.assertTrue(repaired["objects"][0]["body"].endswith("supplied artificial table."))

    def test_patch_cannot_return_text_and_keeps_the_real_completed_failure(self):
        run_production(self.repo, "statistics", model_call=self.adapter, max_steps=1)
        self.invalid_patch = True
        result = run_production(self.repo, "statistics", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["last_result"]["error"], "invalid_statistic_repair_patch")
        self.assertEqual(self.roles, ["extract", "review", "extract"])
        self.assertFalse((self.repo / "memory/records/artificial-period.md").exists())

    def test_unknown_patch_is_not_replayed_after_queue_rename(self):
        run_production(self.repo, "statistics", model_call=self.adapter, max_steps=1)
        self.interrupt_patch = True
        with self.assertRaises(KeyboardInterrupt):
            run_production(self.repo, "statistics", model_call=self.adapter, max_steps=1)
        before = self.number, self.ledger.path.read_bytes()
        prepare_production(self.repo, "new-name", self.paths, use_default_skills=True)
        again = run_production(self.repo, "new-name", model_call=self.adapter)
        self.assertEqual(again["status"], "unknown", again)
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
