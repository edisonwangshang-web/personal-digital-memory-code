"""Safe CLI diagnostics from artificial offline events; no live service calls."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdm import model_calls
from pdm.budget import BudgetBlocked, BudgetLedger
from pdm.model_calls import ModelCallError
import test_model_calls as cli_fixtures


SECRET = "ARTIFICIAL_PRIVATE_VALUE_DO_NOT_RETAIN"


class SafeModelDiagnosticTests(unittest.TestCase):
    setUp = cli_fixtures.ModelCallTests.setUp
    cli = cli_fixtures.ModelCallTests.cli
    call = cli_fixtures.ModelCallTests.call
    event = cli_fixtures.ModelCallTests.event

    def failure(self, event, expected):
        behavior = self.event(event.pop("type"), **event)
        with self.cli(behavior), self.assertRaises(ModelCallError) as caught:
            self.call("answer", {"fixture": SECRET})
        error = caught.exception
        self.assertEqual(expected, error.code)
        self.assertNotIn(SECRET, str(error))
        self.assertNotIn(SECRET, repr(vars(error)))
        self.assertRegex(error.invocation_id, r"^model-[0-9a-f]{32}$")
        return error

    def test_explicit_codes_receive_fixed_categories(self):
        examples = {
            "authentication_error": "cli_auth_required",
            "token_expired": "cli_auth_required",
            "forbidden": "cli_permission_denied",
            "rate_limit_exceeded": "cli_rate_limited",
            "insufficient_quota": "cli_rate_limited",
            "service_unavailable": "cli_service_unavailable",
            "connection_error": "cli_network_failure",
            "http_connection_failed": "cli_network_failure",
            "context_length_exceeded": "cli_request_rejected",
            "invalid_json_schema": "cli_request_rejected",
        }
        for code, category in examples.items():
            with self.subTest(code=code):
                self.failure({"type": "turn.failed", "error": {
                    "code": code, "message": SECRET, "url": "https://example.invalid/" + SECRET}}, category)

    def test_structured_http_statuses_receive_fixed_categories(self):
        for status, category in ((401, "cli_auth_required"), (403, "cli_permission_denied"),
                (429, "cli_rate_limited"), (500, "cli_service_unavailable"),
                (503, "cli_service_unavailable"), (413, "cli_request_rejected")):
            for value in (status, str(status)):
                with self.subTest(status=value):
                    self.failure({"type": "error", "response": {
                        "status_code": value, "error": {"message": SECRET}}}, category)

    def test_matching_nested_code_and_status_do_not_conflict(self):
        self.failure({"type": "error", "status": 401,
            "error": {"type": "AUTHENTICATION_ERROR", "message": SECRET}}, "cli_auth_required")

    def test_conflicting_structured_categories_keep_fallback(self):
        self.failure({"type": "turn.failed", "status": 403,
            "error": {"code": "rate_limit_exceeded", "message": SECRET}}, "cli_turn_failed")

    def test_message_only_failure_does_not_guess_or_expose_reason(self):
        self.failure({"type": "turn.failed", "error": {
            "message": "HTTP 401 network_error token=" + SECRET}}, "cli_turn_failed")
        self.failure({"type": "error", "code": "unknown_" + SECRET,
            "message": SECRET}, "cli_turn_failed")

    def test_wrong_types_unknown_status_and_code_suffix_keep_fallback(self):
        for metadata in ({"status": True}, {"status": 599}, {"status": "401 "},
                {"code": ["unauthorized"]}, {"code": {"unauthorized": SECRET}},
                {"error_code": "unauthorized " + SECRET}, {"code": SECRET * 100}):
            with self.subTest(metadata=metadata):
                self.failure({"type": "error", "error": dict(metadata, message=SECRET)}, "cli_turn_failed")

    def test_stderr_is_counted_but_not_used_as_error_metadata(self):
        behavior = "sys.stderr.write('401 unauthorized ' + " + repr(SECRET) + "); sys.exit(7)"
        with self.cli(behavior), self.assertRaises(ModelCallError) as caught:
            self.call("answer", {})
        self.assertEqual("cli_exit_7", caught.exception.code)
        self.assertNotIn(SECRET, repr(vars(caught.exception)))

    def test_classified_error_is_unknown_and_same_operation_cannot_retry(self):
        operation = "classified-error-once"
        ledger = BudgetLedger.create(self.root / "audit.json", None, 0,
                                    "artificial-diagnostic-test", mode="audit")
        behavior = self.event("turn.failed", error={"code": "rate_limit_exceeded", "message": SECRET})
        with self.cli(behavior), self.assertRaises(ModelCallError) as caught:
            model_calls.call_model("answer", {}, ledger=ledger, operation_id=operation)
        self.assertEqual("cli_rate_limited", caught.exception.code)
        call = ledger.operation(operation)["calls"]["answer"]
        self.assertEqual("unknown", call["status"])
        self.assertEqual("cli_rate_limited", call["error_code"])
        self.assertNotIn(SECRET, json.dumps(call))
        with self.cli(), patch.object(model_calls.subprocess, "Popen") as start:
            with self.assertRaises(BudgetBlocked):
                model_calls.call_model("answer", {}, ledger=ledger, operation_id=operation)
            start.assert_not_called()
        self.assertEqual(1, ledger.snapshot()["started_calls"])
        self.assertEqual("audit", ledger.snapshot()["mode"])
        self.assertEqual(0, self.ledger.snapshot()["started_calls"])

    def test_error_remains_terminal_even_if_stream_has_a_later_result(self):
        behavior = "\n".join((
            self.event("error", code="network_error", message=SECRET),
            self.event("turn.completed", usage={"input_tokens": 1}),
        ))
        with self.cli(behavior), self.assertRaises(ModelCallError) as caught:
            self.call("answer", {})
        self.assertEqual("cli_network_failure", caught.exception.code)
        self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
