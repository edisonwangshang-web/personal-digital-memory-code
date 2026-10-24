"""Scoped confirmation context through real evidence/ask paths; artificial data."""

import copy
import json
import unittest
from unittest.mock import Mock, patch

import test_pending as pending_fixtures
from pdm.contracts import CaptureError
from pdm.controls import record_control
from pdm.evidence import answer, ask_current, ask_sources, build_current_evidence, evidence
from pdm.pending import REGISTRY_PATH, _encode, answer_pending, read_registry, register_pending, resolve_pending
from pdm.pipeline import process_sources, publish


class PendingEvidenceTests(unittest.TestCase):
    def setUp(self):
        # Composition avoids rediscovering the fixture's original test methods.
        self.fixture = pending_fixtures.PendingTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.source = self.fixture.source
        self.item = {**self.fixture.item, "question": "SCOPED_CONFIRMATION_QUESTION：具体从哪一天开始？"}
        self.payloads = []

    def snapshot(self):
        return {"head": pending_fixtures.git(self.repo, "rev-parse", "HEAD"),
                "status": pending_fixtures.git(self.repo, "status", "--porcelain", "--untracked-files=all"),
                "memory": {str(p.relative_to(self.repo)): p.read_bytes()
                           for p in (self.repo / "memory").rglob("*") if p.is_file()}}

    def model(self, role, payload):
        self.assertEqual(role, "answer")
        self.payloads.append(copy.deepcopy(payload))
        return {"output": {"answer": "仅按给定待确认状态回答，未将答复当作已完成。",
                           "citations": [], "limitations": ["未判断个人事实。"]},
                "model": "artificial", "invocation_id": "model-" + "a" * 32,
                "elapsed_seconds": 0.01, "usage": None}

    def answered(self):
        registered = register_pending(self.repo, self.item)
        reply = self.fixture.capture("qa-reply")
        answer_pending(self.repo, registered["item_id"], reply["path"])
        return registered, reply

    def resolved(self):
        registered, reply = self.answered()
        processed = process_sources(self.repo, [reply["path"]], model_call=self.fixture.model)
        publication = publish(self.repo, processed["run_id"])
        resolve_pending(self.repo, registered["item_id"], processed["run_id"])
        return registered, reply, processed, publication

    def rewrite(self, edit):
        registry = read_registry(self.repo)
        edit(registry["items"])
        self.fixture.commit(REGISTRY_PATH, _encode(registry["items"]))

    def assert_code(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_single_open_question_reaches_actual_answer_payload_without_writes(self):
        register_pending(self.repo, self.item)
        before = self.snapshot()
        result = answer(self.repo, self.source["path"], "还有什么待确认？", model_call=self.model)
        context = self.payloads[-1]["evidence"]["pending"]
        self.assertEqual(context["items"][0]["question"], self.item["question"])
        self.assertEqual(context["items"][0]["pending_status"], "open")
        self.assertEqual(context["items"][0]["reply_sources"], [])
        self.assertEqual(context["registry_head"], before["head"])
        self.assertEqual(result["input_payload_bytes"], len(json.dumps(self.payloads[-1], ensure_ascii=False).encode()))
        self.assertEqual(self.snapshot(), before)

    def test_answered_context_reaches_multi_and_current_ask_but_is_not_resolved(self):
        registered, reply = self.answered()
        paths = [self.source["path"], reply["path"]]
        before = self.snapshot()
        for ask in (ask_sources, ask_current):
            with self.subTest(api=ask.__name__):
                ask(self.repo, paths, "这项问题是否已经处理完成？", model_call=self.model)
                selected = self.payloads[-1]["evidence"]
                item = selected["pending"]["items"][0]
                self.assertEqual(item["item_id"], registered["item_id"])
                self.assertEqual(item["question"], self.item["question"])
                self.assertEqual(item["pending_status"], "answered")
                self.assertEqual(item["reply_sources"][0]["path"], reply["path"])
                self.assertIsNone(item["resolution"])
                self.assertEqual(item["resolution_verification"]["status"], "not_applicable")
                self.assertIn("answered仅收到", "".join(selected["limitations"]))
                self.assertEqual(self.snapshot(), before)

    def test_partial_question_scope_and_unselected_reply_never_leak_questions_or_load_bodies(self):
        _, reply = self.answered()
        extra = self.fixture.capture("other-question", "UNSELECTED_RAW_SENTINEL\n")
        register_pending(self.repo, {"question": "UNRELATED_QUESTION_SECRET", "source_refs": [self.fixture.ref(extra)]})
        register_pending(self.repo, {"question": "PARTIAL_QUESTION_SECRET", "source_refs": [self.fixture.ref(self.source), self.fixture.ref(extra)]})
        from pdm import evidence as evidence_module
        original = evidence_module.load_source
        def bounded(repo, path, *args, **kwargs):
            self.assertEqual(path, self.source["path"])
            return original(repo, path, *args, **kwargs)
        with patch("pdm.evidence.load_source", side_effect=bounded), patch("pdm.pipeline.load_source", side_effect=AssertionError("No pending source reads")):
            ask_current(self.repo, [self.source["path"]], "待确认状态？", model_call=self.model)
        context = self.payloads[-1]["evidence"]["pending"]
        serialized = json.dumps(context)
        for secret in (self.item["question"], "PARTIAL_QUESTION_SECRET", "UNRELATED_QUESTION_SECRET", "UNSELECTED_RAW_SENTINEL"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(context["items"], [])
        self.assertEqual(len(context["coverage"]["gaps"]), 2)
        self.assertTrue(any(reply["path"] in gap["missing_source_paths"] for gap in context["coverage"]["gaps"]))
        self.assertFalse(self.payloads[-1]["evidence"]["coverage"]["current_complete"])

    def test_deleted_reply_suppresses_whole_item_before_body_read_including_history(self):
        _, reply = self.answered()
        record_control(self.repo, {"request_key": "delete:reply", "action": "delete", "source_paths": [reply["path"]]})
        from pdm import evidence as evidence_module
        original = evidence_module.load_source
        def bounded(repo, path, *args, **kwargs):
            self.assertNotEqual(path, reply["path"])
            return original(repo, path, *args, **kwargs)
        for history in (False, True):
            with self.subTest(history=history), patch("pdm.evidence.load_source", side_effect=bounded):
                selected = build_current_evidence(self.repo, [self.source["path"], reply["path"]], history=history)
            self.assertEqual(selected["pending"]["items"], [])
            self.assertEqual(selected["pending"]["coverage"]["excluded_item_count"], 1)
            self.assertNotIn(self.item["question"], json.dumps(selected))

    def test_corrected_reply_question_is_only_visible_in_explicit_history(self):
        _, reply = self.answered()
        replacement = self.fixture.capture("reply-correction")
        record_control(self.repo, {"request_key": "correct:reply", "action": "corrects", "source_paths": [reply["path"]],
                                   "replacement_source_paths": [replacement["path"]]})
        paths = [self.source["path"], reply["path"]]
        current = build_current_evidence(self.repo, paths)
        self.assertEqual(current["pending"]["items"], [])
        self.assertNotIn(self.item["question"], json.dumps(current))
        historical = build_current_evidence(self.repo, paths, history=True)
        self.assertEqual(historical["pending"]["items"][0]["question"], self.item["question"])
        self.assertEqual(historical["pending"]["state_basis"], "registry_at_evidence_head")

    def test_withdrawn_registry_state_is_preserved_without_implying_false_facts(self):
        register_pending(self.repo, self.item)
        self.rewrite(lambda items: items[0].update(pending_status="withdrawn"))
        selected = evidence(self.repo, self.source["path"])
        self.assertEqual(selected["pending"]["items"][0]["pending_status"], "withdrawn")
        self.assertIn("withdrawn表示撤回", "".join(selected["limitations"]))

    def test_budget_omitted_source_cannot_reenter_payload_through_its_question(self):
        registered = register_pending(self.repo, self.item)
        large = self.fixture.capture("budget-omitted", "ARTIFICIAL_LARGE_RAW\n" * 6000)
        answer_pending(self.repo, registered["item_id"], large["path"])
        selected = build_current_evidence(self.repo, [self.source["path"], large["path"]], max_bytes=8000)
        self.assertNotIn(self.item["question"], json.dumps(selected))
        self.assertIn(large["path"], selected["pending"]["coverage"]["gaps"][0]["unread_source_paths"])
        self.assertFalse(selected["coverage"]["pending_complete"])
        self.assertLessEqual(len(json.dumps(selected, ensure_ascii=False).encode()), 8000)

    def test_resolved_without_explicit_run_is_unverified_and_does_not_open_run(self):
        _, reply, processed, _ = self.resolved()
        with patch("pdm.pipeline.verify_publication", side_effect=AssertionError("Unselected run opened")):
            selected = build_current_evidence(self.repo, [self.source["path"], reply["path"]])
        item = selected["pending"]["items"][0]
        self.assertEqual(item["pending_status"], "resolved")
        self.assertEqual(item["resolution_verification"]["status"], "not_selected")
        self.assertEqual(selected["pending"]["coverage"]["missing_run_ids"], [processed["run_id"]])
        self.assertFalse(selected["coverage"]["current_complete"])

    def test_selected_resolution_verifies_actual_record_reply_and_association_read_only(self):
        _, reply, processed, publication = self.resolved()
        before = self.snapshot()
        ask_current(self.repo, [self.source["path"], reply["path"]], "处理依据是什么？", [processed["run_id"]], model_call=self.model)
        context = self.payloads[-1]["evidence"]["pending"]
        proof = context["items"][0]["resolution_verification"]
        self.assertEqual(proof["status"], "verified_history")
        self.assertEqual(proof["commit_sha"], publication["commit_sha"])
        self.assertTrue(proof["reply_record_references_verified"])
        self.assertTrue(proof["reply_association_verified"])
        self.assertEqual(self.snapshot(), before)

    def test_selected_forged_resolution_fails_before_answer_model(self):
        _, reply, processed, _ = self.resolved()
        self.rewrite(lambda items: items[0]["resolution"].update(commit_sha="0" * 40))
        model = Mock()
        self.assert_code("pending_publication_mismatch", lambda: ask_current(self.repo,
            [self.source["path"], reply["path"]], "完成了吗？", [processed["run_id"]], model_call=model))
        model.assert_not_called()

    def test_historical_resolution_survives_later_formal_bytes_without_reviving_old_current_answer(self):
        _, reply, processed, publication = self.resolved()
        path = next(path for path in publication["files"] if path.startswith("memory/records/"))
        self.fixture.commit(path, (self.repo / path).read_bytes() + b"\nLater artificial revision.\n")
        before = self.snapshot()
        selected = build_current_evidence(self.repo, [self.source["path"], reply["path"]], [processed["run_id"]])
        self.assertEqual(selected["pending"]["items"][0]["resolution_verification"]["status"], "verified_history")
        self.assertEqual(selected["publications"], [])
        self.assertEqual(selected["coverage"]["unread_runs"][0]["reason"], "historical_publication")
        self.assertEqual(self.snapshot(), before)

    def test_wiki_only_reply_cannot_validate_a_hand_marked_resolved_registry(self):
        _, reply = self.answered()
        def wiki_only(role, payload):
            result = self.fixture.model(role, payload)
            if role == "extract":
                refs = {}
                for source in payload["sources"]:
                    refs[source["path"]] = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                                            "locator": "L1-L1", "quote": source["body"].splitlines()[0]}
                result["output"]["objects"][0]["sources"] = [refs[self.source["path"]]]
                result["output"]["wiki"]["sources"] = [refs[reply["path"]]]
            return result
        processed = process_sources(self.repo, [self.source["path"], reply["path"]], model_call=wiki_only)
        publication = publish(self.repo, processed["run_id"])
        self.rewrite(lambda items: items[0].update(pending_status="resolved", resolution={
            "run_id": processed["run_id"], "commit_sha": publication["commit_sha"], "manifest_path": publication["manifest_path"]}))
        self.assert_code("pending_reply_not_applied", lambda: build_current_evidence(self.repo,
            [self.source["path"], reply["path"]], [processed["run_id"]]))

    def test_forged_reply_association_is_not_accepted_as_verified_resolution(self):
        _, reply, processed, _ = self.resolved()
        self.rewrite(lambda items: items[0]["reply_sources"][0].update(associated_head=reply["commit_sha"]))
        self.assert_code("pending_publication_mismatch", lambda: build_current_evidence(self.repo,
            [self.source["path"], reply["path"]], [processed["run_id"]]))

    def test_all_ask_paths_recheck_hidden_registry_bytes_on_transport_failure(self):
        register_pending(self.repo, self.item)
        extra = self.fixture.capture("additional-authorized")
        original = (self.repo / REGISTRY_PATH).read_bytes()
        pending_fixtures.git(self.repo, "update-index", "--skip-worktree", REGISTRY_PATH)
        for name, invoke in (
                ("single", lambda model: answer(self.repo, self.source["path"], "状态？", model_call=model)),
                ("multiple", lambda model: ask_sources(self.repo, [self.source["path"], extra["path"]], "状态？", model_call=model)),
                ("current", lambda model: ask_current(self.repo, [self.source["path"]], "状态？", model_call=model))):
            with self.subTest(api=name):
                (self.repo / REGISTRY_PATH).write_bytes(original)
                def hidden_change(role, payload):
                    (self.repo / REGISTRY_PATH).write_bytes(original.replace(b'"pending_status":"open"', b'"pending_status":"withdrawn"'))
                    self.assertEqual(pending_fixtures.git(self.repo, "status", "--porcelain"), "")
                    raise RuntimeError("artificial transport error")
                self.assert_code("saved_bytes_changed", lambda: invoke(hidden_change))
        (self.repo / REGISTRY_PATH).write_bytes(original)
        pending_fixtures.git(self.repo, "update-index", "--no-skip-worktree", REGISTRY_PATH)

    def test_registry_change_during_failed_answer_is_detected_by_final_recheck(self):
        register_pending(self.repo, self.item)
        def changed(role, payload):
            self.rewrite(lambda items: items[0].update(pending_status="withdrawn"))
            raise RuntimeError("artificial model failure")
        self.assert_code("evidence_changed", lambda: ask_current(self.repo, [self.source["path"]], "状态？", model_call=changed))


if __name__ == "__main__":
    unittest.main()
