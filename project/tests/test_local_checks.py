"""Local verification must reject incomplete evidence and private archives."""

import copy
import importlib.util
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile


_SCRIPT = Path(__file__).resolve().parents[2] / "project/scripts/check_local.py"
_SPEC = importlib.util.spec_from_file_location("pdm_local_checks", _SCRIPT)
local_checks = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = local_checks
_SPEC.loader.exec_module(local_checks)


class LocalChecksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-local-check-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def reports(self):
        partitions = {
            "decomposed": {"tests": 1, "test_ids_sha256": "1" * 64,
                           "modules": ["test_decomposed_pipeline"]},
            "other-a": {"tests": 2, "test_ids_sha256": "2" * 64,
                        "modules": ["test_alpha"]},
            "other-b": {"tests": 3, "test_ids_sha256": "3" * 64,
                        "modules": ["test_beta"]},
        }
        return [{
            "schema_version": 1,
            "routing": "whole-modules-heavy-plus-sorted-alternation-v1",
            "selected_shard": name,
            "total_tests": 6,
            "discovery_sha256": "a" * 64,
            "selected_tests": partition["tests"],
            "partitions": copy.deepcopy(partitions),
        } for name, partition in partitions.items()]

    def save_reports(self, reports):
        paths = []
        for index, report in enumerate(reports):
            path = self.root / f"shard-{index}.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            paths.append(path)
        return paths

    def archive(self, names):
        path = self.root / "source.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                archive.writestr(name, "artificial archive fixture\n")
        return path

    def test_python_minor_version_must_match_the_claimed_check(self):
        actual = f"{sys.version_info.major}.{sys.version_info.minor}"
        resolved = local_checks.resolve_python(sys.executable, actual)
        self.assertEqual(Path(resolved["path"]).resolve(), Path(sys.executable).resolve())
        self.assertTrue(resolved["version"].startswith(actual + "."))
        wrong = "3.12" if actual != "3.12" else "3.11"
        with self.assertRaises(ValueError):
            local_checks.resolve_python(sys.executable, wrong)

    def test_complete_matching_shards_report_the_total(self):
        self.assertEqual(local_checks.validate_manifests(self.save_reports(self.reports())), 6)

    def test_missing_duplicate_and_unrecognized_shards_cannot_pass(self):
        paths = self.save_reports(self.reports())
        for name, selected in (("missing", paths[:2]),
                               ("duplicate", [paths[0], paths[0], paths[2]])):
            with self.subTest(case=name), self.assertRaises(ValueError):
                local_checks.validate_manifests(selected)
        reports = self.reports()
        reports[1]["selected_shard"] = "unrecognized"
        with self.assertRaises(ValueError):
            local_checks.validate_manifests(self.save_reports(reports))

    def test_shards_from_different_discoveries_or_partitions_cannot_mix(self):
        for field in ("discovery", "partition"):
            with self.subTest(field=field):
                reports = self.reports()
                if field == "discovery":
                    reports[1]["discovery_sha256"] = "b" * 64
                else:
                    reports[1]["partitions"]["other-a"]["test_ids_sha256"] = "f" * 64
                with self.assertRaises(ValueError):
                    local_checks.validate_manifests(self.save_reports(reports))

    def test_selected_counts_and_total_must_agree(self):
        for field in ("selected", "total"):
            with self.subTest(field=field):
                reports = self.reports()
                if field == "selected":
                    reports[1]["selected_tests"] = 3
                else:
                    for report in reports:
                        report["total_tests"] = 7
                with self.assertRaises(ValueError):
                    local_checks.validate_manifests(self.save_reports(reports))

    def test_source_archive_rejects_every_private_root(self):
        required = ["project/src/pdm/cli.py", "pyproject.toml"]
        local_checks.check_archive(self.archive(required))
        for prefix in ("memory/", "fixtures/", "archive/", "project/validation/"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                local_checks.check_archive(self.archive(required + [prefix + "private.md"]))

    def test_source_archive_requires_both_code_entrypoints(self):
        for missing in ("project/src/pdm/cli.py", "pyproject.toml"):
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                local_checks.check_archive(self.archive([
                    name for name in ("project/src/pdm/cli.py", "pyproject.toml") if name != missing]))

    def test_interrupt_reaps_the_child_and_records_an_interrupted_step(self):
        checks = local_checks.Checks(self.root, {"steps": []}, timeout=30)
        process = Mock(pid=12345)
        process.returncode = None
        process.poll.side_effect = lambda: process.returncode

        def wait(*args, **kwargs):
            if process.wait.call_count == 1:
                raise KeyboardInterrupt()
            process.returncode = -signal.SIGKILL
            return process.returncode

        process.wait.side_effect = wait
        with patch.object(local_checks.subprocess, "Popen", return_value=process), \
                patch.object(local_checks.os, "killpg") as kill:
            with self.assertRaises(KeyboardInterrupt):
                checks.run("interrupted", [sys.executable, "artificial-command"], self.root)
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(process.wait.call_count, 2)
        saved = json.loads((self.root / "report.json").read_text())
        self.assertEqual(saved["steps"][0]["status"], "interrupted")
        self.assertEqual(saved["steps"][0]["exit_code"], 130)

    def test_stop_during_launch_cannot_leave_an_untracked_child(self):
        checks = local_checks.Checks(self.root, {"steps": []}, timeout=30)
        process = Mock(pid=12345)
        process.returncode = None
        process.poll.side_effect = lambda: process.returncode

        def wait(*args, **kwargs):
            process.returncode = -signal.SIGKILL
            return process.returncode

        process.wait.side_effect = wait

        def launch(*args, **kwargs):
            # A concurrent stop has observed no registered process yet.
            checks.stopped.set()
            return process

        with patch.object(local_checks.subprocess, "Popen", side_effect=launch), \
                patch.object(local_checks.os, "killpg") as kill:
            with self.assertRaises(RuntimeError):
                checks.run("stopped-during-launch", [sys.executable, "artificial-command"], self.root)
        kill.assert_called_once_with(process.pid, signal.SIGKILL)
        process.wait.assert_called()
        saved = json.loads((self.root / "report.json").read_text())
        self.assertNotEqual(saved["steps"][0]["status"], "running")


if __name__ == "__main__":
    unittest.main()
