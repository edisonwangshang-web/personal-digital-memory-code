"""New feedback schema remains closed across a real decomposed publication."""
import unittest
from pathlib import Path

import test_reuse_refs as reuse_fixture
from pdm.batching import plan_batches, project_batch_sources
from pdm.batch_groups import publish_final_group, verify_group_publication
from pdm.contracts import parse_markdown
from pdm.pipeline import PipelineGit, _read_fixed_candidate, process_sources, publish, resume_decomposed_processing
from pdm.skill_support import resolve_skill_refs


class FeedbackDecomposedTests(unittest.TestCase):
    setUp = reuse_fixture.ReuseReferencePublicationTests.setUp
    model = reuse_fixture.ReuseReferencePublicationTests.model

    def test_new_schema_covers_each_node_and_restores_publication_without_model_calls(self):
        old = process_sources(self.repo, [self.saved['path']], model_call=self.model, skill_bundle=self.old_bundle)
        self.assertEqual(old['status'], 'passed', old)
        publish(self.repo, old['run_id'])
        paths = [self.saved['path']]
        plan = plan_batches(self.repo, paths, max_input_bytes=128000, payload_overhead_bytes=16000)
        projection = project_batch_sources(self.repo, plan, plan['batches'][0]['batch_id'], source_paths=paths)
        self.reuse = True
        self.bundle = resolve_skill_refs('knowledge-extract@10', 'knowledge-review@8')
        start = len(self.calls)
        result = process_sources(self.repo, paths, related_runs=[old['run_id']], model_call=self.model,
            skill_bundle=self.bundle, batch_context=projection['batch_context'], source_views=projection['source_views'],
            review_strategy='decomposed-v1')
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual([role for role, _ in self.calls[start:]], ['extract'] + ['review'] * 4)
        candidate, sources, _, _ = _read_fixed_candidate(PipelineGit(self.repo), result['run_id'])
        self.assertEqual(candidate['reuse_quality_requirements'], [])
        run_root = self.repo / 'memory/runs/processes' / result['run_id']
        for path in run_root.rglob('*.md'):
            with self.subTest(path=str(path.relative_to(run_root))):
                self.assertEqual(parse_markdown(path.read_bytes())[0]['schema_version'], '0.12')
        before = PipelineGit(self.repo).head(), len(self.calls)
        reopened = resume_decomposed_processing(self.repo, result['run_id'], allowed_source_paths=paths, model_call=lambda *a, **k: self.fail('must reuse completed proof'))
        self.assertEqual(reopened['status'], 'passed', reopened)
        self.assertEqual(before, (PipelineGit(self.repo).head(), len(self.calls)))
        saved = publish_final_group(self.repo, plan, candidate['objects'], [result['run_id']], source_paths=paths)
        self.assertEqual(verify_group_publication(self.repo, saved['run_id'], source_paths=paths)['run_id'], saved['run_id'])


if __name__ == '__main__':
    unittest.main()
