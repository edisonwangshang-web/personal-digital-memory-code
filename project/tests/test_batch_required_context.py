"""Answered-source projections over isolated captures; no real model calls."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_batching
from pdm.batch_groups import _coverage
from pdm.batching import (plan_batches, project_batch_sources,
                          project_batch_with_context, validate_plan,
                          validate_source_views)
from pdm.controls import record_control


class RequiredBatchContextTests(unittest.TestCase):
    setUp = test_batching.BatchingTests.setUp
    source = test_batching.BatchingTests.source
    readonly = test_batching.BatchingTests.readonly
    assert_error = test_batching.BatchingTests.assert_error

    def expanded(self):
        paths = [self.source(f"reply-{index}", f"Artificial reply {index}.\nAll applicable fixture conditions retained.\n")
                 for index in range(9)]
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        original = plan["batches"][0]
        indexed = {fragment["fragment_id"]: fragment for fragment in plan["fragments"]}
        original_paths = {indexed[identifier]["path"] for identifier in original["fragment_ids"]}
        reply = next(path for path in paths if path not in original_paths)
        return paths, plan, original, reply

    def project(self, paths, plan, batch, required, **options):
        return project_batch_with_context(self.repo, plan, batch["batch_id"], source_paths=paths,
                                          required_source_paths=required, **options)

    def test_reply_in_another_batch_is_complete_and_global_without_false_parent_coverage(self):
        paths, plan, batch, reply = self.expanded()
        before = self.readonly()
        result = self.project(paths, plan, batch, [reply])
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(len(result["source_views"]), 9)
        self.assertEqual(result["batch_context"]["parent_batch_ids"], [batch["batch_id"]])
        self.assertNotEqual(result["batch_context"]["batch_id"], batch["batch_id"])
        view = next(view for view in result["source_views"] if view["path"] == reply)
        self.assertEqual([line["line"] for fragment in view["fragments"] for line in fragment["lines"]], [1, 2])
        self.assertTrue(validate_source_views(validate_plan(self.repo, plan), result["source_views"],
                                              result["batch_context"]))
        # Actual fragments, rather than synthetic names, establish full-plan
        # coverage and remain compatible with group proof reconstruction.
        _coverage(plan, [{"batch_context": result["batch_context"]}])
        self.assertEqual(before, self.readonly())

    def test_complete_reply_sections_and_prerequisite_are_added_without_invented_fragments(self):
        original = self.source("original", "Artificial original problem.\n")
        reply = self.source("section-reply", "Applies to all artificial conditions.\n# First\n"
                            + "a" * 500 + "\n# Second\n" + "b" * 500 + "\n")
        paths = [original, reply]
        full = plan_batches(self.repo, paths, max_input_bytes=100_000, max_sources=1)
        reply_ids = [fragment["fragment_id"] for fragment in full["fragments"] if fragment["path"] == reply]
        partial = project_batch_sources(self.repo, full,
            next(batch["batch_id"] for batch in full["batches"] if reply_ids[0] in batch["fragment_ids"]),
            source_paths=paths, fragment_ids=reply_ids[:2])
        plan = plan_batches(self.repo, paths, max_input_bytes=partial["input_bytes"] + 10, max_sources=1)
        original_ids = {fragment["fragment_id"] for fragment in plan["fragments"] if fragment["path"] == original}
        batch = next(batch for batch in plan["batches"] if original_ids <= set(batch["fragment_ids"]))
        self.assertGreaterEqual(len(plan["batches"]), 3)
        result = self.project(paths, plan, batch, [reply], max_bytes=100_000)
        self.assertTrue(result["coverage"]["complete"])
        view = next(view for view in result["source_views"] if view["path"] == reply)
        self.assertEqual([fragment["locator"] for fragment in view["fragments"]], ["L1-L1", "L2-L3", "L4-L5"])
        self.assertEqual({fragment["fragment_id"] for fragment in view["fragments"]}, set(reply_ids))
        for fragment in view["fragments"]:
            fixed = next(item for item in plan["fragments"] if item["fragment_id"] == fragment["fragment_id"])
            self.assertTrue(set(fixed["parent_fragment_ids"]) <= set(result["coverage"]["fragment_ids"]))
        self.assertTrue(validate_source_views(validate_plan(self.repo, plan), result["source_views"],
                                              result["batch_context"]))

    def test_no_required_context_is_exact_legacy_projection(self):
        path = self.source("legacy", "Artificial source.\n")
        plan = plan_batches(self.repo, [path], max_input_bytes=100_000)
        batch = plan["batches"][0]
        for limit in (None, 1):
            with self.subTest(limit=limit):
                self.assertEqual(self.project([path], plan, batch, [], max_bytes=limit),
                    project_batch_sources(self.repo, plan, batch["batch_id"], source_paths=[path], max_bytes=limit))

    def test_duplicate_required_scope_is_rejected_and_order_is_stable(self):
        paths, plan, batch, reply = self.expanded()
        self.assert_error("invalid_batch_scope", lambda: self.project(paths, plan, batch, [reply, reply]))
        first = self.project(paths, plan, batch, [paths[0], reply] if paths[0] != reply else [paths[1], reply])
        reverse = self.project(list(reversed(paths)), plan, batch,
                               list(reversed(first["coverage"]["required_source_paths"])))
        self.assertEqual(first, reverse)

    def test_missing_authorization_or_out_of_plan_reply_rejected_before_any_body_is_opened(self):
        paths, plan, batch, reply = self.expanded()
        other = self.source("outside-plan", "Artificial unrelated reply.\n")
        with patch("pdm.batching.load_source", side_effect=AssertionError("unauthorized body opened")):
            self.assert_error("batch_scope_mismatch", lambda: self.project(
                [path for path in paths if path != reply], plan, batch, [reply]))
            self.assert_error("batch_required_source_mismatch", lambda: self.project(
                [*paths, other], plan, batch, [other]))

    def test_full_context_capacity_blocks_without_any_partial_model_input(self):
        paths, plan, batch, reply = self.expanded()
        ready = self.project(paths, plan, batch, [reply])
        blocked = self.project(paths, plan, batch, [reply], max_bytes=ready["input_bytes"] - 1)
        self.assertEqual(blocked["status"], "blocked_capacity")
        self.assertFalse(blocked["coverage"]["complete"])
        self.assertTrue(blocked["coverage"]["budget_exhausted"])
        self.assertEqual(blocked["source_views"], [])
        self.assertEqual(blocked["batch_context"], ready["batch_context"])
        self.assertEqual(blocked["input_bytes"], ready["input_bytes"])
        self.assertEqual(set(blocked["coverage"]["omitted_fragment_ids"]), set(ready["coverage"]["fragment_ids"]))
        exact = self.project(paths, plan, batch, [reply], max_bytes=ready["input_bytes"])
        self.assertEqual(exact, ready)

    def test_default_capacity_uses_fixed_plan_budget_for_complete_added_context(self):
        paths, initial, batch, reply = self.expanded()
        original = project_batch_sources(self.repo, initial, batch["batch_id"], source_paths=paths)
        plan = plan_batches(self.repo, paths, max_input_bytes=original["input_bytes"] + 1)
        # Locate the largest original batch: adding any complete missing source
        # is necessarily larger than its own fixed input allowance here.
        batch = max(plan["batches"], key=lambda value: value["input_bytes"])
        indexed = {fragment["fragment_id"]: fragment for fragment in plan["fragments"]}
        included = {indexed[identifier]["path"] for identifier in batch["fragment_ids"]}
        reply = next(path for path in paths if path not in included)
        result = self.project(paths, plan, batch, [reply])
        self.assertEqual(result["status"], "blocked_capacity")
        self.assertEqual(result["source_views"], [])

    def test_changed_plan_or_controls_are_rejected_without_suppressing_evidence_silently(self):
        paths, plan, batch, reply = self.expanded()
        changed = deepcopy(plan)
        changed["fragments"][0]["parent_fragment_ids"] = [plan["fragments"][-1]["fragment_id"]]
        self.assert_error("batch_plan_mismatch", lambda: self.project(paths, changed, batch, [reply]))
        record_control(self.repo, {"request_key": "fixture:required-reply-delete", "action": "delete",
                                   "source_paths": [reply]})
        with patch("pdm.batching.load_source", side_effect=AssertionError("changed controlled body opened")):
            self.assert_error("batch_snapshot_changed", lambda: self.project(paths, plan, batch, [reply]))

    def test_existing_control_of_needed_fragment_blocks_entire_projection(self):
        paths, plan, batch, reply = self.expanded()
        def controlled(snapshot, path, locator, **options):
            return {"excluded": path == reply, "restrictions": []}
        with patch("pdm.batching.source_locator_policy", side_effect=controlled):
            result = self.project(paths, plan, batch, [reply])
        self.assertEqual(result["status"], "blocked_control")
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["source_views"], [])
        expected = {fragment["fragment_id"] for fragment in plan["fragments"] if fragment["path"] == reply}
        self.assertEqual(set(result["coverage"]["controlled_fragment_ids"]), expected)

    def test_runtime_version_changed_during_projection_invalidates_the_complete_result(self):
        path = self.source("runtime", "Artificial original condition.\n")
        plan = plan_batches(self.repo, [path], max_input_bytes=100_000)
        versions = plan["snapshot"]["versions"]
        with patch("pdm.batching.execution_versions", side_effect=[versions, {**versions, "changed": True}]):
            self.assert_error("batch_snapshot_changed", lambda: self.project(
                [path], plan, plan["batches"][0], [path]))

    def test_all_projected_batches_keep_exact_plan_coverage_without_duplicating_line_identity(self):
        paths, plan, original, reply = self.expanded()
        results = [self.project(paths, plan, batch, [reply]) for batch in plan["batches"]]
        _coverage(plan, [{"batch_context": result["batch_context"]} for result in results])
        reply_fragments = [next(view for view in result["source_views"] if view["path"] == reply)["fragments"]
                           for result in results]
        self.assertTrue(all(value == reply_fragments[0] for value in reply_fragments))
        again = self.project(paths, plan, original, [reply])
        self.assertEqual(results[0], again)
        new_reply = self.source("new-reply", "Newly answered artificial condition.\nSecond complete reply line.\n")
        new_plan = plan_batches(self.repo, [*paths, new_reply], max_input_bytes=100_000)
        new_batch = new_plan["batches"][0]
        new_projection = self.project([*paths, new_reply], new_plan, new_batch, [reply, new_reply])
        self.assertNotEqual(new_projection["batch_context"]["batch_id"], again["batch_context"]["batch_id"])
        new_view = next(view for view in new_projection["source_views"] if view["path"] == new_reply)
        self.assertEqual([line["line"] for fragment in new_view["fragments"] for line in fragment["lines"]], [1, 2])

