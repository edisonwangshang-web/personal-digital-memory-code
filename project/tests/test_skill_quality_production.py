"""Offline integration: frozen Skills follow queues through actual Git publication."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from pdm.capture import capture
from pdm.contracts import CaptureError, parse_markdown
from pdm.production import prepare_production, run_production
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit
import test_production


class SkillQualityProductionTests(unittest.TestCase):
    setUp = test_production.ProductionTests.setUp
    state = test_production.ProductionTests.state
    attempts = test_production.ProductionTests.attempts

    def sources(self, count=1):
        return [capture(self.repo, {"source_key": f"quality:note:{i}", "source_type": "note",
                "acquisition_status": "full_text", "body": f"Artificial learning observation {i}.\n"})["path"]
                for i in range(count)]

    def transport(self, role, payload, timeout=120):
        from quality_fixtures import add_quality, passed_review
        response = test_production.ProductionTests.transport(self, role, payload, timeout)
        if role == "extract":
            response["output"] = add_quality(response["output"], payload["quality_context"])
            response["output"]["reuse_refs"] = []
        else:
            response["output"] = passed_review(payload["required_checks"], payload.get("skill_bundle"))
        return response

    def test_default_new_queue_freezes_bundle_and_publishes_verified_group(self):
        paths = self.sources()
        prepare_production(self.repo, "quality", paths, use_default_skills=True)
        frozen = self.state("quality")["request"]["skill_bundle"]
        self.assertEqual(frozen, resolve_skill_refs())
        # Restart never tries to resolve a possibly changed installed Skill.
        with patch("pdm.skill_support.resolve_skill_refs", side_effect=AssertionError("must use snapshot")):
            reused = prepare_production(self.repo, "quality", paths, use_default_skills=True)
        self.assertTrue(reused["reused"])
        result = run_production(self.repo, "quality", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "review"])
        for _, payload in self.payloads:
            self.assertEqual(payload["skill_bundle"], frozen)
        git = PipelineGit(self.repo)
        meta, _ = parse_markdown(git.read(git.head(),
            "memory/runs/processes/queues/quality/state.md"))
        self.assertEqual(meta["schema_version"], "0.20")
        before = git.head(), self.ledger.path.read_bytes()
        self.assertEqual(run_production(self.repo, "quality", model_call=self.adapter)["status"], "published")
        self.assertEqual((git.head(), self.ledger.path.read_bytes()), before)
        from pdm.production import _save
        changed = self.state("quality")
        changed["request"].pop("skill_bundle")
        _save(git, "memory/runs/processes/queues/quality/state.md", changed, "production_queue", git.head())
        before = git.head(), self.ledger.path.read_bytes()
        with self.assertRaises(CaptureError) as caught:
            run_production(self.repo, "quality", model_call=self.adapter)
        self.assertEqual(caught.exception.code, "queue_publication_skill_mismatch")
        self.assertEqual((git.head(), self.ledger.path.read_bytes()), before)

    def test_source_batches_keep_exact_quality_scope_and_require_full_final_scope(self):
        from pdm.batching import plan_batches, merge_batch_sources
        from pdm.batch_groups import verify_group_publication
        from pdm.pipeline import _candidate_bindings, _read_reviewed
        paths = self.sources(2)
        # Exercise real source-separated planning, projections and processing;
        # only the planner's supported source-count option differs.
        def one_source_batches(*args, **kwargs):
            return plan_batches(*args, **kwargs, max_sources=1)
        with patch("pdm.batching.plan_batches", side_effect=one_source_batches):
            prepared = prepare_production(self.repo, "separate", paths, use_default_skills=True)
        self.assertEqual(prepared["coverage"]["batch_count"], 2)
        git = PipelineGit(self.repo)
        initial_request = deepcopy(self.state("separate")["request"])
        for count in (1, 2):
            result = run_production(self.repo, "separate", model_call=self.adapter, max_steps=1)
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(len(result["completed_batches"]), count)
            self.assertIsNone(result["final_run"])
            run_id = result["last_result"]["run_id"]
            candidate, _, _ = _read_reviewed(git, run_id)
            projected_paths = [source["path"] for source in candidate["source_views"]]
            self.assertEqual(len(projected_paths), 1)
            self.assertEqual([source["path"] for source in _candidate_bindings(candidate)], projected_paths)
            self.assertEqual([source["path"] for source in candidate["quality_context"]["sources"]], projected_paths)
            self.assertEqual(self.state("separate")["request"], initial_request)
            self.assertEqual(set(self.state("separate")["plan"]["source_paths"]), set(paths))
        self.assertEqual(self.roles, ["extract", "review"] * 2)
        self.assertEqual(git.text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                  "memory/records", "memory/topics"), "")
        before = git.head(), self.ledger.path.read_bytes(), len(self.attempts()), self.number
        def omit_final_source(*args, **kwargs):
            projection = merge_batch_sources(*args, **kwargs)
            projection["source_views"] = projection["source_views"][:1]
            return projection
        with patch("pdm.batching.merge_batch_sources", side_effect=omit_final_source):
            with self.assertRaises(CaptureError) as caught:
                run_production(self.repo, "separate", model_call=self.adapter, max_steps=1)
        self.assertEqual(caught.exception.code, "queue_projection_scope_mismatch")
        self.assertEqual((git.head(), self.ledger.path.read_bytes(), len(self.attempts()), self.number), before)
        result = run_production(self.repo, "separate", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "review"] * 3)
        final, _, _ = _read_reviewed(git, result["final_run"])
        self.assertEqual({source["path"] for source in _candidate_bindings(final)}, set(paths))
        self.assertEqual(len(final["merge_inputs"]), 2)
        publication = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=paths)
        self.assertEqual(len(publication["files"]), 4)

    def test_existing_legacy_queue_is_not_silently_upgraded(self):
        paths = self.sources()
        prepare_production(self.repo, "legacy", paths)
        prepare_production(self.repo, "legacy", paths, use_default_skills=True)
        self.assertNotIn("skill_bundle", self.state("legacy")["request"])
        with self.assertRaises(CaptureError) as caught:
            prepare_production(self.repo, "legacy", paths, skill_bundle=resolve_skill_refs())
        self.assertEqual(caught.exception.code, "queue_identity_conflict")

    def test_missing_check_prevents_formal_publication(self):
        paths = self.sources()
        prepare_production(self.repo, "missing", paths, skill_bundle=resolve_skill_refs())
        normal = self.transport
        def omitted(role, payload, timeout=120):
            response = normal(role, payload, timeout)
            if role == "review":
                response["output"]["checks"].pop()
            return response
        from pdm.budget import BudgetedModelCall
        result = run_production(self.repo, "missing",
            model_call=BudgetedModelCall(self.ledger, "worker", omitted), max_steps=1)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                                    "memory/records", "memory/topics"), "")

    def test_unknown_legacy_attempt_cannot_be_replayed_by_new_quality_queue(self):
        paths = self.sources()
        prepare_production(self.repo, "old", paths)
        from pdm.budget import BudgetedModelCall
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            run_production(self.repo, "old", model_call=BudgetedModelCall(self.ledger, "worker", interrupted))
        prepare_production(self.repo, "quality", paths, skill_bundle=resolve_skill_refs())
        before = self.ledger.path.read_bytes()
        result = run_production(self.repo, "quality", model_call=self.adapter)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(len(self.attempts()), 1)

    def test_invalid_bundle_stops_before_any_queue_write(self):
        paths = self.sources()
        bundle = deepcopy(resolve_skill_refs())
        bundle["sha256"] = "sha256-" + "0" * 64
        before = PipelineGit(self.repo).head()
        with self.assertRaises(ValueError):
            prepare_production(self.repo, "bad", paths, skill_bundle=bundle)
        self.assertEqual(PipelineGit(self.repo).head(), before)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 0)


if __name__ == "__main__":
    unittest.main()
