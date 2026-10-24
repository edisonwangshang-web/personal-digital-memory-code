"""Schema write gates use only isolated fixtures, never the real memory store."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.compatibility import (
    MAX_HEADER_BYTES,
    ensure_memory_compatible,
    inspect_memory_schema,
)
from pdm.contracts import CaptureError, markdown
from pdm.doctor import inspect_environment


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
    ).stdout.decode().strip()


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Isolated Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture baseline")
        self.payload = {
            "source_key": "compatibility-fixture", "source_type": "conversation",
            "platform": "isolated-fixture", "original_url": None,
            "acquisition_status": "full_text", "body": "用户：仅用于隔离测试。\n",
            "original_time": None,
        }

    def save(self, relative, content):
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def commit_fixtures(self):
        git(self.repo, "add", "--all")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-m", "isolated memory fixture")

    def snapshot(self):
        return {
            "head": git(self.repo, "rev-parse", "HEAD"),
            "tree": git(self.repo, "rev-parse", "HEAD^{tree}"),
            "files": {
                str(path.relative_to(self.repo)): path.read_bytes()
                for path in self.repo.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(self.repo).parts
                and not path.is_symlink()
            },
        }

    def assert_error(self, code, operation):
        with self.assertRaises(CaptureError) as context:
            operation()
        self.assertEqual(context.exception.code, code)
        return context.exception

    def assert_result(self, status, versions, *, scanned=None, unknown=None):
        result = inspect_memory_schema(self.repo)
        self.assertEqual(result["status"], status)
        self.assertEqual(result["writable"], status == "compatible")
        self.assertEqual(result["supported_schema_versions"], ["0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7", "0.8", "0.9", "0.10"])
        self.assertEqual(result["detected_known_versions"], versions)
        for key in ("scanned_metadata_files", "unknown_files", "ignored_files"):
            self.assertIsInstance(result[key], int)
        if scanned is not None:
            self.assertEqual(result["scanned_metadata_files"], scanned)
        if unknown is not None:
            self.assertEqual(result["unknown_files"], unknown)
        return result

    def test_missing_or_empty_memory_is_compatible(self):
        self.assert_result("compatible", [], scanned=0, unknown=0)
        (self.repo / "memory").mkdir()
        self.assert_result("compatible", [], scanned=0, unknown=0)

    def test_initial_explanations_and_personal_templates_are_out_of_scope(self):
        for relative in (
            "memory/README.md", "memory/sources/conversations/README.md",
            "memory/sources/inbox/README.md", "memory/topics/README.md",
            "memory/个人概况.md", "memory/domains/健康生活.md", "memory/领域模板.md",
            "memory/staging/.gitkeep",
        ):
            self.save(relative, b"Plain template text, not schema metadata.\n")
        self.assert_result("compatible", [], scanned=0, unknown=0)

    def test_foundation_sources_and_receipts_remain_writable(self):
        first = capture(self.repo, self.payload)
        self.assert_result("compatible", ["0.1"], scanned=2, unknown=0)
        second = capture(self.repo, {**self.payload, "source_key": "second-fixture"})
        self.assertTrue(second["local_saved"])
        self.assertNotEqual(first["path"], second["path"])
        self.assert_result("compatible", ["0.1"], scanned=4, unknown=0)
        environment = inspect_environment(self.repo)
        self.assertTrue(environment["memory_writable"])
        self.assertTrue(environment["local_ready"])

    def test_runtime_schema_is_rejected_in_every_managed_area(self):
        for relative in (
            "memory/sources/conversations/raw.md", "memory/sources/inbox/raw.md",
            "memory/records/record.md", "memory/topics/topic.md",
            "memory/staging/candidate.md", "memory/runs/run.md", "memory/pending.md",
        ):
            with self.subTest(relative=relative):
                path = self.save(relative, markdown({"schema_version": "pdm-1"}, "isolated text"))
                self.assert_result("incompatible", ["pdm-1"], scanned=1, unknown=0)
                self.assert_error("incompatible_memory_schema", lambda: ensure_memory_compatible(self.repo))
                path.unlink()

    def test_mixed_known_schemas_reject_the_entire_write(self):
        self.save("memory/sources/conversations/old.md", markdown({"schema_version": "0.1"}))
        self.save("memory/records/new.md", markdown({"schema_version": "pdm-1"}))
        self.assert_result("incompatible", ["0.1", "pdm-1"], scanned=2, unknown=0)
        self.assert_error("incompatible_memory_schema", lambda: ensure_memory_compatible(self.repo))

    def test_pipeline_metadata_and_original_capture_can_coexist(self):
        self.save("memory/staging/run-fixture/candidate.md", markdown({"schema_version": "0.2"}, "fixture"))
        self.commit_fixtures()
        result = capture(self.repo, self.payload)
        self.assertTrue(result["local_saved"])
        self.assert_result("compatible", ["0.1", "0.2"], scanned=3, unknown=0)
        # The original-source format and identity have not changed.
        from pdm.contracts import parse_markdown
        metadata, body = parse_markdown((self.repo / result["path"]).read_bytes())
        self.assertEqual(metadata["schema_version"], "0.1")
        self.assertEqual(body, self.payload["body"])

    def test_runs_json_alone_is_unknown_and_never_opened(self):
        path = self.save("memory/runs/status.json", b'{"schema_version":"0.1","secret":"fixture"}')
        original_open = os.open

        def reject_json_open(candidate, *args, **kwargs):
            if Path(candidate).suffix.lower() == ".json":
                self.fail("Schema detection must not open a JSON body")
            return original_open(candidate, *args, **kwargs)

        with patch("pdm.compatibility.os.open", side_effect=reject_json_open):
            self.assert_result("unknown", [], scanned=1, unknown=1)
            self.assert_error("unknown_memory_schema", lambda: ensure_memory_compatible(self.repo))

    def test_multisource_schema_coexists_without_rewriting_original_sources(self):
        first = capture(self.repo, self.payload)
        original = (self.repo / first["path"]).read_bytes()
        self.save("memory/staging/run-multisource.md", markdown({"schema_version": "0.3"}, "isolated run"))
        self.commit_fixtures()
        self.assert_result("compatible", ["0.1", "0.3"], scanned=3, unknown=0)
        second = capture(self.repo, {**self.payload, "source_key": "multisource-second"})
        self.assertTrue(second["local_saved"])
        self.assertEqual((self.repo / first["path"]).read_bytes(), original)

    def test_program_support_not_package_declaration_controls_new_schema_writes(self):
        self.save("memory/staging/run-multisource.md", markdown({"schema_version": "0.3"}, "isolated run"))
        self.commit_fixtures()
        before = self.snapshot()
        with patch("pdm.compatibility.SUPPORTED_SCHEMA_VERSIONS", ("0.1", "0.2")):
            self.assertFalse(inspect_environment(self.repo)["local_ready"])
            self.assert_error("incompatible_memory_schema", lambda: capture(self.repo, self.payload))
        self.assertEqual(self.snapshot(), before)

    def test_missing_invalid_or_private_schema_values_fail_closed(self):
        for metadata in ({}, {"schema_version": None}, {"schema_version": 1},
                         {"schema_version": "PRIVATE_FIXTURE_SCHEMA_VALUE"}):
            with self.subTest(kind=type(metadata.get("schema_version")).__name__):
                path = self.save("memory/records/fixture.md", markdown(metadata))
                result = self.assert_result("unknown", [], scanned=1, unknown=1)
                error = self.assert_error("unknown_memory_schema", lambda: ensure_memory_compatible(self.repo))
                self.assertNotIn("PRIVATE_FIXTURE_SCHEMA_VALUE", json.dumps(result))
                self.assertNotIn("PRIVATE_FIXTURE_SCHEMA_VALUE", str(error))
                path.unlink()

    def test_malformed_headers_fail_closed_without_echoing_content(self):
        for content in (
            b"PRIVATE_FIXTURE_BODY\n", b"---\n{PRIVATE_FIXTURE_BODY}\n---\n",
            b"---\n[]\n---\n", b'---\n{"schema_version":"0.1"}\n',
            b'---\n{"schema_version":"0.1", "private":"\xff"}\n---\n',
            b'---\n{"schema_version":"pdm-1","schema_version":"0.1"}\n---\n',
        ):
            with self.subTest(content_length=len(content)):
                path = self.save("memory/sources/inbox/fixture.md", content)
                result = inspect_memory_schema(self.repo)
                self.assertEqual(result["status"], "unknown")
                self.assertFalse(result["writable"])
                error = self.assert_error("unknown_memory_schema", lambda: ensure_memory_compatible(self.repo))
                self.assertNotIn("PRIVATE_FIXTURE_BODY", json.dumps(result))
                self.assertNotIn("PRIVATE_FIXTURE_BODY", str(error))
                path.unlink()

    def test_oversized_header_fails_closed(self):
        content = b'---\n{"schema_version":"0.1","padding":"' + b"x" * MAX_HEADER_BYTES + b'"}\n---\n'
        self.save("memory/sources/inbox/large.md", content)
        self.assert_result("unknown", [], scanned=1, unknown=1)
        self.assert_error("unknown_memory_schema", lambda: ensure_memory_compatible(self.repo))

    def test_unreadable_metadata_fails_closed_without_echoing_io_error(self):
        path = self.save("memory/sources/inbox/fixture.md", markdown({"schema_version": "0.1"}))
        original_open = os.open

        def deny_fixture(candidate, *args, **kwargs):
            if Path(candidate).name == path.name:
                raise PermissionError("PRIVATE_FIXTURE_IO_ERROR")
            return original_open(candidate, *args, **kwargs)

        with patch("pdm.compatibility.os.open", side_effect=deny_fixture):
            result = inspect_memory_schema(self.repo)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["writable"])
            error = self.assert_error("unknown_memory_schema", lambda: ensure_memory_compatible(self.repo))
        self.assertNotIn("PRIVATE_FIXTURE_IO_ERROR", json.dumps(result))
        self.assertNotIn("PRIVATE_FIXTURE_IO_ERROR", str(error))

    def test_supported_header_does_not_require_a_decodable_markdown_body(self):
        self.save("memory/records/fixture.md", markdown({"schema_version": "0.1"}) + b"\xff\xfe" * 100_000)
        self.assert_result("compatible", ["0.1"], scanned=1, unknown=0)

    def test_scanner_reads_only_unbuffered_header_lines_and_no_body_bytes(self):
        header = markdown({"schema_version": "0.1", "fixture": "multiline metadata"})
        self.save("memory/sources/inbox/fixture.md", header + b"PRIVATE_FIXTURE_BODY" * 100_000)
        original_fdopen = os.fdopen
        opened = []
        testcase = self

        class HeaderOnlyReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def fileno(self):
                return self.stream.fileno()

            def readline(self, size=-1):
                testcase.assertGreater(size, 0, "Every metadata read must have a byte bound")
                testcase.assertLess(self.stream.tell(), len(header), "Must stop at the closing delimiter")
                value = self.stream.readline(size)
                testcase.assertLessEqual(self.stream.tell(), len(header), "Must not consume any Markdown body bytes")
                return value

            def read(self, *args):
                testcase.fail("Schema detection must not read the file body")

            def readlines(self, *args):
                testcase.fail("Schema detection must not read the file body")

        def header_only_stream(descriptor, *args, **kwargs):
            self.assertEqual(kwargs.get("buffering"), 0, "Buffering could prefetch private body bytes")
            stream = original_fdopen(descriptor, *args, **kwargs)
            opened.append(stream)
            return HeaderOnlyReader(stream)

        with patch("pdm.compatibility.os.fdopen", side_effect=header_only_stream):
            self.assert_result("compatible", ["0.1"], scanned=1, unknown=0)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)

    def test_directory_read_failure_blocks_writes(self):
        self.save("memory/sources/inbox/fixture.md", markdown({"schema_version": "0.1"}))
        with patch("pdm.compatibility.os.scandir", side_effect=PermissionError("PRIVATE_DIRECTORY_ERROR")):
            result = inspect_memory_schema(self.repo)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["writable"])
            self.assert_error("unknown_memory_schema", lambda: ensure_memory_compatible(self.repo))
        self.assertNotIn("PRIVATE_DIRECTORY_ERROR", json.dumps(result))

    def test_entry_budget_exhaustion_blocks_writes(self):
        for number in range(3):
            self.save(f"memory/records/fixture-{number}.md", markdown({"schema_version": "0.1"}))
        with patch("pdm.compatibility.MAX_SCAN_ENTRIES", 2):
            result = inspect_memory_schema(self.repo)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["writable"])
            self.assert_error("unknown_memory_schema", lambda: ensure_memory_compatible(self.repo))

    def test_depth_budget_exhaustion_blocks_writes(self):
        self.save("memory/records/one/two/three/fixture.md", markdown({"schema_version": "0.1"}))
        with patch("pdm.compatibility.MAX_SCAN_DEPTH", 1):
            result = inspect_memory_schema(self.repo)
            self.assertEqual(result["status"], "unknown")
            self.assertFalse(result["writable"])
            self.assert_error("unknown_memory_schema", lambda: ensure_memory_compatible(self.repo))

    def test_known_incompatibility_is_not_hidden_by_unknown_files(self):
        self.save("memory/sources/inbox/unknown.md", markdown({"schema_version": "unrecognized"}))
        self.save("memory/records/runtime.md", markdown({"schema_version": "pdm-1"}))
        self.assert_result("incompatible", ["pdm-1"], scanned=2, unknown=1)
        self.assert_error("incompatible_memory_schema", lambda: ensure_memory_compatible(self.repo))

    def test_unknown_extension_under_managed_scope_is_not_assumed_compatible(self):
        self.save("memory/staging/fixture.dat", b"private fixture")
        result = inspect_memory_schema(self.repo)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["writable"])

    def test_metadata_symlink_is_rejected_without_opening_the_target(self):
        outside = self.root / "outside.md"
        outside.write_bytes(markdown({"schema_version": "0.1"}, "PRIVATE_OUTSIDE_FIXTURE"))
        target = self.repo / "memory/records/symlink.md"
        target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        original_open = os.open

        def reject_link_open(candidate, *args, **kwargs):
            if Path(candidate).name in {target.name, outside.name}:
                self.fail("Schema detection must not open a symlink or its target")
            return original_open(candidate, *args, **kwargs)

        with patch("pdm.compatibility.os.open", side_effect=reject_link_open):
            result = inspect_memory_schema(self.repo)
            self.assertFalse(result["writable"])
            self.assertTrue(result["path_rejected"])
            self.assert_error("path_rejected", lambda: ensure_memory_compatible(self.repo))

    def test_managed_directory_symlink_is_rejected_without_traversal(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "fixture.md").write_bytes(markdown({"schema_version": "0.1"}))
        target = self.repo / "memory/sources"
        target.parent.mkdir()
        target.symlink_to(outside, target_is_directory=True)
        result = inspect_memory_schema(self.repo)
        self.assertFalse(result["writable"])
        self.assertTrue(result["path_rejected"])
        self.assert_error("path_rejected", lambda: ensure_memory_compatible(self.repo))
        self.assertEqual(list(outside.iterdir()), [outside / "fixture.md"])

    def test_schema_rejected_capture_preserves_head_tree_and_files(self):
        for schema, code in (("pdm-1", "incompatible_memory_schema"), ("PRIVATE_SCHEMA", "unknown_memory_schema")):
            with self.subTest(code=code):
                self.save("memory/records/fixture.md", markdown({"schema_version": schema}, "fixture body"))
                self.commit_fixtures()
                before = self.snapshot()
                self.assert_error(code, lambda: capture(self.repo, self.payload, before["head"]))
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")
                self.assertFalse((self.repo / ".git/pdm-capture.lock").exists())

    def test_doctor_is_read_only_and_local_ready_requires_writable_memory(self):
        for schema, status in (("0.1", "compatible"), ("pdm-1", "incompatible"), ("PRIVATE_SCHEMA", "unknown")):
            with self.subTest(status=status):
                self.save("memory/records/fixture.md", markdown({"schema_version": schema}, "fixture body"))
                self.commit_fixtures()
                before = self.snapshot()
                result = inspect_environment(self.repo)
                self.assertEqual(result["memory_schema"]["status"], status)
                self.assertEqual(result["memory_writable"], status == "compatible")
                self.assertEqual(result["local_ready"], status == "compatible")
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(git(self.repo, "status", "--porcelain"), "")
                for key in ("remote_read_write", "independent_model_call", "scheduled_environment"):
                    self.assertEqual(result[key], "unverified")


if __name__ == "__main__":
    unittest.main()
