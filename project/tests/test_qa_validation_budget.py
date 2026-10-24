"""Synthetic QA validation against a real shared call ledger; no live model."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm import evidence as qa
from pdm.pending import register_pending
from pdm.portable import prepare_reading_package, verify_reading_package


class QAValidationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pdm-qa-budget-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        configure_test_repository(self.repo)
        for key, value in (("user.name", "Artificial QA Budget"),
                           ("user.email", "fixture@example.invalid"),
                           ("commit.gpgsign", "false")):
            self.git("config", "--local", key, value)
        self.git("commit", "--allow-empty", "-m", "artificial QA baseline")
        self.sources = [capture(self.repo, {
            "source_key": "fixture:qa-budget:" + str(index), "source_type": "note",
            "acquisition_status": "full_text", "body": "人工资料，不表示任何真实个人状态。\n",
        }) for index in range(2)]
        self.paths = [source["path"] for source in self.sources]
        self.question = "人工资料说了什么？"

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True,
            text=True, timeout=30,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        ).stdout.strip()

    def snapshot(self):
        return {"head": self.git("rev-parse", "HEAD"), "status": self.git("status", "--porcelain"),
                "memory": {str(path.relative_to(self.repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted((self.repo / "memory").rglob("*")) if path.is_file()}}

    def build(self, mode):
        if mode == "single":
            return qa.evidence(self.repo, self.paths[0])
        if mode == "multi":
            return qa.build_evidence_sources(self.repo, self.paths)
        return qa.build_current_evidence(self.repo, self.paths)

    def ask(self, mode, model_call, *, limit=None):
        if mode == "single":
            return qa.answer(self.repo, self.paths[0], self.question, model_call=model_call)
        if mode == "multi":
            return qa.ask_sources(self.repo, self.paths, self.question, model_call=model_call)
        options = {"max_bytes": limit} if limit is not None else {}
        return qa.ask_current(self.repo, self.paths, self.question, model_call=model_call, **options)

    def ledger(self, name):
        return BudgetLedger.create(self.root / (name + ".json"), 1, 0,
                                   "synthetic QA validation grant", repo=self.repo)

    def add_pending(self, question, source=None):
        source = self.sources[0] if source is None else source
        return register_pending(self.repo, {"question": question, "source_refs": [
            {key: source[key] for key in ("source_id", "source_version", "path")}]})

    def test_invalid_inline_consumes_one_call_and_cannot_replay_in_any_entrypoint(self):
        ref = {key: self.sources[0][key] for key in ("source_id", "source_version")}
        for mode in ("single", "multi", "current"):
            with self.subTest(mode=mode):
                ledger = self.ledger(mode)
                payloads = []

                def transport(role, payload, **kwargs):
                    self.assertEqual(role, "answer")
                    payloads.append(payload)
                    return {"output": {"answer": "人工事实。[99]", "citations": [
                        {**ref, "locator": "L1-L1"}], "limitations": []},
                        "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                        "invocation_id": "model-" + "1" * 32}

                adapter = BudgetedModelCall(ledger, "once", transport=transport)
                before = self.snapshot()
                with self.assertRaises(CaptureError) as caught:
                    self.ask(mode, adapter)
                self.assertEqual(caught.exception.code, "invalid_inline_citation")
                state = ledger.snapshot()
                self.assertEqual((state["used"], state["started_calls"], state["remaining"]), (1, 1, 0))
                # Transport completion is not acceptance of its answer. It
                # remains charged even when the later evidence validator fails.
                call = state["operations"]["once"]["calls"]["answer"]
                self.assertEqual(call["status"], "completed")
                self.assertEqual(call["payload_sha256"], hashlib.sha256(json.dumps(
                    payloads[0], ensure_ascii=False, allow_nan=False, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest())
                charged = ledger.path.read_bytes()
                with self.assertRaises(BudgetBlocked) as replay:
                    self.ask(mode, adapter)
                self.assertEqual(replay.exception.code, "budget_call_already_started")
                self.assertEqual(len(payloads), 1)
                self.assertEqual(ledger.path.read_bytes(), charged)
                self.assertEqual(self.snapshot(), before)

    def test_pending_over_full_input_budget_never_starts_or_reserves_a_call(self):
        limits = {mode: len(qa._encoded({"question": self.question, "evidence": self.build(mode)})) + 128
                  for mode in ("single", "multi", "current")}
        self.add_pending("ARTIFICIAL_LONG_PENDING_" + "人工待确认问题。" * 256)
        before = self.snapshot()
        for mode, limit in limits.items():
            with self.subTest(mode=mode):
                ledger = self.ledger("pending-" + mode)
                adapter = BudgetedModelCall(ledger, "too-large", transport=lambda *args, **kwargs: self.fail(
                    "pending budget refusal must occur before the transport"))
                initial = ledger.path.read_bytes()
                with patch.object(qa, "MAX_EVIDENCE_BYTES", limit), self.assertRaises(CaptureError) as caught:
                    self.ask(mode, adapter, limit=limit)
                self.assertEqual(caught.exception.code, "evidence_limit")
                self.assertEqual(ledger.path.read_bytes(), initial)
                state = ledger.snapshot()
                self.assertEqual((state["used"], state["started_calls"], state["reserved_calls"]), (0, 0, 0))
                self.assertEqual(state["operations"], {})
                self.assertEqual(self.snapshot(), before)

    def test_portable_pending_uses_the_same_scope_and_full_package_budget(self):
        selected = self.add_pending("SELECTED_ARTIFICIAL_PENDING_QUESTION")
        self.add_pending("UNSELECTED_ARTIFICIAL_PENDING_QUESTION", self.sources[1])
        before = self.snapshot()
        output = self.root / "reading.json"
        result = prepare_reading_package(self.repo, [self.paths[0]], [self.question], output=output)
        payload = json.loads(output.read_bytes())["payload"]
        expected = qa.build_current_evidence(self.repo, [self.paths[0]])
        self.assertEqual(payload["evidence"], expected)
        self.assertEqual([item["item_id"] for item in payload["evidence"]["pending"]["items"]],
                         [selected["item_id"]])
        self.assertNotIn("UNSELECTED_ARTIFICIAL_PENDING_QUESTION", output.read_text())
        verified = verify_reading_package(output, expected_sha256=result["sha256"])
        self.assertEqual((verified["model_calls"], verified["second_model_reading"]), (0, "unverified"))
        # Fit the same package without pending, while keeping all question/rule
        # bytes. The added scoped registry context alone crosses this cap.
        without_pending = json.loads(output.read_bytes())
        without_pending["payload"]["evidence"].pop("pending")
        from pdm.contracts import canonical
        limit = len(canonical(without_pending)) + 1
        self.assertLess(limit, output.stat().st_size)
        rejected = self.root / "too-large-reading.json"
        with patch("pdm.portable.MAX_PACKAGE_BYTES", limit), self.assertRaises(CaptureError) as caught:
            prepare_reading_package(self.repo, [self.paths[0]], [self.question], output=rejected)
        self.assertEqual(caught.exception.code, "reading_package_limit")
        self.assertFalse(rejected.exists())
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
