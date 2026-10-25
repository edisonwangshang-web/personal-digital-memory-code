"""Full processing admission must be read-only and precede call reservations."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from quality_fixtures import add_quality, passed_review
from test_pipeline import git
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.pipeline import PipelineGit, _load, preflight_processing, process_sources
from pdm.model_calls import request_binding
from pdm.skill_support import resolve_skill_refs


class ProcessingPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'dev')
        configure_test_repository(self.repo)
        git(self.repo, 'config', 'user.name', 'Artificial Preflight Fixture')
        git(self.repo, 'config', 'user.email', 'preflight@example.invalid')
        git(self.repo, 'commit', '--allow-empty', '-m', 'artificial baseline')
        self.path = capture(self.repo, {'source_key': 'artificial:learning:preflight',
            'source_type': 'note', 'platform': 'artificial', 'original_url': None,
            'acquisition_status': 'full_text', 'original_time': None,
            'body': '人工学习资料：材料按主题组织。\n'})['path']
        self.bundle = resolve_skill_refs('knowledge-extract@10', 'knowledge-review@8')
        self.roles = []
        self.requests = []
        self.ledger = BudgetLedger.create(self.root / 'audit.json', 4, 0,
                                         'artificial preflight only', repo=self.repo)
        self.adapter = BudgetedModelCall(self.ledger, 'artificial:preflight', transport=self.transport)

    def snapshot(self):
        return (git(self.repo, 'rev-parse', 'HEAD'),
                {str(p.relative_to(self.repo)): p.read_bytes() for p in self.repo.rglob('*')
                 if p.is_file() and '.git' not in p.relative_to(self.repo).parts},
                self.ledger.path.read_bytes())

    def transport(self, role, payload, timeout=120):
        self.roles.append(role)
        self.requests.append(copy.deepcopy(payload))
        if role == 'extract':
            source = payload['sources'][0]
            ref = {k: source['metadata'][k] for k in ('source_id', 'source_version')}
            ref.update(locator='L1-L1', quote=source['body'].splitlines()[0])
            record = {'id': 'artificial-preflight-record', 'type': 'knowledge', 'title': '人工学习',
                'source_kind': 'user_statement', 'sources': [ref], 'domains': ['learning'],
                'valid_from': None, 'valid_to': None, 'verification_status': 'supported',
                'lifecycle_status': 'undetermined', 'body': source['body'].strip()}
            output = add_quality({'objects': [record], 'wiki': {'id': 'artificial-preflight-topic',
                'title': '人工主题', 'sources': [ref], 'body': record['body']}, 'reuse_refs': []}, payload['quality_context'])
        else:
            output = passed_review(payload['required_checks'])
        return {'output': output, 'model': 'artificial-fixture', 'usage': None,
                'elapsed_seconds': 0, 'invocation_id': 'model-' + str(len(self.roles)).zfill(32)}

    def test_ready_preflight_matches_actual_request_without_writes(self):
        before = self.snapshot()
        checked = preflight_processing(self.repo, [self.path], related_runs=[], skill_bundle=self.bundle)
        self.assertEqual(checked['status'], 'ready')
        self.assertEqual(checked['model_calls'], 0)
        self.assertEqual(before, self.snapshot())
        result = process_sources(self.repo, [self.path], related_runs=[], skill_bundle=self.bundle,
                                 model_call=self.adapter)
        self.assertEqual(result['status'], 'passed', result)
        candidate = _load((self.repo / result['stage_path']).read_bytes(), 'candidate')
        self.assertEqual(candidate['extraction_request_binding']['bytes'], checked['request_bytes'])
        self.assertEqual(candidate['extraction_request_binding'], request_binding('extract', self.requests[0]))
        self.assertEqual(self.roles, ['extract', 'review'])

    def test_over_limit_preflight_and_direct_processing_never_reserve_or_write(self):
        before = self.snapshot()
        with patch('pdm.model_calls.MAX_INPUT_BYTES', 64):
            checked = preflight_processing(self.repo, [self.path], related_runs=[], skill_bundle=self.bundle)
            self.assertEqual(checked['status'], 'blocked_capacity')
            self.assertEqual(before, self.snapshot())
            with self.assertRaises(CaptureError) as raised:
                process_sources(self.repo, [self.path], related_runs=[], skill_bundle=self.bundle,
                                model_call=self.adapter)
        self.assertEqual(raised.exception.code, 'input_limit')
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.roles, [])
        self.assertEqual(self.ledger.snapshot()['started_calls'], 0)

    def test_pending_transaction_is_reported_without_recovery_side_effects(self):
        journal = PipelineGit(self.repo)._journal_path()
        journal.write_bytes(b'{"artificial":"pending recovery marker"}')
        before = self.snapshot(), journal.read_bytes()
        with self.assertRaises(CaptureError) as raised:
            preflight_processing(self.repo, [self.path], related_runs=[], skill_bundle=self.bundle)
        self.assertEqual(raised.exception.code, 'preflight_recovery_required')
        self.assertEqual(before, (self.snapshot(), journal.read_bytes()))

    def test_preflight_cannot_accept_hidden_transport_or_resume_options(self):
        before = self.snapshot()
        for options in ({'model_call': self.adapter}, {'_preflight_only': False}, {'_resume_unstarted': {}}):
            with self.subTest(options=tuple(options)):
                with self.assertRaises(CaptureError) as raised:
                    preflight_processing(self.repo, [self.path], **options)
                self.assertEqual(raised.exception.code, 'invalid_preflight')
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
