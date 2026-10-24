"""Inline citation contracts using artificial data; no cloud or real memory."""

from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm import evidence as evidence_module


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class InlineCitationEntryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial baseline")
        self.sources = [capture(self.repo, {"source_key": f"fixture:inline:{number}", "source_type": "note",
                        "acquisition_status": "full_text", "body": f"人工资料{number}。\n时间未知。\n"})
                        for number in range(2)]
        self.paths = [source["path"] for source in self.sources]
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.before = self.snapshot()
        self.calls = 0

    def snapshot(self):
        return {str(path.relative_to(self.repo)): path.read_bytes()
                for path in (self.repo / "memory").rglob("*") if path.is_file()}

    def model(self, text, *, bad_locator=False, foreign=False, empty=False):
        def invoke(role, payload):
            self.calls += 1
            self.assertEqual(role, "answer")
            selected = payload["evidence"]
            source = selected["sources"][0]["source"] if "sources" in selected else selected["source"]
            citation = {"source_id": "src-foreign" if foreign else source["source_id"],
                        "source_version": source["source_version"],
                        "locator": "L99-L99" if bad_locator else "L1-L1"}
            return {"output": {"answer": text, "citations": [] if empty else [citation],
                               "limitations": ["仅人工样例。"]}, "invocation_id": "artificial-answer"}
        return invoke

    def invoke(self, entry, model):
        if entry == "single":
            return evidence_module.answer(self.repo, self.paths[0], "人工问题？", model_call=model)
        if entry == "multi":
            return evidence_module.ask_sources(self.repo, self.paths, "人工问题？", model_call=model)
        if entry == "delegated":
            return evidence_module.ask_sources(self.repo, self.paths[:1], "人工问题？", model_call=model)
        return evidence_module.ask_current(self.repo, self.paths, "人工问题？", model_call=model)

    def assert_readonly(self):
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(self.snapshot(), self.before)

    def test_current_rejects_structurally_valid_citation_with_inline_99(self):
        with self.assertRaises(CaptureError) as caught:
            self.invoke("current", self.model("人工资料说明。[99]"))
        self.assertEqual(caught.exception.code, "invalid_inline_citation")
        self.assertEqual(self.calls, 1)
        self.assert_readonly()

    def test_all_entrypoints_reject_wrong_or_empty_array_indices(self):
        for entry in ("single", "multi", "delegated", "current"):
            for text, empty in (("人工资料。[1,99]", False), ("没有来源却标注。[1]", True)):
                with self.subTest(entry=entry, text=text), self.assertRaises(CaptureError) as caught:
                    self.invoke(entry, self.model(text, empty=empty))
                self.assertEqual(caught.exception.code, "invalid_inline_citation")
                self.assert_readonly()
        self.assertEqual(self.calls, 8)

    def test_valid_markers_and_legacy_answers_remain_compatible(self):
        for entry in ("single", "multi", "delegated", "current"):
            for text, empty in (("人工资料。[1]", False), ("人工资料（旧式无编号）。", False),
                                ("资料不足，不能确认。", True)):
                with self.subTest(entry=entry, text=text):
                    result = self.invoke(entry, self.model(text, empty=empty))
                    self.assertEqual(result["answer"], text)
                    self.assert_readonly()
        self.assertEqual(self.calls, 12)

    def test_valid_inline_index_does_not_replace_source_or_line_checks(self):
        for entry in ("single", "multi", "current"):
            for changes in ({"bad_locator": True}, {"foreign": True}):
                with self.subTest(entry=entry, changes=changes), self.assertRaises(CaptureError) as caught:
                    self.invoke(entry, self.model("人工资料。[1]", **changes))
                self.assertEqual(caught.exception.code, "invalid_citation")
                self.assert_readonly()


class InlineCitationSyntaxTests(unittest.TestCase):
    def output(self, answer, count=3):
        return {"answer": answer, "citations": [{"source_id": f"artificial-{number}",
                "source_version": "artificial-version", "locator": "L1-L1"} for number in range(count)],
                "limitations": ["人工范围。"]}

    def assert_invalid(self, text, count=3):
        with self.assertRaises(CaptureError) as caught:
            evidence_module._validate_answer(self.output(text, count))
        self.assertEqual(caught.exception.code, "invalid_inline_citation")

    def test_individual_adjacent_group_and_range_markers(self):
        for text in ("结论[1]。", "结论[1][2][3]。", "结论[1, 2;3]。", "结论[1，2、3；1]。",
                     "结论[1-3]。", "结论[1-2, 3]。", "结论[ 1 - 3 ]。", "结论[3-3]。"):
            with self.subTest(text=text):
                output = self.output(text)
                before = deepcopy(output)
                evidence_module._validate_answer(output)
                self.assertEqual(output, before)

    def test_unicode_equivalent_markers_use_the_same_bounds(self):
        for text in ("结论［１，２］。", "结论【١–٣】。", "结论[¹,²]。"):
            with self.subTest(text=text):
                evidence_module._validate_answer(self.output(text))
        for text in ("结论［９９］。", "结论【٠】。", "结论[١،٩٩]。", "结论[１—９９]。"):
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_invalid_number_and_group_forms_cannot_silently_skip_validation(self):
        for marker in ("[0]", "[4]", "[99]", "[-1]", "[+1]", "[01]", "[1.0]", "[^99]", "[1e2]",
                       "[1,99]", "[1-99]", "[3-1]", "[1,]", "[1,,2]", "[1/2]", "[1 2]",
                       "[1:2]", "[1†source]", "[--1]", "[1", "[1\n]", "[1[99]]"):
            with self.subTest(marker=marker):
                self.assert_invalid("结论" + marker)
        self.assert_invalid("结论[1]", count=0)
        self.assert_invalid("结论[" + "9" * 5000 + "]")

    def test_closed_markdown_literals_and_links_are_not_citation_markers(self):
        for text in ("示例 `[99]`。", "示例 ``含`和[99]``。", "```text\n[99]\n```\n普通说明。",
                     "~~~\n[99]\n~~~~\n普通说明。", "[99](https://example.invalid/path)",
                     "[文档](https://example.invalid/[99])", "![99](https://example.invalid/img)",
                     "[文档](https://example.invalid/a(b))", r"字面方括号 \[99\]。",
                     "2026年，99次，第1项。[说明] [L1-L2]"):
            with self.subTest(text=text):
                evidence_module._validate_answer(self.output(text, count=0))

    def test_unclosed_literals_quotes_and_partial_links_do_not_hide_markers(self):
        for text in ("`未闭合[99]", "```text\n[99]", "~~~\n[99]", "``不匹配[99]`",
                     "[99](https://example.invalid", "> 原话中的[99]", "“原话[99]”",
                     "[99][undefined]", r"双反斜线 \\[99]", "```\n合法代码\n```\n正文[99]",
                     "```lang`invalid\n正文[99]\n```"):
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_unicode_normalization_cannot_create_markdown_exemptions(self):
        for text in ("｀[99]｀", "［99］(https://example.invalid)", "[99]（时间未知）",
                     "｀｀｀\n[99]\n｀｀｀", "＼[99＼]"):
            with self.subTest(text=text):
                self.assert_invalid(text)

    def test_legacy_without_markers_and_refusal_with_limitations(self):
        evidence_module._validate_answer(self.output("旧回答不带正文编号。"))
        evidence_module._validate_answer(self.output("资料不足，拒绝补造。", count=0))

    def test_shape_validation_is_preserved(self):
        for output in (None, {"answer": "文本"}, {**self.output("文本"), "extra": True},
                       {**self.output("文本"), "citations": [{"source_id": "人工"}]}):
            with self.subTest(output=output), self.assertRaises(CaptureError) as caught:
                evidence_module._validate_answer(output)
            self.assertEqual(caught.exception.code, "invalid_answer")

