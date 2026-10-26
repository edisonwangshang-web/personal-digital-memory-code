"""Artificial Git data only; no receiver or external model is invoked."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError, canonical
from pdm.portable import prepare_reading_package, verify_reading_package


class ReadingPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pdm-portable-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        configure_test_repository(self.repo)
        self.git("config", "user.name", "Artificial Reading Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("commit", "--allow-empty", "-m", "artificial baseline")
        self.body = "用户：我在比较方法，还没有决定。\n原始时间未知。\n"
        self.source = self.save("artificial:reading", self.body)
        self.output = self.root / "reading.json"
        self.questions = ["这份资料表达了什么？", "用户已经作出决定了吗？"]

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "-C", str(self.repo), *args],
            check=True, capture_output=True, text=True, timeout=30,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        ).stdout.strip()

    def save(self, key, body):
        return capture(self.repo, {"source_key": key, "source_type": "note", "platform": "fixture",
                                  "original_url": None, "acquisition_status": "full_text",
                                  "original_time": None, "body": body})

    def prepare(self, **options):
        return prepare_reading_package(self.repo, [self.source["path"]], self.questions,
                                       output=options.pop("output", self.output), **options)

    def test_fixed_package_has_evidence_and_questions_without_model_answers(self):
        head = self.git("rev-parse", "HEAD")
        result = self.prepare()
        value = self.output.read_bytes()
        payload = json.loads(value)["payload"]
        verified = verify_reading_package(self.output, expected_sha256=result["sha256"])
        self.assertEqual(verified["status"], "integrity_verified")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(verified["second_model_reading"], "unverified")
        self.assertEqual(payload["questions"], self.questions)
        self.assertEqual(payload["scope"]["source_paths"], [self.source["path"]])
        self.assertEqual(payload["data_head"], head)
        self.assertIn(self.body, json.dumps(payload["evidence"], ensure_ascii=False).replace("\\n", "\n"))
        self.assertNotIn("answers", payload)
        self.assertNotIn("expected_answers", payload)
        self.assertEqual(hashlib.sha256(value).hexdigest(), result["sha256"])
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_unselected_source_body_is_not_exported(self):
        other = self.save("artificial:unselected", "人工未选资料，不应导出。\n")
        self.prepare()
        value = self.output.read_text()
        self.assertNotIn("人工未选资料", value)
        self.assertNotIn(other["source_id"], value)

    def test_frozen_answer_rules_verify_without_loading_current_prompt(self):
        # A historical package keeps its own rules bytes, not the current
        # prompt file. The synthetic label is not an actual old model result.
        historical = "# 人工历史规则 v7\n仅按包中限定证据作答。\n"
        with patch("pdm.portable.Path.read_text", return_value=historical):
            prepared = self.prepare()
        original = self.output.read_bytes()
        payload = json.loads(original)["payload"]
        self.assertEqual(payload["answer_rules"], historical)
        self.assertEqual(payload["answer_rules_sha256"],
                         hashlib.sha256(historical.encode()).hexdigest())
        with patch("pdm.portable.Path.read_text",
                   side_effect=AssertionError("verification must not reload current rules")):
            verified = verify_reading_package(
                self.output, expected_sha256=prepared["sha256"])
        self.assertEqual(verified["status"], "integrity_verified")
        self.assertEqual(verified["second_model_reading"], "unverified")
        self.assertEqual(self.output.read_bytes(), original)

        # Rehashing only the envelope cannot silently replace the frozen rules.
        package = json.loads(original)
        package["payload"]["answer_rules"] = "# 人工替换规则 v8\n"
        package["payload_sha256"] = hashlib.sha256(canonical(package["payload"])).hexdigest()
        self.output.write_bytes(canonical(package) + b"\n")
        with self.assertRaises(CaptureError):
            verify_reading_package(self.output)

    def test_questions_reject_answer_objects_duplicates_and_empty_input(self):
        for questions in ([], [""], ["same", "same"], [{"question": "Q", "expected": "A"}], ["x" * 4097]):
            with self.subTest(questions=type(questions[0]).__name__ if questions else "empty"):
                with self.assertRaises(CaptureError):
                    prepare_reading_package(self.repo, [self.source["path"]], questions, output=self.output)
                self.assertFalse(self.output.exists())

    def test_export_cannot_dirty_data_repository(self):
        with self.assertRaisesRegex(CaptureError, "限定读取包"):
            self.prepare(output=self.repo / "export.json")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_existing_output_is_never_overwritten(self):
        self.output.write_bytes(b"existing fixture")
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual(self.output.read_bytes(), b"existing fixture")

    def test_wrong_pin_and_modified_questions_are_rejected(self):
        self.prepare()
        with self.assertRaises(CaptureError) as caught:
            verify_reading_package(self.output, expected_sha256="0" * 64)
        self.assertEqual(caught.exception.code, "reading_package_mismatch")
        value = json.loads(self.output.read_bytes())
        value["payload"]["questions"][0] = "changed fixture question"
        self.output.write_text(json.dumps(value))
        with self.assertRaises(CaptureError):
            verify_reading_package(self.output)

    def test_duplicate_fields_and_symlink_input_are_rejected(self):
        self.output.write_text('{"payload":{},"payload":{},"payload_sha256":"x"}')
        with self.assertRaises(CaptureError):
            verify_reading_package(self.output)
        link = self.root / "link.json"
        link.symlink_to(self.output)
        with self.assertRaises(OSError):
            verify_reading_package(link)

    def test_changed_head_during_read_prevents_export(self):
        from pdm.evidence import build_current_evidence

        def changed(*args, **kwargs):
            result = build_current_evidence(*args, **kwargs)
            self.git("commit", "--allow-empty", "-m", "artificial concurrent update")
            return result

        with patch("pdm.evidence.build_current_evidence", side_effect=changed), self.assertRaises(CaptureError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "head_conflict")
        self.assertFalse(self.output.exists())

    def test_package_budget_is_enforced_without_truncation(self):
        with patch("pdm.portable.MAX_PACKAGE_BYTES", 100), self.assertRaises(CaptureError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.code, "reading_package_limit")
        self.assertFalse(self.output.exists())

    def test_internally_rehashed_scope_mismatch_and_malformed_documents_are_rejected(self):
        self.prepare()
        original = self.output.read_bytes()
        for change in (lambda payload: payload["evidence"].update(head="0" * 40),
                       lambda payload: payload["evidence"]["coverage"].update(requested_source_paths=[]),
                       lambda payload: payload["evidence"].update(publications=[None])):
            with self.subTest(change=change):
                package = json.loads(original)
                change(package["payload"])
                package["payload_sha256"] = hashlib.sha256(canonical(package["payload"])).hexdigest()
                self.output.write_bytes(canonical(package) + b"\n")
                with self.assertRaises(CaptureError):
                    verify_reading_package(self.output)

    def test_symlink_parent_cannot_redirect_private_package(self):
        destination = self.root / "destination"
        destination.mkdir()
        link = self.root / "redirect"
        link.symlink_to(destination, target_is_directory=True)
        with self.assertRaises(OSError):
            self.prepare(output=link / "reading.json")
        self.assertFalse((destination / "reading.json").exists())


if __name__ == "__main__":
    unittest.main()
