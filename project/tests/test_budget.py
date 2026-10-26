"""Offline, synthetic shared budget tests; no sign-in or personal data."""
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall


def _compete(path, operation):
    try:
        ledger = BudgetLedger(path)
        ledger.start(operation, "answer", "a" * 64)
        return "started"
    except BudgetBlocked as error:
        return error.code


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pdm-budget-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "calls.json"

    def create(self, limit=6, initial=0):
        return BudgetLedger.create(self.path, limit, initial, "synthetic-grant")

    def blocked(self, code, action):
        with self.assertRaises(BudgetBlocked) as raised:
            action()
        self.assertEqual(code, raised.exception.code)
        self.assertEqual(0, raised.exception.attempted_calls)
        return raised.exception

    def test_create_is_explicit_private_and_never_resets(self):
        ledger = self.create(18, 18)
        before = self.path.read_bytes()
        self.blocked("budget_already_exists", lambda: self.create(100, 0))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(0o600, self.path.stat().st_mode & 0o777)
        self.assertEqual(0o600, Path(str(self.path) + ".lock").stat().st_mode & 0o777)
        self.assertEqual(0, BudgetLedger(self.path).snapshot()["remaining"])
        called = []
        adapter = BudgetedModelCall(ledger, "next", lambda *args, **kwargs: called.append(1))
        self.blocked("budget_exhausted", lambda: adapter("answer", {}))
        self.assertEqual([], called)
        self.assertEqual(0, adapter.started_count)

    def test_create_requires_valid_explicit_allowance_and_reference(self):
        for limit, initial in ((True, 0), (2, True), (-1, 0), (2, 3), (1_000_001, 0)):
            with self.subTest(limit=limit, initial=initial):
                self.blocked("invalid_budget_limit", lambda: self.create(limit, initial))
        self.blocked("invalid_budget_authorization", lambda: BudgetLedger.create(self.path, 2, 0, ""))
        self.blocked("invalid_budget_authorization", lambda: BudgetLedger.create(self.path, 2, 0, "\ud800"))
        self.assertFalse(self.path.exists())

    def test_pair_reservation_atomic_and_idempotent_and_not_started(self):
        ledger = self.create(2)
        adapter = BudgetedModelCall(ledger, "run-1", lambda *args, **kwargs: {})
        adapter.reserve_calls(2)
        revision = ledger.snapshot()["revision"]
        adapter.reserve_calls(2)
        self.assertEqual(revision, ledger.snapshot()["revision"])
        self.assertEqual(2, adapter.reserved_count)
        self.assertEqual(0, adapter.started_count)
        self.blocked("budget_exhausted", lambda: adapter.for_operation("other").reserve_calls(1))
        adapter("extract", {"fixture": "first"})
        adapter("review", {"fixture": "second"})
        self.assertEqual(2, adapter.started_count)
        self.assertEqual(0, ledger.snapshot()["remaining"])

    def test_configured_timeout_follows_operations_without_changing_legacy_default(self):
        ledger = self.create()
        observed = []
        def transport(role, payload, timeout):
            observed.append(timeout)
            return {}
        adapter = BudgetedModelCall(ledger, "configured", transport, default_timeout=900)
        adapter("answer", {})
        adapter.for_operation("child")("answer", {})
        adapter.for_operation("longer-child", default_timeout=1800)("answer", {})
        adapter.for_operation("explicit-shorter")("answer", {}, timeout=7)
        BudgetedModelCall(ledger, "legacy", transport)("answer", {})
        self.assertEqual(observed, [900, 900, 1800, 7, 120])
        self.assertEqual(adapter.default_timeout, 900)

    def test_invalid_timeout_never_starts_or_changes_the_ledger(self):
        from pdm.model_calls import ModelCallError
        ledger = self.create()
        before = self.path.read_bytes()
        adapter = BudgetedModelCall(ledger, "invalid-timeout", lambda *args, **kwargs: self.fail("transport"))
        for value in (0, -1, 1801, True, 0.5, None, "900"):
            with self.subTest(timeout=value):
                for action in (lambda: BudgetedModelCall(ledger, "invalid-default", default_timeout=value),
                               lambda: adapter("answer", {}, timeout=value)):
                    with self.assertRaises(ModelCallError) as caught:
                        action()
                    self.assertEqual(caught.exception.code, "invalid_timeout")
                self.assertEqual(self.path.read_bytes(), before)

    def test_longer_timeout_does_not_replay_an_unknown_call(self):
        from pdm.model_calls import ModelCallError
        ledger = self.create()
        seen = []
        def timed_out(role, payload, timeout):
            seen.append(timeout)
            raise ModelCallError("timeout", "model-" + "a" * 32, timeout)
        adapter = BudgetedModelCall(ledger, "fixed-operation", timed_out, default_timeout=900)
        with self.assertRaises(ModelCallError):
            adapter("answer", {})
        call = adapter.operation_state()["calls"]["answer"]
        self.assertEqual((call["status"], call["error_code"]), ("unknown", "timeout"))
        before = self.path.read_bytes()
        reopened = BudgetedModelCall(BudgetLedger(self.path), "fixed-operation", timed_out, default_timeout=1800)
        self.blocked("budget_call_already_started", lambda: reopened("answer", {}))
        self.assertEqual((seen, self.path.read_bytes()), ([900], before))

    def test_production_guard_preserves_configured_timeout_for_child_review(self):
        from pdm.production import _guarded
        ledger = self.create()
        seen, guards = [], []
        adapter = BudgetedModelCall(ledger, "root", lambda role, payload, timeout: seen.append(timeout) or {},
                                   default_timeout=900)
        guarded = _guarded(adapter, lambda *args: guards.append(args), Path("/tmp/artificial-repo"), "queue", "run")
        guarded("extract", {})
        guarded.for_operation("decomposed-review")("review", {})
        self.assertEqual(seen, [900, 900])
        self.assertEqual(len(guards), 2)

    def test_pair_insufficient_starts_nothing(self):
        ledger = self.create(1)
        adapter = BudgetedModelCall(ledger, "run-1", lambda *args, **kwargs: self.fail("transport"))
        self.blocked("budget_exhausted", lambda: adapter.reserve_calls(2))
        self.assertIsNone(adapter.operation_state())
        self.assertEqual(1, ledger.snapshot()["remaining"])

    def test_operation_role_replay_and_changed_payload_are_rejected(self):
        called = []
        adapter = BudgetedModelCall(self.create(), "op", lambda *args, **kwargs: called.append(1) or {})
        adapter("answer", {"fixture": "one"})
        for payload in ({"fixture": "one"}, {"fixture": "changed"}):
            error = self.blocked("budget_call_already_started", lambda: adapter("answer", payload))
            self.assertEqual(1, error.started_count)
        self.assertEqual([1], called)
        self.blocked("budget_call_already_started", lambda: adapter.reserve_calls(2))

    def test_cross_instances_operations_and_dates_share_pool(self):
        ledger = self.create(3)
        for operation, role in (("manual", "extract"), ("scheduled-next-day", "answer"), ("qa", "answer")):
            adapter = BudgetedModelCall(BudgetLedger(self.path), operation, lambda *args, **kwargs: {})
            adapter(role, {})
        self.assertEqual(3, ledger.snapshot()["used"])
        self.assertEqual(3, ledger.snapshot()["started_calls"])
        self.blocked("budget_exhausted", lambda: BudgetLedger(self.path).reserve("next", ["answer"]))

    def test_unknown_and_crash_window_are_not_refunded_or_replayed(self):
        ledger = self.create(3)
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        adapter = BudgetedModelCall(ledger, "unknown", interrupted)
        with self.assertRaises(KeyboardInterrupt):
            adapter("answer", {})
        self.assertEqual("unknown", adapter.operation_state()["calls"]["answer"]["status"])
        reopened = BudgetedModelCall(BudgetLedger(self.path), "unknown", lambda *args, **kwargs: self.fail("replay"))
        self.blocked("budget_call_already_started", lambda: reopened("answer", {}))
        ledger.start("crashed", "answer", "a" * 64)
        self.assertEqual("started", BudgetLedger(self.path).operation("crashed")["calls"]["answer"]["status"])
        self.blocked("budget_call_already_started", lambda: ledger.start("crashed", "answer", "a" * 64))
        self.assertEqual(2, ledger.snapshot()["used"])

    def test_last_slot_cross_process_race_only_one_start(self):
        self.create(1)
        with ProcessPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_compete, [str(self.path)] * 2, ["first", "second"]))
        self.assertEqual(["budget_exhausted", "started"], sorted(results))
        self.assertEqual(1, BudgetLedger(self.path).snapshot()["started_calls"])

    def test_same_operation_cross_process_race_only_one_start(self):
        self.create(2)
        with ProcessPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(_compete, [str(self.path)] * 2, ["same", "same"]))
        self.assertEqual(["budget_call_already_started", "started"], sorted(results))

    def test_no_payload_output_or_arbitrary_exception_text_is_persisted(self):
        ledger = BudgetLedger.create(self.path, 2, 0, "PRIVATE_AUTH_REFERENCE_FIXTURE")
        payload = {"question": "PRIVATE_QUESTION_FIXTURE", "body": "PRIVATE_BODY_FIXTURE"}
        adapter = BudgetedModelCall(ledger, "safe-op", lambda *args, **kwargs: {"answer": "PRIVATE_OUTPUT_FIXTURE"})
        adapter("answer", payload)
        raw = self.path.read_text()
        for secret in ("PRIVATE_AUTH_REFERENCE_FIXTURE", "PRIVATE_QUESTION_FIXTURE", "PRIVATE_BODY_FIXTURE", "PRIVATE_OUTPUT_FIXTURE"):
            self.assertNotIn(secret, raw)
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(digest, adapter.operation_state()["calls"]["answer"]["payload_sha256"])

    def test_transport_gets_frozen_payload_matching_digest(self):
        original = {"fixture": ["original"]}
        def transport(role, payload, **kwargs):
            original["fixture"].append("caller-changed")
            self.assertEqual(["original"], payload["fixture"])
            return {}
        BudgetedModelCall(self.create(), "frozen", transport)("answer", original)

    def test_start_write_failure_prevents_transport(self):
        ledger = self.create()
        called = []
        adapter = BudgetedModelCall(ledger, "op", lambda *args, **kwargs: called.append(1))
        with patch("pdm.budget.os.replace", side_effect=OSError("PRIVATE_PATH")):
            self.blocked("budget_write_failed", lambda: adapter("answer", {}))
        self.assertEqual([], called)
        self.assertEqual(0, ledger.snapshot()["started_calls"])

    def test_finish_write_failure_keeps_started_and_cannot_return_success(self):
        ledger = self.create()
        adapter = BudgetedModelCall(ledger, "op", lambda *args, **kwargs: {})
        with patch.object(ledger, "finish", side_effect=BudgetBlocked("budget_write_failed")):
            with self.assertRaises(BudgetBlocked) as raised:
                adapter("answer", {})
        self.assertEqual("budget_write_failed", raised.exception.code)
        self.assertEqual(1, raised.exception.attempted_calls)
        self.assertEqual(1, raised.exception.started_count)
        self.assertEqual("op", raised.exception.operation_id)
        self.assertEqual(1, ledger.snapshot()["started_calls"])
        self.assertEqual("started", adapter.operation_state()["calls"]["answer"]["status"])
        self.blocked("budget_call_already_started", lambda: adapter("answer", {}))

    def test_directory_sync_failure_after_replace_never_starts_transport(self):
        ledger = self.create()
        called = []
        adapter = BudgetedModelCall(ledger, "op", lambda *args, **kwargs: called.append(1))
        real_sync = os.fsync
        def sync(fd):
            import stat
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("PRIVATE_PATH")
            return real_sync(fd)
        with patch("pdm.budget.os.fsync", side_effect=sync):
            self.blocked("budget_write_failed", lambda: adapter("answer", {}))
        self.assertEqual([], called)
        self.assertEqual(0, adapter.observed_started_count)
        self.assertEqual(1, adapter.started_count)
        self.blocked("budget_call_already_started", lambda: adapter("answer", {}))

    def test_error_record_write_failure_reports_started_call_and_keeps_charge(self):
        ledger = self.create()
        def failing_transport(*args, **kwargs):
            raise RuntimeError("PRIVATE_TRANSPORT_ERROR_FIXTURE")
        adapter = BudgetedModelCall(ledger, "error-op", failing_transport)
        with patch.object(ledger, "finish", side_effect=BudgetBlocked("budget_write_failed")):
            with self.assertRaises(BudgetBlocked) as raised:
                adapter("answer", {})
        self.assertEqual("budget_write_failed", raised.exception.code)
        self.assertEqual(1, raised.exception.attempted_calls)
        self.assertEqual(1, raised.exception.started_count)
        self.assertEqual("error-op", raised.exception.operation_id)
        self.assertNotIn("PRIVATE_", str(raised.exception))
        self.assertEqual(1, adapter.started_count)
        self.assertEqual("started", adapter.operation_state()["calls"]["answer"]["status"])
        self.blocked("budget_call_already_started", lambda: adapter("answer", {}))

    def test_missing_and_corrupt_states_fail_closed(self):
        self.blocked("budget_missing", lambda: BudgetLedger(self.path))
        ledger = self.create()
        valid = self.path.read_bytes()
        for raw in (b"{}", b'{"schema_version":"1","schema_version":"1"}', b"[" * 2000, b"not-json"):
            with self.subTest(raw=raw[:30]):
                self.path.write_bytes(raw)
                self.blocked("invalid_budget_state", ledger.snapshot)
        self.path.write_bytes(valid)
        self.path.unlink()
        self.blocked("budget_missing", ledger.snapshot)

    def test_schema_counters_and_call_invariants_are_strict(self):
        ledger = self.create()
        ledger.start("op", "answer", "a" * 64)
        valid = json.loads(self.path.read_text())
        variants = []
        for field, value in (("schema_version", "future"), ("limit", True), ("initial_used", 100), ("revision", -1)):
            state = json.loads(json.dumps(valid))
            state[field] = value
            variants.append(state)
        state = json.loads(json.dumps(valid))
        state["operations"]["op"]["calls"]["answer"]["finished_at"] = "2026-10-16T00:00:00+00:00"
        variants.append(state)
        for state in variants:
            self.path.write_text(json.dumps(state))
            self.blocked("invalid_budget_state", ledger.snapshot)

    def test_permission_symlink_hardlink_and_fifo_are_rejected(self):
        ledger = self.create()
        self.path.chmod(0o644)
        self.blocked("unsafe_budget_permissions", ledger.snapshot)
        self.path.chmod(0o600)
        alias = self.root / "alias.json"
        alias.symlink_to(self.path)
        self.blocked("unsafe_budget_path", lambda: BudgetLedger(alias))
        alias.unlink()
        os.link(self.path, alias)
        self.blocked("unsafe_budget_permissions", ledger.snapshot)
        alias.unlink()
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        self.blocked("unsafe_budget_permissions", ledger.snapshot)

    def test_parent_permissions_and_ancestor_symlink_rejected(self):
        self.root.chmod(0o755)
        self.blocked("unsafe_budget_permissions", self.create)
        self.root.chmod(0o700)
        real = self.root / "real"
        real.mkdir(mode=0o700)
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        self.blocked("unsafe_budget_path", lambda: BudgetLedger.create(link / "calls.json", 2, 0, "fixture"))

    def test_repository_and_linked_git_metadata_locations_rejected(self):
        repo = self.root / "repo"
        repo.mkdir(mode=0o700)
        self.blocked("budget_inside_repository", lambda: BudgetLedger.create(repo / "calls.json", 2, 0, "fixture", repo=repo))
        gitdir = self.root / "linked-metadata"
        gitdir.mkdir(mode=0o700)
        common = self.root / "common-metadata"
        common.mkdir(mode=0o700)
        (repo / ".git").write_text("gitdir: " + str(gitdir))
        (gitdir / "commondir").write_text(str(common))
        for root in (gitdir, common):
            self.blocked("budget_inside_repository", lambda: BudgetLedger.create(root / "calls.json", 2, 0, "fixture", repo=repo))

    def test_lock_permissions_missing_lock_and_symlink_lock_fail_closed(self):
        ledger = self.create()
        lock = Path(str(self.path) + ".lock")
        lock.chmod(0o644)
        self.blocked("unsafe_budget_permissions", ledger.snapshot)
        lock.unlink()
        self.blocked("budget_missing", ledger.snapshot)
        lock.symlink_to(self.path)
        self.blocked("budget_io_failed", ledger.snapshot)

    def test_git_pointers_are_bounded_and_nofollow(self):
        repo = self.root / "repo"
        repo.mkdir(mode=0o700)
        (repo / ".git").write_bytes(b"gitdir: " + b"x" * 4097)
        self.blocked("unsafe_budget_path", lambda: BudgetLedger.create(self.path, 2, 0, "fixture", repo=repo))
        (repo / ".git").unlink()
        pointer = self.root / "pointer"
        pointer.write_text("gitdir: anywhere")
        (repo / ".git").symlink_to(pointer)
        self.blocked("unsafe_budget_path", lambda: BudgetLedger.create(self.path, 2, 0, "fixture", repo=repo))

    def test_live_ledger_binding_rejects_replaced_grant_identity(self):
        ledger = self.create(6)
        original = self.path.read_bytes()
        for field, value in (("pool_id", "budget-" + "f" * 32),
                             ("authorization_sha256", "f" * 64), ("limit", 7), ("initial_used", 1)):
            state = json.loads(original)
            state[field] = value
            self.path.write_text(json.dumps(state))
            self.blocked("budget_identity_changed", ledger.snapshot)
        self.path.write_bytes(original)

    def test_bare_git_and_invalid_path_are_rejected(self):
        bare = self.root / "bare"
        bare.mkdir(mode=0o700)
        (bare / "HEAD").write_text("ref: refs/heads/fixture\n")
        (bare / "objects").mkdir()
        self.blocked("budget_inside_repository", lambda: BudgetLedger.create(bare / "calls.json", 2, 0, "fixture"))
        self.blocked("unsafe_budget_path", lambda: BudgetLedger(self.root / "bad\0name"))


if __name__ == "__main__":
    unittest.main()
