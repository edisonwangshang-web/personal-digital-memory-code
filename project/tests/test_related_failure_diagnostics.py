"""Artificial associated-record failures retain diagnosis without publication."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine_fixtures import isolated_engine
from git_fixtures import configure_test_repository
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError, canonical
from pdm.model_calls import request_binding
from pdm.pipeline import (_hash, _load, _stage_path, _updated_objects,
                          process_sources, publish)
from pdm.production import (_read, prepare_production, production_status,
                            run_production)
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit
from quality_fixtures import add_quality, passed_review
from test_pipeline import git


class RelatedFailureDiagnosticTests(unittest.TestCase):
    MARKER = "ARTIFICIAL_PRIVATE_RELATED_DIAG_5832"
    DEBUG = "ARTIFICIAL_HOST_ENVELOPE_NOT_TO_SAVE"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-related-diagnostic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "data"
        self.repo.mkdir(mode=0o700)
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Related Diagnostic")
        git(self.repo, "config", "user.email", "related-diagnostic@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial data baseline")
        isolated_engine(self)
        self.path = capture(self.repo, {"source_key": "artificial:associated-diagnostic",
            "source_type": "note", "acquisition_status": "full_text",
            "body": '\ufeff"date","steps"\n"2028-01-01","6"\n"2028-01-02","8"\n'})["path"]
        self.bundle = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.ledger = BudgetLedger.create(self.root / "audit.json", None, 0,
            "Artificial associated diagnostics only", mode="audit")
        self.calls, self.responses = [], []
        self.omit, self.large = False, False
        first = process_sources(self.repo, [self.path], related_runs=[], skill_bundle=self.bundle,
            model_call=BudgetedModelCall(self.ledger, "initial", self.transport))
        self.assertEqual("passed", first["status"], first)
        self.initial = first
        self.publication = publish(self.repo, first["run_id"])
        self.formal = {path: (self.repo / path).read_bytes() for path in self.publication["files"]}
        self.old_audit = deepcopy(self.ledger.snapshot()["operations"])
        self.initial_calls = len(self.calls)
        self.omit = True

    def transport(self, role, payload, timeout=120):
        self.calls.append((role, deepcopy(payload)))
        if role == "extract":
            item = payload["sources"][0]
            lines = (item["body"].splitlines() if "body" in item else
                     [line["text"] for fragment in item["fragments"] for line in fragment["lines"]])
            ref = {"source_id": item["metadata"]["source_id"],
                   "source_version": item["metadata"]["source_version"],
                   "locator": "L1-L3", "quote": lines[0]}
            ids = (["new-only"] if self.omit else ["old-z", "old-a"])
            if self.large:
                ids = ["new-large-" + str(number) for number in range(20)]
            records = [{"id": identifier, "type": "knowledge", "title": "人工学习观测",
                "source_kind": "external_source", "domains": ["learning"], "sources": [deepcopy(ref)],
                "valid_from": None, "valid_to": None, "verification_status": "unverified",
                "lifecycle_status": "undetermined", "body": "人工学习观测扫描2行。 " + self.MARKER
                    + ("x" * 28000 if self.large else "")} for identifier in ids]
            wiki = {"id": "initial-topic", "title": "人工学习主题", "body": "仅汇总所选人工原文。",
                    "sources": [deepcopy(ref)]}
            output = {"objects": records, "reuse_refs": []}
            if payload.get("output_contract") == "batch-v1":
                output["wikis"] = [{**wiki, "record_ids": ids}]
            else:
                output["wiki"] = wiki
            output = add_quality(output, payload["quality_context"])
            stat = next(row for row in payload["quality_context"]["statistics"] if row["operation"] == "row_count")
            output["quality"]["statistic_claims"] = [{"stat_id": stat["stat_id"], "record_id": identifier,
                "body_evidence": "扫描2行"} for identifier in ids]
        else:
            output = passed_review(payload["required_checks"], payload["skill_bundle"])
        response = {"output": output, "model": "artificial-related-model", "usage": None,
            "elapsed_seconds": 0.01, "invocation_id": "model-" + f"{len(self.calls):032x}",
            "host_debug": self.DEBUG, "hidden_reasoning": self.DEBUG}
        self.responses.append(deepcopy(response))
        return response

    def run_failure(self, queue="associated"):
        prepared = prepare_production(self.repo, queue, [self.path], allowed_run_ids=[self.initial["run_id"]],
                                      skill_bundle=self.bundle)
        self.assertIn(prepared["status"], {"ready", "failed"})
        result = run_production(self.repo, queue,
            model_call=BudgetedModelCall(self.ledger, queue, self.transport), max_steps=1)
        self.assertEqual("failed", result["status"], result)
        last = result["last_result"]
        self.assertFalse((self.repo / _stage_path(last["run_id"])).exists())
        self.assertFalse((self.repo / f"memory/runs/processes/{last['run_id']}/review.md").exists())
        self.assertEqual(self.formal, {path: (self.repo / path).read_bytes() for path in self.formal})
        self.assertEqual(["extract"], [role for role, _ in self.calls[self.initial_calls:]])
        failure = _load((self.repo / last["failure_path"]).read_bytes(), "failure")
        return result, failure

    def assert_private_diagnostic(self, result, failure, code):
        self.assertEqual(code, failure["error"])
        diagnostic = failure["extraction_diagnostic"]
        self.assertEqual("unvalidated_not_publishable", diagnostic["status"])
        safe = {key: self.responses[-1][key] for key in
                ("output", "model", "invocation_id", "elapsed_seconds", "usage")}
        self.assertEqual(safe, diagnostic["response"])
        self.assertEqual(_hash(safe), diagnostic["response_sha256"])
        self.assertEqual(len(canonical(safe)), diagnostic["response_bytes"])
        self.assertEqual(request_binding(*self.calls[-1]), diagnostic["request_binding"])
        self.assertEqual("validated_output", diagnostic["validation_target"])
        self.assertEqual({"stat_id", "record_id", "body_evidence"},
                         set(diagnostic["response"]["output"]["quality"]["statistic_claims"][0]))
        self.assertEqual({"stat_id", "record_id", "body_evidence", "value", "sources"},
                         set(diagnostic["validated_output"]["quality"]["statistic_claims"][0]))
        self.assertEqual("completed", diagnostic["validation_context"]["completed_extract"]["status"])
        text = (self.repo / result["last_result"]["failure_path"]).read_text()
        self.assertNotIn(self.DEBUG, text)
        self.assertNotIn(self.MARKER, repr(result))
        self.assertNotIn(self.MARKER, self.ledger.path.read_text())
        for operation, old in self.old_audit.items():
            self.assertEqual(old, self.ledger.snapshot()["operations"][operation])
        return diagnostic

    def test_real_omission_retains_exact_paths_and_safe_raw_materialized_bindings(self):
        result, failure = self.run_failure()
        diagnostic = self.assert_private_diagnostic(result, failure, "related_records_omitted")
        self.assertEqual({"missing_record_paths": ["memory/records/old-a.md", "memory/records/old-z.md"],
            "required_record_paths": ["memory/records/old-a.md", "memory/records/old-z.md"],
            "returned_record_paths": ["memory/records/new-only.md"]},
            diagnostic["validation_error"]["details"])

    def test_other_existing_association_errors_use_the_same_private_diagnostic_path(self):
        # One actual returned output reaches the exact same already-existing
        # catch for each other permitted association error. Historical readers
        # still call the real validator with check_current=False.
        for index, code in enumerate(("related_identity_changed", "correction_evidence_missing", "required_related_context")):
            def fail_current(git_adapter, objects, related, **options):
                if options.get("check_current"):
                    raise CaptureError(code, "Artificial associated error.", details={"artificial_case": code})
                return _updated_objects(git_adapter, objects, related, **options)
            self.initial_calls = len(self.calls)
            with self.subTest(code=code), patch("pdm.pipeline._updated_objects", side_effect=fail_current):
                result, failure = self.run_failure()
            diagnostic = self.assert_private_diagnostic(result, failure, code)
            self.assertEqual({"artificial_case": code}, diagnostic["validation_error"]["details"])

    def test_diagnostic_is_not_a_candidate_and_does_not_refund_or_reset_the_counter(self):
        result, failure = self.run_failure()
        self.assert_private_diagnostic(result, failure, "related_records_omitted")
        adapter = PipelineGit(self.repo)
        paths = adapter.text("ls-tree", "-r", "--name-only", "HEAD", "--",
                             "memory/runs/processes/queue-attempts").splitlines()
        self.assertEqual(1, len(paths))
        counter = _read(adapter, paths[0], "production_attempts")
        self.assertEqual([1], [row["attempt"] for row in counter["attempts"]])
        self.assertEqual("failed", counter["attempts"][0]["status"])
        before = adapter.head(), self.ledger.path.read_bytes(), (self.repo / paths[0]).read_bytes(), len(self.calls)
        with self.assertRaises(CaptureError):
            publish(self.repo, result["last_result"]["run_id"])
        self.assertEqual("failed", production_status(self.repo, "associated")["status"])
        self.assertEqual(before, (adapter.head(), self.ledger.path.read_bytes(),
                                 (self.repo / paths[0]).read_bytes(), len(self.calls)))

    def test_combined_engineering_checkout_does_not_retain_returned_private_body(self):
        directory = self.repo / "project/src/pdm"
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("# Artificial combined checkout only.\n")
        git(self.repo, "add", "project/src/pdm/__init__.py")
        git(self.repo, "commit", "-m", "artificial combined engineering boundary")
        result, failure = self.run_failure()
        self.assertEqual("related_records_omitted", failure["error"])
        self.assertEqual({"status": "not_saved_engineering_root"}, failure["extraction_diagnostic"])
        self.assertNotIn(self.MARKER, (self.repo / result["last_result"]["failure_path"]).read_text())

    def test_oversized_association_failure_explicitly_degrades_without_claiming_response_saved(self):
        self.large = True
        result, failure = self.run_failure()
        self.assertEqual("related_records_omitted", failure["error"])
        diagnostic = failure["extraction_diagnostic"]
        self.assertEqual("not_saved_capacity", diagnostic["status"])
        self.assertNotIn("response", diagnostic)
        self.assertNotIn("validated_output", diagnostic)
        self.assertEqual("related_records_omitted", diagnostic["validation_error"]["code"])
        self.assertEqual(["memory/records/old-a.md", "memory/records/old-z.md"],
                         diagnostic["validation_error"]["details"]["missing_record_paths"])
        self.assertLess((self.repo / result["last_result"]["failure_path"]).stat().st_size, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
