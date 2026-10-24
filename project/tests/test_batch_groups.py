"""Real conditional multi-Wiki commits with independently reviewed fake models."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_pipeline
from pdm.batch_groups import publish_final_group, verify_group_publication
from pdm.batching import merge_batch_sources, plan_batches, project_batch_sources
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.controls import record_control
from pdm.pipeline import _committed, _load, process_sources
from pdm.transaction import PipelineGit


class BatchGroupTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    assert_error = test_pipeline.PipelineTests.assert_error
    commit_change = test_pipeline.PipelineTests.commit_change

    def sources(self):
        return [capture(self.repo, {"source_key": "fixture:group:" + str(number), "source_type": "note",
                    "acquisition_status": "full_text", "body": f"用户：隔离事实{number}，尚未执行。\n"})["path"]
                for number in range(2)]

    def model(self, role, payload):
        self.number += 1
        self.roles.append(role)
        if role == "extract":
            objects, wikis = [], []
            for source in payload["sources"]:
                line = source["fragments"][0]["lines"][0]
                suffix = source["metadata"]["source_id"][-10:]
                ref = {"source_id": source["metadata"]["source_id"],
                       "source_version": source["metadata"]["source_version"],
                       "locator": f"L{line['line']}-L{line['line']}", "quote": line["text"]}
                objects.append({"id": "record-" + suffix, "type": "knowledge", "title": "隔离事实",
                    "source_kind": "user_statement", "sources": [ref], "domains": ["learning"],
                    "valid_from": None, "valid_to": None, "verification_status": "unverified",
                    "lifecycle_status": "undetermined", "body": line["text"]})
                wikis.append({"id": "topic-" + suffix, "title": "隔离主题", "body": line["text"],
                              "sources": [ref], "record_ids": ["record-" + suffix]})
            output = {"objects": objects, "wikis": wikis}
        else:
            output = {"verdict": self.verdict, "reasons": ["独立核对原文、跨批对象与主题的全部关系。"]}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def reviewed(self, plan, paths, projection=None):
        projection = projection or merge_batch_sources(self.repo, plan,
            [batch["batch_id"] for batch in plan["batches"]], source_paths=paths, max_bytes=100_000)
        result = process_sources(self.repo, paths, related_runs=[], model_call=self.model,
            batch_context=projection["batch_context"], source_views=projection["source_views"])
        candidate = _load(_committed(PipelineGit(self.repo), result["stage_path"]), "candidate")
        return result, candidate

    def ready(self):
        paths = self.sources()
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000, max_sources=1)
        self.assertEqual(len(plan["batches"]), 2)
        result, candidate = self.reviewed(plan, paths)
        self.assertEqual(result["status"], "passed")
        return paths, plan, result, candidate

    def publish(self, paths, plan, reviewed, candidate, **kwargs):
        return publish_final_group(self.repo, plan, candidate["objects"], [reviewed["run_id"]], source_paths=paths, **kwargs)

    def no_formal(self):
        git = PipelineGit(self.repo)
        self.assertEqual(git.text("ls-tree", "-r", "--name-only", "HEAD", "--", "memory/records", "memory/topics"), "")

    def test_multi_wiki_final_review_publishes_all_objects_in_one_commit(self):
        paths, plan, reviewed, candidate = self.ready()
        result = self.publish(paths, plan, reviewed, candidate)
        self.assertEqual(result["mode"], "batch_group")
        self.assertEqual(len(result["files"]), 4)
        self.assertTrue(result["receipt_saved"])
        git = PipelineGit(self.repo)
        changed = git.text("diff-tree", "--no-commit-id", "--name-only", "-r", result["commit_sha"]).splitlines()
        self.assertEqual(set(changed), set(result["files"]) | {result["manifest_path"], f"memory/runs/processes/grouped/{plan['plan_id']}.md"})
        self.assertEqual(git.text("rev-parse", result["commit_sha"] + "^"), reviewed["head"])
        self.assertFalse(git.path(f"memory/runs/processes/{reviewed['run_id']}/manifest.md").exists())
        manifest = _load(_committed(git, result["manifest_path"]), "batch_group_manifest", "0.6")
        self.assertEqual(manifest["proofs"][0]["review_commit"], reviewed["head"])
        self.assertEqual(manifest["plan_sha256"], result["plan_sha256"])
        self.assertTrue(result["authority_snapshots"])
        self.assertEqual(verify_group_publication(self.repo, result["run_id"], source_paths=paths)["commit_sha"], result["commit_sha"])

    def test_group_is_readable_by_catalog_and_authorized_context_without_a_group_stage(self):
        from pdm.context import discover_context
        from pdm.domain_catalog import build_domain_catalog
        from pdm.pipeline import verify_publication
        paths, plan, reviewed, candidate = self.ready()
        result = self.publish(paths, plan, reviewed, candidate)
        catalog = build_domain_catalog(self.repo, paths, [result["run_id"]])
        self.assertEqual(len(catalog["domains"]["learning"]["records"]), 2)
        self.assertEqual(len(catalog["domains"]["learning"]["topics"]), 2)
        self.assertTrue(catalog["catalog_complete_within_selected_scope"])
        context = discover_context(self.repo, paths, allowed_source_paths=paths, allowed_run_ids=[result["run_id"]])
        self.assertEqual(context["related_runs"], [result["run_id"]])
        self.assertEqual(verify_publication(self.repo, result["run_id"], source_paths=paths)["commit_sha"], result["commit_sha"])

    def test_next_group_can_reuse_old_group_and_preserve_original_reviews(self):
        paths, plan, reviewed, candidate = self.ready()
        first = self.publish(paths, plan, reviewed, candidate)
        old_bytes = {path: (self.repo / path).read_bytes() for path in first["files"]}
        extra = capture(self.repo, {"source_key": "fixture:group:extra", "source_type": "note",
                        "acquisition_status": "full_text", "body": "用户：新的独立学习事实，尚未执行。\n"})["path"]
        paths = sorted([*paths, extra])
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000, max_sources=1)
        projection = merge_batch_sources(self.repo, plan, [batch["batch_id"] for batch in plan["batches"]],
                                         source_paths=paths, max_bytes=100_000)
        reviewed = process_sources(self.repo, paths, related_runs=[first["run_id"]], model_call=self.model,
            batch_context=projection["batch_context"], source_views=projection["source_views"])
        self.assertEqual(reviewed["status"], "passed")
        candidate = _load(_committed(PipelineGit(self.repo), reviewed["stage_path"]), "candidate")
        second = self.publish(paths, plan, reviewed, candidate)
        self.assertEqual(len(second["files"]), 6)
        self.assertEqual(old_bytes, {path: (self.repo / path).read_bytes() for path in first["files"]})
        self.assertTrue(all(second["object_origins"][path] == first["object_origins"][path] for path in first["files"]))
        self.assertTrue(verify_group_publication(self.repo, first["run_id"], historical=True)["historical"])

    def test_same_plan_repeat_is_zero_write_and_zero_model(self):
        paths, plan, reviewed, candidate = self.ready()
        first = self.publish(paths, plan, reviewed, candidate)
        git, calls = PipelineGit(self.repo), self.number
        head = git.head()
        second = self.publish(paths, plan, reviewed, candidate)
        self.assertEqual(second["commit_sha"], first["commit_sha"])
        self.assertEqual(git.head(), head)
        self.assertEqual(self.number, calls)
        self.assert_error("batch_group_plan_already_published", lambda: self.publish(paths, plan, reviewed, candidate, group_id="run-" + "f" * 32))

    def test_same_plan_new_certificate_cannot_report_old_bytes_as_new_requested_output(self):
        paths, plan, first, candidate = self.ready()
        second, second_candidate = self.reviewed(plan, paths)
        self.publish(paths, plan, first, candidate)
        head = PipelineGit(self.repo).head()
        self.assert_error("batch_group_final_objects_mismatch", lambda: self.publish(paths, plan, second, second_candidate))
        self.assertEqual(PipelineGit(self.repo).head(), head)

    def test_changed_or_omitted_final_object_cannot_borrow_review(self):
        paths, plan, reviewed, candidate = self.ready()
        for final in (candidate["objects"][:-1], deepcopy(candidate["objects"])):
            if len(final) == len(candidate["objects"]):
                final[0]["body"] += "未审核的附加结论。"
            self.assert_error("batch_group_final_objects_mismatch", lambda: publish_final_group(
                self.repo, plan, final, [reviewed["run_id"]], source_paths=paths))
        self.no_formal()

    def test_a_local_review_cannot_claim_whole_plan_coverage(self):
        paths = self.sources()
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000, max_sources=1)
        first = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=paths)
        reviewed, candidate = self.reviewed(plan, paths, first)
        self.assert_error("batch_group_incomplete_coverage", lambda: self.publish(paths, plan, reviewed, candidate))
        self.no_formal()

    def test_local_draft_collection_cannot_bypass_final_independent_review(self):
        paths = self.sources()
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000, max_sources=1)
        reviewed_runs, objects = [], []
        for batch in plan["batches"]:
            local = project_batch_sources(self.repo, plan, batch["batch_id"], source_paths=paths)
            result = process_sources(self.repo, paths, related_runs=[], model_call=self.model, draft_only=True,
                batch_context=local["batch_context"], source_views=local["source_views"])
            self.assertEqual(result["status"], "passed")
            candidate = _load(_committed(PipelineGit(self.repo), result["stage_path"]), "candidate")
            self.assertTrue(candidate["batch_material"]["draft_only"])
            reviewed_runs.append(result["run_id"])
            objects.extend(candidate["objects"])
        # Together they cover the plan and every local Wiki has its records,
        # but neither local draft is a final merge/relationship certificate.
        self.assertEqual(len(objects), 4)
        head, calls = PipelineGit(self.repo).head(), self.number
        self.assert_error("batch_requires_final_review", lambda: publish_final_group(
            self.repo, plan, objects, reviewed_runs, source_paths=paths))
        self.assertEqual(PipelineGit(self.repo).head(), head)
        self.assertEqual(self.number, calls)
        self.no_formal()

    def test_parent_names_without_their_fragments_do_not_prove_coverage(self):
        paths = self.sources()
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000, max_sources=1)
        first = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=paths)
        first["batch_context"]["parent_batch_ids"] = [batch["batch_id"] for batch in plan["batches"]]
        # The synthetic context cannot name itself as parent.
        first["batch_context"]["batch_id"] = "batch-" + "f" * 64
        reviewed, candidate = self.reviewed(plan, paths, first)
        self.assert_error("batch_group_incomplete_parent_coverage", lambda: self.publish(paths, plan, reviewed, candidate))
        self.no_formal()

    def test_two_different_current_certificates_for_one_path_are_not_silently_merged(self):
        paths, plan, first, _ = self.ready()
        second, candidate = self.reviewed(plan, paths)
        self.assert_error("batch_group_object_conflict", lambda: publish_final_group(self.repo, plan,
            candidate["objects"], [first["run_id"], second["run_id"]], source_paths=paths))
        self.no_formal()

    def test_review_failure_cannot_publish_group(self):
        paths = self.sources()
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000, max_sources=1)
        self.verdict = "needs_revision"
        reviewed, candidate = self.reviewed(plan, paths)
        self.assert_error("review_not_passed", lambda: self.publish(paths, plan, reviewed, candidate))
        self.no_formal()

    def test_new_controls_and_code_changes_invalidate_pending_group(self):
        paths, plan, reviewed, candidate = self.ready()
        with patch("pdm.batching.execution_versions", return_value={"changed": True}):
            self.assert_error("batch_snapshot_changed", lambda: self.publish(paths, plan, reviewed, candidate))
        record_control(self.repo, {"request_key": "group:unrelated-control", "action": "delete",
                                  "source_paths": [self.saved["path"]], "object_paths": []})
        self.assert_error("batch_snapshot_changed", lambda: self.publish(paths, plan, reviewed, candidate))
        self.no_formal()

    def test_scope_is_checked_before_source_bodies_and_unchanged_head_is_required(self):
        paths, plan, reviewed, candidate = self.ready()
        with patch("pdm.batching.load_source", side_effect=AssertionError("unselected source read")):
            self.assert_error("batch_group_scope_mismatch", lambda: self.publish(paths[:1], plan, reviewed, candidate))
        self.assert_error("head_conflict", lambda: self.publish(paths, plan, reviewed, candidate, expected_head=plan["snapshot"]["head"]))
        self.no_formal()

    def test_candidate_modified_after_review_is_rejected(self):
        paths, plan, reviewed, candidate = self.ready()
        path = reviewed["stage_path"]
        self.commit_change(path, (self.repo / path).read_bytes() + b"\nchanged after review\n")
        with self.assertRaises(CaptureError):
            self.publish(paths, plan, reviewed, candidate)
        self.no_formal()

    def test_receipt_interruption_reuses_original_group_without_republication(self):
        paths, plan, reviewed, candidate = self.ready()
        write = PipelineGit.write_files

        def interrupted(git, files, *args, **kwargs):
            if any(path.endswith("/receipt.md") for path in files):
                raise CaptureError("isolated_receipt_interruption", "isolated")
            return write(git, files, *args, **kwargs)

        self.assert_error("isolated_receipt_interruption", lambda: self._with_write_patch(interrupted,
            lambda: self.publish(paths, plan, reviewed, candidate)))
        git = PipelineGit(self.repo)
        group_commit, calls = git.head(), self.number
        restored = self.publish(paths, plan, reviewed, candidate)
        self.assertEqual(restored["commit_sha"], group_commit)
        self.assertEqual(self.number, calls)
        self.assertTrue(restored["receipt_saved"])

    def _with_write_patch(self, callback, action):
        with patch.object(PipelineGit, "write_files", callback):
            return action()

    def test_current_and_historical_reading_are_distinct_and_old_versions_remain_bound(self):
        paths, plan, reviewed, candidate = self.ready()
        result = self.publish(paths, plan, reviewed, candidate)
        with patch("pdm.batching.execution_versions", return_value={"newer_program": True}):
            self.assertEqual(verify_group_publication(self.repo, result["run_id"], source_paths=paths)["versions"], plan["snapshot"]["versions"])
        path = sorted(result["files"])[0]
        self.commit_change(path, (self.repo / path).read_bytes() + b"\nLater independent artificial change.\n")
        self.assert_error("batch_group_file_mismatch", lambda: verify_group_publication(self.repo, result["run_id"], source_paths=paths))
        restored = verify_group_publication(self.repo, result["run_id"], source_paths=paths, historical=True)
        self.assertTrue(restored["historical"])
        self.assertEqual(restored["commit_sha"], result["commit_sha"])

    def test_later_unrelated_controls_do_not_rewrite_original_group_proof(self):
        paths, plan, reviewed, candidate = self.ready()
        result = self.publish(paths, plan, reviewed, candidate)
        record_control(self.repo, {"request_key": "group:later-control", "action": "delete",
                                  "source_paths": [self.saved["path"]], "object_paths": []})
        restored = verify_group_publication(self.repo, result["run_id"], source_paths=paths, historical=True)
        self.assertEqual(restored["commit_sha"], result["commit_sha"])
        self.assertEqual(restored["controls_digest"], plan["snapshot"]["controls_digest"])


if __name__ == "__main__":
    unittest.main()
