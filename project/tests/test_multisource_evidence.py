"""Explicit multi-source evidence, artificial data and no external model calls."""

from contextlib import nullcontext
from copy import deepcopy
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
from pdm.evidence import answer, ask_sources, build_evidence_sources, evidence
from pdm.pipeline import load_source, process_sources, publish


def digest(data):
    return "sha256-" + hashlib.sha256(data).hexdigest()


def fixture_source(letter, body, *, status="full_text", version_letter=None):
    identifier = "src-" + letter * 64
    version = "sha256-" + (version_letter or letter) * 64
    return {"source_id": identifier, "source_version": version,
            "path": f"memory/sources/inbox/{identifier}/{version}.md",
            "commit_sha": "a" * 40, "body": body,
            "metadata": {"source_type": "article" if letter == "a" else "note",
                         "acquisition_status": status,
                         "original_url": f"https://example.invalid/{letter}", "original_time": None},
            "source_bytes_sha256": digest(body.encode()),
            "receipt_path": f"memory/runs/captures/{identifier}/{version}.md",
            "receipt_sha256": digest((body + "receipt").encode())}


class FakeGit:
    head_value = "a" * 40
    dirty = False
    files = {}

    def __init__(self, repo):
        pass

    def head(self):
        return self.head_value

    def lock(self):
        return nullcontext()

    def run(self, *args):
        if args[0] != "status":
            raise AssertionError(args)
        return b" M artificial.md\n" if self.dirty else b""

    def _existing(self, path):
        return self.files.get(path)

    def read(self, head, path):
        return self.files[path]


class MultiSourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path("/artificial")
        self.sources = [fixture_source("a", "文章：一种人工学习方法。\n仅为观点。\n"),
                        fixture_source("b", "用户：我还没有选择方法。\n时间未知。\n")]
        self.paths = [source["path"] for source in self.sources]
        self.source_map = {source["path"]: source for source in self.sources}
        self.loaded = []
        FakeGit.head_value = "a" * 40
        FakeGit.dirty = False
        self.run_id = "run-" + "d" * 32
        self.review = f"memory/runs/processes/{self.run_id}/review.md"
        self.refs = [{"source_id": source["source_id"], "source_version": source["source_version"],
                      "locator": "L1-L1", "quote": source["body"].splitlines()[0]} for source in self.sources]
        FakeGit.files = {}
        for folder, name in (("records", "knowledge-artificial"), ("topics", "topic-artificial")):
            metadata = {"schema_version": "0.2", "id": name, "sources": deepcopy(self.refs),
                        "content_version": "sha256-" + "c" * 64,
                        "generated_by": {"run_id": self.run_id}, "review_status": "passed",
                        "review_ref": self.review}
            FakeGit.files[f"memory/{folder}/{name}.md"] = markdown(metadata, "人工多来源整理正文。\n")
        self.publication = {"source": self.binding(self.sources[0]),
                            "additional_sources": [self.binding(self.sources[1])],
                            "manifest_path": f"memory/runs/processes/{self.run_id}/manifest.md",
                            "commit_sha": "b" * 40, "run_id": self.run_id, "review_path": self.review,
                            "files": {path: digest(data) for path, data in FakeGit.files.items()}}

        def load(repo, path):
            self.loaded.append(path)
            return deepcopy(self.source_map[path])

        for name, value in (("PipelineGit", FakeGit), ("control_guard", lambda *args: None),
                            ("build_pending_context", lambda *args, **kwargs: None),
                            ("load_source", load), ("verify_publication", lambda *args, **kwargs: deepcopy(self.publication))):
            patcher = patch("pdm.evidence." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.payloads = []

    @staticmethod
    def binding(source):
        return {key: source[key] for key in ("source_id", "source_version", "path", "commit_sha",
                                             "source_bytes_sha256", "receipt_path", "receipt_sha256")}

    def model(self, output=None, after=None):
        def invoke(role, payload):
            self.assertEqual(role, "answer")
            self.payloads.append(payload)
            if after is not None:
                after()
            return {"output": output or {"answer": "两份资料分别是文章观点与用户未选择状态。",
                                        "citations": [{key: ref[key] for key in
                                                       ("source_id", "source_version", "locator")}
                                                      for ref in self.refs],
                                        "limitations": ["人工样例，原始时间未知。"]},
                    "invocation_id": "fixture-multi-answer", "elapsed_seconds": 0.125,
                    "model": "fixture", "usage": {"input_tokens": 20}}
        return invoke

    def assert_code(self, expected, operation):
        with self.assertRaises(CaptureError) as caught:
            operation()
        self.assertEqual(caught.exception.code, expected)

    def test_selection_is_explicit_normalized_and_unprocessed_without_run(self):
        result = build_evidence_sources(self.repo, list(reversed(self.paths)))
        self.assertEqual([entry["source"]["path"] for entry in result["sources"]], self.paths)
        self.assertEqual(self.loaded, self.paths)
        self.assertEqual(result["scope"], "explicit_sources")
        self.assertEqual(result["knowledge_status"], "unprocessed_or_unverified")
        self.assertIsNone(result["publication"])
        self.assertTrue(all(entry["knowledge_status"] == "unprocessed_or_unverified" for entry in result["sources"]))
        self.assertEqual(result["sources"][1]["lines"][1], {"line": 2, "text": "时间未知。"})

    def test_invalid_selection_is_rejected_before_read_or_model(self):
        cases = [("invalid_source_selection", []), ("invalid_source_selection", self.paths[0]),
                 ("too_many_sources", self.paths * 5), ("duplicate_source", [self.paths[0]] * 2),
                 ("conflicting_source_versions", [self.paths[0], self.paths[0].replace("sha256-" + "a" * 64,
                                                                                     "sha256-" + "c" * 64)]),
                 ("path_rejected", [self.paths[0], "memory/records/secret.md"])]
        for code, paths in cases:
            with self.subTest(code=code):
                model = Mock()
                self.assert_code(code, lambda: ask_sources(self.repo, paths, "人工问题？", model_call=model))
                model.assert_not_called()
        self.assertEqual(self.loaded, [])

    def test_legacy_single_source_evidence_and_answer_bytes_are_preserved(self):
        del self.publication["additional_sources"]
        plain = evidence(self.repo, self.paths[0])
        self.assertEqual(build_evidence_sources(self.repo, [self.paths[0]]), plain)
        output = {"answer": "人工。", "citations": [], "limitations": ["资料有限。"]}
        old = answer(self.repo, self.paths[0], "人工？", model_call=self.model(output))
        new = ask_sources(self.repo, [self.paths[0]], "人工？", model_call=self.model(output))
        self.assertEqual(old, new)
        self.assertEqual(self.payloads[-1], self.payloads[-2])

    def test_verified_publication_includes_reviewed_bodies_once_and_original_coordinates(self):
        result = ask_sources(self.repo, self.paths, "这些资料如何回应同一问题？", self.run_id,
                             model_call=self.model())
        selected = self.payloads[0]["evidence"]
        self.assertEqual(result["knowledge_status"], "published")
        self.assertEqual(result["evidence_scope"], "explicit_sources_and_verified_publication")
        self.assertEqual(len(selected["publication"]["documents"]), 2)
        self.assertTrue(all(doc["body"] == "人工多来源整理正文。\n" for doc in selected["publication"]["documents"]))
        self.assertEqual(len(result["citations"]), 2)
        self.assertTrue(any("不是额外的独立事实" in text for text in result["limitations"]))
        self.assertEqual(set(result["evidence_publication"]["object_versions"]), set(self.publication["files"]))
        for key, value in (("evidence", selected), ("input_payload", self.payloads[0])):
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.assertEqual(result[key + "_bytes"], len(encoded))
            self.assertEqual(result[key + "_sha256"], digest(encoded))

    def test_selected_set_must_match_all_publication_bindings(self):
        self.assert_code("evidence_source_mismatch", lambda: build_evidence_sources(self.repo, [self.paths[0]], self.run_id))
        self.publication["additional_sources"].append(self.binding(fixture_source("c", "未选范围。")))
        self.assert_code("evidence_source_mismatch", lambda: build_evidence_sources(self.repo, self.paths, self.run_id))
        self.publication["additional_sources"].pop()
        for field in ("source_version", "commit_sha", "source_bytes_sha256", "receipt_sha256"):
            with self.subTest(field=field):
                original = self.publication["additional_sources"][0][field]
                self.publication["additional_sources"][0][field] = "wrong"
                self.assert_code("evidence_source_mismatch", lambda: build_evidence_sources(self.repo, self.paths, self.run_id))
                self.publication["additional_sources"][0][field] = original

    def test_unselected_wrong_version_and_out_of_range_citations_rejected(self):
        valid = {key: self.refs[0][key] for key in ("source_id", "source_version", "locator")}
        for changes in ({"source_id": "src-unselected"}, {"source_version": "sha256-wrong"},
                        {"locator": "L1-L3"}, {"locator": "L2-L1"}):
            with self.subTest(changes=changes):
                output = {"answer": "人工。", "citations": [{**valid, **changes}], "limitations": []}
                self.assert_code("invalid_citation", lambda: ask_sources(self.repo, self.paths, "人工？",
                                                                        model_call=self.model(output)))

    def test_citation_line_numbers_are_bounded_before_integer_conversion(self):
        reference = {key: self.sources[0][key] for key in ("source_id", "source_version")}
        for label, locator in (("long_start", "L" + "9" * 5000 + "-L1"),
                               ("long_end", "L1-L" + "9" * 5000),
                               ("ten_digits", "L1-L1000000000"),
                               ("bounded_out_of_range", "L1-L999999999")):
            with self.subTest(label=label):
                output = {"answer": "人工资料。[1]", "citations": [{**reference, "locator": locator}],
                          "limitations": []}
                self.assert_code("invalid_citation", lambda: ask_sources(
                    self.repo, self.paths, "人工？", model_call=self.model(output)))
        for source in self.sources:
            for locator in ("L1-L1", "L2-L2", "L1-L2"):
                with self.subTest(source=source["source_id"], boundary=locator):
                    output = {"answer": "人工资料。[1]", "citations": [{
                        **{key: source[key] for key in ("source_id", "source_version")}, "locator": locator}],
                        "limitations": []}
                    result = ask_sources(self.repo, self.paths, "人工？", model_call=self.model(output))
                    self.assertEqual(result["citations"], output["citations"])

    def test_link_only_keeps_url_and_status_without_invented_body_citation(self):
        link = fixture_source("b", "", status="link_only")
        self.source_map[self.paths[1]] = link
        selected = build_evidence_sources(self.repo, self.paths)
        entry = selected["sources"][1]
        self.assertEqual(entry["lines"], [])
        self.assertEqual(entry["source"]["metadata"]["original_url"], "https://example.invalid/b")
        output = {"answer": "第二份只有链接，正文未取得。", "citations": [], "limitations": ["正文未取得。"]}
        result = ask_sources(self.repo, self.paths, "链接说了什么？", model_call=self.model(output))
        self.assertEqual(result["evidence_sources"][1]["acquisition_status"], "link_only")
        self.assertTrue(any("仅链接资料未取得正文" in text for text in result["limitations"]))
        self.assert_code("invalid_citation", lambda: ask_sources(self.repo, self.paths, "人工？", model_call=self.model()))

    def test_publication_does_not_mark_unreferenced_text_or_link_only_as_processed(self):
        for linked in (False, True):
            with self.subTest(linked=linked):
                if linked:
                    self.source_map[self.paths[1]] = fixture_source("b", "", status="link_only")
                    self.publication["additional_sources"] = [self.binding(self.source_map[self.paths[1]])]
                for path in FakeGit.files:
                    metadata = {"schema_version": "0.2", "sources": [self.refs[0]],
                                "review_status": "passed", "review_ref": self.review,
                                "generated_by": {"run_id": self.run_id}}
                    FakeGit.files[path] = markdown(metadata, "只整理第一份人工观点。")
                    self.publication["files"][path] = digest(FakeGit.files[path])
                selected = build_evidence_sources(self.repo, self.paths, self.run_id)
                self.assertEqual(selected["knowledge_status"], "published")
                self.assertEqual(selected["sources"][0]["knowledge_status"], "published")
                self.assertEqual(selected["sources"][1]["knowledge_status"], "unprocessed_or_unverified")
                output = {"answer": "第一份是已审核整理，第二份未形成受审知识。",
                          "citations": [{key: self.refs[0][key] for key in
                                         ("source_id", "source_version", "locator")}],
                          "limitations": ["第二份仅作上下文。"]}
                result = ask_sources(self.repo, self.paths, "人工？", self.run_id, model_call=self.model(output))
                self.assertEqual(result["evidence_sources"][1]["knowledge_status"], "unprocessed_or_unverified")
                if linked:
                    self.assertEqual(result["evidence_sources"][1]["acquisition_status"], "link_only")

    def test_formal_link_only_body_reference_is_rejected(self):
        self.source_map[self.paths[1]] = fixture_source("b", "", status="link_only")
        self.publication["additional_sources"] = [self.binding(self.source_map[self.paths[1]])]
        self.assert_code("evidence_source_mismatch", lambda: build_evidence_sources(self.repo, self.paths, self.run_id))

    def test_formal_references_validate_selected_versions_actual_lines_and_quotes(self):
        path = next(iter(FakeGit.files))
        for changes in ({"source_id": "other"}, {"source_version": "wrong"},
                        {"locator": "L1-L3"}, {"quote": "原文没有此句"}):
            with self.subTest(changes=changes):
                metadata = {"schema_version": "0.2", "sources": [{**self.refs[0], **changes}],
                            "review_status": "passed", "review_ref": self.review,
                            "generated_by": {"run_id": self.run_id}}
                data = markdown(metadata, "人工正文。")
                FakeGit.files[path] = data
                self.publication["files"][path] = digest(data)
                self.assert_code("evidence_source_mismatch", lambda: build_evidence_sources(self.repo, self.paths, self.run_id))

    def test_combined_full_payload_budget_rejects_without_truncation_or_call(self):
        model = Mock()
        with patch("pdm.evidence.MAX_EVIDENCE_BYTES", 64):
            self.assert_code("evidence_limit", lambda: ask_sources(self.repo, self.paths, "人工？", model_call=model))
        selected = build_evidence_sources(self.repo, self.paths)
        with patch("pdm.evidence.MAX_EVIDENCE_BYTES", len(json.dumps(selected, ensure_ascii=False).encode()) + 10):
            self.assert_code("evidence_limit", lambda: ask_sources(self.repo, self.paths, "问" * 1000, model_call=model))
        model.assert_not_called()

    def test_dirty_or_changed_head_before_and_after_model_fail_closed(self):
        FakeGit.dirty = True
        model = Mock()
        self.assert_code("dirty_worktree", lambda: ask_sources(self.repo, self.paths, "人工？", model_call=model))
        model.assert_not_called()
        FakeGit.dirty = False
        self.assert_code("dirty_worktree", lambda: ask_sources(self.repo, self.paths, "人工？",
                         model_call=self.model(after=lambda: setattr(FakeGit, "dirty", True))))
        FakeGit.dirty = False
        self.assert_code("head_conflict", lambda: ask_sources(self.repo, self.paths, "人工？",
                         model_call=self.model(after=lambda: setattr(FakeGit, "head_value", "c" * 40))))

    def test_each_selected_source_and_formal_bytes_are_rechecked(self):
        original = self.source_map[self.paths[1]]["body"]
        self.assert_code("evidence_source_mismatch", lambda: ask_sources(self.repo, self.paths, "人工？",
            model_call=self.model(after=lambda: self.source_map[self.paths[1]].__setitem__("body", "被改动。"))))
        self.source_map[self.paths[1]]["body"] = original
        path = list(FakeGit.files)[-1]
        self.assert_code("publication_mismatch", lambda: ask_sources(self.repo, self.paths, "人工？", self.run_id,
            model_call=self.model(after=lambda: FakeGit.files.__setitem__(path, b"tampered"))))

    def test_revalidation_also_runs_when_model_fails(self):
        def fail(role, payload):
            FakeGit.head_value = "c" * 40
            raise RuntimeError("artificial transport failure")
        self.assert_code("head_conflict", lambda: ask_sources(self.repo, self.paths, "人工？", model_call=fail))

    def test_answer_requires_gap_without_citations(self):
        self.assert_code("unsupported_answer", lambda: ask_sources(self.repo, self.paths, "人工？",
            model_call=self.model({"answer": "无依据。", "citations": [], "limitations": []})))


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class RealGitMultiSourceEvidenceTests(unittest.TestCase):
    def test_published_multi_source_run_includes_only_bound_sources_and_readonly_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "-b", "main")
            configure_test_repository(repo)
            git(repo, "config", "user.name", "Isolated Test")
            git(repo, "config", "user.email", "fixture@example.invalid")
            git(repo, "commit", "--allow-empty", "-m", "artificial baseline")
            inputs = [("article", "文章：这是人工观点。\n"), ("conversation", "用户：我尚未决定。\n"),
                      ("attachment_link", ""), ("note", "UNSELECTED_ARTIFICIAL_SENTINEL\n")]
            saved = [capture(repo, {"source_key": f"fixture:published-evidence:{number}", "source_type": kind,
                                   "acquisition_status": "full_text" if body else "link_only", "body": body,
                                   "original_url": None if body else "https://example.invalid/attachment"})
                     for number, (kind, body) in enumerate(inputs)]
            paths = [item["path"] for item in saved[:3]]
            calls = []

            def model(role, payload):
                calls.append((role, payload))
                if role == "extract":
                    refs = [{"source_id": item["metadata"]["source_id"],
                             "source_version": item["metadata"]["source_version"], "locator": "L1-L1",
                             "quote": item["body"].splitlines()[0]} for item in payload["sources"] if item["body"]]
                    output = {"objects": [{"id": "knowledge-artificial-joint", "type": "knowledge",
                              "title": "人工联合资料", "source_kind": "model_inference", "sources": refs,
                              "domains": [], "valid_from": None, "valid_to": None,
                              "verification_status": "unverified", "lifecycle_status": "undetermined",
                              "body": "文章观点与用户尚未决定是不同表达。"}],
                              "wiki": {"id": "topic-artificial-joint", "title": "人工联合主题",
                                       "body": "人工主题受审正文。", "sources": refs}}
                elif role == "review":
                    output = {"verdict": "passed", "reasons": ["人工样例忠实。"]}
                else:
                    citations = [{"source_id": entry["source"]["source_id"],
                                  "source_version": entry["source"]["source_version"], "locator": "L1-L1"}
                                 for entry in payload["evidence"]["sources"] if entry["lines"]]
                    output = {"answer": "文章是观点，用户尚未决定；附件只有链接。",
                              "citations": citations, "limitations": ["附件正文未取得，时间未知。"]}
                return {"output": output, "invocation_id": "model-" + f"{len(calls):032x}",
                        "model": "artificial", "elapsed_seconds": 0.125, "usage": None}

            processed = process_sources(repo, paths, model_call=model)
            self.assertEqual(processed["status"], "passed")
            published = publish(repo, processed["run_id"])
            head = git(repo, "rev-parse", "HEAD")
            before = {str(path.relative_to(repo)): path.read_bytes() for path in (repo / "memory").rglob("*") if path.is_file()}
            result = ask_sources(repo, list(reversed(paths)), "这些资料如何回应同一问题？",
                                 processed["run_id"], model_call=model)
            selected = calls[-1][1]["evidence"]
            self.assertEqual(set(result["evidence_publication"]["files"]), set(published["files"]))
            self.assertEqual(len(selected["publication"]["documents"]), 2)
            self.assertIn("人工主题受审正文。", json.dumps(selected, ensure_ascii=False))
            self.assertNotIn("UNSELECTED_ARTIFICIAL_SENTINEL", json.dumps(calls, ensure_ascii=False))
            self.assertNotIn(saved[3]["source_id"], json.dumps(calls, ensure_ascii=False))
            link_entry = next(entry for entry in selected["sources"] if not entry["lines"])
            self.assertEqual(link_entry["knowledge_status"], "unprocessed_or_unverified")
            self.assertEqual(link_entry["source"]["metadata"]["original_url"], "https://example.invalid/attachment")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), head)
            self.assertEqual(git(repo, "status", "--porcelain"), "")
            self.assertEqual(before, {str(path.relative_to(repo)): path.read_bytes()
                                      for path in (repo / "memory").rglob("*") if path.is_file()})

            # A run identifier does not authorize reading additional sources:
            # scope must be rejected before the verifier loads any unselected
            # source body, including through the legacy one-source API.
            loaded = []
            def selected_only(repo, source_path, commit=None):
                loaded.append(source_path)
                if source_path == saved[1]["path"]:
                    self.fail("Verifier opened an unselected source body")
                return load_source(repo, source_path, commit)

            for operation in (
                    lambda: build_evidence_sources(repo, [paths[0], saved[3]["path"]], processed["run_id"]),
                    lambda: evidence(repo, paths[0], processed["run_id"])):
                with patch("pdm.pipeline.load_source", side_effect=selected_only):
                    with self.assertRaises(CaptureError) as caught:
                        operation()
                    self.assertEqual(caught.exception.code, "evidence_source_mismatch")
                self.assertNotIn(saved[1]["path"], loaded)

    def test_unprocessed_sources_are_read_only_and_unselected_source_never_enters_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            git(repo, "init", "-b", "main")
            configure_test_repository(repo)
            git(repo, "config", "user.name", "Isolated Test")
            git(repo, "config", "user.email", "fixture@example.invalid")
            git(repo, "commit", "--allow-empty", "-m", "artificial baseline")
            saved = [capture(repo, {"source_key": f"fixture:multi-evidence:{number}", "source_type": kind,
                                    "acquisition_status": "full_text", "body": body})
                     for number, kind, body in ((1, "article", "文章：人工观点。\n"),
                                               (2, "conversation", "用户：人工选择。\n"),
                                               (3, "note", "UNSELECTED_ARTIFICIAL_SENTINEL\n"))]
            paths = [item["path"] for item in saved[:2]]
            head = git(repo, "rev-parse", "HEAD")
            before = {str(path.relative_to(repo)): path.read_bytes() for path in (repo / "memory").rglob("*") if path.is_file()}
            payloads = []

            def model(role, payload):
                self.assertEqual(role, "answer")
                payloads.append(payload)
                citations = [{"source_id": entry["source"]["source_id"],
                              "source_version": entry["source"]["source_version"], "locator": "L1-L1"}
                             for entry in payload["evidence"]["sources"]]
                return {"output": {"answer": "人工观点与用户选择。", "citations": citations,
                                   "limitations": ["未审核。"]}}

            result = ask_sources(repo, paths, "这些资料如何回应同一问题？", model_call=model)
            serialized = json.dumps(payloads, ensure_ascii=False)
            self.assertNotIn("UNSELECTED_ARTIFICIAL_SENTINEL", serialized)
            self.assertNotIn(saved[2]["source_id"], serialized)
            self.assertEqual(len(result["evidence_sources"]), 2)
            self.assertEqual(git(repo, "rev-parse", "HEAD"), head)
            self.assertEqual(git(repo, "status", "--porcelain"), "")
            self.assertEqual(before, {str(path.relative_to(repo)): path.read_bytes()
                                      for path in (repo / "memory").rglob("*") if path.is_file()})


if __name__ == "__main__":
    unittest.main()
