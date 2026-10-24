"""Explicit formal-object reading through a real isolated production chain.

All sources, extraction/Review responses and answer calls are artificial. No
personal repository, network transport or persistent model cache is involved.
"""

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

import test_batch_groups as group_fixtures
from pdm.batching import _fragment
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.controls import record_control
from pdm.evidence import ask_current, build_current_evidence
from pdm.pending import register_pending
from pdm.pipeline import load_source
from pdm.transaction import PipelineGit


class SelectedCurrentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = group_fixtures.BatchGroupTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo = self.fixture.repo
        self.sources = [capture(self.repo, {"source_key": "fixture:selected-reading:" + str(index),
                        "source_type": "note", "acquisition_status": "full_text", "body": body})
                        for index, body in enumerate((
                            "SELECTED_ARTIFICIAL_FACT\nPRIVATE_UNSELECTED_SECOND_LINE\n",
                            "UNRELATED_ARTIFICIAL_LONG_SOURCE_" + "Z" * 8000 + "\n"))]
        self.paths = [source["path"] for source in self.sources]
        self.fixture.sources = lambda: self.paths
        paths, plan, reviewed, candidate = self.fixture.ready()
        self.publication = self.fixture.publish(paths, plan, reviewed, candidate)
        self.run = self.publication["run_id"]
        self.record = next(obj["path"] for obj in candidate["objects"]
                           if obj["path"].startswith("memory/records/")
                           and obj["metadata"]["sources"][0]["source_id"] == self.sources[0]["source_id"])
        self.topic = next(obj["path"] for obj in candidate["objects"]
                          if obj["path"].startswith("memory/topics/")
                          and obj["metadata"]["sources"][0]["source_id"] == self.sources[0]["source_id"])
        self.payloads = []

    def snapshot(self):
        git = PipelineGit(self.repo)
        return {"head": git.head(), "status": git.run("status", "--porcelain", "--untracked-files=all"),
                "memory": {str(path.relative_to(self.repo)): path.read_bytes()
                           for path in (self.repo / "memory").rglob("*") if path.is_file()}}

    def assert_code(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def model(self, role, payload):
        self.assertEqual(role, "answer")
        self.payloads.append(deepcopy(payload))
        source = payload["evidence"]["sources"][0]["source"]
        return {"output": {"answer": "人工样例事实有来源支持。[1]", "citations": [
                    {"source_id": source["source_id"], "source_version": source["source_version"], "locator": "L1-L1"}],
                    "limitations": ["仅回答明确选择的人工知识。"]},
                "model": "artificial", "invocation_id": "model-" + "a" * 32,
                "elapsed_seconds": 0.01, "usage": None}

    def selected(self, **kwargs):
        return build_current_evidence(self.repo, self.paths, [self.run], object_paths=[self.topic], **kwargs)

    def test_capture_extract_review_group_select_topic_answer_is_read_only_and_compact(self):
        self.assertEqual(self.fixture.roles, ["extract", "review"])
        before = self.snapshot()
        result = ask_current(self.repo, self.paths, "人工事实是什么？", [self.run], object_paths=[self.topic],
                             compact_sources=True, max_bytes=14000, model_call=self.model)
        selected = self.payloads[-1]["evidence"]
        self.assertEqual(result["citations"][0]["locator"], "L1-L1")
        self.assertEqual({doc["path"] for pub in selected["publications"] for doc in pub["documents"]},
                         {self.topic, self.record})
        self.assertEqual(set(selected["publications"][0]["files"]), {self.topic, self.record})
        self.assertEqual(set(selected["publications"][0]["object_origins"]), {self.topic, self.record})
        self.assertEqual(selected["coverage"]["requested_object_paths"], [self.topic])
        self.assertEqual(selected["coverage"]["dependency_object_paths"], [self.record])
        self.assertEqual(selected["coverage"]["read_object_paths"], sorted([self.topic, self.record]))
        self.assertEqual(selected["coverage"]["not_selected_object_paths"],
                         sorted(set(self.publication["files"]) - {self.topic, self.record}))
        self.assertTrue(selected["coverage"]["object_selection_complete"])
        self.assertFalse(selected["coverage"]["current_complete"])
        self.assertEqual(selected["coverage"]["not_needed_source_paths"], [self.paths[1]])
        self.assertEqual(selected["coverage"]["read_source_paths"], [self.paths[0]])
        self.assertEqual(selected["coverage"]["fragment_missing_ranges"], {self.paths[0]: ["L2-L2"]})
        self.assertEqual(len(selected["sources"]), 1)
        entry = selected["sources"][0]
        self.assertEqual(entry["source"]["body"], "")
        self.assertEqual(entry["source"]["content_scope"], "fragment")
        self.assertEqual(entry["lines"], [{"line": 1, "text": "SELECTED_ARTIFICIAL_FACT"}])
        text = json.dumps(selected)
        self.assertNotIn("PRIVATE_UNSELECTED_SECOND_LINE", text)
        self.assertNotIn("UNRELATED_ARTIFICIAL_LONG_SOURCE", text)
        self.assertEqual(self.snapshot(), before)

    def test_default_payload_is_unchanged_and_explicit_compact_mode_preserves_all_lines(self):
        before = self.snapshot()
        legacy = build_current_evidence(self.repo, self.paths, [self.run])
        self.assertEqual(legacy, build_current_evidence(self.repo, self.paths, [self.run],
                                                       object_paths=None, compact_sources=False))
        compact = build_current_evidence(self.repo, self.paths, [self.run], compact_sources=True)
        self.assertEqual(compact["publications"], legacy["publications"])
        self.assertEqual(compact["coverage"], legacy["coverage"])
        for old, new in zip(legacy["sources"], compact["sources"]):
            self.assertEqual(new["lines"], old["lines"])
            self.assertEqual(new["source"]["body"], "")
            self.assertEqual(new["source"]["content_scope"], "provided_lines")
            self.assertEqual(old["source"]["body"].splitlines(), [line["text"] for line in new["lines"]])
        self.assertLess(len(json.dumps(compact)), len(json.dumps(legacy)))
        self.assertEqual(self.snapshot(), before)

    def test_budget_is_applied_after_selection_and_never_silently_drops_requested_objects(self):
        legacy = build_current_evidence(self.repo, self.paths, [self.run], max_bytes=14000)
        self.assertEqual(legacy["publications"], [])
        self.assertTrue(legacy["coverage"]["budget_exhausted"])
        self.assertEqual(len(self.selected(max_bytes=14000)["publications"][0]["documents"]), 2)
        call = Mock(side_effect=AssertionError("Selected evidence must fit before a model call"))
        before = self.snapshot()
        self.assert_code("selected_evidence_limit", lambda: ask_current(
            self.repo, self.paths, "人工事实？", [self.run], object_paths=[self.topic],
            compact_sources=True, max_bytes=2000, model_call=call))
        call.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_record_selection_does_not_add_an_unrequested_topic(self):
        selected = build_current_evidence(self.repo, self.paths, [self.run], object_paths=[self.record])
        self.assertEqual(selected["coverage"]["dependency_object_paths"], [])
        self.assertEqual([doc["path"] for pub in selected["publications"] for doc in pub["documents"]], [self.record])

    def test_nonexistent_selection_or_insufficient_source_authorization_fails_before_answer(self):
        call = Mock(side_effect=AssertionError("No model call for unavailable selection"))
        before = self.snapshot()
        self.assert_code("object_selection_unavailable", lambda: ask_current(
            self.repo, self.paths, "人工事实？", [self.run], object_paths=["memory/records/missing-artificial.md"], model_call=call))
        self.assert_code("evidence_source_mismatch", lambda: ask_current(
            self.repo, self.paths[:1], "人工事实？", [self.run], object_paths=[self.topic], model_call=call))
        call.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_invalid_selection_is_rejected_without_reading_or_calling_a_model(self):
        with patch("pdm.evidence.PipelineGit", side_effect=AssertionError("No repository read")):
            for value in ([], self.topic, [self.topic, self.topic], ["../private.md"], [True]):
                with self.subTest(value=value):
                    self.assert_code("invalid_object_selection", lambda: build_current_evidence(
                        self.repo, self.paths, [self.run], object_paths=value))
            self.assert_code("invalid_object_selection", lambda: build_current_evidence(
                self.repo, self.paths, object_paths=[self.topic]))
            self.assert_code("invalid_object_selection", lambda: build_current_evidence(
                self.repo, self.paths, [self.run], compact_sources=1))

    def test_topic_record_version_is_checked_after_closure(self):
        from pdm import evidence as module
        original = module._view_document

        def wrong_dependency(*args, **kwargs):
            document = original(*args, **kwargs)
            if document is not None and document["path"] == self.topic:
                document["metadata"]["records"][0]["content_version"] = "sha256-" + "f" * 64
            return document

        with patch("pdm.evidence._view_document", side_effect=wrong_dependency):
            self.assert_code("object_dependency_mismatch", self.selected)

    def test_deleted_selection_is_not_recovered_from_published_history(self):
        record_control(self.repo, {"request_key": "fixture:delete-selected-reading", "action": "delete",
                       "source_paths": [self.paths[0]], "object_paths": [self.record, self.topic]})
        for history in (False, True):
            with self.subTest(history=history):
                self.assert_code("object_selection_unavailable", lambda: self.selected(history=history))

    def test_partial_original_cannot_leak_pending_question_or_cite_an_unprovided_line(self):
        register_pending(self.repo, {"question": "PRIVATE_PENDING_QUESTION_FROM_UNPROVIDED_LINE",
                         "source_refs": [{key: self.sources[0][key] for key in ("source_id", "source_version", "path")}]})
        selected = self.selected(compact_sources=True)
        self.assertEqual(selected["pending"]["items"], [])
        self.assertFalse(selected["coverage"]["pending_complete"])
        self.assertNotIn("PRIVATE_PENDING_QUESTION", json.dumps(selected))

        def invalid_citation(role, payload):
            result = self.model(role, payload)
            result["output"]["citations"][0]["locator"] = "L2-L2"
            return result

        self.assert_code("invalid_citation", lambda: ask_current(self.repo, self.paths, "人工事实？", [self.run],
                         object_paths=[self.topic], compact_sources=True, model_call=invalid_citation))

    def test_requested_fragment_must_cover_every_selected_formal_reference(self):
        source = load_source(self.repo, self.paths[0])
        fragment = _fragment(source, 2, 2, "unused")
        views = [{"path": source["path"], "metadata": source["metadata"], "fragments": [
                  {"fragment_id": fragment["fragment_id"], "locator": "L2-L2",
                   "lines": [{"line": 2, "text": source["body"].splitlines()[1]}]}]}]
        context = {"plan_id": "plan-" + "1" * 64, "batch_id": "batch-" + "2" * 64,
                   "parent_batch_ids": [], "source_ranges": [
                       {key: fragment[key] for key in ("path", "source_id", "source_version", "locator", "fragment_id")}]}
        self.assert_code("object_selection_unavailable", lambda: self.selected(source_views=views, batch_context=context))


if __name__ == "__main__":
    unittest.main()
