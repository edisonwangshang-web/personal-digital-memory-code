"""Bounded answer validation with artificial sources; no cloud or real memory."""

from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError, markdown
from pdm.evidence import answer, evidence
from pdm.pipeline import process, publish


def digest(data):
    return "sha256-" + hashlib.sha256(data).hexdigest()


class FakeGit:
    current_head = "a" * 40
    dirty = False
    files = {}

    def __init__(self, repo):
        pass

    def head(self):
        return self.current_head

    def lock(self):
        return nullcontext()

    def run(self, *args):
        if args[0] != "status":
            raise AssertionError(args)
        return b" M fixture.md\n" if self.dirty else b""

    def _existing(self, path):
        return self.files.get(path)

    def read(self, head, path):
        return self.files[path]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        FakeGit.current_head = "a" * 40
        FakeGit.dirty = False
        self.source = {"source_id": "src-fixture", "source_version": "sha256-fixture",
                       "path": "memory/sources/conversations/fixture.md",
                       "body": "用户：人工测试资料。\n状态与日期未知。\n"}
        self.ref = {"source_id": self.source["source_id"], "source_version": self.source["source_version"],
                    "locator": "L1-L2", "quote": "用户：人工测试资料。"}
        self.review_path = "memory/runs/processes/run-fixture/review.md"
        FakeGit.files = {}
        for folder, name in (("records", "goal-fixture"), ("topics", "topic-fixture")):
            metadata = {"schema_version": "0.2", "id": name, "sources": [self.ref],
                        "content_version": "sha256-" + "1" * 64,
                        "generated_by": {"run_id": "run-fixture"},
                        "review_status": "passed", "review_ref": self.review_path}
            FakeGit.files[f"memory/{folder}/{name}.md"] = markdown(metadata, name + "的受审正文。\n")
        self.publication = {"source": self.source, "manifest_path": "memory/runs/processes/run-fixture/manifest.md",
                            "commit_sha": "b" * 40, "run_id": "run-fixture", "review_path": self.review_path,
                            "files": {path: digest(data) for path, data in FakeGit.files.items()}}
        for name, value in (("PipelineGit", FakeGit), ("control_guard", lambda *args: None),
                            ("build_pending_context", lambda *args, **kwargs: None),
                            ("load_source", lambda *args: dict(self.source)),
                            ("verify_publication", lambda *args, **kwargs: self.publication)):
            patcher = patch("pdm.evidence." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.payloads = []

    def model(self, output=None, after=None, **metrics):
        def call(role, payload):
            self.assertEqual(role, "answer")
            self.payloads.append(payload)
            if after is not None:
                after()
            return {"output": output or {"answer": "资料不足。", "citations": [], "limitations": ["fixture gap"]},
                    "invocation_id": "fixture-invocation", **metrics}
        return call

    def assert_code(self, expected, operation):
        with self.assertRaises(CaptureError) as caught:
            operation()
        self.assertEqual(caught.exception.code, expected)

    def test_evidence_is_explicit_and_publication_optional(self):
        result = evidence(Path("/fixture"), self.source["path"])
        self.assertEqual(result["knowledge_status"], "unprocessed_or_unverified")
        self.assertEqual(result["scope"], "one_explicit_source")
        self.assertIsNone(result["publication"])
        self.assertEqual(result["lines"][1], {"line": 2, "text": "状态与日期未知。"})
        reviewed = evidence(Path("/fixture"), self.source["path"], "run-fixture")
        self.assertEqual(reviewed["knowledge_status"], "published")
        self.assertEqual(reviewed["scope"], "one_explicit_source_and_verified_publication")
        self.assertEqual(len(reviewed["publication"]["documents"]), 2)

    def test_formal_record_and_topic_bodies_enter_payload_and_versions_are_returned(self):
        result = answer(Path("/fixture"), self.source["path"], "原文说了什么？", "run-fixture",
                        model_call=self.model(elapsed_seconds=1.25, usage={"input_tokens": 12}))
        docs = self.payloads[0]["evidence"]["publication"]["documents"]
        self.assertEqual({doc["body"] for doc in docs}, {"goal-fixture的受审正文。\n", "topic-fixture的受审正文。\n"})
        self.assertEqual({doc["path"] for doc in docs}, set(self.publication["files"]))
        self.assertEqual(result["elapsed_seconds"], 1.25)
        self.assertEqual(result["usage"], {"input_tokens": 12})
        self.assertEqual(result["evidence_source"]["source_version"], self.source["source_version"])
        self.assertEqual(result["evidence_publication"]["files"], self.publication["files"])
        self.assertEqual(result["evidence_publication"]["object_versions"][docs[0]["path"]]["schema_version"], "0.2")
        encoded = json.dumps(self.payloads[0], ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.assertEqual(result["input_payload_bytes"], len(encoded))
        self.assertEqual(result["input_payload_sha256"], digest(encoded))
        encoded_evidence = json.dumps(self.payloads[0]["evidence"], ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.assertEqual(result["evidence_bytes"], len(encoded_evidence))
        self.assertEqual(result["evidence_sha256"], digest(encoded_evidence))

    def test_valid_citation_and_limitations_are_retained(self):
        output = {"answer": "原文是一条人工测试资料，日期未知。", "citations": [{
            "source_id": "src-fixture", "source_version": "sha256-fixture", "locator": "L1-L2"}],
            "limitations": ["原始时间未知。"]}
        result = answer(Path("/fixture"), self.source["path"], "原文说了什么？", model_call=self.model(output))
        self.assertEqual(result["citations"], output["citations"])
        self.assertGreater(len(result["limitations"]), 1)
        self.assertEqual(result["model"], "unknown")
        self.assertIsNone(result["elapsed_seconds"])
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["evidence_publication"])
        self.assertEqual(result["knowledge_status"], "unprocessed_or_unverified")

    def test_out_of_scope_or_invalid_line_citations_rejected(self):
        for locator, identity in (("L1-L3", "src-fixture"), ("L2-L1", "src-fixture"),
                                  ("L1-L1", "src-other")):
            output = {"answer": "fixture", "citations": [{"source_id": identity,
                "source_version": "sha256-fixture", "locator": locator}], "limitations": []}
            self.assert_code("invalid_citation", lambda: answer(Path("/fixture"), self.source["path"],
                "fixture?", model_call=self.model(output)))

    def test_no_evidence_requires_gap_and_source_match(self):
        self.assert_code("unsupported_answer", lambda: answer(Path("/fixture"), self.source["path"],
            "fixture?", model_call=self.model({"answer": "fixture", "citations": [], "limitations": []})))
        self.publication["source"] = {**self.source, "source_id": "different-fixture"}
        self.assert_code("evidence_source_mismatch", lambda: evidence(Path("/fixture"), self.source["path"], "run-fixture"))

    def test_publication_path_outside_formal_flat_paths_rejected(self):
        for path in ("memory/sources/secret.md", "memory/records/../secrets.md", "memory/records/nested/other.md"):
            with self.subTest(path=path):
                self.publication["files"] = {path: "sha256-" + "1" * 64}
                self.assert_code("path_rejected", lambda: evidence(Path("/fixture"), self.source["path"], "run-fixture"))

    def test_other_source_or_run_in_formal_metadata_is_rejected(self):
        path = next(iter(self.publication["files"]))
        for ref, generated in (({**self.ref, "source_id": "other"}, "run-fixture"), (self.ref, "run-other")):
            data = markdown({"schema_version": "0.2", "sources": [ref], "review_status": "passed",
                             "review_ref": self.review_path, "generated_by": {"run_id": generated}}, "fixture")
            FakeGit.files[path] = data
            self.publication["files"][path] = digest(data)
            self.assert_code("evidence_source_mismatch", lambda: evidence(Path("/fixture"), self.source["path"], "run-fixture"))

    def test_concurrent_data_change_invalidates_answer(self):
        self.assert_code("head_conflict", lambda: answer(Path("/fixture"), self.source["path"], "fixture?",
            model_call=self.model(after=lambda: setattr(FakeGit, "current_head", "c" * 40))))

    def test_dirty_worktree_before_or_after_model_rejected(self):
        FakeGit.dirty = True
        model = Mock()
        self.assert_code("dirty_worktree", lambda: answer(Path("/fixture"), self.source["path"], "fixture?", model_call=model))
        model.assert_not_called()
        FakeGit.dirty = False
        self.assert_code("dirty_worktree", lambda: answer(Path("/fixture"), self.source["path"], "fixture?",
            model_call=self.model(after=lambda: setattr(FakeGit, "dirty", True))))

    def test_publication_is_rechecked_after_model_even_without_new_commit(self):
        verifier = Mock(side_effect=[self.publication, CaptureError("publication_mismatch", "fixture changed")])
        with patch("pdm.evidence.verify_publication", verifier):
            self.assert_code("publication_mismatch", lambda: answer(Path("/fixture"), self.source["path"],
                "fixture?", "run-fixture", model_call=self.model()))
        self.assertEqual(verifier.call_count, 2)

    def test_formal_bytes_changed_during_model_are_rejected_even_if_verifier_is_stale(self):
        path = next(iter(FakeGit.files))
        self.assert_code("publication_mismatch", lambda: answer(Path("/fixture"), self.source["path"],
            "fixture?", "run-fixture", model_call=self.model(after=lambda: FakeGit.files.__setitem__(path, b"tampered"))))

    def test_formal_bytes_changed_before_read_are_rejected(self):
        FakeGit.files[next(iter(FakeGit.files))] = b"tampered"
        model = Mock()
        self.assert_code("publication_mismatch", lambda: answer(Path("/fixture"), self.source["path"],
            "fixture?", "run-fixture", model_call=model))
        model.assert_not_called()

    def test_budget_never_silently_truncates_or_invokes_model(self):
        model = Mock()
        with patch("pdm.evidence.MAX_EVIDENCE_BYTES", 64):
            self.assert_code("evidence_limit", lambda: answer(Path("/fixture"), self.source["path"],
                "fixture?", "run-fixture", model_call=model))
        model.assert_not_called()
        self.assertEqual(self.source["body"], "用户：人工测试资料。\n状态与日期未知。\n")

    def test_complete_question_payload_also_has_budget(self):
        selected = evidence(Path("/fixture"), self.source["path"])
        budget = len(json.dumps(selected, ensure_ascii=False).encode("utf-8")) + 10
        model = Mock()
        with patch("pdm.evidence.MAX_EVIDENCE_BYTES", budget):
            self.assert_code("evidence_limit", lambda: answer(Path("/fixture"), self.source["path"],
                "A" * 1000, model_call=model))
        model.assert_not_called()

    def test_invalid_metrics_do_not_become_verification_evidence(self):
        for metrics in ({"elapsed_seconds": float("nan")}, {"elapsed_seconds": True},
                        {"usage": {"input_tokens": -1}}, {"usage": {"made_up_metric": 1}}, {"model": "bad model"}):
            with self.subTest(metrics=metrics):
                self.assert_code("invalid_model_invocation", lambda: answer(Path("/fixture"), self.source["path"],
                    "fixture?", model_call=self.model(**metrics)))


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class RealGitEvidenceTests(unittest.TestCase):
    """Temporary Git plus artificial extraction, Review and answer calls."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Isolated Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "isolated baseline")
        self.saved = capture(self.repo, {"source_key": "fixture:evidence", "source_type": "conversation",
            "platform": "isolated", "acquisition_status": "full_text", "body": "用户：请整理人工项目。\n日期未知。\n"})
        self.number = 0
        self.contexts = []

    def artificial_model(self, role, payload):
        self.number += 1
        self.contexts.append((role, payload))
        if role == "answer":
            source = payload["evidence"]["source"]
            output = {"answer": "用户请求整理人工项目，日期未知。", "citations": [{"source_id": source["source_id"],
                "source_version": source["source_version"], "locator": "L1-L2"}], "limitations": ["无法据此判断完成情况。"]}
        elif role == "extract":
            source = payload["sources"][0]
            ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                   "locator": "L1-L2", "quote": "用户：请整理人工项目。"}
            output = {"objects": [{"id": "goal-artificial", "type": "goal", "title": "人工项目",
                "source_kind": "user_statement", "sources": [ref], "domains": [], "valid_from": None, "valid_to": None,
                "verification_status": "unverified", "lifecycle_status": "undetermined", "body": "正式人工记录的正文。"}],
                "wiki": {"id": "topic-artificial", "title": "人工主题", "body": "正式人工主题正文。", "sources": [ref]}}
        else:
            output = {"verdict": "passed", "reasons": ["人工样例忠实性通过"]}
        return {"output": output, "model": "unknown", "usage": None, "elapsed_seconds": 0.125,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def publish_fixture(self):
        processed = process(self.repo, self.saved["path"], model_call=self.artificial_model)
        self.assertEqual(processed["status"], "passed")
        self.published = publish(self.repo, processed["run_id"])
        return processed["run_id"]

    def assert_code(self, code, action):
        with self.assertRaises(CaptureError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def test_real_git_end_to_end_includes_selected_bodies_without_writes_or_other_source(self):
        run_id = self.publish_fixture()
        other = capture(self.repo, {"source_key": "fixture:unselected", "source_type": "conversation",
            "acquisition_status": "full_text", "body": "用户：请整理人工项目。\nUNSELECTED_PRIVATE_ARTIFICIAL_SENTINEL\n"})
        def other_model(role, payload):
            result = self.artificial_model(role, payload)
            if role == "extract":
                result["output"]["objects"][0]["id"] = "goal-unselected"
                result["output"]["objects"][0]["body"] = "UNSELECTED_REVIEWED_ARTIFICIAL_RECORD"
                result["output"]["wiki"]["id"] = "topic-unselected"
                result["output"]["wiki"]["body"] = "UNSELECTED_REVIEWED_ARTIFICIAL_TOPIC"
            return result
        other_run = process(self.repo, other["path"], model_call=other_model)
        self.assertEqual(other_run["status"], "passed")
        publish(self.repo, other_run["run_id"])
        head = git(self.repo, "rev-parse", "HEAD")
        before = {str(path.relative_to(self.repo)): path.read_bytes() for path in (self.repo / "memory").rglob("*.md")}
        result = answer(self.repo, self.saved["path"], "用户请求什么？", run_id, model_call=self.artificial_model)
        payload = self.contexts[-1][1]
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertIn("正式人工记录的正文。", serialized)
        self.assertIn("正式人工主题正文。", serialized)
        self.assertNotIn("UNSELECTED_PRIVATE_ARTIFICIAL_SENTINEL", serialized)
        self.assertNotIn("UNSELECTED_REVIEWED_ARTIFICIAL_RECORD", serialized)
        self.assertNotIn("UNSELECTED_REVIEWED_ARTIFICIAL_TOPIC", serialized)
        self.assertNotIn(other_run["run_id"], serialized)
        self.assertNotIn(other["source_id"], serialized)
        self.assertEqual(result["evidence_publication"]["run_id"], run_id)
        self.assertEqual(result["evidence_source"]["source_version"], self.saved["source_version"])
        self.assertEqual(result["model"], "unknown")
        self.assertIsNone(result["usage"])
        self.assertEqual(result["elapsed_seconds"], 0.125)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(before, {str(path.relative_to(self.repo)): path.read_bytes() for path in (self.repo / "memory").rglob("*.md")})

    def test_wrong_source_for_selected_run_is_rejected_without_model(self):
        run_id = self.publish_fixture()
        other = capture(self.repo, {"source_key": "fixture:other", "source_type": "conversation",
            "acquisition_status": "full_text", "body": "其他人工资料。\n"})
        model = Mock()
        self.assert_code("evidence_source_mismatch", lambda: answer(self.repo, other["path"], "人工问题？", run_id, model_call=model))
        model.assert_not_called()

    def test_committed_review_tampering_is_rejected_before_answer(self):
        run_id = self.publish_fixture()
        path = self.repo / self.published["review_path"]
        path.write_bytes(path.read_bytes() + b"tampered")
        git(self.repo, "add", str(path))
        git(self.repo, "commit", "-m", "isolated review tampering")
        model = Mock()
        with self.assertRaises(CaptureError):
            answer(self.repo, self.saved["path"], "人工问题？", run_id, model_call=model)
        model.assert_not_called()

    def test_uncommitted_formal_or_review_changes_during_answer_are_rejected(self):
        run_id = self.publish_fixture()
        for relative in ("memory/records/goal-artificial.md", self.published["review_path"]):
            with self.subTest(relative=relative):
                path = self.repo / relative
                original = path.read_bytes()
                def mutate(role, payload):
                    result = self.artificial_model(role, payload)
                    path.write_bytes(original + b"tampered")
                    return result
                self.assert_code("dirty_worktree", lambda: answer(self.repo, self.saved["path"],
                    "人工问题？", run_id, model_call=mutate))
                path.write_bytes(original)

    def test_uncommitted_controls_during_answer_are_rejected(self):
        run_id = self.publish_fixture()
        def mutate(role, payload):
            result = self.artificial_model(role, payload)
            path = self.repo / "memory/runs/controls/delete.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(markdown({"schema_version": "0.2"}, "人工删除控制"))
            return result
        self.assert_code("unsupported_controls", lambda: answer(self.repo, self.saved["path"],
            "人工问题？", run_id, model_call=mutate))

    def test_real_git_source_only_does_not_claim_reviewed_publication(self):
        result = answer(self.repo, self.saved["path"], "人工请求？", model_call=self.artificial_model)
        self.assertEqual(result["knowledge_status"], "unprocessed_or_unverified")
        self.assertIsNone(result["evidence_publication"])
        self.assertIsNone(self.contexts[-1][1]["evidence"]["publication"])


if __name__ == "__main__":
    unittest.main()
