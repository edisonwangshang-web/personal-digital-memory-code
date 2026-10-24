"""Offline audit accounting: no personal data, credentials, or model service."""
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall


def _start_audit(path, operation):
    try:
        return BudgetLedger(path).start(operation, "answer", "a" * 64)
    except BudgetBlocked as error:
        return error.code


class AuditBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-audit-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "audit.json"

    def create(self, initial_used=0, authorization="synthetic-ongoing-authorization"):
        return BudgetLedger.create(self.path, None, initial_used, authorization, mode="audit")

    def blocked(self, code, action):
        with self.assertRaises(BudgetBlocked) as raised:
            action()
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def test_audit_schema_is_explicit_unbounded_and_records_large_history(self):
        ledger = self.create(initial_used=1_000_001)
        calls = []
        for index in range(30):
            adapter = BudgetedModelCall(ledger, "work-" + str(index),
                                       lambda *args, **kwargs: calls.append(1) or {})
            adapter.reserve_calls(2)
            adapter("extract", {"synthetic": "extract"})
            adapter("review", {"synthetic": "review"})
        result = BudgetLedger(self.path).snapshot()
        self.assertEqual("2", result["schema_version"])
        self.assertEqual("audit", result["mode"])
        self.assertIsNone(result["limit"])
        self.assertIsNone(result["remaining"])
        self.assertEqual(60, result["reserved_calls"])
        self.assertEqual(60, result["started_calls"])
        self.assertEqual(1_000_061, result["used"])
        self.assertEqual(60, len(calls))

    def test_audit_requires_null_limit_and_distinct_valid_policy(self):
        for limit in (0, 24, 1_000_000, False, "unlimited"):
            with self.subTest(limit=limit):
                self.blocked("invalid_budget_limit", lambda: BudgetLedger.create(
                    self.path, limit, 0, "synthetic-ongoing", mode="audit"))
        for used in (-1, True, None, 1.5):
            with self.subTest(initial_used=used):
                self.blocked("invalid_budget_limit", lambda: self.create(used))
        self.blocked("invalid_budget_mode", lambda: BudgetLedger.create(
            self.path, None, 0, "synthetic-ongoing", mode="unlimited"))
        self.blocked("invalid_budget_authorization", lambda: self.create(authorization=""))
        self.assertFalse(self.path.exists())

    def test_audit_cannot_overwrite_or_convert_exhausted_finite_grant(self):
        ledger = BudgetLedger.create(self.path, 24, 24, "synthetic-special-grant")
        before = self.path.read_bytes()
        self.blocked("budget_already_exists", self.create)
        self.assertEqual(before, self.path.read_bytes())
        snapshot = ledger.snapshot()
        self.assertEqual("1", snapshot["schema_version"])
        self.assertNotIn("mode", snapshot)
        self.assertEqual(0, snapshot["remaining"])
        self.blocked("budget_exhausted", lambda: ledger.start("next", "answer", "a" * 64))

    def test_audit_is_separate_pool_with_explicit_authorization_digest(self):
        legacy = BudgetLedger.create(self.root / "legacy.json", 24, 24, "synthetic-special")
        before = (self.root / "legacy.json").read_bytes()
        audit = self.create(initial_used=24, authorization="synthetic-ongoing")
        self.assertNotEqual(legacy.snapshot()["pool_id"], audit.snapshot()["pool_id"])
        self.assertNotEqual(legacy.snapshot()["authorization_sha256"],
                            audit.snapshot()["authorization_sha256"])
        self.assertEqual(24, audit.snapshot()["initial_used"])
        self.assertEqual(before, (self.root / "legacy.json").read_bytes())

    def test_schema_mode_limit_and_fields_are_strictly_validated(self):
        ledger = self.create()
        valid = json.loads(self.path.read_bytes())
        malformed = [dict(valid, mode="budget"), dict(valid, limit=100),
                     dict(valid, initial_used=True), dict(valid, schema_version="3"),
                     dict(valid, unexpected="field")]
        missing = dict(valid)
        missing.pop("mode")
        malformed.append(missing)
        for state in malformed:
            with self.subTest(state=state):
                self.path.write_text(json.dumps(state))
                self.blocked("invalid_budget_state", ledger.snapshot)
        self.path.write_text(json.dumps(valid))
        self.assertEqual("audit", ledger.snapshot()["mode"])

    def test_open_handle_binds_policy_and_cannot_be_switched(self):
        ledger = BudgetLedger.create(self.path, 3, 0, "synthetic-finite")
        state = json.loads(self.path.read_bytes())
        state.update(schema_version="2", mode="audit", limit=None)
        self.path.write_text(json.dumps(state))
        self.blocked("budget_identity_changed", ledger.snapshot)

    def test_pair_reservation_idempotent_but_started_role_and_payload_not_replayed(self):
        adapter = BudgetedModelCall(self.create(), "same-op", lambda *args, **kwargs: {})
        adapter.reserve_calls(2)
        revision = adapter.ledger.snapshot()["revision"]
        adapter.reserve_calls(2)
        self.assertEqual(revision, adapter.ledger.snapshot()["revision"])
        adapter("extract", {"synthetic": "first"})
        for payload in ({"synthetic": "first"}, {"synthetic": "changed"}):
            self.blocked("budget_call_already_started", lambda: adapter("extract", payload))
        self.blocked("budget_call_already_started", lambda: adapter.reserve_calls(2))
        adapter("review", {"synthetic": "review"})
        self.assertEqual(2, adapter.started_count)
        self.assertEqual(2, adapter.reserved_count)

    def test_unknown_and_started_calls_survive_reopen_without_replay(self):
        ledger = self.create()
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        adapter = BudgetedModelCall(ledger, "unknown", interrupted)
        with self.assertRaises(KeyboardInterrupt):
            adapter("answer", {"synthetic": "one"})
        self.assertEqual("unknown", adapter.operation_state()["calls"]["answer"]["status"])
        ledger.start("crashed", "answer", "a" * 64)
        reopened = BudgetLedger(self.path)
        for operation in ("unknown", "crashed"):
            self.blocked("budget_call_already_started", lambda: reopened.start(
                operation, "answer", "a" * 64))
        self.assertEqual(2, reopened.snapshot()["used"])
        self.assertIsNone(reopened.snapshot()["remaining"])

    def test_same_operation_cross_process_race_starts_once(self):
        self.create()
        with ProcessPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_start_audit, [str(self.path)] * 2, ["same", "same"]))
        self.assertEqual(1, results.count(1))
        self.assertEqual(1, results.count("budget_call_already_started"))
        self.assertEqual(1, BudgetLedger(self.path).snapshot()["started_calls"])

    def test_distinct_operations_cross_process_account_without_lost_updates(self):
        self.create(initial_used=24)
        operations = ["attempt-" + str(index) for index in range(16)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(_start_audit, [str(self.path)] * len(operations), operations))
        self.assertEqual([1] * len(operations), results)
        snapshot = BudgetLedger(self.path).snapshot()
        self.assertEqual(40, snapshot["used"])
        self.assertEqual(16, snapshot["started_calls"])

    def test_start_write_failure_sends_nothing_and_retains_original_bytes(self):
        ledger = self.create()
        before = self.path.read_bytes()
        sent = []
        adapter = BudgetedModelCall(ledger, "failed-start",
                                   lambda *args, **kwargs: sent.append(1) or {})
        with patch("pdm.budget.os.replace", side_effect=OSError("synthetic failure")):
            self.blocked("budget_write_failed", lambda: adapter("answer", {}))
        self.assertEqual([], sent)
        self.assertEqual(before, self.path.read_bytes())

    def test_finish_write_failure_keeps_started_and_cannot_return_success(self):
        ledger = self.create()
        adapter = BudgetedModelCall(ledger, "failed-finish", lambda *args, **kwargs: {})
        with patch.object(ledger, "finish", side_effect=BudgetBlocked("budget_write_failed")):
            error = self.blocked("budget_write_failed", lambda: adapter("answer", {}))
        self.assertEqual(1, error.attempted_calls)
        self.assertEqual(1, error.started_count)
        reopened = BudgetedModelCall(BudgetLedger(self.path), "failed-finish",
                                    lambda *args, **kwargs: self.fail("replayed"))
        self.blocked("budget_call_already_started", lambda: reopened("answer", {}))

    def test_storage_capacity_is_explicit_failure_and_never_authorizes_unrecorded_call(self):
        ledger = self.create()
        before = self.path.read_bytes()
        adapter = BudgetedModelCall(ledger, "capacity", lambda *args, **kwargs: self.fail("sent"))
        with patch("pdm.budget.MAX_LEDGER_BYTES", len(before) + 5):
            self.blocked("budget_state_limit", lambda: adapter("answer", {}))
        self.assertEqual(before, self.path.read_bytes())
        self.assertIsNone(ledger.operation("capacity"))

    def test_payload_authorization_output_and_private_permissions_are_preserved(self):
        ledger = self.create(authorization="PRIVATE_AUTHORIZATION_FIXTURE")
        payload = {"synthetic": "PRIVATE_PAYLOAD_FIXTURE"}
        adapter = BudgetedModelCall(ledger, "privacy", lambda *args, **kwargs: {
            "answer": "PRIVATE_ANSWER_FIXTURE"})
        adapter("answer", payload)
        raw = self.path.read_text()
        self.assertNotIn("PRIVATE_", raw)
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual(0o600, Path(str(self.path) + ".lock").stat().st_mode & 0o777)
        canonical = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                               sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(canonical).hexdigest(),
                         adapter.operation_state()["calls"]["answer"]["payload_sha256"])


if __name__ == "__main__":
    unittest.main()
