"""Separate engineering and knowledge identities using disposable Git checkouts."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
import test_pipeline
from pdm import engine_provenance
from pdm.contracts import CaptureError
from pdm.pipeline import (_hash, _load, execution_versions, process_sources, publish,
                          verify_publication)
from pdm.transaction import PipelineGit


class EngineProvenanceTests(unittest.TestCase):
    model = test_pipeline.PipelineTests.model

    def setUp(self):
        test_pipeline.PipelineTests.setUp(self)
        code_temp = tempfile.TemporaryDirectory(prefix="pdm-artificial-code-")
        self.addCleanup(code_temp.cleanup)
        self.code = Path(code_temp.name)
        test_pipeline.git(self.code, "init", "-b", "dev")
        configure_test_repository(self.code)
        test_pipeline.git(self.code, "config", "user.name", "Artificial Code Fixture")
        test_pipeline.git(self.code, "config", "user.email", "code@example.invalid")
        module = self.code / "project/src/pdm/engine_provenance.py"
        module.parent.mkdir(parents=True)
        module.write_bytes(Path(engine_provenance.__file__).read_bytes())
        test_pipeline.git(self.code, "add", ".")
        test_pipeline.git(self.code, "commit", "-m", "artificial engineering snapshot")
        self.program_commit = test_pipeline.git(self.code, "rev-parse", "HEAD")
        (self.code / "README.md").write_text("Artificial documentation-only change.\n")
        test_pipeline.git(self.code, "add", "README.md")
        test_pipeline.git(self.code, "commit", "-m", "artificial documentation change")
        self.code_commit = test_pipeline.git(self.code, "rev-parse", "HEAD")
        loaded = patch.object(engine_provenance, "__file__", str(module))
        loaded.start()
        self.addCleanup(loaded.stop)

    def process(self):
        result = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=self.model)
        self.assertEqual(result["status"], "passed", result)
        return result

    def publication(self):
        result = self.process()
        publish(self.repo, result["run_id"])
        verify_publication(self.repo, result["run_id"], historical=True)
        return result, self.repo / f"memory/runs/processes/{result['run_id']}/engine.md"

    def assert_changed(self, run_id):
        with self.assertRaises(CaptureError) as raised:
            verify_publication(self.repo, run_id, historical=True)
        self.assertEqual(raised.exception.code, "engine_provenance_changed")

    def test_loaded_core_commit_ignores_data_head_and_fake_data_engine(self):
        data_git = PipelineGit(self.repo)
        original = execution_versions(data_git)
        self.assertEqual(original["program_commit"], self.program_commit)
        self.assertNotEqual(self.code_commit, self.program_commit)
        self.assertNotEqual(data_git.head(), self.program_commit)
        fake = self.repo / "project/src/pdm/fixture.py"
        fake.parent.mkdir(parents=True)
        fake.write_text("# This data-repository file is never imported.\n")
        test_pipeline.git(self.repo, "add", "project")
        test_pipeline.git(self.repo, "commit", "-m", "artificial misleading data engineering path")
        self.assertEqual(execution_versions(data_git), original)
        test_pipeline.git(self.repo, "commit", "--allow-empty", "-m", "artificial data-only advancement")
        self.assertEqual(execution_versions(data_git), original)

    def test_data_only_publication_binds_same_commit_reservation_and_lists_authority(self):
        data_git = PipelineGit(self.repo)
        data_base = data_git.head()
        result = self.process()
        run_id = result["run_id"]
        sidecar_path = f"memory/runs/processes/{run_id}/engine.md"
        started_path = f"memory/runs/processes/{run_id}/started.md"
        sidecar = engine_provenance.read_reservation(data_git, run_id)
        self.assertFalse((self.repo / "project").exists())
        self.assertEqual(sidecar["repository"], engine_provenance.REPOSITORY)
        self.assertEqual(sidecar["code_commit"], self.code_commit)
        self.assertEqual(sidecar["versions"]["program_commit"], self.program_commit)
        self.assertEqual(sidecar["data_base_head"], data_base)
        self.assertEqual(sidecar["started_sha256"], _hash((self.repo / started_path).read_bytes()))
        started = _load((self.repo / started_path).read_bytes(), "processing_attempt")
        self.assertEqual(sidecar["processing_key"], started["processing_key"])
        self.assertEqual(data_git.text("log", "-1", "--format=%H", "--", sidecar_path),
                         data_git.text("log", "-1", "--format=%H", "--", started_path))
        publish(self.repo, run_id)
        verified = verify_publication(self.repo, run_id, historical=True)
        self.assertIn(sidecar_path, {item["path"] for item in verified["authority_snapshots"]})
        self.assertEqual(self.roles, ["extract", "review"])

    def test_deleted_engine_proof_cannot_fall_back_to_legacy_absence(self):
        result, sidecar = self.publication()
        sidecar.unlink()
        test_pipeline.git(self.repo, "add", "-u")
        test_pipeline.git(self.repo, "commit", "-m", "artificial deleted provenance")
        self.assert_changed(result["run_id"])

    def test_changed_then_restored_engine_bytes_still_fail_immutable_history(self):
        result, sidecar = self.publication()
        original = sidecar.read_bytes()
        sidecar.write_bytes(original + b"\n")
        test_pipeline.git(self.repo, "add", "-u")
        test_pipeline.git(self.repo, "commit", "-m", "artificial provenance modification")
        self.assert_changed(result["run_id"])
        sidecar.write_bytes(original)
        test_pipeline.git(self.repo, "add", "-u")
        test_pipeline.git(self.repo, "commit", "-m", "artificial byte restoration")
        self.assert_changed(result["run_id"])

    def test_unrelated_branch_merge_preserves_original_reservation(self):
        base = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        branch = test_pipeline.git(self.repo, "branch", "--show-current")
        result, sidecar = self.publication()
        started = sidecar.with_name("started.md")
        original = sidecar.read_bytes(), started.read_bytes()
        test_pipeline.git(self.repo, "checkout", "-b", "artificial-unrelated", base)
        test_pipeline.git(self.repo, "commit", "--allow-empty", "-m", "artificial unrelated branch change")
        test_pipeline.git(self.repo, "checkout", branch)
        test_pipeline.git(self.repo, "merge", "--no-ff", "artificial-unrelated", "-m", "artificial unrelated merge")
        self.assertEqual(len(test_pipeline.git(self.repo, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        self.assertEqual((sidecar.read_bytes(), started.read_bytes()), original)
        verified = verify_publication(self.repo, result["run_id"], historical=True)
        self.assertIn(sidecar.relative_to(self.repo).as_posix(),
                      {item["path"] for item in verified["authority_snapshots"]})

    def test_ours_merge_cannot_hide_engine_edits_on_another_branch(self):
        result, sidecar = self.publication()
        branch = test_pipeline.git(self.repo, "branch", "--show-current")
        original = sidecar.read_bytes()
        test_pipeline.git(self.repo, "checkout", "-b", "artificial-provenance-edit")
        sidecar.write_bytes(original + b"\n")
        test_pipeline.git(self.repo, "add", "-u")
        test_pipeline.git(self.repo, "commit", "-m", "artificial branch provenance modification")
        test_pipeline.git(self.repo, "checkout", branch)
        test_pipeline.git(self.repo, "merge", "--no-ff", "-s", "ours", "artificial-provenance-edit",
                          "-m", "artificial merge retaining original bytes")
        self.assertEqual(len(test_pipeline.git(self.repo, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        self.assertEqual(sidecar.read_bytes(), original)
        self.assert_changed(result["run_id"])

    def test_later_sidecar_cannot_be_attached_to_an_old_reservation(self):
        deferred = {}
        original = engine_provenance.reservation_file

        def postpone(*args):
            deferred.update(original(*args))
            return {}

        with patch.object(engine_provenance, "reservation_file", side_effect=postpone):
            result = self.process()
        publish(self.repo, result["run_id"])
        self.assertIsNone(engine_provenance.read_reservation(PipelineGit(self.repo), result["run_id"]))
        verify_publication(self.repo, result["run_id"], historical=True)
        for relative, raw in deferred.items():
            (self.repo / relative).write_bytes(raw)
        test_pipeline.git(self.repo, "add", "memory")
        test_pipeline.git(self.repo, "commit", "-m", "artificial late provenance insertion")
        self.assert_changed(result["run_id"])


if __name__ == "__main__":
    unittest.main()
