"""Manifest-scoped daily context from ordinary and grouped publications."""

import json
import unittest
from unittest.mock import patch

import test_pipeline
import test_production
from pdm.batching import merge_batch_sources, plan_batches
from pdm.contracts import CaptureError, canonical, markdown, parse_markdown
from pdm.pipeline import process_sources, publish
from pdm.production import run_production
from pdm.scheduler import configure_schedule, plan_daily
from pdm.transaction import PipelineGit


class SchedulerPublicationTests(unittest.TestCase):
    setUp = test_production.ProductionTests.setUp
    sources = test_production.ProductionTests.sources
    prepare = test_production.ProductionTests.prepare
    transport = test_production.ProductionTests.transport

    def configure(self, paths, run):
        return configure_schedule(self.repo, {
            "schedule_id": "manifest-context", "daily_time": "22:00",
            "allowed_source_ids": [], "allowed_source_paths": paths,
            "related_run_ids": [run], "remote_mode": "local_only",
            "execution_policy": "continuous-v1", "processing_engine": "legacy-v1"})

    def group(self):
        paths, _ = self.prepare(count=2)
        result = run_production(self.repo, "queue", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        return paths, result["publication"]["run_id"]

    def rewrite(self, path, change):
        git = PipelineGit(self.repo)
        before = git._existing(path)
        meta, body = parse_markdown(before)
        value = json.loads(body)
        change(value)
        with git.lock():
            git.write_changes({path: markdown(meta, canonical(value).decode() + "\n")},
                              {path: before}, git.head(), "artificial publication mutation")

    def test_legacy_publication_still_supplies_related_daily_context(self):
        result = process_sources(self.repo, [self.saved["path"]],
                                 model_call=lambda role, payload: test_pipeline.PipelineTests.model(self, role, payload))
        published = publish(self.repo, result["run_id"])
        self.configure([self.saved["path"]], published["run_id"])
        before = PipelineGit(self.repo).head()
        plan = plan_daily(self.repo, "manifest-context")
        self.assertEqual(plan["related_run_ids"], [published["run_id"]])
        self.assertEqual(plan["items"][0]["source_paths"], [self.saved["path"]])
        self.assertEqual(PipelineGit(self.repo).head(), before)

    def test_ordinary_batch_manifest_06_is_accepted(self):
        paths = self.sources(2)
        plan = plan_batches(self.repo, paths, max_input_bytes=100000)
        projected = merge_batch_sources(self.repo, plan, [item["batch_id"] for item in plan["batches"]],
                                        source_paths=paths, max_bytes=100000)
        result = process_sources(self.repo, paths, model_call=self.adapter,
                                 batch_context=projected["batch_context"], source_views=projected["source_views"])
        self.assertEqual(result["status"], "passed", result)
        published = publish(self.repo, result["run_id"])
        meta, _ = parse_markdown((self.repo / published["manifest_path"]).read_bytes())
        self.assertEqual((meta["kind"], meta["schema_version"]), ("manifest", "0.6"))
        self.configure(paths, published["run_id"])
        daily = plan_daily(self.repo, "manifest-context")
        self.assertEqual(daily["related_run_ids"], [published["run_id"]])
        self.assertTrue(all(item["source_paths"] == sorted(paths) for item in daily["items"]))

    def test_group_manifest_is_verified_without_inventing_a_group_stage(self):
        paths, run = self.group()
        missing_stage = f"memory/staging/{run}.md"
        self.assertFalse((self.repo / missing_stage).exists())
        self.configure(paths, run)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number
        original = PipelineGit.read
        def no_group_stage(git, commit, path):
            self.assertNotEqual(path, missing_stage, "a publication group has no extraction stage")
            return original(git, commit, path)
        with patch.object(PipelineGit, "read", no_group_stage):
            daily = plan_daily(self.repo, "manifest-context")
        self.assertEqual(daily["related_run_ids"], [run])
        self.assertTrue(all(item["source_paths"] == sorted(paths) for item in daily["items"]))
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number), before)

    def test_group_scope_is_checked_before_any_source_body(self):
        paths, run = self.group()
        self.configure(paths[:1], run)
        before = PipelineGit(self.repo).head(), self.number
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("out-of-scope source opened")):
            with self.assertRaises(CaptureError) as caught:
                plan_daily(self.repo, "manifest-context")
        self.assertEqual(caught.exception.code, "invalid_schedule_scope")
        self.assertEqual((PipelineGit(self.repo).head(), self.number), before)

    def test_group_manifest_cannot_hide_an_unauthorized_plan_source(self):
        paths, run = self.group()
        path = f"memory/runs/processes/{run}/manifest.md"
        def omit_source(value):
            value["source"] = next(binding for binding in [value["source"], *value["additional_sources"]]
                                   if binding["path"] == paths[0])
            value["additional_sources"] = []
        self.rewrite(path, omit_source)
        self.configure(paths[:1], run)
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("hidden source opened")):
            with self.assertRaises(CaptureError) as caught:
                plan_daily(self.repo, "manifest-context")
        self.assertIn(caught.exception.code, {"batch_group_scope_mismatch", "batch_scope_mismatch"})

    def test_group_review_tampering_cannot_become_daily_context(self):
        paths, run = self.group()
        _, body = parse_markdown((self.repo / f"memory/runs/processes/{run}/manifest.md").read_bytes())
        proof = json.loads(body)["proofs"][0]
        self.rewrite(proof["review_path"], lambda value: value["output"].update(reasons=["forged review"]))
        self.configure(paths, run)
        before = PipelineGit(self.repo).head(), self.number
        with self.assertRaises(CaptureError):
            plan_daily(self.repo, "manifest-context")
        self.assertEqual((PipelineGit(self.repo).head(), self.number), before)


if __name__ == "__main__":
    unittest.main()
