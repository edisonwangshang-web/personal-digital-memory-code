"""Offline model transport tests using a disposable, artificial CLI process.

No test contacts a model, reads sign-in files or accesses personal memory.
"""
import json
import os
from contextlib import contextmanager
from pathlib import Path
import stat
import sys
import tempfile
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from pdm import model_calls
from pdm.model_calls import ModelCallError
from pdm.budget import BudgetBlocked, BudgetLedger
import uuid


ANSWER = {'answer': '隔离样例', 'citations': [], 'limitations': []}
REVIEW = {'verdict': 'passed', 'reasons': ['人工样例，与真实审核无关。']}


class ModelCallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pdm-model-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.transcript = self.root / 'transcript.json'
        self.executable = self.root / 'codex-fixture'
        self.ledger = BudgetLedger.create(self.root / 'budget.json', 1000, 0, 'synthetic-test')

    def call(self, role, payload, **kwargs):
        return model_calls.call_model(role, payload, ledger=self.ledger,
                                      operation_id='test-' + uuid.uuid4().hex, **kwargs)

    def test_default_live_call_requires_budget_before_process(self):
        with patch.object(model_calls.subprocess, 'Popen') as start:
            for options in ({}, {'ledger': self.ledger}, {'operation_id': 'op'}):
                with self.assertRaises(BudgetBlocked) as raised:
                    model_calls.call_model('answer', {}, **options)
                self.assertEqual('budget_binding_required', raised.exception.code)
            start.assert_not_called()
        self.assertEqual(0, self.ledger.snapshot()['started_calls'])

    def test_validation_and_missing_cli_do_not_record_a_start(self):
        with patch.object(model_calls.shutil, 'which', return_value=None):
            with self.assertRaises(ModelCallError):
                self.call('answer', {})
        with self.cli(), self.assertRaises(ModelCallError):
            self.call('answer', {'fixture': 'x' * model_calls.MAX_INPUT_BYTES})
        self.assertEqual(0, self.ledger.snapshot()['started_calls'])

    def test_exhausted_budget_never_starts_cli(self):
        exhausted = BudgetLedger.create(self.root / 'exhausted.json', 18, 18, 'synthetic-exhausted')
        with self.cli(), patch.object(model_calls.subprocess, 'Popen') as start:
            with self.assertRaises(BudgetBlocked) as raised:
                model_calls.call_model('answer', {}, ledger=exhausted, operation_id='next')
            self.assertEqual('budget_exhausted', raised.exception.code)
            start.assert_not_called()

    def test_live_launch_boundary_records_start_and_does_not_replay(self):
        with self.cli():
            result = model_calls.call_model('answer', {}, ledger=self.ledger, operation_id='once')
        state = self.ledger.operation('once')
        self.assertEqual(1, state['started_calls'])
        self.assertEqual('completed', state['calls']['answer']['status'])
        self.assertEqual(result['invocation_id'], state['calls']['answer']['invocation_id'])
        with self.cli(), patch.object(model_calls.subprocess, 'Popen') as start:
            with self.assertRaises(BudgetBlocked):
                model_calls.call_model('answer', {}, ledger=self.ledger, operation_id='once')
            start.assert_not_called()

    def cli(self, behavior=None, output=ANSWER, model=None):
        metadata = {'type': 'thread.started', 'thread_id': 'fixture-thread'}
        if model is not None:
            metadata['model'] = model
        events = [metadata,
                  {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message',
                    'text': json.dumps(output, ensure_ascii=False)}},
                  {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 2}}]
        source = '\n'.join([
            '#!' + sys.executable,
            'import json, os, pathlib, sys, time',
            'prompt = sys.stdin.read()',
            'schema = pathlib.Path(sys.argv[sys.argv.index("--output-schema") + 1])',
            'transcript = ' + repr(str(self.transcript)),
            'pathlib.Path(transcript).write_text(json.dumps({"args":sys.argv[1:],',
            ' "cwd":os.getcwd(),"files":os.listdir(),"env":dict(os.environ),',
            ' "schema":json.loads(schema.read_text()),"prompt":prompt}))',
            behavior if behavior is not None else
            'print("\\n".join(json.dumps(x, ensure_ascii=False) for x in ' + repr(events) + '))',
            '',
        ])
        self.executable.write_text(source, encoding='utf-8')
        self.executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return patch.object(model_calls.shutil, 'which', return_value=str(self.executable))

    def event(self, kind, **values):
        return 'print(' + repr(json.dumps(dict(type=kind, **values))) + ')'

    def error(self, code, behavior, **kwargs):
        with self.cli(behavior):
            with self.assertRaises(ModelCallError) as raised:
                self.call('answer', {'fixture': 'PRIVATE_FIXTURE_ONLY'}, **kwargs)
        self.assertEqual(code, raised.exception.code)
        self.assertNotIn('PRIVATE_FIXTURE_ONLY', str(raised.exception))
        self.assertRegex(raised.exception.invocation_id, r'^model-[0-9a-f]{32}$')
        return raised.exception

    def test_isolated_empty_cwd_stdin_schema_and_disabled_capabilities(self):
        source_id = 'src-' + 'a' * 64
        source_version = 'sha256-' + 'b' * 64
        with self.cli():
            result = self.call('answer', {'source_id': source_id, 'source_version': source_version})
        captured = json.loads(self.transcript.read_text())
        self.assertEqual([], captured['files'])
        self.assertEqual('empty-workspace', Path(captured['cwd']).name)
        self.assertFalse(Path(captured['cwd']).exists())
        self.assertIn(source_id, captured['prompt'])
        self.assertIn(source_version, captured['prompt'])
        self.assertEqual(model_calls.OUTPUT_SCHEMAS['answer'], captured['schema'])
        args = captured['args']
        self.assertEqual('exec', args[0])
        self.assertEqual('-', args[-1])
        self.assertNotIn('resume', args)
        for flag in ('--ephemeral', '--ignore-user-config', '--skip-git-repo-check', '--json', '--strict-config'):
            self.assertIn(flag, args)
        self.assertEqual('read-only', args[args.index('--sandbox') + 1])
        disabled = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == '--disable']
        self.assertEqual({'shell_tool', 'apps', 'plugins', 'hooks', 'multi_agent', 'memories',
                          'shell_snapshot', 'remote_plugin', 'request_permissions_tool'}, set(disabled))
        configs = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == '-c']
        self.assertIn('approval_policy="never"', configs)
        self.assertIn('web_search="disabled"', configs)
        self.assertIn('project_doc_max_bytes=0', configs)
        self.assertIn('skills.bundled.enabled=false', configs)
        self.assertIn('skills.include_instructions=false', configs)
        self.assertEqual('unknown', result['model'])
        self.assertEqual({'input_tokens': 1, 'output_tokens': 2}, result['usage'])
        self.assertEqual(ANSWER, result['output'])

    def test_independent_extract_and_review_calls_have_distinct_workspaces(self):
        with self.cli(output={'objects': [], 'wiki': {'id': '', 'title': '', 'body': '', 'sources': []}}):
            first = self.call('extract', {'fixture': 'first-only'})
        first_input = json.loads(self.transcript.read_text())
        with self.cli(output=REVIEW):
            second = self.call('review', {'fixture': 'second-only'})
        second_input = json.loads(self.transcript.read_text())
        self.assertNotEqual(first['invocation_id'], second['invocation_id'])
        self.assertNotEqual(first_input['cwd'], second_input['cwd'])
        self.assertNotIn('first-only', second_input['prompt'])
        self.assertIn('本次调用不继承加工会话', second_input['prompt'])

    def test_environment_omits_api_keys_and_thread_context(self):
        with self.cli(), patch.dict(os.environ, {'OPENAI_API_KEY': 'fixture-secret',
                'CODEX_THREAD_ID': 'fixture-thread', 'GIT_DIR': '/fixture/repository',
                'PDM_PRIVATE_FIXTURE': 'private-value'}):
            self.call('answer', {})
        env = json.loads(self.transcript.read_text())['env']
        for key in ('OPENAI_API_KEY', 'CODEX_THREAD_ID', 'GIT_DIR', 'PDM_PRIVATE_FIXTURE'):
            self.assertNotIn(key, env)
        self.assertEqual(os.environ.get('HOME'), env.get('HOME'))

    def test_actual_model_metadata_is_recorded_only_when_present(self):
        with self.cli(model='fixture-model-actual'):
            result = self.call('answer', {})
        self.assertEqual('fixture-model-actual', result['model'])
        with self.cli(model='PRIVATE FIXTURE\nunsafe'):
            result = self.call('answer', {})
        self.assertEqual('unknown', result['model'])

    def test_usage_keeps_supported_counts_without_echoing_unknown_fields(self):
        from pdm.pipeline import _call_metadata

        known = {'input_tokens': 21, 'output_tokens': 13, 'cached_input_tokens': 0,
                 'total_tokens': 34, 'reasoning_output_tokens': 5}
        usage = {**known, 'PRIVATE_NUMERIC_USAGE_KEY_FIXTURE': 987,
                 'PRIVATE_TEXT_USAGE_KEY_FIXTURE': 'PRIVATE_USAGE_VALUE_FIXTURE'}
        behavior = '\n'.join([
            self.event('item.completed', item={'type': 'agent_message', 'text': json.dumps(ANSWER)}),
            self.event('turn.completed', usage=usage),
        ])
        with self.cli(behavior):
            result = self.call('answer', {})
        self.assertEqual(known, result['usage'])
        self.assertEqual(known, _call_metadata(result)['usage'])
        self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_absent_unsupported_or_invalid_usage_is_not_invented(self):
        for usage in (None, {}, {'PRIVATE_NUMERIC_USAGE_KEY_FIXTURE': 123},
                      {'input_tokens': True, 'output_tokens': -1,
                       'cached_input_tokens': 1.5, 'total_tokens': '17'}):
            with self.subTest(usage=usage):
                behavior = '\n'.join([
                    self.event('item.completed', item={'type': 'agent_message', 'text': json.dumps(ANSWER)}),
                    self.event('turn.completed', usage=usage),
                ])
                with self.cli(behavior):
                    result = self.call('answer', {})
                self.assertIsNone(result['usage'])
                self.assertNotIn('PRIVATE_', json.dumps(result))

    def test_invalid_request_and_timeouts_do_not_start_cli(self):
        with patch.object(model_calls.subprocess, 'Popen') as start:
            for role, payload in (('unknown', {}), ([], {}), ('answer', [])):
                with self.subTest(role=role):
                    with self.assertRaises(ModelCallError) as error:
                        self.call(role, payload)
                    self.assertEqual('invalid_request', error.exception.code)
            for timeout in (0, model_calls.MAX_TIMEOUT_SECONDS + 1, 0.5, True, None):
                with self.subTest(timeout=timeout):
                    with self.assertRaises(ModelCallError) as error:
                        self.call('answer', {}, timeout=timeout)
                    self.assertEqual('invalid_timeout', error.exception.code)
            start.assert_not_called()

    def test_explicit_longer_bounded_timeout_accepts_structured_output(self):
        with self.cli():
            result = self.call('answer', {}, timeout=1800)
        self.assertEqual('unknown', result['model'])
        self.assertIn('answer', result['output'])

    def test_missing_cli_safe_error(self):
        with patch.object(model_calls.shutil, 'which', return_value=None):
            with self.assertRaises(ModelCallError) as error:
                self.call('answer', {})
        self.assertEqual('codex_not_found', error.exception.code)

    def test_oversized_input_is_rejected_without_process(self):
        with self.cli(), patch.object(model_calls.subprocess, 'Popen') as start:
            with self.assertRaises(ModelCallError) as error:
                self.call('answer', {'fixture': 'x' * model_calls.MAX_INPUT_BYTES})
            self.assertEqual('input_limit', error.exception.code)
            start.assert_not_called()

    def test_invalid_payload_is_rejected_without_leaking_value(self):
        with self.cli():
            for payload in ({'value': float('nan')}, {'value': object()}, {'value': '\ud800'}):
                with self.subTest(payload_type=type(payload['value']).__name__):
                    with self.assertRaises(ModelCallError) as error:
                        self.call('answer', payload)
                    self.assertEqual('invalid_payload_or_prompt', error.exception.code)

    def test_timeout_kills_cli_and_returns_safe_code(self):
        original = model_calls.subprocess.Popen
        children = []
        def start(*args, **kwargs):
            process = original(*args, **kwargs)
            children.append(process)
            return process
        with patch.object(model_calls.subprocess, 'Popen', side_effect=start):
            error = self.error('timeout', 'time.sleep(30)', timeout=1)
        self.assertLess(error.elapsed_seconds, 5)
        self.assertEqual(1, len(children))
        self.assertIsNotNone(children[0].poll())

    def test_final_kill_waits_until_local_child_is_stopped(self):
        process = Mock(pid=31415)
        process.poll.side_effect = [None, -9]
        process.wait.side_effect = [model_calls.subprocess.TimeoutExpired('artificial-cli', 5), -9]
        with patch.object(model_calls.os, 'killpg', create=True) as group_kill:
            model_calls._stop(process)
        self.assertEqual([unittest.mock.call(timeout=5), unittest.mock.call(timeout=5)],
                         process.wait.call_args_list)
        self.assertEqual(1 if os.name == 'posix' else 2, process.kill.call_count)
        if os.name == 'posix':
            group_kill.assert_called_once_with(process.pid, model_calls.signal.SIGKILL)

    def test_final_kill_without_confirmed_exit_is_not_accepted(self):
        process = Mock(pid=31415)
        process.poll.return_value = None
        process.wait.side_effect = model_calls.subprocess.TimeoutExpired('artificial-cli', 5)
        with patch.object(model_calls.os, 'killpg', create=True):
            with self.assertRaises(model_calls.subprocess.TimeoutExpired):
                model_calls._stop(process)
        self.assertEqual(2, process.wait.call_count)

    def test_output_limit_counts_stderr_without_exposing_it(self):
        with patch.object(model_calls, 'MAX_STREAM_BYTES', 100):
            self.error('output_limit', 'sys.stderr.write("PRIVATE_FIXTURE_ONLY" * 1000)')

    def test_rejects_all_tool_and_unknown_items(self):
        for item_type in ('command_execution', 'mcp_tool_call', 'web_search', 'file_change', 'future-item'):
            with self.subTest(item_type=item_type):
                self.error('tool_or_unsupported_item', self.event('item.started', item={'type': item_type}))

    def test_reasoning_is_never_in_result(self):
        behavior = '\n'.join([
            self.event('item.completed', item={'type': 'reasoning', 'text': 'PRIVATE_REASONING_FIXTURE'}),
            self.event('item.completed', item={'type': 'agent_message', 'text': json.dumps(ANSWER)}),
            self.event('turn.completed'),
        ])
        with self.cli(behavior):
            result = self.call('answer', {})
        self.assertNotIn('PRIVATE_REASONING_FIXTURE', json.dumps(result))
        self.assertIsNone(result['usage'])

    def test_invalid_event_json_and_shape(self):
        for behavior, code in (("print('not-json')", 'invalid_event_json'),
                ("print('[]')", 'invalid_event_shape'),
                ("print('{}')", 'invalid_event_shape'),
                ("print('{\"type\":\"turn.started\",\"type\":\"turn.completed\"}')", 'invalid_event_json'),
                ("print('{\"type\":\"turn.started\",\"value\":NaN}')", 'invalid_event_json'),
                ("print('[' * 2000 + ']' * 2000)", 'invalid_event_json')):
            with self.subTest(code=code, behavior=behavior):
                self.error(code, behavior)

    def test_failed_turn_and_nonzero_exit_are_safe(self):
        self.error('cli_turn_failed', self.event('turn.failed', error={'message': 'PRIVATE_FIXTURE_ONLY'}))
        self.error('cli_turn_failed', self.event('error', message='PRIVATE_FIXTURE_ONLY'))
        self.error('cli_exit_7', 'sys.stderr.write("PRIVATE_FIXTURE_ONLY"); sys.exit(7)')

    def test_unknown_top_level_event_is_rejected(self):
        self.error('unsupported_event', self.event('future.event'))

    def test_missing_or_duplicate_completion_is_rejected(self):
        self.error('missing_final_output', self.event('turn.completed'))
        behavior = '\n'.join([
            self.event('item.completed', item={'type': 'agent_message', 'text': json.dumps(ANSWER)}),
            self.event('turn.completed'), self.event('turn.completed')])
        self.error('missing_final_output', behavior)

    def test_output_json_must_be_unambiguous(self):
        for value in ('not-json', '{"answer":"a","answer":"b","citations":[],"limitations":[]}',
                      '[' * 2000 + ']' * 2000):
            with self.subTest(value=value[:20]):
                behavior = '\n'.join([self.event('item.completed', item={'type': 'agent_message', 'text': value}),
                                      self.event('turn.completed')])
                self.error('invalid_output_json', behavior)

    def test_output_schema_rejects_unrequested_fields_and_invalid_citations(self):
        for output in (dict(ANSWER, unexpected='PRIVATE_FIXTURE_ONLY'),
                       dict(ANSWER, citations=[{'source_id': 'src-fixture', 'source_version': 'sha256-fixture',
                                               'locator': 'L1'}]),
                       {'verdict': 'passed', 'reasons': []}):
            with self.subTest(output=output):
                with self.cli(output=output):
                    with self.assertRaises(ModelCallError) as error:
                        self.call('answer', {})
                    self.assertEqual('invalid_output_schema', error.exception.code)

    def test_cli_io_error_does_not_echo_os_diagnostic(self):
        with self.cli(), patch.object(model_calls.subprocess, 'Popen', side_effect=OSError('PRIVATE_FIXTURE_ONLY')):
            with self.assertRaises(ModelCallError) as error:
                self.call('answer', {})
        self.assertEqual('cli_io_error', error.exception.code)
        self.assertNotIn('PRIVATE_FIXTURE_ONLY', str(error.exception))

    @contextmanager
    def mocked_process(self):
        pipes = [Mock(closed=False) for _ in range(3)]
        for descriptor, pipe in enumerate(pipes, 40):
            pipe.fileno.return_value = descriptor
        process = Mock(stdin=pipes[0], stdout=pipes[1], stderr=pipes[2])
        process.wait.return_value = 0
        selector = Mock()
        selector.get_map.side_effect = [True, False]
        selector.select.return_value = [(SimpleNamespace(data='stdout', fd=41,
                                                         fileobj=pipes[1]), 1)]
        events = [{'type': 'item.completed', 'item': {'type': 'agent_message',
                   'text': json.dumps(ANSWER)}}, {'type': 'turn.completed'}]
        stream = ('\n'.join(json.dumps(event) for event in events) + '\n').encode()
        with patch.object(model_calls.shutil, 'which', return_value='/synthetic/codex'), \
                patch.object(model_calls.subprocess, 'Popen', return_value=process) as popen, \
                patch.object(model_calls.selectors, 'DefaultSelector', return_value=selector) as factory, \
                patch.object(model_calls.os, 'set_blocking') as blocking, \
                patch.object(model_calls.os, 'read', return_value=stream), \
                patch.object(model_calls, '_stop') as stop:
            yield SimpleNamespace(process=process, pipes=pipes, selector=selector,
                                  popen=popen, factory=factory, blocking=blocking, stop=stop)

    def assert_cleanup_attempted(self, mocked, *, selector_created=True):
        mocked.stop.assert_called_once_with(mocked.process)
        if selector_created:
            mocked.selector.close.assert_called_once_with()
        else:
            mocked.selector.close.assert_not_called()
        for pipe in mocked.pipes:
            pipe.close.assert_called_once_with()

    def assert_unknown_operation(self, operation_id):
        state = self.ledger.operation(operation_id)
        self.assertEqual(1, state['reserved_calls'])
        self.assertEqual(1, state['started_calls'])
        self.assertEqual('unknown', state['calls']['answer']['status'])
        self.assertNotIn('PRIVATE_', json.dumps(state))

    def test_mocked_valid_output_cleans_all_resources(self):
        with self.mocked_process() as mocked:
            result = model_calls.call_model('answer', {}, ledger=self.ledger,
                                            operation_id='cleanup-success')
        self.assertEqual(ANSWER, result['output'])
        self.assert_cleanup_attempted(mocked)
        self.assertEqual('completed', self.ledger.operation('cleanup-success')['calls']['answer']['status'])

    def test_partial_selector_initialization_failure_always_cleans_child_and_pipes(self):
        stages = [('construct', 0), *[('blocking', index) for index in range(3)],
                  *[('register', index) for index in range(3)]]
        for stage, index in stages:
            with self.subTest(stage=stage, index=index), self.mocked_process() as mocked:
                failure = OSError('PRIVATE_SETUP_DIAGNOSTIC_FIXTURE')
                if stage == 'construct':
                    mocked.factory.side_effect = failure
                elif stage == 'blocking':
                    mocked.blocking.side_effect = [None] * index + [failure]
                else:
                    mocked.selector.register.side_effect = [None] * index + [failure]
                operation = 'setup-' + stage + '-' + str(index)
                with self.assertRaises(ModelCallError) as raised:
                    model_calls.call_model('answer', {}, ledger=self.ledger, operation_id=operation)
                self.assertEqual('cli_io_error', raised.exception.code)
                self.assertNotIn('PRIVATE_', str(raised.exception))
                self.assertNotIn('PRIVATE_', ''.join(traceback.format_exception(raised.exception)))
                self.assert_cleanup_attempted(mocked, selector_created=stage != 'construct')
                self.assert_unknown_operation(operation)
                with self.assertRaises(BudgetBlocked):
                    model_calls.call_model('answer', {}, ledger=self.ledger, operation_id=operation)
                mocked.popen.assert_called_once()

    def test_cleanup_failures_attempt_remaining_resources_and_never_return_answer(self):
        for stage in ('selector', 'stop', 'stdin', 'stdout', 'stderr', 'all'):
            with self.subTest(stage=stage), self.mocked_process() as mocked:
                actions = {'selector': mocked.selector.close, 'stop': mocked.stop,
                           'stdin': mocked.pipes[0].close, 'stdout': mocked.pipes[1].close,
                           'stderr': mocked.pipes[2].close}
                for name, action in actions.items():
                    if stage in (name, 'all'):
                        action.side_effect = OSError('PRIVATE_CLEANUP_DIAGNOSTIC_FIXTURE')
                operation = 'cleanup-failure-' + stage
                with self.assertRaises(ModelCallError) as raised:
                    model_calls.call_model('answer', {}, ledger=self.ledger, operation_id=operation)
                self.assertEqual('cli_cleanup_error', raised.exception.code)
                self.assertNotIn('PRIVATE_', str(raised.exception))
                self.assertNotIn('PRIVATE_', ''.join(traceback.format_exception(raised.exception)))
                self.assert_cleanup_attempted(mocked)
                self.assert_unknown_operation(operation)

    def test_primary_safe_failure_survives_cleanup_errors(self):
        with self.mocked_process() as mocked:
            primary = ModelCallError('cli_permission_denied', 'model-' + 'a' * 32)
            mocked.selector.select.side_effect = primary
            for action in (mocked.selector.close, mocked.stop,
                           *[pipe.close for pipe in mocked.pipes]):
                action.side_effect = OSError('PRIVATE_CLEANUP_DIAGNOSTIC_FIXTURE')
            with self.assertRaises(ModelCallError) as raised:
                model_calls.call_model('answer', {}, ledger=self.ledger,
                                        operation_id='primary-failure')
        self.assertIs(primary, raised.exception)
        self.assert_cleanup_attempted(mocked)
        self.assert_unknown_operation('primary-failure')

    def test_timeout_with_confirmed_cleanup_retains_original_error(self):
        with self.mocked_process() as mocked:
            primary = ModelCallError('timeout', 'model-' + 'a' * 32)
            mocked.selector.select.side_effect = primary
            with self.assertRaises(ModelCallError) as raised:
                model_calls.call_model('answer', {}, ledger=self.ledger,
                                       operation_id='timeout-stopped')
        self.assertIs(primary, raised.exception)
        self.assert_cleanup_attempted(mocked)
        self.assert_unknown_operation('timeout-stopped')
        self.assertEqual('timeout', self.ledger.operation('timeout-stopped')['calls']['answer']['error_code'])

    def test_timeout_with_unconfirmed_cleanup_is_not_retryable_timeout(self):
        for stage in ('selector', 'stop', 'stdin', 'stdout', 'stderr', 'all'):
            with self.subTest(stage=stage), self.mocked_process() as mocked:
                primary = ModelCallError('timeout', 'model-' + 'a' * 32)
                mocked.selector.select.side_effect = primary
                actions = {'selector': mocked.selector.close, 'stop': mocked.stop,
                           'stdin': mocked.pipes[0].close, 'stdout': mocked.pipes[1].close,
                           'stderr': mocked.pipes[2].close}
                for name, action in actions.items():
                    if stage in (name, 'all'):
                        action.side_effect = OSError('PRIVATE_CLEANUP_DIAGNOSTIC_FIXTURE')
                operation = 'timeout-cleanup-' + stage
                with self.assertRaises(ModelCallError) as raised:
                    model_calls.call_model('answer', {}, ledger=self.ledger,
                                           operation_id=operation)
                self.assertEqual('timeout_cleanup_unknown', raised.exception.code)
                self.assertEqual(primary.invocation_id, raised.exception.invocation_id)
                self.assertNotIn('PRIVATE_', ''.join(traceback.format_exception(raised.exception)))
                self.assert_cleanup_attempted(mocked)
                self.assert_unknown_operation(operation)
                self.assertEqual('timeout_cleanup_unknown',
                                 self.ledger.operation(operation)['calls']['answer']['error_code'])

    def test_timeout_cleanup_cancellation_does_not_become_retryable(self):
        with self.mocked_process() as mocked:
            mocked.selector.select.side_effect = ModelCallError('timeout', 'model-' + 'a' * 32)
            mocked.stop.side_effect = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                model_calls.call_model('answer', {}, ledger=self.ledger,
                                       operation_id='timeout-cleanup-cancelled')
        self.assert_cleanup_attempted(mocked)
        self.assert_unknown_operation('timeout-cleanup-cancelled')
        self.assertEqual('transport_outcome_unknown',
                         self.ledger.operation('timeout-cleanup-cancelled')['calls']['answer']['error_code'])

    def test_primary_cancellation_survives_cleanup_errors(self):
        with self.mocked_process() as mocked:
            primary = KeyboardInterrupt()
            mocked.selector.select.side_effect = primary
            mocked.stop.side_effect = OSError('PRIVATE_CLEANUP_DIAGNOSTIC_FIXTURE')
            with self.assertRaises(KeyboardInterrupt) as raised:
                model_calls.call_model('answer', {}, ledger=self.ledger,
                                       operation_id='primary-cancelled')
        self.assertIs(primary, raised.exception)
        self.assert_cleanup_attempted(mocked)
        self.assert_unknown_operation('primary-cancelled')

    def test_setup_and_cleanup_combined_failures_preserve_safe_io_error(self):
        with self.mocked_process() as mocked:
            mocked.selector.register.side_effect = OSError('PRIVATE_SETUP_DIAGNOSTIC_FIXTURE')
            for action in (mocked.selector.close, mocked.stop,
                           *[pipe.close for pipe in mocked.pipes]):
                action.side_effect = RuntimeError('PRIVATE_CLEANUP_DIAGNOSTIC_FIXTURE')
            with self.assertRaises(ModelCallError) as raised:
                model_calls.call_model('answer', {}, ledger=self.ledger,
                                        operation_id='combined-failure')
        self.assertEqual('cli_io_error', raised.exception.code)
        self.assertNotIn('PRIVATE_', str(raised.exception))
        self.assertNotIn('PRIVATE_', ''.join(traceback.format_exception(raised.exception)))
        self.assert_cleanup_attempted(mocked)
        self.assert_unknown_operation('combined-failure')

    def test_setup_cancellation_still_cleans_all_resources(self):
        with self.mocked_process() as mocked:
            mocked.factory.side_effect = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                model_calls.call_model('answer', {}, ledger=self.ledger,
                                        operation_id='setup-cancelled')
        self.assert_cleanup_attempted(mocked, selector_created=False)
        self.assert_unknown_operation('setup-cancelled')

    def test_cleanup_cancellation_does_not_skip_other_resources(self):
        with self.mocked_process() as mocked:
            mocked.selector.close.side_effect = OSError('PRIVATE_CLEANUP_DIAGNOSTIC_FIXTURE')
            mocked.stop.side_effect = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                model_calls.call_model('answer', {}, ledger=self.ledger,
                                        operation_id='cleanup-cancelled')
        self.assert_cleanup_attempted(mocked)
        self.assert_unknown_operation('cleanup-cancelled')


if __name__ == '__main__':
    unittest.main()
