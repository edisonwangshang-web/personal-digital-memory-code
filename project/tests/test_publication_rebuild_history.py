"""A new reviewed generation survives older cleanup on another parent."""

import unittest

import test_skill_quality_pipeline as fixture
from test_pipeline import git
from pdm.batch_groups import publish_final_group
from pdm.batching import plan_batches, project_batch_sources
from pdm.contracts import CaptureError
from pdm.controls import record_control
from pdm.domain_catalog import build_domain_catalog
from pdm.pipeline import publish, verify_publication


class PublicationRebuildHistoryTests(unittest.TestCase):
    setUp = fixture.SkillQualityPipelineTests.setUp
    process = fixture.SkillQualityPipelineTests.process
    read = fixture.SkillQualityPipelineTests.read
    assert_error = fixture.SkillQualityPipelineTests.assert_error

    def model(self, role, payload):
        response = fixture.SkillQualityPipelineTests.model(self, role, payload)
        if role == "extract" and "batch_context" in payload:
            output = response["output"]
            output["wikis"] = [{**output.pop("wiki"), "record_ids": ["sport-plan"]}]
        return response

    def snapshot(self):
        return (git(self.repo, "rev-parse", "HEAD"), len(self.calls),
                {str(path.relative_to(self.repo)): path.read_bytes()
                 for path in (self.repo / "memory").rglob("*.md")})

    def delete(self, path):
        git(self.repo, "rm", path)
        git(self.repo, "commit", "-m", "fixture: explicit knowledge cleanup")

    def restore(self, path, data):
        (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / path).write_bytes(data)
        git(self.repo, "add", path)
        git(self.repo, "commit", "-m", "fixture: restore identical bytes")

    def reviewed_publication(self, mode):
        paths = [self.saved["path"]]
        if mode == "ordinary":
            reviewed = self.process()
            self.assertEqual(reviewed["status"], "passed", reviewed)
            repeat = lambda: publish(self.repo, reviewed["run_id"])
        else:
            plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
            projection = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"],
                                               source_paths=paths)
            reviewed = self.process(related_runs=[], batch_context=projection["batch_context"],
                                    source_views=projection["source_views"])
            self.assertEqual(reviewed["status"], "passed", reviewed)
            candidate = self.read(reviewed["stage_path"], "candidate")
            repeat = lambda: publish_final_group(self.repo, plan, candidate["objects"],
                                                 [reviewed["run_id"]], source_paths=paths)
        return repeat(), repeat

    def integrated_rebuild(self, mode):
        source_head = git(self.repo, "rev-parse", "HEAD")
        old, _ = self.reviewed_publication("ordinary")
        for path in old["files"]:
            self.delete(path)
        cleanup = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "checkout", "-b", "fresh-source-generation", source_head)
        new, repeat = self.reviewed_publication(mode)
        data_head = git(self.repo, "rev-parse", "HEAD")
        # Preserve both true ancestors while selecting only new reviewed files.
        merged = git(self.repo, "commit-tree", git(self.repo, "rev-parse", "HEAD^{tree}"),
                     "-p", cleanup, "-p", data_head, "-m", "fixture: integrate fresh publication")
        git(self.repo, "reset", "--hard", merged)
        self.assertTrue(git(self.repo, "log", "--full-history", "--format=%H", "--diff-filter=D",
                            "HEAD", "--", "memory/records/sport-plan.md"))
        return new, repeat

    def assert_rebuild_readable(self, mode):
        published, repeat = self.integrated_rebuild(mode)
        before = self.snapshot()
        for historical in (False, True):
            result = verify_publication(self.repo, published["run_id"],
                source_paths=[self.saved["path"]], historical=historical)
            self.assertEqual(result["commit_sha"], published["commit_sha"])
            self.assertEqual(len(result["files"]), 2)
        catalog = build_domain_catalog(self.repo, [self.saved["path"]], [published["run_id"]])
        self.assertEqual(len(catalog["domains"]["health"]["records"]), 1)
        self.assertEqual(len(catalog["domains"]["health"]["topics"]), 1)
        self.assertTrue(catalog["catalog_complete_within_selected_scope"])
        self.assertEqual(repeat()["commit_sha"], published["commit_sha"])
        self.assertEqual(self.snapshot(), before)

    def test_ordinary_fresh_review_survives_old_cleanup_parent(self):
        self.assert_rebuild_readable("ordinary")

    def test_group_fresh_review_survives_old_cleanup_parent(self):
        self.assert_rebuild_readable("group")

    def assert_later_delete_rejected(self, mode):
        published, _ = self.reviewed_publication(mode)
        path = "memory/records/sport-plan.md"
        data = (self.repo / path).read_bytes()
        self.delete(path)
        for restored in (False, True):
            if restored:
                self.restore(path, data)
            before = self.snapshot()
            for historical in (False, True):
                with self.assertRaises(CaptureError):
                    verify_publication(self.repo, published["run_id"],
                        source_paths=[self.saved["path"]], historical=historical)
            self.assertEqual(self.snapshot(), before)

    def test_ordinary_postpublication_delete_and_restore_remain_rejected(self):
        self.assert_later_delete_rejected("ordinary")

    def test_group_postpublication_delete_and_restore_remain_rejected(self):
        self.assert_later_delete_rejected("group")

    def assert_descendant_branch_deletion_rejected(self, mode, *, delete_at_merge):
        published, _ = self.reviewed_publication(mode)
        path = "memory/records/sport-plan.md"
        data = (self.repo / path).read_bytes()
        complete = git(self.repo, "rev-parse", "HEAD")
        complete_tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        if delete_at_merge:
            # The deleting change exists only in a merge's result, not either
            # parent's tree. A later byte-identical restoration is still deleted.
            git(self.repo, "commit", "--allow-empty", "-m", "fixture: other parent")
            other = git(self.repo, "rev-parse", "HEAD")
            git(self.repo, "rm", path)
            deleted_tree = git(self.repo, "write-tree")
            merged = git(self.repo, "commit-tree", deleted_tree, "-p", complete, "-p", other,
                         "-m", "fixture: deleting merge")
            git(self.repo, "reset", "--hard", merged)
            self.restore(path, data)
        else:
            self.delete(path)
            deleted = git(self.repo, "rev-parse", "HEAD")
            merged = git(self.repo, "commit-tree", complete_tree, "-p", complete, "-p", deleted,
                         "-m", "fixture: merge restores original tree")
            git(self.repo, "reset", "--hard", merged)
            # The old default-simplified Git query hid this actual later delete.
            self.assertFalse(git(self.repo, "log", "--format=%H", "--diff-filter=D",
                                 "HEAD", "--", path))
        before = self.snapshot()
        for historical in (False, True):
            self.assert_error("publication_mismatch" if mode == "ordinary" else "batch_group_file_deleted",
                lambda: verify_publication(self.repo, published["run_id"],
                    source_paths=[self.saved["path"]], historical=historical))
        self.assertEqual(self.snapshot(), before)

    def test_ordinary_descendant_branch_delete_cannot_hide_behind_merge(self):
        self.assert_descendant_branch_deletion_rejected("ordinary", delete_at_merge=False)

    def test_group_descendant_branch_delete_cannot_hide_behind_merge(self):
        self.assert_descendant_branch_deletion_rejected("group", delete_at_merge=False)

    def test_ordinary_merge_delete_and_restore_remain_rejected(self):
        self.assert_descendant_branch_deletion_rejected("ordinary", delete_at_merge=True)

    def test_group_merge_delete_and_restore_remain_rejected(self):
        self.assert_descendant_branch_deletion_rejected("group", delete_at_merge=True)

    def assert_control_remains_effective(self, mode):
        published, _ = self.integrated_rebuild(mode)
        record_control(self.repo, {"request_key": "fixture:delete-rebuilt-source",
            "action": "delete", "source_paths": [self.saved["path"]],
            "object_paths": sorted(published["files"])})
        before = self.snapshot()
        for historical in (False, True):
            self.assert_error("source_excluded", lambda: verify_publication(
                self.repo, published["run_id"], source_paths=[self.saved["path"]],
                historical=historical))
        self.assertEqual(self.snapshot(), before)

    def test_ordinary_control_delete_still_blocks_rebuilt_knowledge(self):
        self.assert_control_remains_effective("ordinary")

    def test_group_control_delete_still_blocks_rebuilt_knowledge(self):
        self.assert_control_remains_effective("group")


if __name__ == "__main__":
    unittest.main()
