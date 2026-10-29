"""Exercise public commands together on explicit artificial correction data."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quality_fixtures import passed_review
import test_controls
from pdm.cli import main
from pdm.budget import BudgetLedger
from pdm.contracts import parse_markdown
from pdm.pipeline import process_sources
from pdm.skill_support import DEFAULT_EXTRACT_SKILL_REF, DEFAULT_REVIEW_SKILL_REF


class M1CommandWorkflowTests(unittest.TestCase):
    def setUp(self):
        test_controls.ControlsTests.setUp(self)
        self.budget_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.budget_temp.cleanup)
        self.budget_path = Path(self.budget_temp.name) / "budget.json"
        BudgetLedger.create(self.budget_path, 8, 0, "isolated-command-workflow", repo=self.repo)
        self.review_requests = []

    def invoke(self, *arguments, success=True):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            status = main(list(arguments))
        if success:
            self.assertEqual(status, 0, errors.getvalue() or output.getvalue())
            return json.loads(output.getvalue())
        self.assertEqual(status, 2)
        return json.loads(errors.getvalue())

    def model(self, role, payload):
        self.calls += 1
        self.assertEqual(payload["quality_contract"], "skill-quality-v1")
        expected = DEFAULT_EXTRACT_SKILL_REF if role == "extract" else DEFAULT_REVIEW_SKILL_REF
        self.assertEqual(payload["skill_bundle"]["skills"][role]["ref"], expected)
        if role == "extract":
            chosen = next((source for source in payload["sources"] if "用户纠正" in source["body"]), payload["sources"][0])
            text = chosen["body"].splitlines()[0]
            ref = {"source_id": chosen["metadata"]["source_id"], "source_version": chosen["metadata"]["source_version"],
                   "locator": "L1-L1", "quote": text}
            output = {"objects": [{"id": "state-artificial", "type": "state", "title": "人工状态",
                       "source_kind": "user_statement", "sources": [ref], "domains": [], "valid_from": None,
                       "valid_to": None, "verification_status": "unverified", "lifecycle_status": "undetermined",
                       "body": text}], "wiki": {"id": "topic-artificial", "title": "人工主题",
                       "sources": [ref], "body": text}, "reuse_refs": []}
            # Summarize the correction history separately from the effective
            # current state. Neither the old value nor an unprocessed target
            # is presented as a completed current observation.
            coverage = []
            for target in payload["quality_context"]["targets"]:
                selected = (target["source_id"], target["source_version"]) == (
                    ref["source_id"], ref["source_version"])
                record_id, summary = "state-artificial", text
                if not selected:
                    old_source = next(source for source in payload["sources"]
                        if (source["metadata"]["source_id"], source["metadata"]["source_version"]) ==
                           (target["source_id"], target["source_version"]))
                    old_text = old_source["body"].splitlines()[0]
                    old_ref = {"source_id": target["source_id"], "source_version": target["source_version"],
                               "locator": "L1-L1", "quote": old_text}
                    record_id = "correction-history-artificial"
                    summary = f"原始资料曾记录：{old_text}。用户纠正原文明确写为：{text}。旧说法仅作纠正对象保留，不作为有效值使用。"
                    output["objects"].append({"id": record_id, "type": "knowledge", "title": "人工纠正历史",
                        "source_kind": "user_statement", "sources": [old_ref, ref], "domains": [],
                        "valid_from": None, "valid_to": None, "verification_status": "unverified",
                        "lifecycle_status": "undetermined", "body": summary})
                coverage.append({
                    "target_id": target["target_id"],
                    "status": "summarized", "record_ids": [record_id],
                    "body_evidence": [{"record_id": record_id, "text": summary}], "reason": "",
                })
            output["quality"] = {"coverage_disposition": coverage, "statistic_claims": [], "temporal_claims": []}
        else:
            self.review_requests.append(payload)
            self.assertEqual(payload["candidate"]["extraction_output"]["quality"]["statistic_claims"], [])
            self.assertTrue(payload["required_checks"])
            output = passed_review(payload["required_checks"], payload.get("skill_bundle"))
        return {"output": output, "model": "artificial", "invocation_id": "model-" + f"{self.calls:032x}",
                "elapsed_seconds": 0.01, "usage": None}

    def process(self, paths, related=None):
        arguments = ["process", "--repo", str(self.repo), "--incremental",
                     "--budget-ledger", str(self.budget_path), "--operation-id", f"workflow-{self.calls}"]
        for path in paths:
            arguments += ["--source", path]
        for run in related or []:
            arguments += ["--related-run", run]

        def actual(*args, **kwargs):
            adapter = kwargs.pop("model_call")
            adapter.transport = lambda role, payload, timeout=120: self.model(role, payload)
            return process_sources(*args, model_call=adapter, **kwargs)

        with patch("pdm.pipeline.process_sources", side_effect=actual):
            return self.invoke(*arguments)

    def test_correction_before_after_publication_package_and_delete_use_one_controlled_path(self):
        with tempfile.TemporaryDirectory(prefix="pdm-cli-artifacts-") as directory:
            artifacts = Path(directory)
            old = self.process([self.paths[0]])
            self.assertEqual(old["status"], "passed")
            metadata, body = parse_markdown((self.repo / old["stage_path"]).read_bytes())
            self.assertEqual(metadata["schema_version"], "0.22")
            self.assertEqual(json.loads(body)["quality_contract"], "skill-quality-v1")
            original = self.invoke("publish", "--repo", str(self.repo), "--run", old["run_id"])
            payload = {"request_key": "artificial:cli-correction", "action": "corrects",
                       "source_paths": [self.paths[0]], "replacement_source_paths": [self.paths[1]],
                       "basis_source_path": self.paths[1], "object_paths": sorted(original["files"])}
            control_input = artifacts / "control.json"
            control_input.write_text(json.dumps(payload))
            plan = self.invoke("control", "plan", "--repo", str(self.repo), "--input", str(control_input))
            self.assertEqual(plan["action"], "corrects")
            self.invoke("control", "apply", "--repo", str(self.repo), "--input", str(control_input))
            view_arguments = ["evidence", "--repo", str(self.repo), "--current", "--source", self.paths[0],
                              "--source", self.paths[1]]
            before = self.invoke(*view_arguments)
            self.assertEqual([entry["source"]["path"] for entry in before["sources"]], [self.paths[1]])
            self.assertNotIn("旧人工数值：2", json.dumps(before["sources"], ensure_ascii=False))
            self.assertEqual(before["sources"][0]["knowledge_status"], "unprocessed_or_unverified")
            update = self.process(self.paths[:2], [old["run_id"]])
            self.assertEqual(update["status"], "passed")
            metadata, body = parse_markdown((self.repo / update["stage_path"]).read_bytes())
            self.assertEqual(metadata["schema_version"], "0.22")
            candidate = json.loads(body)
            selected_paths = {(source["source_id"], source["source_version"]): source["path"]
                              for source in candidate["quality_context"]["sources"]}
            source_paths = {target["target_id"]: selected_paths[(target["source_id"], target["source_version"])]
                            for target in candidate["quality_context"]["targets"]}
            dispositions = {source_paths[item["target_id"]]: item
                            for item in candidate["extraction_output"]["quality"]["coverage_disposition"]}
            self.assertEqual(dispositions[self.paths[0]]["status"], "summarized")
            self.assertEqual(dispositions[self.paths[0]]["record_ids"], ["correction-history-artificial"])
            self.assertIn("旧说法仅作纠正对象保留", dispositions[self.paths[0]]["body_evidence"][0]["text"])
            self.assertEqual(dispositions[self.paths[1]]["status"], "summarized")
            self.assertIn("实际是1", dispositions[self.paths[1]]["body_evidence"][0]["text"])
            review_meta, review_body = parse_markdown((self.repo / update["review_path"]).read_bytes())
            self.assertEqual(review_meta["schema_version"], "0.22")
            reviewed = json.loads(review_body)["output"]
            self.assertEqual(reviewed["verdict"], "passed")
            self.assertEqual(len(self.review_requests), 2)
            self.assertEqual({item["check_id"] for item in reviewed["checks"]},
                             {item["check_id"] for item in self.review_requests[-1]["required_checks"]})
            publication = self.invoke("publish", "--repo", str(self.repo), "--run", update["run_id"])
            after = self.invoke(*view_arguments, "--run", update["run_id"])
            self.assertEqual(after["knowledge_status"], "published")
            self.assertEqual([entry["source"]["path"] for entry in after["sources"]], [self.paths[1]])
            self.assertEqual(len(after["publications"]), 1)
            self.assertEqual(len(after["publications"][0]["documents"]), 2)
            self.assertTrue(all("实际是1" in document["body"]
                                for published in after["publications"] for document in published["documents"]))
            number = self.calls
            historical = self.invoke(*view_arguments, "--history", "--run", old["run_id"])
            self.assertEqual(len(historical["publications"]), 1)
            self.assertEqual(len(historical["publications"][0]["documents"]), 2)
            self.assertTrue(all("旧人工数值：2" in document["body"]
                                for published in historical["publications"] for document in published["documents"]))
            self.assertEqual(self.calls, number)
            repeated = self.process(self.paths[:2], [old["run_id"]])
            self.assertEqual(repeated["status"], "already_processed")
            self.assertEqual(self.calls, number)
            questions = artifacts / "questions.json"
            questions.write_text(json.dumps(["当前人工状态是什么，依据在哪里？"]))
            package = artifacts / "reading.json"
            export = self.invoke("reading-package", "prepare", "--repo", str(self.repo),
                                 "--source", self.paths[0], "--source", self.paths[1],
                                 "--run", update["run_id"], "--questions", str(questions), "--output", str(package))
            verified = self.invoke("reading-package", "verify", str(package), "--expected-sha256", export["sha256"])
            self.assertEqual(verified["second_model_reading"], "unverified")
            self.assertEqual(verified["model_calls"], 0)
            deletion = {"request_key": "artificial:cli-delete", "action": "delete",
                        "source_paths": self.paths[:2], "object_paths": sorted(publication["files"])}
            control_input.write_text(json.dumps(deletion))
            self.invoke("control", "apply", "--repo", str(self.repo), "--input", str(control_input))
            deleted = self.invoke(*view_arguments)
            self.assertEqual(deleted["sources"], [])
            self.assertTrue(all(not (self.repo / path).exists() for path in publication["files"]))
            failed = self.invoke("verify-publication", "--repo", str(self.repo), "--run", update["run_id"],
                                 "--historical", success=False)
            self.assertEqual(failed["error"], "source_excluded")
            self.assertTrue((self.repo / self.paths[0]).exists())
            self.assertEqual(test_controls.git(self.repo, "status", "--porcelain"), "")

    def test_duplicate_control_input_fields_are_rejected_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.json"
            source.write_text('{"action":"corrects","action":"delete","source_paths":[]}')
            head = test_controls.git(self.repo, "rev-parse", "HEAD")
            with patch("pdm.controls.record_control") as mutation:
                result = self.invoke("control", "apply", "--repo", str(self.repo), "--input", str(source), success=False)
            mutation.assert_not_called()
            self.assertEqual(result["error"], "ValueError")
            self.assertEqual(test_controls.git(self.repo, "rev-parse", "HEAD"), head)


if __name__ == "__main__":
    unittest.main()
