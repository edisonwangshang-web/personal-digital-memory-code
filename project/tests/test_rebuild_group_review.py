"""Explicit fresh rebuilds retain real group and Review boundaries; artificial inputs."""

from copy import deepcopy
import unittest

import test_decomposed_pipeline as fixture
from pdm.batch_groups import _proofs, publish_final_group, verify_group_publication
from pdm.batching import plan_batches, project_batch_sources, validate_plan
from pdm.contracts import CaptureError
from pdm.decomposed_review import COUNTERS, _hash, _node_identity, verify_decomposed_review
from pdm.pipeline import _candidate_payload, _load, _load_sources
from pdm.rebuild import prepare_rebuild
from pdm.transaction import PipelineGit


class RebuildGroupReviewTests(unittest.TestCase):
    setUp = fixture.DecomposedPipelineTests.setUp
    sources = fixture.DecomposedPipelineTests.sources
    prepare = fixture.DecomposedPipelineTests.prepare
    model = fixture.DecomposedPipelineTests.model
    run_batch = fixture.DecomposedPipelineTests.run_batch
    candidate = fixture.DecomposedPipelineTests.candidate

    def checked_error(self, code, callback):
        before = self.git.head(), self.number
        with self.assertRaises(CaptureError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual((self.git.head(), self.number), before)

    def old_publication(self):
        paths, plan, projection = self.prepare()
        self.git = PipelineGit(self.repo)
        reviewed = self.run_batch(paths, projection)
        self.assertEqual(reviewed["status"], "passed", reviewed)
        old = self.candidate(reviewed)
        published = publish_final_group(self.repo, plan, old["objects"],
                                        [reviewed["run_id"]], source_paths=paths)
        counters = {name: (self.repo / name).read_bytes() for name in self.git.text(
            "ls-tree", "-r", "--name-only", "HEAD", "--", COUNTERS).splitlines()}
        self.assertTrue(counters)
        return paths, published, counters

    def rebuilt(self):
        paths, published, counters = self.old_publication()
        cleanup = prepare_rebuild(self.repo, "artificial-fresh-generation", paths, sorted(published["files"]))
        context = cleanup["rebuild_context"]
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000, rebuild_context=context)
        projection = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=paths)
        reviewed = self.run_batch(paths, projection, rebuild_context=context)
        self.assertEqual(reviewed["status"], "passed", reviewed)
        candidate = self.candidate(reviewed)
        self.assertEqual(candidate["related"], [])
        self.assertEqual(candidate["rebuild_context"], context)
        return paths, plan, reviewed, candidate, context, counters

    def publish_rebuilt(self):
        paths, plan, reviewed, candidate, context, counters = self.rebuilt()
        publication = publish_final_group(self.repo, plan, candidate["objects"],
                                         [reviewed["run_id"]], source_paths=paths)
        return paths, publication, context, counters

    def test_same_paths_fresh_review_publish_and_authority_keep_old_counters(self):
        paths, publication, context, counters = self.publish_rebuilt()
        review = _load((self.repo / publication["review_path"]).read_bytes(), "review")
        candidate = _load((self.repo / review["stage_path"]).read_bytes(), "candidate")
        sources = _load_sources(self.repo, paths, max_sources=256)
        self.assertIn(context, verify_decomposed_review(self.git, candidate, review,
                      _candidate_payload(self.git, candidate, sources), sources))
        for historical in (False, True):
            verified = verify_group_publication(self.repo, publication["run_id"],
                                                source_paths=paths, historical=historical)
            self.assertIn(context, verified["authority_snapshots"])
            self.assertEqual(verified["files"], publication["files"])
        self.assertTrue(all((self.repo / path).read_bytes() == raw for path, raw in counters.items()))
        new_counters = set(self.git.text("ls-tree", "-r", "--name-only", "HEAD", "--", COUNTERS).splitlines())
        self.assertEqual(len(new_counters), 2 * len(counters))

    def test_plan_snapshot_binds_receipt_and_rejects_relabeling(self):
        paths, plan, _, _, context, _ = self.rebuilt()
        self.assertEqual(plan["snapshot"]["rebuild_context"], context)
        changed = deepcopy(plan)
        changed["snapshot"]["rebuild_context"]["sha256"] = "sha256-" + "0" * 64
        self.checked_error("rebuild_context_changed", lambda: validate_plan(self.repo, changed, source_paths=paths))

    def test_group_cannot_drop_generation_from_frozen_plan(self):
        paths, plan, reviewed, _, _, _ = self.rebuilt()
        changed = deepcopy(plan)
        del changed["snapshot"]["rebuild_context"]
        self.checked_error("batch_group_rebuild_context_mismatch", lambda: _proofs(
            self.git, changed, [reviewed["run_id"]], paths, historical=True))

    def test_receipt_removal_breaks_historical_group_proof(self):
        paths, published, context, _ = self.publish_rebuilt()
        with self.git.lock():
            self.git.write_changes({context["path"]: None},
                                   {context["path"]: (self.repo / context["path"]).read_bytes()},
                                   self.git.check(), "fixture: remove rebuild authority")
        for historical in (False, True):
            self.checked_error("rebuild_context_changed", lambda: verify_group_publication(
                self.repo, published["run_id"], source_paths=paths, historical=historical))

    def test_later_delete_and_restore_remain_deleted_for_rebuilt_publication(self):
        paths, published, _, _ = self.publish_rebuilt()
        path = next(iter(published["files"]))
        raw = (self.repo / path).read_bytes()
        with self.git.lock():
            self.git.write_changes({path: None}, {path: raw}, self.git.check(), "fixture: delete new knowledge")
            self.git.write_files({path: raw}, self.git.check(), "fixture: restore identical new knowledge")
        for historical in (False, True):
            self.checked_error("batch_group_file_deleted", lambda: verify_group_publication(
                self.repo, published["run_id"], source_paths=paths, historical=historical))


class RebuildNodeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.unit = {"kind": "object", "object_paths": ["memory/records/example.md"], "fragment_ids": ["fragment-one"]}
        self.plan = {"inventory": [{"id": "example", "path": "memory/records/example.md"}],
                     "source_bindings": [{"source_id": "source-one", "source_version": "version-one", "path": "source.md"}]}
        self.context = {"path": "memory/runs/processes/rebuilds/" + "1" * 64 + ".md",
                        "commit_sha": "2" * 40, "sha256": "sha256-" + "3" * 64}

    def test_legacy_identity_has_exact_original_fields_and_values(self):
        self.assertEqual(_node_identity(self.plan, self.unit), {
            "kind": "object", "targets": ["example"], "source_versions": self.plan["source_bindings"],
            "fragments": ["fragment-one"]})

    def test_explicit_generation_isolates_counter_without_changing_queue_identity(self):
        old = _node_identity(self.plan, self.unit)
        self.plan["rebuild_context"] = deepcopy(self.context)
        new = _node_identity(self.plan, self.unit)
        self.assertNotEqual(_hash(old), _hash(new))
        renamed = {**deepcopy(self.plan), "run_id": "another-run", "plan_id": "another-plan", "versions": {"program": "later"}}
        self.assertEqual(_node_identity(renamed, self.unit), new)
        another = deepcopy(self.plan)
        another["rebuild_context"]["sha256"] = "sha256-" + "4" * 64
        self.assertNotEqual(_node_identity(another, self.unit), new)


if __name__ == "__main__":
    unittest.main()
