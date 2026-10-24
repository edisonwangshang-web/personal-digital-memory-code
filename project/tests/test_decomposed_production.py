"""Durable production resumes the same frozen Review, with no new extraction."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_production
from pdm.budget import BudgetedModelCall
from pdm.model_calls import ModelCallError
from pdm.production import prepare_production, run_production
from pdm.transaction import PipelineGit


class DecomposedProductionTests(unittest.TestCase):
    setUp = test_production.ProductionTests.setUp
    sources = test_production.ProductionTests.sources
    transport = test_production.ProductionTests.transport
    attempts = test_production.ProductionTests.attempts
    assert_no_formal = test_production.ProductionTests.assert_no_formal

    def prepare(self):
        paths = self.sources(1)
        prepare_production(self.repo, "queue", paths, review_strategy="decomposed-v1")
        return paths

    def test_known_unstarted_review_resumes_without_replaying_extract_or_passed_unit(self):
        self.prepare()
        adapter = BudgetedModelCall(self.ledger, "worker")
        started_units = []
        blocked = False

        def transport(role, payload, timeout=120, *, on_start):
            nonlocal blocked
            if role == "review" and started_units and not blocked:
                blocked = True
                raise ModelCallError("codex_not_found", "model-" + "f" * 32)
            on_start()
            if role == "review":
                started_units.append(payload["review_scope"]["unit_id"])
            return self.transport(role, payload, timeout)

        with patch("pdm.model_calls._call_transport", side_effect=transport):
            first = run_production(self.repo, "queue", model_call=adapter)
            self.assertEqual(first["status"], "partial", first)
            self.assertEqual(self.roles, ["extract", "review"])
            self.assert_no_formal()
            original = first["active_run_id"]
            result = run_production(self.repo, "queue", model_call=adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(result["final_run"], original)
        self.assertEqual(self.roles.count("extract"), 1)
        self.assertEqual(len(started_units), len(set(started_units)))
        self.assertEqual(len(self.attempts()[0]["attempts"]), 1)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
        again = run_production(self.repo, "queue", model_call=adapter)
        self.assertEqual(again["publication"], result["publication"])
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes()), before)

    def test_unknown_child_cannot_be_replayed_by_restart_or_new_queue(self):
        paths = self.prepare()
        adapter = BudgetedModelCall(self.ledger, "worker")
        launched = []

        def transport(role, payload, timeout=120, *, on_start):
            on_start()
            if role == "review":
                launched.append(deepcopy(payload["review_scope"]))
                raise KeyboardInterrupt()
            return self.transport(role, payload, timeout)

        with patch("pdm.model_calls._call_transport", side_effect=transport):
            with self.assertRaises(KeyboardInterrupt):
                run_production(self.repo, "queue", model_call=adapter)
        self.assertEqual(len(launched), 1)
        calls = self.ledger.snapshot()["started_calls"]
        result = run_production(self.repo, "queue", model_call=adapter)
        self.assertEqual(result["status"], "unknown", result)
        prepare_production(self.repo, "renamed", paths, review_strategy="decomposed-v1")
        again = run_production(self.repo, "renamed", model_call=adapter)
        self.assertEqual(again["status"], "unknown", again)
        self.assertEqual(self.ledger.snapshot()["started_calls"], calls)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 1)
        self.assert_no_formal()


if __name__ == "__main__":
    unittest.main()
