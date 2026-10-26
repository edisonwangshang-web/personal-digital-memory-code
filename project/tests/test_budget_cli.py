"""Exercise explicit CLI budget bindings without a live model or personal data."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall
from pdm.cli import main
from pdm.skill_support import resolve_skill_refs, validate_skill_bundle


class BudgetCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-budget-cli-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = self.directory / "data"
        self.repo.mkdir(mode=0o700)
        self.private = self.directory / "budgets"
        self.private.mkdir(mode=0o700)
        self.ledger = self.private / "calls.json"

    def invoke(self, arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(arguments)
        return code, output.getvalue(), errors.getvalue()

    def create(self, limit=10, initial_used=0):
        code, output, errors = self.invoke([
            "budget", "create", "--repo", str(self.repo), "--ledger", str(self.ledger),
            "--limit", str(limit), "--initial-used", str(initial_used),
            "--authorization-ref", "isolated-engineering-test",
        ])
        self.assertEqual(code, 0, errors)
        return json.loads(output)

    def binding(self, operation="isolated-operation"):
        return ["--budget-ledger", str(self.ledger), "--operation-id", operation]

    def test_processing_cli_timeout_reaches_the_actual_transport(self):
        self.create(limit=8)
        seen = []
        def transport(role, payload, timeout, *, on_start):
            on_start("model-" + "a" * 32)
            seen.append(timeout)
            return {"invocation_id": "model-" + "a" * 32}
        def process(repo, paths, head, *, model_call, **options):
            model_call.reserve_calls(2)
            model_call("extract", {})
            model_call("review", {})
            return {"status": "passed"}
        def production(repo, queue, *, model_call, **options):
            model_call.for_operation(model_call.operation_id + ":child")("review", {})
            return {"status": "published"}
        cases = ((["process", "--source", "selected"], "pdm.pipeline.process_sources", process, 900),
                 (["process", "--source", "selected", "--model-timeout", "1800"], "pdm.pipeline.process_sources", process, 1800),
                 (["process", "--source", "selected", "--revision-from-run", "run-" + "a" * 32],
                  "pdm.pipeline.process_sources", process, 900),
                 (["production", "run", "--id", "selected", "--local-only"], "pdm.production.run_production", production, 900),
                 (["production", "run", "--id", "selected", "--local-only", "--model-timeout", "601"],
                  "pdm.production.run_production", production, 601))
        for index, (arguments, target, entry, expected) in enumerate(cases):
            before = len(seen)
            with self.subTest(arguments=arguments), patch(target, side_effect=entry), \
                    patch("pdm.cli._selected_skills", return_value=None), \
                    patch("pdm.model_calls._call_transport", side_effect=transport):
                code, _, errors = self.invoke([*arguments, "--repo", str(self.repo), *self.binding(f"timeout-{index}")])
            self.assertEqual(code, 0, errors)
            self.assertTrue(seen[before:])
            self.assertEqual(set(seen[before:]), {expected})

    def test_invalid_cli_timeout_is_rejected_before_opening_the_ledger(self):
        self.create()
        before = self.ledger.read_bytes()
        for timeout in ("0", "1801", "-1"):
            for arguments in (["process", "--source", "selected"],
                              ["production", "run", "--id", "selected"]):
                with self.subTest(arguments=arguments, timeout=timeout), \
                        patch("pdm.budget.BudgetLedger", side_effect=AssertionError("ledger must not be opened")), \
                        patch("pdm.model_calls._call_transport", side_effect=AssertionError("transport")):
                    code, _, errors = self.invoke([*arguments, "--repo", str(self.repo), *self.binding(),
                                                  "--model-timeout", timeout])
                self.assertEqual((code, json.loads(errors)["error"]), (2, "invalid_timeout"))
                self.assertEqual(self.ledger.read_bytes(), before)

    def test_dedicated_recovery_keeps_its_default_and_accepts_an_explicit_timeout(self):
        from pdm.cli import _model_budget, parser
        self.create()
        before = self.ledger.read_bytes()
        entries = (["resume-unstarted-revision", "--run", "run-" + "a" * 32, "--source", "selected",
                    "--reservation", "reservation.json", "--expected-reservation-sha256", "sha256-" + "a" * 64],
                   ["revalidate-completed-extraction", "--run", "run-" + "a" * 32, "--source", "selected",
                    "--attestation", "attestation.json", "--expected-attestation-sha256", "sha256-" + "a" * 64,
                    "--original-payload", "payload.json", "--response", "response.json",
                    "--original-resume-context", "resume.json"])
        for entry in entries:
            for extra, expected in (([], 120), (["--model-timeout", "900"], 900)):
                with self.subTest(command=entry[0], extra=extra):
                    args = parser().parse_args([*entry, "--repo", str(self.repo), *self.binding(), *extra])
                    self.assertEqual(_model_budget(args).default_timeout, expected)
                    self.assertEqual(self.ledger.read_bytes(), before)

    def assert_skill_binding(self, bundle):
        validate_skill_bundle(bundle)
        self.assertEqual(bundle, resolve_skill_refs())
        self.assertEqual(bundle["skills"]["extract"]["ref"], "knowledge-extract@10")
        self.assertEqual(bundle["skills"]["review"]["ref"], "knowledge-review@8")

    def test_create_requires_explicit_initial_usage_and_never_overwrites(self):
        state = self.create(limit=7, initial_used=3)
        self.assertEqual((state["limit"], state["initial_used"], state["used"], state["remaining"]),
                         (7, 3, 3, 4))
        self.assertEqual(state["operations"], {})
        self.assertNotIn("isolated-engineering-test", self.ledger.read_text())
        before = self.ledger.read_bytes()
        code, output, errors = self.invoke([
            "budget", "create", "--repo", str(self.repo), "--ledger", str(self.ledger),
            "--limit", "70", "--initial-used", "0", "--authorization-ref", "different-audit-ref",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(errors)["error"], "budget_already_exists")
        self.assertEqual(output, "")
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_status_does_not_change_ledger_or_create_a_missing_one(self):
        expected = self.create(limit=5, initial_used=5)
        lock = self.ledger.with_name(self.ledger.name + ".lock")
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.ledger, lock)]
        code, output, errors = self.invoke([
            "budget", "status", "--repo", str(self.repo), "--ledger", str(self.ledger),
        ])
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output), expected)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.ledger, lock)], before)
        missing = self.private / "missing.json"
        code, _, errors = self.invoke([
            "budget", "status", "--repo", str(self.repo), "--ledger", str(missing),
        ])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(errors)["error"], "budget_missing")
        self.assertFalse(missing.exists())
        self.assertFalse(missing.with_name(missing.name + ".lock").exists())

    def test_create_rejects_ledger_inside_the_data_repository(self):
        inside = self.repo / "calls.json"
        code, output, errors = self.invoke([
            "budget", "create", "--repo", str(self.repo), "--ledger", str(inside),
            "--limit", "2", "--initial-used", "0", "--authorization-ref", "test-only",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(errors)["error"], "budget_inside_repository")
        self.assertEqual(output, "")
        self.assertEqual(list(self.repo.iterdir()), [])

    def test_missing_budget_flags_reject_before_model_entry_or_trigger_read(self):
        commands = [
            (["process", "--repo", str(self.repo), "--source", "selected.md"], "pdm.pipeline.process_sources"),
            (["ask", "--repo", str(self.repo), "--source", "selected.md", "--question", "PRIVATE_QUESTION"],
             "pdm.evidence.answer"),
            (["schedule", "run", "--repo", str(self.repo), "--id", "daily", "--trigger", "/missing/trigger.json"],
             "pdm.scheduler.run_daily"),
        ]
        for arguments, target in commands:
            for flags, expected in (([], "budget_ledger_required"),
                                    (["--budget-ledger", str(self.ledger)], "budget_operation_required")):
                with self.subTest(command=arguments[:2], flags=flags), patch(target) as entry, \
                        patch("pdm.model_calls._call_transport") as transport, patch("pdm.cli._input_json") as read:
                    code, output, errors = self.invoke(arguments + flags)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(errors)["error"], expected)
                self.assertNotIn("PRIVATE_QUESTION", errors)
                self.assertEqual(output, "")
                entry.assert_not_called()
                transport.assert_not_called()
                read.assert_not_called()
        self.assertEqual(list(self.private.iterdir()), [])

    def test_argument_errors_return_only_safe_code(self):
        commands = [
            ["budget", "create", "--repo", str(self.repo), "--ledger", str(self.ledger),
             "--limit", "SECRET_INVALID", "--initial-used", "0", "--authorization-ref", "AUDIT_PRIVATE"],
            ["budget", "create", "--repo", str(self.repo), "--ledger", str(self.ledger),
             "--limit", "2", "--authorization-ref", "AUDIT_PRIVATE"],
            ["ask", "--repo", str(self.repo), "--source", "selected.md", "--question", "PRIVATE_QUESTION",
             "--unknown-option", "SECRET_INVALID"],
        ]
        for arguments in commands:
            with self.subTest(arguments=arguments):
                code, output, errors = self.invoke(arguments)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(errors), {"ok": False, "error": "invalid_arguments"})
            self.assertEqual(output, "")
        self.assertEqual(list(self.private.iterdir()), [])

    def test_each_model_entry_receives_the_selected_persistent_binding(self):
        self.create()
        trigger = self.directory / "trigger.json"
        trigger.write_text("{}")
        cases = [
            (["process", "--source", "one.md"], "pdm.pipeline.process_sources", {"status": "passed"}),
            (["process", "--source", "one.md", "--source", "two.md"],
             "pdm.pipeline.process_sources", {"status": "passed"}),
            (["process", "--source", "one.md", "--incremental", "--related-run", "old-run"],
             "pdm.pipeline.process_sources", {"status": "already_processed"}),
            (["ask", "--source", "one.md", "--question", "artificial?"], "pdm.evidence.answer", {}),
            (["ask", "--source", "one.md", "--source", "two.md", "--question", "artificial?"],
             "pdm.evidence.ask_sources", {}),
            (["ask", "--source", "one.md", "--question", "artificial?", "--current", "--budget-bytes", "1000"],
             "pdm.evidence.ask_current", {}),
            (["schedule", "run", "--id", "daily", "--trigger", str(trigger)],
             "pdm.scheduler.run_daily", {"status": "completed"}),
        ]
        for arguments, target, result in cases:
            with self.subTest(command=arguments), patch(target, return_value=result) as entry:
                code, _, errors = self.invoke(arguments + ["--repo", str(self.repo)] + self.binding())
            self.assertEqual(code, 0, errors)
            adapter = entry.call_args.kwargs["model_call"]
            self.assertIsInstance(adapter, BudgetedModelCall)
            self.assertEqual(adapter.ledger.path, self.ledger)
            self.assertEqual(adapter.ledger.repo, self.repo)
            self.assertEqual(adapter.operation_id, "isolated-operation")
            if arguments[0] == "process":
                self.assert_skill_binding(entry.call_args.kwargs["skill_bundle"])
                self.assertEqual(entry.call_args.args[1],
                                 [arguments[index + 1] for index, value in enumerate(arguments)
                                  if value == "--source"])
        self.assertEqual(BudgetLedger(self.ledger).snapshot()["used"], 0)

    def test_process_ask_and_schedule_share_one_balance_and_retries_do_not_start_again(self):
        self.create(limit=4)
        started = []

        def transport(role, payload, timeout=120, *, on_start):
            on_start("model-" + "a" * 32)
            started.append(role)
            return {"output": {}, "invocation_id": "model-" + "a" * 32}

        def process(repo, source_paths, expected_head, *, model_call, skill_bundle):
            self.assertEqual(source_paths, ["selected.md"])
            self.assert_skill_binding(skill_bundle)
            model_call.reserve_calls(2)
            model_call("extract", {"artificial": "selected"})
            model_call("review", {"artificial": "candidate"})
            return {"status": "passed"}

        def answer(repo, source, question, run, *, model_call):
            model_call.reserve_calls(1)
            model_call("answer", {"artificial": "question"})
            return {"answer": "artificial"}

        def schedule(repo, schedule_id, trigger, expected_head, *, model_call):
            attempt = model_call.for_operation(model_call.operation_id + ".attempt-1")
            attempt.reserve_calls(2)
            self.fail("exhausted schedule must not begin an attempt")

        trigger = self.directory / "trigger.json"
        trigger.write_text("{}")
        with patch("pdm.model_calls._call_transport", side_effect=transport), \
                patch("pdm.pipeline.process_sources", side_effect=process), \
                patch("pdm.evidence.answer", side_effect=answer), \
                patch("pdm.scheduler.run_daily", side_effect=schedule):
            process_args = ["process", "--repo", str(self.repo), "--source", "selected.md"] + self.binding("process-1")
            code, _, errors = self.invoke(process_args)
            self.assertEqual(code, 0, errors)
            code, _, errors = self.invoke([
                "ask", "--repo", str(self.repo), "--source", "selected.md", "--question", "artificial?",
            ] + self.binding("answer-1"))
            self.assertEqual(code, 0, errors)
            before = self.ledger.read_bytes()
            code, output, errors = self.invoke([
                "schedule", "run", "--repo", str(self.repo), "--id", "daily", "--trigger", str(trigger),
            ] + self.binding("daily-1"))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(errors), {"ok": False, "error": "budget_exhausted",
                                                  "attempted_calls": 0, "operation_started_calls": 0})
            self.assertEqual(output, "")
            self.assertEqual(self.ledger.read_bytes(), before)
            code, _, errors = self.invoke(process_args)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(errors), {"ok": False, "error": "budget_call_already_started",
                                                  "attempted_calls": 0, "operation_started_calls": 2})
        self.assertEqual(started, ["extract", "review", "answer"])
        state = BudgetLedger(self.ledger).snapshot()
        self.assertEqual((state["used"], state["remaining"], state["started_calls"]), (3, 1, 3))
        self.assertNotIn("daily-1.attempt-1", state["operations"])

    def test_zero_model_commands_do_not_require_or_open_a_ledger(self):
        with patch("pdm.cli._model_budget", side_effect=AssertionError("unexpected budget")), \
                patch("pdm.evidence.evidence", return_value={"model_calls": 0}) as evidence, \
                patch("pdm.pipeline.publish", return_value={"status": "published"}) as publish, \
                patch("pdm.scheduler.schedule_status", return_value={"status": "paused"}) as status:
            for arguments in (
                ["evidence", "--source", "selected.md"],
                ["publish", "--run", "selected-run"],
                ["schedule", "status", "--id", "daily"],
            ):
                code, _, errors = self.invoke(arguments + ["--repo", str(self.repo)])
                self.assertEqual(code, 0, errors)
        evidence.assert_called_once()
        publish.assert_called_once()
        status.assert_called_once()
        self.assertEqual(list(self.private.iterdir()), [])

    def test_post_start_accounting_failure_reports_call_without_echoing_payload(self):
        self.create(limit=2)
        started = []

        def answer(repo, source, question, run, *, model_call):
            model_call.reserve_calls(1)
            model_call("answer", {"question": question, "body": "PRIVATE_SOURCE_BODY"})
            self.fail("an accounting failure must not return an answer")

        for outcome in ("completed", "transport_error"):
            def transport(role, payload, timeout=120, *, on_start):
                on_start("model-" + "b" * 32)
                started.append(role)
                if outcome == "transport_error":
                    raise RuntimeError("PRIVATE_TRANSPORT_DETAIL")
                return {"output": {"answer": "PRIVATE_ANSWER"}, "invocation_id": "model-" + "b" * 32}

            with self.subTest(outcome=outcome), \
                    patch("pdm.evidence.answer", side_effect=answer), \
                    patch("pdm.model_calls._call_transport", side_effect=transport), \
                    patch.object(BudgetLedger, "finish", side_effect=BudgetBlocked("budget_write_failed")):
                code, output, errors = self.invoke([
                    "ask", "--repo", str(self.repo), "--source", "selected.md", "--question", "PRIVATE_QUESTION",
                ] + self.binding(outcome))
            self.assertEqual(code, 2)
            self.assertEqual(output, "")
            self.assertEqual(json.loads(errors), {"ok": False, "error": "budget_write_failed",
                                                  "attempted_calls": 1, "operation_started_calls": 1})
            self.assertNotIn("PRIVATE", errors)
        self.assertEqual(started, ["answer", "answer"])
        state = BudgetLedger(self.ledger).snapshot()
        self.assertEqual((state["used"], state["remaining"], state["started_calls"]), (2, 0, 2))
        self.assertTrue(all(operation["calls"]["answer"]["status"] == "started"
                            for operation in state["operations"].values()))

    def test_review_start_failure_preserves_known_extraction_count(self):
        self.create(limit=2)
        started = []
        after_extract = []

        def transport(role, payload, timeout=120, *, on_start):
            on_start("model-" + "c" * 32)
            started.append(role)
            return {"output": {}, "invocation_id": "model-" + "c" * 32}

        def process(repo, source_paths, expected_head, *, model_call, skill_bundle):
            self.assertEqual(source_paths, ["selected.md"])
            self.assert_skill_binding(skill_bundle)
            model_call.reserve_calls(2)
            model_call("extract", {"body": "PRIVATE_SOURCE_BODY"})
            after_extract.append(self.ledger.read_bytes())
            with patch.object(model_call.ledger, "start", side_effect=BudgetBlocked("budget_write_failed")):
                model_call("review", {"candidate": "PRIVATE_CANDIDATE"})
            self.fail("failed review start must not succeed")

        arguments = ["process", "--repo", str(self.repo), "--source", "selected.md"] + self.binding("pair")
        with patch("pdm.pipeline.process_sources", side_effect=process), \
                patch("pdm.model_calls._call_transport", side_effect=transport):
            code, output, errors = self.invoke(arguments)
            self.assertEqual(code, 2)
            self.assertEqual(output, "")
            self.assertEqual(json.loads(errors), {"ok": False, "error": "budget_write_failed",
                                                  "attempted_calls": 0, "operation_started_calls": 1})
            self.assertNotIn("PRIVATE", errors)
            self.assertEqual(self.ledger.read_bytes(), after_extract[0])
            code, _, errors = self.invoke(arguments)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(errors)["error"], "budget_call_already_started")
        self.assertEqual(started, ["extract"])
        state = BudgetLedger(self.ledger).snapshot()
        self.assertEqual((state["used"], state["remaining"], state["started_calls"]), (2, 0, 1))
        self.assertEqual(set(state["operations"]["pair"]["calls"]), {"extract"})

    def test_returned_budget_blocked_schedule_status_is_nonzero(self):
        self.create()
        trigger = self.directory / "trigger.json"
        trigger.write_text("{}")
        with patch("pdm.scheduler.run_daily", return_value={"status": "budget_blocked", "attempted_calls": 0}):
            code, output, errors = self.invoke([
                "schedule", "run", "--repo", str(self.repo), "--id", "daily", "--trigger", str(trigger),
            ] + self.binding())
        self.assertEqual(code, 4, errors)
        self.assertEqual(json.loads(output)["attempted_calls"], 0)


if __name__ == "__main__":
    unittest.main()
