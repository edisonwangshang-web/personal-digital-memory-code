"""New CLI paths use disposable data and mocked transport, never real models."""

import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch, sentinel

import test_runtime
from pdm.budget import BudgetLedger
from pdm.capture import capture
from pdm.cli import main
from pdm.skill_support import resolve_skill_refs, validate_skill_bundle


class M1InterfaceTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp

    def json_file(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def command(self, arguments):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(arguments)
        return code, json.loads(output.getvalue() or error.getvalue())

    def new_source(self):
        return capture(self.repo, {"source_key": "m1-interface-isolated", "source_type": "note",
            "platform": "isolated", "original_url": None, "acquisition_status": "full_text",
            "body": "第一条隔离事实。\n\n第二条隔离事实。\n", "original_time": None})["path"]

    def audit_ledger(self):
        path = self.state / "audit.json"
        BudgetLedger.create(path, None, 0, "isolated-interface-audit", repo=self.repo, mode="audit")
        return path

    def assert_skill_binding(self, bundle):
        validate_skill_bundle(bundle)
        self.assertEqual(bundle, resolve_skill_refs())
        self.assertEqual(bundle["skills"]["extract"]["ref"], "knowledge-extract@8")
        self.assertEqual(bundle["skills"]["review"]["ref"], "knowledge-review@6")

    def test_environment_bind_and_verify_are_explicit_clone_local_operations(self):
        before = test_runtime.git(self.repo, "rev-parse", "HEAD")
        code, value = self.command(["environment", "bind", "--repo", str(self.repo),
                                    "--input", self.json_file("binding.json", self.binding)])
        self.assertEqual((code, value["status"]), (0, "bound"))
        code, value = self.command(["environment", "verify", "--repo", str(self.repo), "--require-binding"])
        self.assertEqual((code, value["status"]), (0, "verified"))
        self.assertEqual(test_runtime.git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(test_runtime.git(self.repo, "status", "--porcelain"), "")

    def test_runner_probe_cannot_promote_receipt_names_into_platform_success(self):
        self.command(["environment", "bind", "--repo", str(self.repo),
                      "--input", self.json_file("binding.json", self.binding)])
        code, value = self.command(["runner", "probe", "--repo", str(self.repo),
            "--input", self.json_file("runner.json", self.config),
            "--receipts", self.json_file("refs.json", ["platform-record-reference"])])
        self.assertEqual((code, value["status"], value["model_calls"]), (4, "blocked", 0))
        self.assertTrue(all(item["status"] == "unverified" for item in value["capabilities"].values()))
        code, value = self.command(["runner", "probe", "--repo", str(self.repo),
            "--input", self.json_file("runner.json", self.config),
            "--receipts", self.json_file("fake.json", [{"passed": True}])])
        self.assertEqual((code, value["error"]), (2, "invalid_capability_receipt"))

    def test_runner_probe_requires_binding_but_legacy_verify_does_not(self):
        code, result = self.command(["environment", "verify", "--repo", str(self.repo)])
        self.assertEqual((code, result["status"]), (0, "unbound"))
        code, result = self.command(["runner", "probe", "--repo", str(self.repo),
            "--input", self.json_file("runner.json", self.config)])
        self.assertEqual((code, result["error"]), (2, "environment_binding_required"))

    def test_audit_creation_has_no_limit_and_keeps_explicit_prior_usage(self):
        ledger = self.state / "audit.json"
        code, result = self.command(["budget", "create", "--repo", str(self.repo), "--ledger", str(ledger),
            "--mode", "audit", "--initial-used", "24", "--authorization-ref", "isolated-daily-policy"])
        self.assertEqual(code, 0)
        self.assertEqual((result["mode"], result["limit"], result["remaining"], result["initial_used"]),
                         ("audit", None, None, 24))
        original = ledger.read_bytes()
        code, result = self.command(["budget", "status", "--repo", str(self.repo), "--ledger", str(ledger)])
        self.assertEqual((code, result["used"]), (0, 24))
        self.assertEqual(ledger.read_bytes(), original)

    def test_audit_limit_and_missing_budget_limit_fail_without_creating_a_ledger(self):
        for extra, error in ((["--mode", "audit", "--limit", "100"], "invalid_budget_limit"),
                             (["--mode", "budget"], "budget_limit_required")):
            path = self.state / "not-created.json"
            code, result = self.command(["budget", "create", "--repo", str(self.repo), "--ledger", str(path),
                "--initial-used", "0", "--authorization-ref", "fixture", *extra])
            self.assertEqual((code, result["error"]), (2, error))
            self.assertFalse(path.exists())

    def test_process_and_ask_open_the_same_audit_ledger_without_resetting_it(self):
        ledger = self.audit_ledger()
        def process(repo, source_paths, expected_head=None, *, model_call, skill_bundle):
            self.assertEqual(source_paths, ["explicit-source"])
            self.assert_skill_binding(skill_bundle)
            self.assertEqual(model_call.ledger.snapshot()["mode"], "audit")
            self.assertIsNone(model_call.ledger.snapshot()["limit"])
            self.assertEqual(model_call.operation_id, "fixed-process")
            return {"status": "passed"}
        with patch("pdm.pipeline.process_sources", side_effect=process):
            self.assertEqual(self.command(["process", "--repo", str(self.repo), "--source", "explicit-source",
                "--budget-ledger", str(ledger), "--operation-id", "fixed-process"])[0], 0)
        def answer(repo, source, question, run=None, *, model_call):
            self.assertEqual(model_call.ledger.path, ledger)
            self.assertEqual(model_call.operation_id, "fixed-answer")
            return {"answer": "isolated", "citations": []}
        with patch("pdm.evidence.answer", side_effect=answer):
            self.assertEqual(self.command(["ask", "--repo", str(self.repo), "--source", "explicit-source",
                "--question", "isolated", "--budget-ledger", str(ledger), "--operation-id", "fixed-answer"])[0], 0)
        self.assertEqual(BudgetLedger(ledger, repo=self.repo).snapshot()["started_calls"], 0)

    def test_batch_plan_project_use_real_sources_and_preserve_global_read_coverage(self):
        source = self.new_source()
        before = test_runtime.git(self.repo, "rev-parse", "HEAD")
        code, plan = self.command(["batch", "plan", "--repo", str(self.repo), "--source", source,
            "--max-input-bytes", "20000", "--overhead-bytes", "128"])
        self.assertEqual((code, plan["status"]), (0, "ready"))
        plan_path = self.json_file("batch-plan.json", plan)
        arguments = ["batch", "project", "--repo", str(self.repo), "--source", source,
                     "--plan", plan_path, "--batch-id", plan["batches"][0]["batch_id"]]
        code, projection = self.command(arguments)
        self.assertEqual(code, 0)
        self.assertTrue(projection["coverage"]["complete"])
        self.assertTrue(projection["coverage"]["global_locators"])
        code, projection = self.command([*arguments, "--budget-bytes", "1"])
        self.assertEqual(code, 4)
        self.assertFalse(projection["coverage"]["complete"])
        self.assertEqual(test_runtime.git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(test_runtime.git(self.repo, "status", "--porcelain"), "")

    def test_current_reading_passes_explicit_objects_and_compact_option(self):
        objects = ["memory/topics/learning-period.md", "memory/records/work-decision.md"]
        base = ["--repo", str(self.repo), "--source", "explicit-source", "--current",
                "--object", objects[0], "--object", objects[1], "--compact-sources"]
        with patch("pdm.evidence.build_current_evidence", return_value={"scope": "selected"}) as read:
            self.assertEqual(self.command(["evidence", *base]), (0, {"scope": "selected"}))
        self.assertEqual(read.call_args.kwargs, {
            "history": False, "object_paths": objects, "compact_sources": True})
        with patch("pdm.cli._model_budget", return_value=sentinel.model), patch(
                "pdm.evidence.ask_current", return_value={"answer": "selected"}) as ask:
            self.assertEqual(self.command(["ask", *base, "--question", "依据是什么？"]),
                             (0, {"answer": "selected"}))
        self.assertEqual(ask.call_args.kwargs, {
            "history": False, "model_call": sentinel.model,
            "object_paths": objects, "compact_sources": True})

    def test_object_reading_requires_current_before_any_model_call(self):
        base = ["--repo", str(self.repo), "--source", "explicit-source"]
        for selection in (["--object", "memory/records/work-decision.md"], ["--compact-sources"]):
            with self.subTest(selection=selection), patch("pdm.cli._model_budget", return_value=sentinel.model), patch(
                    "pdm.evidence.answer") as answer:
                for command in (["evidence", *base, *selection],
                                ["ask", *base, *selection, "--question", "依据是什么？"]):
                    code, result = self.command(command)
                    self.assertEqual(code, 2)
                    self.assertEqual(result["error"], "ValueError")
                answer.assert_not_called()

    def test_context_discovery_and_empty_continuation_plan_are_read_only(self):
        source = self.new_source()
        before = test_runtime.git(self.repo, "rev-parse", "HEAD")
        code, result = self.command(["context", "discover", "--repo", str(self.repo),
            "--source", source, "--allow-source", source])
        self.assertEqual(code, 0)
        self.assertEqual(result["related_runs"], [])
        code, result = self.command(["context", "continuations", "plan", "--repo", str(self.repo),
            "--allow-source", source])
        self.assertEqual((code, result["jobs"], result["model_calls"]), (0, [], 0))
        self.assertEqual(test_runtime.git(self.repo, "rev-parse", "HEAD"), before)

    def test_pending_list_preserves_list_output_and_registration_repair_exit_status(self):
        before = test_runtime.git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.command(["pending", "list", "--repo", str(self.repo)]), (0, []))
        with patch("pdm.pending.list_pending", return_value=[{"pending_status": "open"}]):
            self.assertEqual(self.command(["pending", "list", "--repo", str(self.repo)]),
                             (0, [{"pending_status": "open"}]))
        for status, expected in (("registration_required", 4), ("needs_confirmation", 0)):
            with patch("pdm.pipeline.resume_pending_registration", return_value={"status": status, "model_calls": 0}) as repair:
                code, result = self.command(["pending", "repair-registration", "--repo", str(self.repo),
                    "--run", "run-" + "a" * 32, "--source", "explicit-source"])
            self.assertEqual((code, result["status"]), (expected, status))
            repair.assert_called_once_with(self.repo, "run-" + "a" * 32,
                                           allowed_source_paths=["explicit-source"], expected_head=None)
        self.assertEqual(test_runtime.git(self.repo, "rev-parse", "HEAD"), before)

    def test_continuation_execution_uses_immutable_job_id_as_call_operation(self):
        job = {"job_id": "continuation-" + "f" * 64, "source_paths": ["fixed-source"],
               "related_runs": ["fixed-run"], "head": self.base}
        job_path, ledger = self.json_file("job.json", job), self.audit_ledger()
        def execute(repo, value, *, process_call):
            processed = process_call(repo, value)
            self.assertEqual(processed["status"], "passed")
            return {"status": "resolved", "resolved": True}
        def process(repo, paths, head, *, related_runs, model_call, skill_bundle):
            self.assertEqual((paths, head, related_runs), (["fixed-source"], self.base, ["fixed-run"]))
            self.assert_skill_binding(skill_bundle)
            self.assertEqual(model_call.operation_id, job["job_id"])
            self.assertEqual(model_call.ledger.snapshot()["mode"], "audit")
            return {"status": "passed"}
        with patch("pdm.context.execute_continuation", side_effect=execute), patch("pdm.pipeline.process_sources", side_effect=process):
            self.assertEqual(self.command(["context", "continuations", "execute", "--repo", str(self.repo),
                "--input", job_path, "--budget-ledger", str(ledger)])[0], 0)
        code, result = self.command(["context", "continuations", "execute", "--repo", str(self.repo),
            "--input", job_path, "--budget-ledger", str(ledger), "--operation-id", "changed-id"])
        self.assertEqual((code, result["error"]), (2, "continuation_operation_mismatch"))

    def test_drain_forwards_persisted_continuation_and_reports_required_reconciliation(self):
        trigger, continuation = {"kind": "manual"}, {"fixed": "continuation"}
        trigger_path = self.json_file("trigger.json", trigger)
        continuation_path = self.json_file("continuation.json", continuation)
        with patch("pdm.cli._model_budget", return_value=sentinel.calls), \
                patch("pdm.scheduler.drain_schedule", return_value={"status": "partial", "next_action": "continue"}) as drain:
            code, _ = self.command(["schedule", "drain", "--repo", str(self.repo), "--id", "daily",
                "--trigger", trigger_path, "--continuation", continuation_path, "--max-slices", "2",
                "--window-seconds", "5", "--operation-id", "fixed-window"])
        self.assertEqual(code, 0)
        drain.assert_called_once_with(self.repo, "daily", trigger, None, model_call=sentinel.calls,
                                      max_slices=2, window_seconds=5, continuation=continuation)
        with patch("pdm.cli._model_budget", return_value=sentinel.calls), \
                patch("pdm.scheduler.drain_schedule", return_value={"status": "reconciliation_required", "next_action": "reconcile"}):
            self.assertEqual(self.command(["schedule", "drain", "--repo", str(self.repo), "--id", "daily",
                "--trigger", trigger_path, "--operation-id", "fixed-window"])[0], 4)

    def test_promotion_cli_refuses_private_data_in_actual_merge_tree(self):
        (self.repo / "memory").mkdir()
        (self.repo / "memory/dev-only.md").write_text("isolated development data")
        test_runtime.git(self.repo, "add", ".")
        test_runtime.git(self.repo, "commit", "-m", "development data")
        dev = test_runtime.git(self.repo, "rev-parse", "HEAD")
        tree = test_runtime.git(self.repo, "rev-parse", "HEAD^{tree}")
        candidate = test_runtime.git(self.repo, "commit-tree", tree, "-p", self.base, "-p", dev, "-m", "candidate")
        code, result = self.command(["promotion", "check", "--repo", str(self.repo), "--base", self.base,
            "--candidate", candidate, "--dev-commit", dev])
        self.assertEqual((code, result["status"]), (4, "blocked"))
        self.assertEqual(result["protected_changes"][0]["path"], "memory/dev-only.md")
        self.assertEqual(test_runtime.git(self.repo, "rev-parse", "master"), self.base)

    def test_prepare_cli_creates_an_unattached_object_with_code_and_keeps_dev_data(self):
        source = self.new_source()
        (self.repo / "program.py").write_text("isolated program")
        test_runtime.git(self.repo, "add", ".")
        test_runtime.git(self.repo, "commit", "-m", "development program")
        dev = test_runtime.git(self.repo, "rev-parse", "HEAD")
        raw = (self.repo / source).read_bytes()
        refs = test_runtime.git(self.repo, "show-ref")
        code, result = self.command(["promotion", "prepare", "--repo", str(self.repo),
            "--base", self.base, "--dev-commit", dev])
        self.assertEqual((code, result["status"]), (0, "prepared"))
        self.assertEqual(result["report"]["status"], "passed")
        self.assertEqual(test_runtime.git(self.repo, "show-ref"), refs)
        self.assertEqual((self.repo / source).read_bytes(), raw)
        self.assertEqual(test_runtime.git(self.repo, "ls-tree", "-r", "--name-only", result["candidate_commit"], "memory"), "")

    def test_production_prepare_and_status_persist_a_real_plan_without_model_calls(self):
        source = self.new_source()
        code, prepared = self.command(["production", "prepare", "--repo", str(self.repo), "--id", "fixture-queue",
            "--source", source])
        self.assertEqual((code, prepared["status"]), (0, "ready"))
        self.assertTrue((self.repo / prepared["state_path"]).is_file())
        head = test_runtime.git(self.repo, "rev-parse", "HEAD")
        code, status = self.command(["production", "status", "--repo", str(self.repo), "--id", "fixture-queue"])
        self.assertEqual((code, status["status"]), (0, "ready"))
        self.assertEqual(status["delivery"], {"scope": "local_only", "remote_status": "not_requested"})
        self.assertEqual(test_runtime.git(self.repo, "rev-parse", "HEAD"), head)

    def test_new_production_worker_requires_binding_and_explicit_local_override(self):
        ledger = self.audit_ledger()
        arguments = ["production", "run", "--repo", str(self.repo), "--id", "fixture-queue",
                     "--budget-ledger", str(ledger), "--operation-id", "worker"]
        code, result = self.command(arguments)
        self.assertEqual((code, result["error"]), (2, "environment_binding_required"))
        for extra, binding in (([], True), (["--local-only"], False)):
            with patch("pdm.cli._model_budget", return_value=sentinel.calls), \
                    patch("pdm.production.run_production", return_value={"status": "ready"}) as run:
                code, result = self.command([*arguments, "--max-steps", "2", *extra])
            self.assertEqual(code, 0)
            run.assert_called_once_with(self.repo, "fixture-queue", model_call=sentinel.calls,
                                         max_steps=2, require_binding=binding)
            self.assertEqual(result["delivery"]["remote_status"], "not_requested")

    def test_reading_batch_projection_keeps_the_whole_authorized_source_scope(self):
        projected = {"source_views": [{"path": "selected-one"}], "batch_context": {"fixed": "context"},
                     "coverage": {"complete": True}}
        plan = self.json_file("read-plan.json", {"fixed": "plan"})
        with patch("pdm.batching.project_batch_sources", return_value=projected) as projection, \
                patch("pdm.evidence.build_current_evidence", return_value={"scope": "selected"}) as read:
            code, _ = self.command(["evidence", "--current", "--repo", str(self.repo),
                "--source", "selected-one", "--source", "selected-two", "--plan", plan, "--batch", "fixed-batch"])
        self.assertEqual(code, 0)
        projection.assert_called_once_with(self.repo, {"fixed": "plan"}, "fixed-batch",
                                            source_paths=["selected-one", "selected-two"])
        read.assert_called_once_with(self.repo, ["selected-one", "selected-two"], None, history=False,
                                     source_views=projected["source_views"], batch_context=projected["batch_context"])

    def test_multi_batch_reading_merges_explicit_selection_and_passes_same_views_to_ask(self):
        projected = {"source_views": [{"path": "selected"}], "batch_context": {"fixed": "merge"},
                     "coverage": {"complete": True}}
        plan = self.json_file("read-plan.json", {"fixed": "plan"})
        with patch("pdm.cli._model_budget", return_value=sentinel.calls), \
                patch("pdm.batching.merge_batch_sources", return_value=projected) as merge, \
                patch("pdm.evidence.ask_current", return_value={"answer": "isolated"}) as answer:
            code, _ = self.command(["ask", "--current", "--repo", str(self.repo), "--source", "selected",
                "--plan", plan, "--batch", "batch-a", "--batch", "batch-b", "--question", "isolated",
                "--budget-bytes", "12345"])
        self.assertEqual(code, 0)
        merge.assert_called_once_with(self.repo, {"fixed": "plan"}, ["batch-a", "batch-b"],
                                      source_paths=["selected"], max_bytes=12345)
        answer.assert_called_once_with(self.repo, ["selected"], "isolated", None, history=False,
            model_call=sentinel.calls, max_bytes=12345, source_views=projected["source_views"],
            batch_context=projected["batch_context"])

    def test_missing_plan_or_blocked_projection_cannot_reach_answering(self):
        code, result = self.command(["evidence", "--current", "--repo", str(self.repo),
            "--source", "selected", "--batch", "missing-plan"])
        self.assertEqual((code, result["error"]), (2, "batch_plan_and_selection_required"))
        plan = self.json_file("read-plan.json", {"fixed": "plan"})
        with patch("pdm.cli._model_budget", return_value=sentinel.calls), \
                patch("pdm.batching.project_batch_sources", return_value={"coverage": {"complete": False}, "source_views": []}), \
                patch("pdm.evidence.ask_current") as answer:
            code, result = self.command(["ask", "--current", "--repo", str(self.repo), "--source", "selected",
                "--question", "isolated", "--plan", plan, "--batch", "blocked"])
        self.assertEqual((code, result["error"]), (2, "batch_read_projection_incomplete"))
        answer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
