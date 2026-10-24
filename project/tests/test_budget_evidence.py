"""Persistent budgets across real read-only answer paths with artificial data."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import test_current_evidence as current_fixtures
from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall
from pdm.contracts import CaptureError
from pdm.evidence import answer, ask_current, ask_sources
from pdm.pipeline import process_sources, publish


class BudgetEvidenceTests(unittest.TestCase):
    def setUp(self):
        # Reuse only fixture construction, without inheriting its test methods.
        current_fixtures.CurrentEvidenceTests.setUp(self)
        processed = process_sources(
            self.repo, self.paths[:2], model_call=lambda role, payload:
            current_fixtures.CurrentEvidenceTests.model(self, role, payload))
        self.assertEqual(processed["status"], "passed")
        self.run_id = processed["run_id"]
        self.publication = publish(self.repo, self.run_id)
        temporary = tempfile.TemporaryDirectory(prefix="pdm-evidence-budget-")
        self.addCleanup(temporary.cleanup)
        self.ledger_path = Path(temporary.name) / "calls.json"
        self.answer_payloads = []

    def create(self, limit, initial_used=0):
        return BudgetLedger.create(self.ledger_path, limit, initial_used,
                                   "artificial-evidence-test", repo=self.repo)

    def adapter(self, operation_id, transport=None):
        return BudgetedModelCall(BudgetLedger(self.ledger_path, repo=self.repo), operation_id,
                                 self.transport if transport is None else transport)

    def data_snapshot(self):
        return {
            "head": current_fixtures.git(self.repo, "rev-parse", "HEAD"),
            "status": current_fixtures.git(self.repo, "status", "--porcelain", "--untracked-files=all"),
            "memory": {str(path.relative_to(self.repo)): path.read_bytes()
                       for path in (self.repo / "memory").rglob("*") if path.is_file()},
        }

    def entries(self):
        return (
            ("single", lambda adapter: answer(self.repo, self.paths[0], "人工资料说了什么？", model_call=adapter)),
            ("multiple", lambda adapter: ask_sources(self.repo, self.paths[:2], "两份人工资料说了什么？",
                                                       self.run_id, model_call=adapter)),
            ("current", lambda adapter: ask_current(self.repo, self.paths[:2], "当前限定人工资料是什么？",
                                                      [self.run_id], model_call=adapter)),
        )

    def transport(self, role, payload, timeout=120):
        self.assertEqual(role, "answer")
        self.answer_payloads.append(payload)
        selected = payload["evidence"]
        sources = ([selected["source"]] if "source" in selected else
                   [entry["source"] for entry in selected["sources"] if entry["lines"]])
        output = {"answer": "仅根据所选人工资料回答。", "citations": [
            {"source_id": source["source_id"], "source_version": source["source_version"], "locator": "L1-L1"}
            for source in sources], "limitations": ["隔离人工资料，时间与实际状态未核验。"]}
        return {"output": output, "invocation_id": "model-" + f"{100 + len(self.answer_payloads):032x}",
                "model": "artificial", "elapsed_seconds": 0.01, "usage": None}

    def test_all_answer_entries_reopen_one_pool_without_changing_data(self):
        original = self.create(limit=3)
        before = self.data_snapshot()
        self.assertEqual(before["status"], "")
        for index, (name, entry) in enumerate(self.entries(), 1):
            with self.subTest(entry=name):
                adapter = self.adapter("answer-" + name)
                result = entry(adapter)
                self.assertTrue(result["citations"])
                self.assertEqual(adapter.observed_started_count, 1)
                reopened = BudgetLedger(self.ledger_path, repo=self.repo).snapshot()
                self.assertEqual(reopened["pool_id"], original.snapshot()["pool_id"])
                self.assertEqual((reopened["used"], reopened["started_calls"], reopened["remaining"]),
                                 (index, index, 3 - index))
                self.assertEqual(self.data_snapshot(), before)
        multiple = self.answer_payloads[1]["evidence"]["publication"]
        current = self.answer_payloads[2]["evidence"]["publications"]
        self.assertEqual(set(multiple["files"]), set(self.publication["files"]))
        self.assertEqual(len(current), 1)
        self.assertEqual(set(current[0]["files"]), set(self.publication["files"]))
        serialized = json.dumps(self.answer_payloads, ensure_ascii=False)
        self.assertNotIn("UNSELECTED_ARTIFICIAL_SENTINEL", serialized)
        self.assertNotIn(self.sources[2]["source_id"], serialized)
        operation = BudgetLedger(self.ledger_path).operation("answer-single")
        self.assertEqual(operation["calls"]["answer"]["status"], "completed")
        transport = Mock()
        with self.assertRaises(BudgetBlocked) as raised:
            self.entries()[0][1](self.adapter("after-exhaustion", transport))
        self.assertEqual(raised.exception.code, "budget_exhausted")
        self.assertEqual(raised.exception.attempted_calls, 0)
        transport.assert_not_called()
        self.assertEqual(self.data_snapshot(), before)

    def test_empty_pool_blocks_every_answer_before_transport_and_leaves_no_operation(self):
        self.create(limit=1, initial_used=1)
        before = self.data_snapshot()
        ledger_before = self.ledger_path.read_bytes()
        for name, entry in self.entries():
            with self.subTest(entry=name):
                transport = Mock()
                adapter = self.adapter("blocked-" + name, transport)
                with self.assertRaises(BudgetBlocked) as raised:
                    entry(adapter)
                self.assertEqual(raised.exception.code, "budget_exhausted")
                self.assertEqual((raised.exception.attempted_calls, adapter.observed_started_count), (0, 0))
                transport.assert_not_called()
                self.assertIsNone(adapter.operation_state())
                self.assertEqual(self.ledger_path.read_bytes(), ledger_before)
                self.assertEqual(self.data_snapshot(), before)

    def test_transport_errors_remain_charged_and_cannot_be_replayed(self):
        self.create(limit=3)
        before = self.data_snapshot()
        for index, (name, entry) in enumerate(self.entries(), 1):
            with self.subTest(entry=name):
                transport = Mock(side_effect=RuntimeError("artificial transport failure"))
                adapter = self.adapter("error-" + name, transport)
                with self.assertRaises(RuntimeError):
                    entry(adapter)
                transport.assert_called_once()
                self.assertEqual(adapter.observed_started_count, 1)
                ledger = BudgetLedger(self.ledger_path, repo=self.repo)
                operation = ledger.operation("error-" + name)
                self.assertEqual(operation["calls"]["answer"]["status"], "unknown")
                self.assertEqual(operation["calls"]["answer"]["error_code"], "transport_outcome_unknown")
                replay = Mock()
                with self.assertRaises(BudgetBlocked) as raised:
                    entry(self.adapter("error-" + name, replay))
                self.assertEqual(raised.exception.code, "budget_call_already_started")
                self.assertEqual(raised.exception.attempted_calls, 0)
                replay.assert_not_called()
                self.assertEqual((ledger.snapshot()["used"], ledger.snapshot()["remaining"]), (index, 3 - index))
                self.assertEqual(self.data_snapshot(), before)

    def test_invalid_answer_is_charged_after_transport_without_data_writes(self):
        self.create(limit=3)
        before = self.data_snapshot()
        for index, (name, entry) in enumerate(self.entries(), 1):
            with self.subTest(entry=name):
                transport = Mock(return_value={"output": {"answer": "invalid artificial response"}})
                adapter = self.adapter("invalid-" + name, transport)
                with self.assertRaises(CaptureError) as raised:
                    entry(adapter)
                self.assertEqual(raised.exception.code, "invalid_answer")
                transport.assert_called_once()
                ledger = BudgetLedger(self.ledger_path, repo=self.repo)
                self.assertEqual(ledger.operation("invalid-" + name)["calls"]["answer"]["status"], "completed")
                self.assertEqual((ledger.snapshot()["used"], ledger.snapshot()["remaining"]), (index, 3 - index))
                self.assertEqual(self.data_snapshot(), before)


if __name__ == "__main__":
    unittest.main()
