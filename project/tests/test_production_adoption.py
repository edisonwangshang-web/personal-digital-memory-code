"""Adopt exact operator publications without replaying failed queue attempts."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from pdm.batch_groups import _marker_path, publish_final_group
from pdm.batching import merge_batch_sources
from pdm.budget import BudgetedModelCall
from pdm.contracts import CaptureError
from pdm.pipeline import _committed, _load, _read_reviewed, process_sources
from pdm.production import _save, prepare_production, production_status, run_production
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit
import test_production
import test_skill_quality_production


class ProductionAdoptionTests(unittest.TestCase):
    setUp = test_production.ProductionTests.setUp
    sources = test_skill_quality_production.SkillQualityProductionTests.sources
    transport = test_skill_quality_production.SkillQualityProductionTests.transport
    state = test_production.ProductionTests.state
    attempts = test_production.ProductionTests.attempts

    def new_bundle(self):
        return resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")

    def prepare(self, name="adoption", bundle=None, paths=None, **options):
        paths = paths or self.sources()
        prepared = prepare_production(self.repo, name, paths, skill_bundle=bundle or self.new_bundle(), **options)
        self.assertEqual(prepared["status"], "ready", prepared)
        return paths, prepared

    def operator_publication(self, paths, name="adoption", bundle=None):
        state = self.state(name)
        plan = state["plan"]
        projection = merge_batch_sources(self.repo, plan,
            [item["batch_id"] for item in plan["batches"]], source_paths=paths,
            max_bytes=plan["max_input_bytes"])
        adapter = self.adapter.for_operation("artificial-operator:" + name)
        processed = process_sources(self.repo, paths, related_runs=state["related_runs"],
            batch_context=projection["batch_context"], source_views=projection["source_views"],
            review_strategy=state["request"].get("review_strategy", "legacy-v1"),
            skill_bundle=bundle or state["request"]["skill_bundle"], model_call=adapter)
        self.assertEqual(processed["status"], "passed", processed)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), processed["run_id"])
        published = publish_final_group(self.repo, plan, candidate["objects"], [processed["run_id"]], source_paths=paths)
        return processed, published

    def protected_attempt_bytes(self):
        git = PipelineGit(self.repo)
        names = git.text("ls-tree", "-r", "--name-only", "HEAD", "--", "memory/runs/processes/queue-attempts").splitlines()
        return {name: (self.repo / name).read_bytes() for name in names}

    def test_operator_group_adoption_persists_once_and_preserves_three_failed_attempts(self):
        paths, _ = self.prepare("failed")
        failures = [CaptureError("invalid_model_output", "Artificial completed extraction rejected"),
                    CaptureError("invalid_model_output", "Artificial completed extraction rejected"),
                    CaptureError("candidate_too_large", "Artificial completed extraction exceeded storage")]
        with patch("pdm.pipeline._objects", side_effect=failures):
            failed = run_production(self.repo, "failed", model_call=self.adapter, max_steps=3)
        self.assertEqual(failed["status"], "blocked_capacity", failed)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 3)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 3)
        frozen_queue = (self.repo / "memory/runs/processes/queues/failed/state.md").read_bytes()
        counters = self.protected_attempt_bytes()
        _, ready = self.prepare(paths=paths)
        self.assertNotEqual(ready["plan_id"], failed["plan_id"])
        processed, published = self.operator_publication(paths)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number
        old_revision = self.state("adoption")["revision"]
        result = run_production(self.repo, "adoption", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertTrue(result["adopted"])
        self.assertEqual(result["steps"], 0)
        stored = production_status(self.repo, "adoption")
        self.assertEqual(stored["status"], "published")
        self.assertEqual(stored["publication"]["run_id"], published["run_id"])
        self.assertEqual(stored["final_run"], processed["run_id"])
        state = self.state("adoption")
        self.assertEqual(state["revision"], old_revision + 1)
        self.assertTrue(state["last_result"]["adopted"])
        self.assertEqual(state["last_result"]["reviewed_run_ids"], [processed["run_id"]])
        self.assertEqual(state["last_result"]["model_calls"], 0)
        self.assertNotEqual(PipelineGit(self.repo).head(), before[0])
        self.assertEqual((self.ledger.path.read_bytes(), self.number), before[1:])
        self.assertEqual(self.protected_attempt_bytes(), counters)
        self.assertEqual((self.repo / "memory/runs/processes/queues/failed/state.md").read_bytes(), frozen_queue)
        after = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number, (self.repo / ready["state_path"]).read_bytes()
        repeated = run_production(self.repo, "adoption", model_call=self.adapter)
        self.assertEqual(repeated["status"], "published")
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number,
                          (self.repo / ready["state_path"]).read_bytes()), after)

    def test_foreign_plan_marker_cannot_change_raw_queue(self):
        paths, ready = self.prepare()
        self.prepare("foreign", paths=paths, max_input_bytes=100000)
        _, foreign = self.operator_publication(paths, "foreign")
        git = PipelineGit(self.repo)
        target = _marker_path(self.state("adoption")["plan"])
        foreign_marker = _marker_path(self.state("foreign")["plan"])
        with git.lock():
            git.write_files({target: _committed(git, foreign_marker)}, git.check(), "artificial wrong-plan marker")
        before = git.head(), self.ledger.path.read_bytes(), (self.repo / ready["state_path"]).read_bytes(), self.number
        with self.assertRaises(CaptureError) as caught:
            run_production(self.repo, "adoption", model_call=self.adapter)
        self.assertEqual(caught.exception.code, "batch_group_marker_mismatch")
        self.assertEqual((git.head(), self.ledger.path.read_bytes(), (self.repo / ready["state_path"]).read_bytes(), self.number), before)
        self.assertEqual(production_status(self.repo, "adoption")["status"], "ready")

    def test_same_plan_group_with_wrong_frozen_skills_cannot_be_adopted(self):
        paths, ready = self.prepare()
        self.operator_publication(paths, bundle=resolve_skill_refs("knowledge-extract@9", "knowledge-review@7"))
        git = PipelineGit(self.repo)
        before = git.head(), self.ledger.path.read_bytes(), (self.repo / ready["state_path"]).read_bytes(), self.number
        with self.assertRaises(CaptureError) as caught:
            run_production(self.repo, "adoption", model_call=self.adapter)
        self.assertEqual(caught.exception.code, "queue_publication_skill_mismatch")
        self.assertEqual((git.head(), self.ledger.path.read_bytes(), (self.repo / ready["state_path"]).read_bytes(), self.number), before)

    def test_existing_final_candidate_cannot_be_replaced_by_another_operator_run(self):
        paths, ready = self.prepare()
        self.operator_publication(paths)
        git = PipelineGit(self.repo)
        state = deepcopy(self.state("adoption"))
        state["final_run"] = "run-" + "f" * 32
        with git.lock():
            _save(git, ready["state_path"], state, "production_queue", git.check())
        before = git.head(), self.ledger.path.read_bytes(), (self.repo / ready["state_path"]).read_bytes(), self.number
        with self.assertRaises(CaptureError) as caught:
            run_production(self.repo, "adoption", model_call=self.adapter)
        self.assertEqual(caught.exception.code, "queue_publication_run_mismatch")
        self.assertEqual((git.head(), self.ledger.path.read_bytes(), (self.repo / ready["state_path"]).read_bytes(), self.number), before)

    def test_old_skill_queue_retains_effective_only_adoption(self):
        paths, ready = self.prepare(bundle=resolve_skill_refs("knowledge-extract@9", "knowledge-review@7"))
        _, published = self.operator_publication(paths)
        git = PipelineGit(self.repo)
        before = git.head(), self.ledger.path.read_bytes(), (self.repo / ready["state_path"]).read_bytes(), self.number
        result = run_production(self.repo, "adoption", model_call=self.adapter)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["publication"]["run_id"], published["run_id"])
        self.assertEqual(result["queue_status"], "ready")
        self.assertEqual(production_status(self.repo, "adoption")["status"], "ready")
        self.assertEqual((git.head(), self.ledger.path.read_bytes(), (self.repo / ready["state_path"]).read_bytes(), self.number), before)


if __name__ == "__main__":
    unittest.main()
