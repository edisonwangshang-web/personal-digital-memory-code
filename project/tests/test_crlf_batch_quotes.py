"""Real capture/projection/publication over artificial, byte-preserved CRLF CSV.

The fake transport exercises the single-line quoting strategy supplied by the
extraction rules. It does not claim that a live model will follow those rules.
"""

from copy import deepcopy
import unittest

import test_pipeline
from pdm.batching import plan_batches, project_batch_sources
from pdm.capture import capture
from pdm.contracts import CaptureError, parse_markdown
from pdm.pipeline import load_source, process_sources, publish, verify_publication
from pdm.transaction import PipelineGit


BODY = ('日期,步数,说明\r\n'
        '单位,步,仅人工样例\r\n'
        '2026-10-01,1234," 保留 空格\t与,标点 "\r\n'
        '2026-10-02,2345,未完整佩戴\r\n')


class CrlfBatchQuoteTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp

    def prepare_crlf(self):
        saved = capture(self.repo, {'source_key': 'fixture:crlf:monitoring.csv',
            'source_type': 'note', 'acquisition_status': 'full_text', 'body': BODY})
        self.path = saved['path']
        self.source = load_source(self.repo, self.path)
        self.assertEqual(self.source['body'], BODY)
        self.source_bytes = (self.repo / self.path).read_bytes()
        self.assertTrue(self.source_bytes.endswith(BODY.encode('utf-8')))
        self.capture_bytes = {str(path.relative_to(self.repo)): path.read_bytes()
            for path in (self.repo / 'memory/runs/captures').rglob('*.md')}
        plan = plan_batches(self.repo, [self.path], max_input_bytes=100_000)
        self.assertEqual(plan['status'], 'ready')
        self.assertTrue(plan['coverage']['complete'])
        self.projection = project_batch_sources(self.repo, plan, plan['batches'][0]['batch_id'],
                                               source_paths=[self.path], max_bytes=100_000)
        self.assertTrue(self.projection['coverage']['complete'])
        self.lines = {line['line']: line['text'] for view in self.projection['source_views']
                      for fragment in view['fragments'] for line in fragment['lines']}
        self.assertEqual(self.lines, dict(enumerate(BODY.splitlines(), 1)))
        self.assertNotIn('\r', ''.join(self.lines.values()))
        self.extraction_quote = None
        self.locator = None

    def model(self, role, payload):
        self.number += 1
        self.roles.append(role)
        self.contexts.append(deepcopy(payload))
        if role == 'extract':
            view = payload['sources'][0]
            refs = [{'source_id': view['metadata']['source_id'],
                     'source_version': view['metadata']['source_version'],
                     'locator': self.locator or f'L{number}-L{number}',
                     'quote': self.extraction_quote if self.extraction_quote is not None else self.lines[number]}
                    for number in (3, 4)]
            output = {'objects': [{'id': 'artificial-crlf-monitoring', 'type': 'knowledge',
                'title': '人工监控样例', 'source_kind': 'external_source', 'sources': refs,
                'domains': ['health'], 'valid_from': None, 'valid_to': None,
                'verification_status': 'unverified', 'lifecycle_status': 'undetermined',
                'body': '人工样例记录两行数据，其中第二行注明未完整佩戴。'}],
                'wikis': [{'id': 'artificial-crlf-topic', 'title': '人工监控主题',
                    'body': '只是人工CSV测试，保留未完整佩戴的限定。', 'sources': refs,
                    'record_ids': ['artificial-crlf-monitoring']}]}
        else:
            output = {'verdict': 'passed', 'reasons': ['人工样例逐行引文与完整原文精确匹配。']}
        return {'output': output, 'model': 'artificial-crlf', 'usage': None,
                'elapsed_seconds': 0.01, 'invocation_id': f'model-{self.number:032x}'}

    def run_batch(self):
        return process_sources(self.repo, [self.path], related_runs=[], model_call=self.model,
            batch_context=self.projection['batch_context'], source_views=self.projection['source_views'],
            review_strategy='decomposed-v1')

    def assert_original_bytes(self):
        self.assertEqual((self.repo / self.path).read_bytes(), self.source_bytes)
        self.assertEqual(load_source(self.repo, self.path)['body'], BODY)
        self.assertEqual({str(path.relative_to(self.repo)): path.read_bytes()
            for path in (self.repo / 'memory/runs/captures').rglob('*.md')}, self.capture_bytes)

    def test_single_line_global_quotes_publish_without_normalizing_crlf_source(self):
        self.prepare_crlf()
        result = self.run_batch()
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(self.roles.count('extract'), 1)
        self.assertEqual(self.roles.count('review'), 4)
        self.assert_original_bytes()
        publication = publish(self.repo, result['run_id'], result['head'])
        for historical in (False, True):
            checked = verify_publication(self.repo, result['run_id'], source_paths=[self.path], historical=historical)
            self.assertEqual(checked['commit_sha'], publication['commit_sha'])
        metadata, _ = parse_markdown((self.repo / 'memory/records/artificial-crlf-monitoring.md').read_bytes())
        self.assertEqual([ref['locator'] for ref in metadata['sources']], ['L3-L3', 'L4-L4'])
        self.assertEqual([ref['quote'] for ref in metadata['sources']], [self.lines[3], self.lines[4]])
        self.assertIn('\t', metadata['sources'][0]['quote'])
        self.assertIn('" 保留 空格', metadata['sources'][0]['quote'])
        self.assert_original_bytes()
        self.assertEqual(PipelineGit(self.repo).text('status', '--porcelain'), '')

    def test_lf_joined_multiline_quote_in_crlf_source_is_still_rejected(self):
        self.prepare_crlf()
        self.extraction_quote = self.lines[3] + '\n' + self.lines[4]
        self.locator = 'L3-L4'
        self.assertNotIn(self.extraction_quote, BODY)
        result = self.run_batch()
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['error'], 'invalid_quote')
        self.assertEqual(self.roles, ['extract'])
        self.assertFalse((self.repo / 'memory/records').exists())
        self.assertFalse((self.repo / 'memory/topics').exists())
        self.assertFalse((self.repo / f"memory/staging/{result['run_id']}.md").exists())
        with self.assertRaises(CaptureError):
            publish(self.repo, result['run_id'])
        self.assert_original_bytes()

    def test_actual_crlf_multiline_excerpt_remains_exactly_compatible(self):
        # The validator retains its exact-source contract. The new rule chooses
        # single-line references because projection omits original separators;
        # it does not change or weaken the underlying quote validator.
        self.prepare_crlf()
        self.extraction_quote = self.lines[3] + '\r\n' + self.lines[4]
        self.locator = 'L3-L4'
        result = self.run_batch()
        self.assertEqual(result['status'], 'passed', result)
        publication = publish(self.repo, result['run_id'], result['head'])
        self.assertEqual(verify_publication(self.repo, result['run_id'], source_paths=[self.path])['commit_sha'],
                         publication['commit_sha'])
        self.assert_original_bytes()


if __name__ == '__main__':
    unittest.main()
