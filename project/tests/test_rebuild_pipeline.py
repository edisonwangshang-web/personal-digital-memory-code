"""Explicit fresh rebuilds use only artificial source/model fixtures."""

import copy
import unittest

import test_multisource_pipeline
import test_pipeline
from quality_fixtures import add_quality, passed_review
from pdm.pipeline import (
    _candidate_binding, _computation_snapshots, _load,
    _processing_key, _read_reviewed, _targets_clear, preflight_processing, process_sources,
    publish, verify_publication,
)
from pdm.rebuild import prepare_rebuild
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class RebuildPipelineTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    model = test_multisource_pipeline.MultisourcePipelineTests.model
    commit_change = test_pipeline.PipelineTests.commit_change
    assert_error = test_pipeline.PipelineTests.assert_error

    def initial(self):
        old = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=self.model)
        publication = publish(self.repo, old["run_id"])
        self.old_run = old["run_id"]
        self.targets = sorted(publication["files"])
        self.context = prepare_rebuild(self.repo, "fixture:explicit-rebuild",
                                      [self.saved["path"]], self.targets)["rebuild_context"]
        return old, publication

    def quality_model(self, role, payload):
        result = self.model(role, payload)
        if role == "extract":
            result["output"] = add_quality(result["output"], payload["quality_context"])
            result["output"]["reuse_refs"] = []
        else:
            result["output"] = passed_review(payload["required_checks"])
        return result

    def fresh(self, *, model_call=None, **options):
        return process_sources(self.repo, [self.saved["path"]], related_runs=[],
            model_call=model_call or self.quality_model, rebuild_context=self.context,
            skill_bundle=resolve_skill_refs("knowledge-extract@10", "knowledge-review@8"), **options)

    def test_cleared_same_ids_fresh_extract_review_publish_and_read(self):
        self.initial()
        self.contexts.clear()
        result = self.fresh()
        self.assertEqual(result["status"], "passed")
        candidate, review, _ = _read_reviewed(PipelineGit(self.repo), result["run_id"])
        self.assertEqual(candidate["rebuild_context"], self.context)
        for payload in self.contexts:
            self.assertEqual(payload["related"], [])
            self.assertEqual(payload["rebuild_context"], self.context)
            self.assertNotIn("revision_context", payload)
            self.assertNotIn("continuation_context", payload)
        self.assertEqual(self.contexts[0]["reuse_quality_requirements"], [])
        self.assertEqual(candidate["extraction_output"]["reuse_refs"], [])
        self.assertTrue(all(op["action"] == "create" for op in candidate["operations"].values()))
        self.assertNotEqual(result["run_id"], self.old_run)
        publication = publish(self.repo, result["run_id"])
        self.assertEqual(sorted(publication["files"]), self.targets)
        self.assertEqual(publication["rebuild_context"], self.context)
        for historical in (False, True):
            verified = verify_publication(self.repo, result["run_id"], historical=historical)
            self.assertEqual(verified["commit_sha"], publication["commit_sha"])
        snapshots = _computation_snapshots(PipelineGit(self.repo), candidate)
        self.assertIn(self.context, snapshots)
        calls, head = self.number, test_pipeline.git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.fresh()["status"], "already_processed")
        self.assertEqual(self.number, calls)
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(publish(self.repo, result["run_id"])["head"], head)

    def test_plain_deleted_target_guard_remains_strict(self):
        old, _ = self.initial()
        objects = _load((self.repo / old["stage_path"]).read_bytes(), "candidate")["objects"]
        self.assert_error("target_exists_or_deleted", lambda: _targets_clear(PipelineGit(self.repo), objects))
        self.assertTrue(all(not (self.repo / path).exists() for path in self.targets))

    def test_preflight_is_complete_but_makes_no_call_or_write(self):
        self.initial()
        head, calls = test_pipeline.git(self.repo, "rev-parse", "HEAD"), self.number
        result = preflight_processing(self.repo, [self.saved["path"]], related_runs=[],
            rebuild_context=self.context,
            skill_bundle=resolve_skill_refs("knowledge-extract@10", "knowledge-review@8"))
        self.assertEqual(result["status"], "ready")
        self.assertGreater(result["request_bytes"], 0)
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.number, calls)

    def test_rebuild_cannot_import_related_revision_merge_or_reused_output(self):
        self.initial()
        head, calls = test_pipeline.git(self.repo, "rev-parse", "HEAD"), self.number
        cases = ({"related_runs": [self.old_run]}, {"revision_from_run": self.old_run},
                 {"continuation_context": {}}, {"merge_runs": [self.old_run]},
                 {"reuse_extraction_run": self.old_run}, {"draft_only": True})
        for options in cases:
            with self.subTest(options=options):
                values = {"related_runs": [], **options}
                self.assert_error("invalid_rebuild_context", lambda: process_sources(
                    self.repo, [self.saved["path"]], model_call=self.model,
                    rebuild_context=self.context, **values))
        self.assertEqual(self.number, calls)
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)

    def rejected(self):
        def model(role, payload):
            response = self.quality_model(role, payload)
            if role == "review":
                response["output"]["verdict"] = "needs_revision"
                response["output"]["reasons"] = ["人工本轮审核要求说明这次原文整理范围。"]
                response["output"]["checks"][0]["result"] = "needs_revision"
            return response
        result = self.fresh(model_call=model)
        self.assertEqual(result["status"], "needs_revision")
        return result

    def test_same_rebuild_generation_can_revise_its_new_rejected_candidate(self):
        self.initial()
        rejected = self.rejected()
        first_revision_call = len(self.contexts)
        def revised(role, payload):
            response = self.quality_model(role, payload)
            if role == "extract":
                response["output"]["objects"][0]["body"] += "本轮仅整理选定人工原文。"
                response["output"]["wiki"]["body"] += "本轮范围以选定人工原文为限。"
                response["output"] = add_quality(response["output"], payload["quality_context"])
            return response
        result = self.fresh(model_call=revised, revision_from_run=rejected["run_id"])
        self.assertEqual(result["status"], "passed")
        request = self.contexts[first_revision_call]
        self.assertEqual(request["revision_context"]["source_run_id"], rejected["run_id"])
        self.assertEqual(request["rebuild_context"], self.context)
        candidate = _load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        self.assertEqual(candidate["revision_context"]["candidate"]["rebuild_context"], self.context)
        self.assertEqual(request["related"], [])
        publication = publish(self.repo, result["run_id"])
        self.assertEqual(verify_publication(self.repo, result["run_id"])["commit_sha"], publication["commit_sha"])

    def test_rebuild_revision_cannot_drop_its_explicit_context(self):
        self.initial()
        rejected = self.rejected()
        head, calls = test_pipeline.git(self.repo, "rev-parse", "HEAD"), self.number
        self.assert_error("invalid_rebuild_context", lambda: process_sources(self.repo,
            [self.saved["path"]], related_runs=[], revision_from_run=rejected["run_id"],
            model_call=self.quality_model))
        self.assertEqual(self.number, calls)
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)

    def test_reappeared_target_blocks_before_preflight_or_model(self):
        _, publication = self.initial()
        target = self.targets[0]
        self.commit_change(target, PipelineGit(self.repo).read(publication["commit_sha"], target))
        head, calls = test_pipeline.git(self.repo, "rev-parse", "HEAD"), self.number
        self.assert_error("rebuild_target_changed", lambda: self.fresh())
        self.assertEqual(self.number, calls)
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)

    def test_binding_and_request_include_exact_rebuild_reference(self):
        self.initial()
        result = self.fresh()
        candidate = _load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        original = candidate["binding"]
        other = copy.deepcopy(candidate)
        other["rebuild_context"]["sha256"] = "sha256-" + "f" * 64
        self.assertNotEqual(_candidate_binding(other), original)
        bindings = {"source": candidate["source"]}
        arguments = (bindings, candidate["versions"], candidate["controls_digest"])
        self.assertNotEqual(_processing_key(*arguments),
                            _processing_key(*arguments, rebuild_context=self.context))
        self.assertEqual(_processing_key(*arguments), _processing_key(*arguments, rebuild_context=None))


if __name__ == "__main__":
    unittest.main()
