"""Selected-input provenance stays complete without widening unit body scope."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_decomposed_review as fixtures
from pdm.contracts import CaptureError
from pdm.decomposed_review import (REQUEST_CONTRACT, _input_scope_inventory, _plan, _request,
                                   run_decomposed_review)
from pdm.pipeline import _load


class DecomposedScopeTests(unittest.TestCase):
    setUp = fixtures.DecomposedReviewTests.setUp
    sources = fixtures.DecomposedReviewTests.sources
    model = fixtures.DecomposedReviewTests.model
    freeze = fixtures.DecomposedReviewTests.freeze
    run_review = fixtures.DecomposedReviewTests.run_review
    verify = fixtures.DecomposedReviewTests.verify
    counters = fixtures.DecomposedReviewTests.counters

    def prepare(self):
        return fixtures.DecomposedReviewTests.prepare(self, 2)

    def test_every_unit_has_exact_selected_capture_scope_without_extra_source_body(self):
        self.freeze()
        result = self.run_review()
        self.assertEqual(result['status'], 'passed', result)
        self.assertTrue(self.verify(result))
        plan = _load((self.repo / result['proof']['plan_path']).read_bytes(), 'decomposed_review_plan', '0.8')
        self.assertEqual(plan['request_contract'], REQUEST_CONTRACT)
        self.assertEqual(len(plan['input_scope_inventory']), 2)
        requests = self.contexts[1:]
        self.assertEqual(len(requests[0]['sources']), 1)
        for request in requests:
            self.assertEqual(request['request_contract'], REQUEST_CONTRACT)
            self.assertEqual(request['input_scope_inventory'], plan['input_scope_inventory'])
            self.assertEqual(request['input_scope_inventory_sha256'], plan['input_scope_inventory_sha256'])
            for entry in request['input_scope_inventory']:
                self.assertEqual(entry['metadata']['source_id'], entry['source_id'])
                self.assertEqual(entry['metadata']['source_version'], entry['source_version'])
                self.assertEqual(entry['metadata']['acquisition_status'], 'full_text')
                self.assertTrue(entry['metadata_provided'])
                self.assertEqual(entry['provided_line_count'], 1)
                self.assertNotIn('body', entry)
                self.assertNotIn('lines', entry['provided_fragments'][0])
                self.assertIn('receipt_sha256', entry)

    def test_selected_but_unprovided_source_is_explicit_not_assumed_read_or_acquired(self):
        self.freeze()
        payload = deepcopy(self.payload)
        payload['sources'] = payload['sources'][:1]
        inventory = _input_scope_inventory(self.candidate, payload)
        self.assertEqual(len(inventory), 2)
        unread = next(entry for entry in inventory if entry['path'] != payload['sources'][0]['path'])
        self.assertFalse(unread['metadata_provided'])
        self.assertIsNone(unread['metadata'])
        self.assertEqual(unread['provided_fragments'], [])
        self.assertEqual(unread['provided_line_count'], 0)

    def test_inventory_mutation_changes_bound_plan_and_request_and_invalidates_proof(self):
        self.freeze()
        result = self.run_review()
        plan = _load((self.repo / result['proof']['plan_path']).read_bytes(), 'decomposed_review_plan', '0.8')
        original = _request(plan, plan['units'][0], self.candidate, self.payload)
        forged = deepcopy(plan)
        forged['input_scope_inventory'][0]['metadata']['acquisition_status'] = 'link_only'
        changed = _request(forged, forged['units'][0], self.candidate, self.payload)
        self.assertNotEqual(original, changed)
        # The actual immutable result hashes the original full request. A
        # different inventory cannot inherit that result's certificate.
        with patch('pdm.decomposed_review._request', return_value=changed):
            with self.assertRaises(CaptureError):
                self.verify(result)

    def test_legacy_plan_request_is_exact_and_uses_stored_prompt_for_current_reads(self):
        self.freeze()
        original_plan = _plan
        def legacy_plan(*args, **kwargs):
            return original_plan(*args, **{**kwargs, 'request_contract': None})
        with patch('pdm.decomposed_review._plan', legacy_plan):
            result = self.run_review()
        self.assertEqual(result['status'], 'passed', result)
        plan = _load((self.repo / result['proof']['plan_path']).read_bytes(), 'decomposed_review_plan', '0.8')
        self.assertNotIn('request_contract', plan)
        self.assertNotIn('input_scope_inventory', plan)
        self.assertTrue(all('input_scope_inventory' not in request for request in self.contexts[1:]))
        with patch('pdm.decomposed_review._prompt_metadata', side_effect=AssertionError('Old proof must use its actual stored prompt')):
            self.assertTrue(self.verify(result))

    def test_contract_does_not_reset_counter_identity_or_escape_legacy_unknown(self):
        self.freeze()
        def interrupted(role, payload, timeout=120):
            raise KeyboardInterrupt()
        from pdm.budget import BudgetedModelCall
        original_plan = _plan
        def legacy_plan(*args, **kwargs):
            return original_plan(*args, **{**kwargs, 'request_contract': None})
        with patch('pdm.decomposed_review._plan', legacy_plan), self.assertRaises(KeyboardInterrupt):
            self.run_review(BudgetedModelCall(self.ledger, 'fixture', interrupted))
        before = self.ledger.path.read_bytes()
        # Existing plan dispatch stays legacy, and its unknown operation keeps
        # the same logical counter even though new writes default to v1.
        result = self.run_review()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['attempted_calls'], 0)
        self.assertEqual(self.ledger.path.read_bytes(), before)
        self.assertEqual(len(self.counters()[0]['attempts']), 1)


if __name__ == '__main__':
    unittest.main()
