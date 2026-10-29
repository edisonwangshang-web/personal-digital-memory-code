"""Artificial production flows for reviewed anchors and mixed repair routing."""

from copy import deepcopy
import unittest

import test_production
from engine_fixtures import isolated_engine
from pdm.capture import capture
from pdm.contracts import parse_markdown
from pdm.evidence import build_current_evidence
from pdm.knowledge_quality import resolve_statistic
from pdm.pipeline import _hash, _read_reviewed, verify_publication
from pdm.production import prepare_production, run_production
from pdm.transaction import PipelineGit
from quality_fixtures import add_quality, passed_review


class Dev36RepairFlowTests(unittest.TestCase):
    def setUp(self):
        test_production.ProductionTests.setUp(self)
        isolated_engine(self)
        self.mode = "statistic"
        self.review_calls = 0
        self.full_extracts = 0
        self.path = capture(self.repo, {
            "source_key": "artificial:learning-repair-v2", "source_type": "note",
            "acquisition_status": "full_text",
            "body": "date,practice_min,reading_min\n2028-01-01,6,5\n2028-01-02,8,7\n",
        })["path"]
        self.paths = [self.path]

    def prepare(self, *, mixed=False):
        self.mode = "mixed" if mixed else "statistic"
        result = prepare_production(self.repo, "repair", self.paths, use_default_skills=True,
            **({"review_strategy": "decomposed-v1"} if mixed else {}))
        self.assertEqual("ready", result["status"])
        return result

    def missing_bindings(self, context, columns_to_bind=None):
        columns_to_bind = {"practice_min"} if columns_to_bind is None else columns_to_bind
        columns = {target["target_id"]: target.get("column") for target in context["targets"]}
        return [{"stat_id": stat["stat_id"], "record_id": "learning-period",
                 "body_evidence": f"{columns[stat['target_id']]} has 2 supplied observations."}
                for stat in context["statistics"]
                if stat["operation"] == "present_count" and columns[stat["target_id"]] in columns_to_bind]

    def full_output(self, payload):
        self.full_extracts += 1
        source = payload["sources"][0]
        lines = {line["line"]: line["text"] for fragment in source["fragments"]
                 for line in fragment["lines"]}
        ref = {"source_id": source["metadata"]["source_id"],
               "source_version": source["metadata"]["source_version"],
               "locator": "L1-L3", "quote": lines[1]}
        body = ("Only the supplied artificial learning table for 2028-01-01 to 2028-01-02.\n"
                "practice_min has 2 supplied observations.\nreading_min has 2 supplied observations.")
        if self.mode == "mixed" and self.full_extracts == 1:
            body = body.replace("\nreading_min has 2 supplied observations.", "")
        if self.mode == "mixed" and "revision_context" in payload:
            body += f"\nArtificial full content revision {self.full_extracts}; both supplied fields remain represented."
        output = {"objects": [{"id": "learning-period", "type": "knowledge", "title": "Artificial learning period",
            "source_kind": "external_source", "domains": ["learning"], "sources": [ref],
            "valid_from": None, "valid_to": None, "verification_status": "unverified",
            "lifecycle_status": "undetermined", "body": body}],
            "wikis": [{"id": "learning-period-topic", "title": "Artificial learning topic",
                "body": "The supplied artificial learning table contains two separately named fields.",
                "sources": [ref], "record_ids": ["learning-period"]}], "reuse_refs": []}
        output = add_quality(output, payload["quality_context"])
        if self.mode == "mixed" and self.full_extracts == 1:
            # A legitimate object-level statistical finding must not freeze
            # another unfinished target elsewhere in the complete final scope.
            target = next(item for item in payload["quality_context"]["targets"]
                          if item.get("column") == "reading_min")
            row = next(item for item in output["quality"]["coverage_disposition"]
                       if item["target_id"] == target["target_id"])
            row.update(status="deferred", record_ids=[], body_evidence=[],
                reason="Artificial supplied reading observations were not fully organized in this initial draft.")
        # The ordinary fixture omits only practice's relation: the separately
        # stated reading count already has its valid binding. Full content
        # repairs of a mixed draft supply both relations, without any patch.
        bound_columns = ({"reading_min"} if self.mode == "statistic" else
                         {"practice_min", "reading_min"} if self.full_extracts > 1 else set())
        for entry in self.missing_bindings(payload["quality_context"], bound_columns):
            stat = resolve_statistic(payload["quality_context"], next(
                item for item in payload["quality_context"]["statistics"]
                if item["stat_id"] == entry["stat_id"]))
            output["quality"]["statistic_claims"].append({**entry, "value": stat["value"],
                "sources": [{**ref, "locator": "L2-L2", "quote": lines[2]}]})
        return output

    def transport(self, role, payload, timeout=120):
        self.number += 1
        self.roles.append(role)
        self.payloads.append((role, deepcopy(payload)))
        if role == "extract":
            if "quality_repair_context" in payload:
                self.assertEqual("statistic-bindings-repair-v2", payload["quality_repair_context"]["contract"])
                output = {"statistic_bindings": deepcopy(payload["quality_repair_context"]["missing_bindings"])}
            else:
                output = self.full_output(payload)
        else:
            self.review_calls += 1
            output = passed_review(payload["required_checks"], payload["skill_bundle"])
            if self.review_calls == 1 or self.mode == "mixed":
                statistic_only = self.review_calls == 1
                kind = "object_statistics_time" if statistic_only else "object_fidelity"
                check = next(item for item in payload["required_checks"] if item["kind"] == kind
                    and "memory/records/learning-period.md" in item["object_paths"])
                next(item for item in output["checks"] if item["check_id"] == check["check_id"]).update(
                    result="needs_revision", reason="Artificial exact missing relation." if statistic_only
                    else "Artificial content wording still needs an independent full revision.")
                output.update(verdict="needs_revision", reasons=["Artificial repair of the actual supplied scope."],
                    repair_scope={"kind": "statistic_bindings" if statistic_only else "content",
                        "missing_bindings": self.missing_bindings(payload["quality_context"]) if statistic_only else []})
        return {"output": output, "model": "artificial", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def test_reviewed_anchor_patch_freezes_text_then_publishes_with_complete_new_review(self):
        self.prepare()
        first = run_production(self.repo, "repair", model_call=self.adapter, max_steps=1)
        self.assertEqual("needs_revision", first["status"], first)
        old, review, _ = _read_reviewed(PipelineGit(self.repo), first["active_run_id"], allow_nonpassed=True)
        anchor = review["output"]["repair_scope"]["missing_bindings"][0]["body_evidence"]
        second = run_production(self.repo, "repair", model_call=self.adapter, max_steps=1)
        self.assertEqual("ready", second["status"], second)
        new, _, _ = _read_reviewed(PipelineGit(self.repo), second["final_run"])
        self.assertEqual(old["objects"], new["objects"])
        self.assertEqual(old["operations"], new["operations"])
        self.assertEqual("statistic-bindings-repair-v2", new["quality_repair_context"]["contract"])
        self.assertEqual(anchor, new["raw_extraction_output"]["statistic_bindings"][0]["body_evidence"])
        self.assertEqual(_hash(new["raw_extraction_output"]), new["extraction"]["output_sha256"])
        self.assertEqual(["extract", "review"] * 2, self.roles)
        self.assertEqual(1, self.full_extracts)
        final_review = [payload for role, payload in self.payloads if role == "review"][-1]
        self.assertNotIn("quality_repair_context", final_review)
        published = run_production(self.repo, "repair", model_call=self.adapter)
        self.assertEqual("published", published["status"], published)
        group = published["publication"]["run_id"]
        for historical in (False, True):
            proof = verify_publication(self.repo, group, source_paths=self.paths, historical=historical)
            self.assertEqual("published_local", proof["status"])
        metadata, _ = parse_markdown((self.repo / "memory/records/learning-period.md").read_bytes())
        self.assertEqual(old["run_id"], metadata["generated_by"]["run_id"])
        self.assertEqual(f"memory/runs/processes/{new['run_id']}/review.md", metadata["review_ref"])
        self.assertTrue(build_current_evidence(self.repo, self.paths, [group])["coverage"]["current_complete"])

    def test_mixed_content_scope_uses_full_output_and_keeps_original_four_attempt_limit(self):
        self.prepare(mixed=True)
        first = run_production(self.repo, "repair", model_call=self.adapter, max_steps=1)
        self.assertEqual("needs_revision", first["status"], first)
        old, review, _ = _read_reviewed(PipelineGit(self.repo), first["active_run_id"], allow_nonpassed=True)
        self.assertEqual("statistic_bindings", review["output"]["repair_scope"]["kind"])
        self.assertTrue(any(row["status"] == "deferred" for row in old["extraction_output"]["quality"]["coverage_disposition"]))
        result = run_production(self.repo, "repair", model_call=self.adapter, max_steps=8)
        self.assertEqual("attempt_limit", result["status"], result)
        extracts = [payload for role, payload in self.payloads if role == "extract"]
        self.assertEqual(4, len(extracts))
        self.assertTrue(all("quality_repair_context" not in payload for payload in extracts))
        self.assertTrue(all("revision_context" in payload for payload in extracts[1:]))
        attempts = test_production.ProductionTests.attempts(self)
        self.assertEqual([1, 2, 3, 4], [row["attempt"] for row in attempts[0]["attempts"]])
        self.assertFalse((self.repo / "memory/records/learning-period.md").exists())
        before = self.number, self.ledger.path.read_bytes()
        again = run_production(self.repo, "repair", model_call=self.adapter, max_steps=8)
        self.assertEqual("attempt_limit", again["status"])
        self.assertEqual(before, (self.number, self.ledger.path.read_bytes()))


if __name__ == "__main__":
    unittest.main()
