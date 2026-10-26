"""Explicit clear/rebuild permissions on artificial temporary Git data only."""

from pathlib import Path
import hashlib
import tempfile
import unittest

from git_fixtures import configure_test_repository
from test_pipeline import git
from pdm.capture import capture
from pdm.contracts import CaptureError, markdown, parse_markdown
from pdm.controls import record_control
from pdm.rebuild import (allow_rebuild_targets, prepare_rebuild,
                         validate_rebuild_context)
from pdm.transaction import PipelineGit


class RebuildReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-artificial-rebuild-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Rebuild Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial baseline")
        self.source = capture(self.repo, {"source_key": "artificial:rebuild-one", "source_type": "note",
            "acquisition_status": "full_text", "body": "人工资料：只用于重建权限测试。\n"})
        self.sources = [self.source["path"]]
        self.ref = {"source_id": self.source["source_id"], "source_version": self.source["source_version"],
                    "locator": "L1-L1", "quote": "人工资料"}
        self.record = "memory/records/artificial-rebuild.md"
        self.topic = "memory/topics/artificial-rebuild.md"
        self.targets = [self.record, self.topic]
        version = "sha256-" + hashlib.sha256(b"artificial formal fixture").hexdigest()
        self.record_meta = {"schema_version": "0.2", "id": "artificial-rebuild", "type": "knowledge",
            "sources": [self.ref], "content_version": version, "review_status": "passed"}
        self.topic_meta = {**self.record_meta, "type": "wiki", "records": [
            {"id": "artificial-rebuild", "content_version": version}]}
        self.original = {self.record: markdown(self.record_meta, "人工既有记录。\n"),
                         self.topic: markdown(self.topic_meta, "人工既有主题。\n")}
        self.proof = "memory/staging/artificial-history.md"
        self.original[self.proof] = markdown({"schema_version": "0.2", "kind": "candidate"}, "历史人工证明。\n")
        self.original["memory/profile.md"] = b"Artificial unrelated profile\n"
        self.commit(self.original)
        self.base = git(self.repo, "rev-parse", "HEAD")

    def commit(self, files, message="artificial data update"):
        for name, value in files.items():
            path = self.repo / name
            if value is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value)
            git(self.repo, "add", "--", name)
        git(self.repo, "commit", "-m", message)

    def prepare(self):
        return prepare_rebuild(self.repo, "artificial-user-request-one", self.sources, self.targets,
                               expected_head=self.base)

    def objects(self, reference=None):
        return [{"path": self.record, "metadata": {**self.record_meta, "sources": [reference or self.ref]},
                 "body": "全新人工内容。\n"}]

    def assert_code(self, code, action):
        with self.assertRaises(CaptureError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def test_cleanup_is_one_commit_only_selected_formal_and_immutable_receipt(self):
        result = self.prepare()
        context = result["rebuild_context"]
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(set(context), {"path", "commit_sha", "sha256"})
        changes = git(self.repo, "diff-tree", "--no-commit-id", "--name-status", "-r", result["head"]).splitlines()
        self.assertEqual(set(changes), {"A\t" + context["path"], *["D\t" + p for p in self.targets]})
        self.assertEqual(git(self.repo, "show", "-s", "--format=%P", result["head"]), self.base)
        self.assertFalse((self.repo / self.record).exists())
        self.assertFalse((self.repo / self.topic).exists())
        self.assertEqual((self.repo / self.proof).read_bytes(), self.original[self.proof])
        self.assertEqual((self.repo / "memory/profile.md").read_bytes(), self.original["memory/profile.md"])
        value = validate_rebuild_context(PipelineGit(self.repo), context, self.sources)
        self.assertEqual(value["base_head"], self.base)
        self.assertEqual(allow_rebuild_targets(PipelineGit(self.repo), self.objects(), context), set(self.targets))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_replaying_intent_after_new_publication_never_deletes_again(self):
        result = self.prepare()
        self.commit({self.record: self.original[self.record], self.topic: self.original[self.topic]})
        head = git(self.repo, "rev-parse", "HEAD")
        replay = prepare_rebuild(self.repo, "artificial-user-request-one", self.sources, self.targets)
        self.assertEqual(replay["status"], "already_prepared")
        self.assertEqual(replay["head"], head)
        self.assertEqual(replay["rebuild_context"], result["rebuild_context"])
        self.assertEqual((self.repo / self.record).read_bytes(), self.original[self.record])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_existing_intent_cannot_change_target_scope(self):
        self.prepare()
        self.assert_code("rebuild_intent_scope_changed", lambda: prepare_rebuild(
            self.repo, "artificial-user-request-one", self.sources, [self.topic]))

    def test_sources_proofs_and_templates_are_not_formal_clear_targets(self):
        for path in (self.source["path"], self.source["receipt_path"], self.proof, "memory/profile.md"):
            with self.subTest(path=path):
                self.assert_code("invalid_rebuild_scope", lambda: prepare_rebuild(
                    self.repo, "artificial-invalid-target", self.sources, [path]))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertTrue((self.repo / self.record).is_file())

    def test_cannot_clear_other_source_or_drop_dependent_topic(self):
        self.assert_code("rebuild_dependency_scope", lambda: prepare_rebuild(
            self.repo, "artificial-partial", self.sources, [self.record]))
        metadata, body = parse_markdown(self.original[self.record])
        metadata["sources"] = [{**self.ref, "source_id": "src-" + "f" * 64}]
        self.commit({self.record: markdown(metadata, body)})
        before = git(self.repo, "rev-parse", "HEAD")
        self.assert_code("invalid_rebuild_target", lambda: prepare_rebuild(
            self.repo, "artificial-wrong-source", self.sources, self.targets))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertTrue((self.repo / self.topic).exists())

    def test_old_target_touched_then_deleted_cannot_gain_permission_again(self):
        context = self.prepare()["rebuild_context"]
        self.commit({self.record: self.original[self.record]})
        self.commit({self.record: None})
        self.assert_code("rebuild_target_changed", lambda: allow_rebuild_targets(
            PipelineGit(self.repo), self.objects(), context))

    def test_historical_candidate_base_survives_successful_new_target_writes(self):
        result = self.prepare()
        context = result["rebuild_context"]
        self.commit({self.record: self.original[self.record], self.topic: self.original[self.topic]})
        self.assert_code("rebuild_target_changed", lambda: allow_rebuild_targets(
            PipelineGit(self.repo), self.objects(), context))
        self.assertEqual(allow_rebuild_targets(PipelineGit(self.repo), self.objects(), context,
                                             at_commit=result["head"]), set(self.targets))

    def test_rebuilt_output_cannot_claim_an_unselected_source(self):
        context = self.prepare()["rebuild_context"]
        self.assert_code("rebuild_source_scope_changed", lambda: allow_rebuild_targets(
            PipelineGit(self.repo), self.objects({**self.ref, "source_id": "src-" + "f" * 64}), context))

    def test_receipt_tamper_restored_to_original_bytes_is_rejected(self):
        context = self.prepare()["rebuild_context"]
        original = (self.repo / context["path"]).read_bytes()
        self.commit({context["path"]: original + b"\n"})
        self.commit({context["path"]: original})
        self.assert_code("rebuild_context_changed", lambda: validate_rebuild_context(
            PipelineGit(self.repo), context, self.sources))

    def test_receipt_cannot_be_created_after_a_separate_cleanup(self):
        context = self.prepare()["rebuild_context"]
        original = (self.repo / context["path"]).read_bytes()
        git(self.repo, "checkout", "-b", "artificial-forged", self.base)
        self.commit({self.record: None, self.topic: None})
        parent = git(self.repo, "rev-parse", "HEAD")
        header, body = parse_markdown(original)
        import json
        value = json.loads(body)
        value.update(base_head=parent, branch="artificial-forged")
        from pdm.contracts import canonical
        forged = markdown(header, canonical(value).decode() + "\n")
        self.commit({context["path"]: forged})
        forged_context = {"path": context["path"], "commit_sha": git(self.repo, "rev-parse", "HEAD"),
                          "sha256": "sha256-" + hashlib.sha256(forged).hexdigest()}
        self.assert_code("rebuild_cleanup_not_atomic", lambda: validate_rebuild_context(
            PipelineGit(self.repo), forged_context, self.sources))

    def test_source_deletion_control_remains_a_hard_stop(self):
        context = self.prepare()["rebuild_context"]
        record_control(self.repo, {"request_key": "artificial-source-delete", "action": "delete",
            "source_paths": self.sources, "object_paths": [], "replacement_source_paths": [],
            "basis_source_path": None, "effective_from": None})
        self.assert_code("source_excluded", lambda: validate_rebuild_context(
            PipelineGit(self.repo), context, self.sources))
        self.assertTrue((self.repo / self.source["path"]).exists())
        self.assertFalse((self.repo / self.record).exists())

    def test_treesame_merge_carrying_receipt_is_valid(self):
        context = self.prepare()["rebuild_context"]
        git(self.repo, "checkout", "-b", "artificial-rebuild-branch")
        self.commit({"side-note.md": b"Artificial side branch note\n"})
        git(self.repo, "checkout", "dev")
        self.commit({"main-note.md": b"Artificial main branch note\n"})
        git(self.repo, "merge", "--no-ff", "artificial-rebuild-branch", "-m", "artificial treesame merge")
        self.assertEqual(allow_rebuild_targets(PipelineGit(self.repo), self.objects(), context), set(self.targets))
        replay = prepare_rebuild(self.repo, "artificial-user-request-one", self.sources, self.targets)
        self.assertEqual(replay["status"], "already_prepared")
        self.assertEqual(replay["rebuild_context"], context)

    def test_corrected_publications_keep_ordinary_and_rebuilt_history_readable(self):
        from test_rebuild_pipeline import RebuildPipelineTests
        from pdm.pipeline import process_sources, publish, verify_publication
        for rebuilt in (False, True):
            with self.subTest(rebuilt=rebuilt):
                fixture = RebuildPipelineTests()
                fixture.setUp()
                try:
                    processed = process_sources(fixture.repo, [fixture.saved["path"]],
                                                related_runs=[], model_call=fixture.model)
                    publication = publish(fixture.repo, processed["run_id"])
                    if rebuilt:
                        fixture.context = prepare_rebuild(fixture.repo, "artificial-history-correction",
                            [fixture.saved["path"]], sorted(publication["files"]))["rebuild_context"]
                        processed = fixture.fresh()
                        publication = publish(fixture.repo, processed["run_id"])
                    replacement = capture(fixture.repo, {"source_key": "artificial:corrected-source",
                        "source_type": "note", "acquisition_status": "full_text",
                        "body": "人工更正：原说明需要纠正。\n"})
                    record_control(fixture.repo, {"request_key": "artificial-history-correction",
                        "action": "corrects", "source_paths": [fixture.saved["path"]],
                        "object_paths": sorted(publication["files"]),
                        "replacement_source_paths": [replacement["path"]],
                        "basis_source_path": replacement["path"], "effective_from": None})
                    before = git(fixture.repo, "rev-parse", "HEAD")
                    verified = verify_publication(fixture.repo, processed["run_id"], historical=True)
                    self.assertEqual(verified["commit_sha"], publication["commit_sha"])
                    self.assertEqual(git(fixture.repo, "rev-parse", "HEAD"), before)
                    if rebuilt:
                        self.assert_code("rebuild_controls_changed", lambda: validate_rebuild_context(
                            PipelineGit(fixture.repo), fixture.context))
                finally:
                    fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
