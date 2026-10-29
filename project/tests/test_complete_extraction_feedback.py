"""Complete failed-draft feedback and a narrow completed-request transfer."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_production
from engine_fixtures import advance_engine, isolated_engine
from quality_fixtures import add_quality, passed_review
from pdm.batch_groups import verify_group_publication
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.extraction_feedback import (_completed_predecessor_payload, _model_view,
                                     load_feedback, prepare_feedback)
from pdm.pipeline import _committed, _load, _read_reviewed, _revision_attempt_path, _run_path
from pdm.production import prepare_production, run_production
from pdm.revision_context import build_revision_context
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


OLD = ("knowledge-extract@14", "knowledge-review@12")
NEW = ("knowledge-extract@15", "knowledge-review@13")


class CompleteFeedbackViewTests(unittest.TestCase):
    def diagnostic(self):
        return {"response": {"output": {
            "objects": [{"id": "artificial-current-note", "domains": ["learning"],
                         "body": "An artificial current draft with its original bounded interpretation."}],
            "wikis": [{"id": "artificial-topic", "body": "An artificial current topic."}],
            "reuse_refs": [{"path": "memory/records/artificial-old.md", "content_version": "sha256-" + "1" * 64}],
            "quality": {"coverage_disposition": [{"target_id": "artificial-a", "status": "deferred"}],
                        "statistic_claims": [{"record_id": "artificial-current-note", "stat_id": "artificial-stat"}],
                        "temporal_claims": [{"record_id": "artificial-a"}, {"record_id": "artificial-b"}]}}},
            "validation_error": {"code": "quality_temporal_mismatch", "message": "Artificial precise error.",
                                 "details": {"claim_index": 1, "field_path": "/quality/temporal_claims/1"}}}

    def test_complete_view_keeps_current_objects_topics_classification_and_all_quality(self):
        diagnostic = self.diagnostic()
        before = deepcopy(diagnostic)
        view = _model_view(diagnostic, "run-" + "1" * 32, complete=True)
        self.assertEqual(view["draft_contract"], "current-draft-complete-quality-v2")
        self.assertEqual(view["draft"], diagnostic["response"]["output"])
        self.assertEqual(view["validation_error"], diagnostic["validation_error"])
        self.assertEqual(diagnostic, before)
        view["draft"]["quality"]["coverage_disposition"].clear()
        self.assertEqual(diagnostic, before)

    def test_legacy_view_retains_its_exact_partial_quality_contract(self):
        diagnostic = self.diagnostic()
        view = _model_view(diagnostic, "run-" + "1" * 32)
        self.assertNotIn("draft_contract", view)
        self.assertEqual(view["draft"]["quality"], {"temporal_claims": [{"record_id": "artificial-b"}]})

    def test_complete_materialized_output_is_not_replaced_by_a_raw_statistic_patch(self):
        diagnostic = self.diagnostic()
        output = deepcopy(diagnostic["response"]["output"])
        diagnostic.update(validation_target="validated_output", validated_output=output)
        diagnostic["response"]["output"] = {"statistic_bindings": [{"stat_id": "artificial-only-patch"}]}
        view = _model_view(diagnostic, "run-" + "1" * 32, complete=True)
        self.assertEqual(view["draft"], output)
        self.assertNotIn("statistic_bindings", view["draft"])


class CompletedFeedbackTransferTests(unittest.TestCase):
    def setUp(self):
        test_production.ProductionTests.setUp(self)
        self.code = isolated_engine(self)
        self.paths = [capture(self.repo, {"source_key": "artificial:complete-feedback-note",
            "source_type": "note", "acquisition_status": "full_text",
            "body": "说明记载导出当时尚未整理人工学习资料。\n"})["path"],
            capture(self.repo, {"source_key": "artificial:complete-feedback-table",
            "source_type": "note", "acquisition_status": "full_text",
            "body": 'date,reading_min,practice_min\n2028-01-01,6,8\n2028-01-02,7,9\n'})["path"]]

    def transport(self, role, payload, timeout=120):
        response = test_production.ProductionTests.transport(self, role, payload, timeout)
        if role == "extract":
            number = self.roles.count("extract")
            note = next(source for source in payload["sources"]
                        if "说明记载" in source["fragments"][0]["lines"][0]["text"])
            note_id = "record-" + note["metadata"]["source_id"][-10:]
            statement = "当前已经整理人工学习资料。" if number == 3 else "来源当时尚未整理人工学习资料。"
            reference = {"source_id": note["metadata"]["source_id"],
                         "source_version": note["metadata"]["source_version"],
                         "locator": "L1-L1", "quote": note["fragments"][0]["lines"][0]["text"]}
            for record in response["output"]["objects"]:
                record["body"] = (statement if record["id"] == note_id else
                                  "Artificial learning observations retain their exact supplied scope.")
                if number > 1:
                    record["body"] += f"\nArtificial content revision {number}."
            for wiki in response["output"]["wikis"]:
                wiki["body"] = f"Artificial bounded learning topic, content revision {number}."
            output = add_quality(response["output"], payload["quality_context"])
            output["reuse_refs"] = []
            output["quality"]["temporal_claims"] = [{"record_id": note_id, "statement": statement,
                "basis": "source_time", "source_refs": [reference], "system_fact_id": None}]
            if number == 3:
                target = next(target for target in payload["quality_context"]["targets"]
                              if target.get("column") == "reading_min")
                row = next(row for row in output["quality"]["coverage_disposition"]
                           if row["target_id"] == target["target_id"])
                row.update(status="deferred", record_ids=[], body_evidence=[],
                           reason="An artificial supplied observation remains unfinished in this current draft.")
            response["output"] = output
        else:
            response["output"] = passed_review(payload["required_checks"], payload["skill_bundle"])
            if self.roles.count("extract") <= 2:
                response["output"].update(verdict="needs_revision", reasons=["Artificial source scope needs content repair."],
                    repair_scope={"kind": "content", "missing_bindings": []})
                response["output"]["checks"][0].update(result="needs_revision",
                    reason="Artificial independent Review requires a bounded content revision.")
        return response

    def failed_third(self, pair=OLD):
        prepare_production(self.repo, "old-feedback", self.paths, skill_bundle=resolve_skill_refs(*pair))
        for number in (1, 2):
            result = run_production(self.repo, "old-feedback", model_call=self.adapter, max_steps=1)
            self.assertEqual("needs_revision", result["status"], result)
        third = run_production(self.repo, "old-feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual("failed", third["status"], third)
        self.assertEqual("quality_temporal_mismatch", third["last_result"]["error"])
        self.assertEqual([1, 2, 3], [row["attempt"] for row in test_production.ProductionTests.attempts(self)[0]["attempts"]])
        return third

    def prepare_new(self):
        advance_engine(self.code)
        result = prepare_production(self.repo, "new-feedback", self.paths, skill_bundle=resolve_skill_refs(*NEW))
        self.assertEqual("ready", result["status"])

    def test_completed14_failure_continues_same_work_at_four_with_complete_current_quality(self):
        third = self.failed_third()
        git = PipelineGit(self.repo)
        failure_path = _run_path(third["active_run_id"], "failure")
        frozen = _committed(git, failure_path)
        diagnostic = _load(frozen, "failure")["extraction_diagnostic"]
        attempts = deepcopy(test_production.ProductionTests.attempts(self)[0]["attempts"])
        audit = json.loads(self.ledger.path.read_text())
        old_context = build_revision_context(git, attempts[1]["run_id"], source_paths=self.paths)
        old_pointer = _revision_attempt_path(old_context)
        frozen_pointer = _committed(git, old_pointer)
        self.assertEqual(_load(frozen_pointer, "revision_attempt")["run_id"], attempts[2]["run_id"])
        self.prepare_new()
        fourth = run_production(self.repo, "new-feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual("ready", fourth["status"], fourth)
        rows = test_production.ProductionTests.attempts(self)
        self.assertEqual(len(rows), 1)
        self.assertEqual([1, 2, 3, 4], [row["attempt"] for row in rows[0]["attempts"]])
        self.assertEqual(rows[0]["attempts"][:3], attempts)
        payload = [value for role, value in self.payloads if role == "extract"][-1]
        feedback = payload["extraction_feedback"]
        self.assertEqual(feedback["draft_contract"], "current-draft-complete-quality-v2")
        self.assertEqual(feedback["draft"]["quality"], diagnostic["response"]["output"]["quality"])
        self.assertEqual(set(feedback["draft"]["quality"]),
                         {"coverage_disposition", "statistic_claims", "temporal_claims"})
        self.assertTrue(any(row["status"] == "deferred" for row in feedback["draft"]["quality"]["coverage_disposition"]))
        self.assertEqual(feedback["draft"]["objects"], diagnostic["response"]["output"]["objects"])
        self.assertEqual(feedback["validation_error"], diagnostic["validation_error"])
        self.assertEqual(feedback["source_run_id"], third["active_run_id"])
        candidate, _, _ = _read_reviewed(git, fourth["final_run"])
        self.assertEqual(candidate["revision_context"]["source_run_id"], attempts[1]["run_id"])
        self.assertEqual(candidate["extraction_feedback_ref"]["source_run_id"], attempts[2]["run_id"])
        self.assertEqual(_committed(git, old_pointer), frozen_pointer)
        new_pointer = _revision_attempt_path(candidate["revision_context"],
                                            extraction_feedback_ref=candidate["extraction_feedback_ref"])
        self.assertNotEqual(old_pointer, new_pointer)
        self.assertEqual(_load(_committed(git, new_pointer), "revision_attempt")["run_id"], fourth["final_run"])
        published = run_production(self.repo, "new-feedback", model_call=self.adapter)
        self.assertEqual("published", published["status"])
        self.assertEqual(len(verify_group_publication(self.repo, published["publication"]["run_id"],
                                                   source_paths=self.paths)["files"]), 4)
        self.assertEqual(_committed(git, failure_path), frozen)
        self.assertEqual(_committed(git, old_pointer), frozen_pointer)
        for operation, value in audit["operations"].items():
            self.assertEqual(json.loads(self.ledger.path.read_text())["operations"][operation], value)
        before = git.head(), self.number, self.ledger.path.read_bytes()
        repeated = run_production(self.repo, "new-feedback", model_call=self.adapter)
        self.assertEqual("published", repeated["status"])
        self.assertEqual(before, (git.head(), self.number, self.ledger.path.read_bytes()))

    def test_new15_same_version_failure_also_keeps_full_current_quality(self):
        third = self.failed_third(NEW)
        fourth = run_production(self.repo, "old-feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual("ready", fourth["status"], fourth)
        payload = [value for role, value in self.payloads if role == "extract"][-1]
        self.assertEqual(set(payload["extraction_feedback"]["draft"]["quality"]),
                         {"coverage_disposition", "statistic_claims", "temporal_claims"})
        self.assertEqual(payload["extraction_feedback"]["source_run_id"], third["active_run_id"])

    def test_transfer_rejects_scope_rules_bundle_and_fixed_request_tampering(self):
        self.failed_third()
        git = PipelineGit(self.repo)
        last = test_production.ProductionTests.attempts(self)[0]["attempts"][-1]
        ref = prepare_feedback(git, last, self.adapter)
        original = deepcopy([value for role, value in self.payloads if role == "extract"][-1])
        failure = _load(_committed(git, _run_path(last["run_id"], "failure")), "failure")
        context = failure["extraction_diagnostic"]["validation_context"]
        versions = deepcopy(context["versions"])
        versions["program_commit"] = "f" * 40
        versions["program_sha256"] = "sha256-" + "f" * 64
        target = deepcopy(original)
        target["skill_bundle"] = resolve_skill_refs(*NEW)
        target["batch_context"]["plan_id"] = "plan-" + "f" * 64
        target["batch_context"]["batch_id"] = "batch-" + "f" * 64
        target["controls"]["head"] = "f" * 40
        engine = _load(_committed(git, _run_path(last["run_id"], "engine")), "engine_provenance")
        self.assertEqual(original["controls"]["head"], ref["started"]["commit_sha"])
        self.assertNotEqual(original["controls"]["head"], engine["data_base_head"])
        frozen_target = deepcopy(target)
        view = load_feedback(git, ref, target, versions)
        self.assertEqual(view["draft_contract"], "current-draft-complete-quality-v2")
        self.assertEqual(target, frozen_target)
        self.assertEqual(target["controls"]["head"], "f" * 40)
        before = git.head(), self.number, self.ledger.path.read_bytes()
        for change in ("source", "controls", "related", "bundle", "rules", "policy", "ranges", "parent"):
            changed = deepcopy(target)
            if change == "source":
                changed["sources"][0]["fragments"][0]["lines"][0]["text"] += " changed"
            elif change == "controls":
                changed["controls"]["digest"] = "sha256-" + "f" * 64
            elif change == "related":
                changed["related"] = [{"run_id": "run-" + "f" * 32}]
            elif change == "bundle":
                changed["skill_bundle"]["sha256"] = "sha256-" + "f" * 64
            elif change == "rules":
                changed["rules_version"] = "sha256-" + "f" * 64
            elif change == "policy":
                changed["quality_context"]["policy_version"] = "source-quality-v1"
            elif change == "ranges":
                changed["batch_context"]["source_ranges"] = []
            else:
                changed["revision_context"]["source_run_id"] = "run-" + "f" * 32
            with self.subTest(change=change), self.assertRaises(CaptureError):
                load_feedback(git, ref, changed, versions)
        for key in ("scope_sha256", "payload_sha256", "request_binding"):
            changed = deepcopy(context)
            changed[key] = ({"sha256": "sha256-" + "f" * 64, "bytes": 1}
                            if key == "request_binding" else "f" * 64)
            with self.subTest(context=key), self.assertRaises(CaptureError):
                _completed_predecessor_payload(git, ref, target, versions, changed)
        changed_versions = deepcopy(versions)
        changed_versions["domain_policy_sha256"] = "sha256-" + "f" * 64
        with self.assertRaises(CaptureError):
            load_feedback(git, ref, target, changed_versions)
        self.assertEqual(before, (git.head(), self.number, self.ledger.path.read_bytes()))
        failure_path = _run_path(last["run_id"], "failure")
        git.path(failure_path).write_bytes(_committed(git, failure_path) + b"\n")
        git.run("add", "--", failure_path)
        git.run("commit", "-m", "artificial raw diagnostic tamper")
        with self.assertRaises(CaptureError):
            load_feedback(git, ref, target, versions)
        self.assertEqual(before[1:], (self.number, self.ledger.path.read_bytes()))

    def test_completed_audit_is_required_before_transfer_selection(self):
        self.failed_third()
        old = test_production.ProductionTests.attempts(self)[0]["attempts"][-1]
        operation = self.ledger.operation

        def unknown(identity):
            value = operation(identity)
            if identity == old["operation_id"]:
                value["calls"]["extract"]["status"] = "unknown"
            return value

        before = self.number, self.ledger.path.read_bytes()
        with patch.object(self.ledger, "operation", side_effect=unknown), self.assertRaises(CaptureError) as error:
            prepare_feedback(PipelineGit(self.repo), old, self.adapter)
        self.assertEqual(error.exception.code, "extraction_feedback_audit_mismatch")
        self.assertEqual(before, (self.number, self.ledger.path.read_bytes()))
        self.prepare_new()
        counters = deepcopy(test_production.ProductionTests.attempts(self))
        with patch.object(self.ledger, "operation", side_effect=unknown):
            result = run_production(self.repo, "new-feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "blocked_context", result)
        self.assertEqual(result["last_result"]["error"], "extraction_feedback_audit_mismatch")
        self.assertEqual(counters, test_production.ProductionTests.attempts(self))
        self.assertEqual(before, (self.number, self.ledger.path.read_bytes()))


if __name__ == "__main__":
    unittest.main()
