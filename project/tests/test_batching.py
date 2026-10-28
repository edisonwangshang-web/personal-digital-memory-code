"""Conservative semantic batching over artificial immutable Git sources."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_pipeline
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm import batching
from pdm.batching import (assert_fragment_refs, merge_batch_sources, plan_batches,
                          project_batch_sources, validate_plan, validate_source_views)


class BatchingTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    assert_error = test_pipeline.PipelineTests.assert_error

    def source(self, key, body, source_type="note"):
        return capture(self.repo, {"source_key": "fixture:batch:" + key, "source_type": source_type,
                                  "acquisition_status": "full_text", "body": body})["path"]

    def readonly(self):
        return (test_pipeline.git(self.repo, "rev-parse", "HEAD"),
                test_pipeline.git(self.repo, "status", "--porcelain"),
                {str(path.relative_to(self.repo)): path.read_bytes() for path in (self.repo / "memory").rglob("*.md")})

    def table_plan(self):
        body = "day,value\nunit,kg\n" + "".join(f"{number}," + "x" * 350 + "\n" for number in range(6))
        path = self.source("table.csv", body)
        full = plan_batches(self.repo, [path], max_input_bytes=100_000)
        first = [fragment["fragment_id"] for fragment in full["fragments"][:2]]
        projected = project_batch_sources(self.repo, full, full["batches"][0]["batch_id"], source_paths=[path], fragment_ids=first)
        small = plan_batches(self.repo, [path], max_input_bytes=projected["input_bytes"] + 10)
        return path, small

    def test_more_than_eight_explicit_sources_pack_into_bounded_stable_batches(self):
        paths = [self.source(str(number), f"人工独立来源{number}。\n仅隔离资料。\n") for number in range(9)]
        before = self.readonly()
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        self.assertEqual(plan, plan_batches(self.repo, list(reversed(paths)), max_input_bytes=100_000))
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["coverage"]["source_count"], 9)
        self.assertEqual(len(plan["batches"]), 2)
        fragments = {fragment["fragment_id"]: fragment for fragment in plan["fragments"]}
        for batch in plan["batches"]:
            self.assertLessEqual(len({fragments[key]["path"] for key in batch["fragment_ids"]}), 8)
        self.assertEqual(self.readonly(), before)

    def test_source_batch_budget_stays_at_256kb_independently_of_model_transport(self):
        path = self.source("independent-budget", "Artificial independently bounded batch source.\n")
        before = self.readonly()
        plan = plan_batches(self.repo, [path], max_input_bytes=256_000)
        self.assertEqual(plan["status"], "ready")
        self.assert_error("invalid_batch_budget", lambda: plan_batches(self.repo, [path], max_input_bytes=256_001))
        self.assertEqual(self.readonly(), before)

    def test_csv_units_repeat_headers_and_global_lines_without_duplicate_evidence(self):
        path, plan = self.table_plan()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(len(plan["batches"]), 6)
        self.assertEqual(plan["coverage"]["unique_evidence_lines"], 8)
        self.assertEqual(plan["coverage"]["covered_ranges"][path], ["L1-L8"])
        batch = plan["batches"][2]
        projected = project_batch_sources(self.repo, plan, batch["batch_id"], source_paths=[path])
        self.assertTrue(projected["coverage"]["complete"])
        source = validate_plan(self.repo, plan)[0]
        self.assertTrue(validate_source_views([source], projected["source_views"], projected["batch_context"]))
        fragments = projected["source_views"][0]["fragments"]
        self.assertEqual([fragment["locator"] for fragment in fragments], ["L1-L2", "L5-L5"])
        ref = {"source_id": source["source_id"], "source_version": source["source_version"], "locator": "L5-L5"}
        assert_fragment_refs([ref], projected["source_views"])
        self.assert_error("fragment_reference_outside_view", lambda: assert_fragment_refs(
            [{**ref, "locator": "L3-L5"}], projected["source_views"]))
        self.assertFalse(projected["coverage"]["overlap_is_independent_evidence"])

    def test_missing_premise_or_budget_never_returns_silently_truncated_input(self):
        path, plan = self.table_plan()
        batch = plan["batches"][0]
        projected = project_batch_sources(self.repo, plan, batch["batch_id"], source_paths=[path],
                                          fragment_ids=batch["fragment_ids"][1:])
        self.assertFalse(projected["coverage"]["complete"])
        self.assertEqual(projected["source_views"], [])
        self.assertTrue(projected["coverage"]["missing_dependency_fragment_ids"])
        limited = project_batch_sources(self.repo, plan, batch["batch_id"], source_paths=[path], max_bytes=1)
        self.assertEqual(limited["source_views"], [])
        self.assertTrue(limited["coverage"]["budget_exhausted"])

    def test_conversation_and_dependent_prose_remain_atomic_when_too_large(self):
        for kind, body in (("conversation", "用户：前提是人工条件。\n助手：" + "x" * 5000 + "\n用户：继续。\n助手：继续回答。\n"),
                           ("note", "前提条件必须保留。\n\n" + "x" * 5000 + "\n\n所以不能丢前提。\n"),
                           ("note", "# 第一节\n" + "x" * 3000 + "\n# 第二节\n参见上述条件。\n")):
            with self.subTest(kind=kind):
                path = self.source(kind + str(len(body)), body, kind)
                plan = plan_batches(self.repo, [path], max_input_bytes=2200)
                self.assertEqual(plan["status"], "blocked")
                self.assertEqual(plan["batches"], [])
                self.assertEqual(plan["blocked"][0]["reason"], "semantic_unit_exceeds_budget")
                self.assertTrue(plan["coverage"]["uncovered_ranges"][path])

    def test_markdown_section_slices_retain_shared_preamble(self):
        path = self.source("sections", "共同前提：只是隔离示例。\n# A\n" + "a" * 500 + "\n# B\n" + "b" * 500 + "\n")
        full = plan_batches(self.repo, [path], max_input_bytes=100_000)
        self.assertEqual([item["kind"] for item in full["fragments"]], ["section_context", "markdown_section", "markdown_section"])
        first_ids = [item["fragment_id"] for item in full["fragments"][:2]]
        first = project_batch_sources(self.repo, full, full["batches"][0]["batch_id"], source_paths=[path], fragment_ids=first_ids)
        plan = plan_batches(self.repo, [path], max_input_bytes=first["input_bytes"] + 10)
        self.assertEqual(len(plan["batches"]), 2)
        self.assertEqual(plan["coverage"]["unique_evidence_lines"], 5)

    def test_scope_mismatch_rejects_before_any_source_body_load(self):
        path = self.source("scope", "人工来源。\n")
        plan = plan_batches(self.repo, [path], max_input_bytes=100_000)
        with patch("pdm.batching.load_source", side_effect=AssertionError("unselected body opened")):
            self.assert_error("batch_scope_mismatch", lambda: project_batch_sources(
                self.repo, plan, plan["batches"][0]["batch_id"], source_paths=[self.saved["path"]]))

    def test_changed_fragment_plan_or_byte_mapping_cannot_be_used(self):
        path, plan = self.table_plan()
        changed = deepcopy(plan)
        changed["fragments"][0]["locator"] = "L1-L1"
        self.assert_error("batch_plan_mismatch", lambda: validate_plan(self.repo, changed))
        projected = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=[path])
        projected["source_views"][0]["fragments"][0]["lines"][0]["text"] = "tampered"
        self.assert_error("fragment_bytes_mismatch", lambda: validate_source_views(
            validate_plan(self.repo, plan), projected["source_views"], projected["batch_context"]))

    def test_merge_binds_parent_batches_and_refuses_over_budget_without_truncation(self):
        path, plan = self.table_plan()
        parents = [batch["batch_id"] for batch in plan["batches"][:2]]
        merged = merge_batch_sources(self.repo, plan, parents, source_paths=[path], max_bytes=100_000)
        reversed_merge = merge_batch_sources(self.repo, plan, list(reversed(parents)), source_paths=[path], max_bytes=100_000)
        self.assertEqual(merged, reversed_merge)
        self.assertEqual(merged["status"], "ready")
        self.assertEqual(merged["batch_context"]["parent_batch_ids"], sorted(parents))
        self.assertTrue(validate_source_views(validate_plan(self.repo, plan), merged["source_views"], merged["batch_context"]))
        blocked = merge_batch_sources(self.repo, plan, parents, source_paths=[path], max_bytes=1)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["source_views"], [])

    def test_synthetic_merge_can_bind_nine_explicit_sources(self):
        paths = [self.source("merge" + str(number), "人工来源。\n") for number in range(9)]
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        merged = merge_batch_sources(self.repo, plan, [batch["batch_id"] for batch in plan["batches"]],
                                     source_paths=paths, max_bytes=100_000)
        self.assertEqual(len(merged["source_views"]), 9)
        self.assertTrue(validate_source_views(validate_plan(self.repo, plan), merged["source_views"], merged["batch_context"]))
        self.assertTrue(merged["coverage"]["complete"])

    def test_partial_control_suppresses_current_fragment_and_its_dependents(self):
        path, plan = self.table_plan()
        batch = plan["batches"][0]
        with patch("pdm.batching.source_locator_policy", return_value={"excluded": True, "restrictions": []}):
            projected = project_batch_sources(self.repo, plan, batch["batch_id"], source_paths=[path])
            self.assertEqual(projected["source_views"], [])
            self.assertFalse(projected["coverage"]["complete"])
            self.assertEqual(set(projected["coverage"]["controlled_fragment_ids"]), set(batch["fragment_ids"]))
            merged = merge_batch_sources(self.repo, plan, [batch["batch_id"]], source_paths=[path], max_bytes=100_000)
            self.assertEqual(merged["status"], "blocked")
            self.assertEqual(merged["source_views"], [])
            self.assertIn("controlled_fragment", merged["coverage"]["blocked_reasons"])

    def test_fenced_fake_headings_and_explicit_cross_row_references_stay_atomic(self):
        cases = (("fenced", "说明文字。\n```text\n# fake-a\n" + "a" * 700 + "\n# fake-b\n" + "b" * 700 + "\n```\n", "structured_atomic"),
                 ("dependent.csv", "id,value,context\n1,100,initial\n2,200,same as above\n", "dependent_context_atomic"))
        for key, body, kind in cases:
            with self.subTest(key=key):
                path = self.source(key, body)
                plan = plan_batches(self.repo, [path], max_input_bytes=100_000)
                self.assertEqual(len(plan["fragments"]), 1)
                self.assertEqual(plan["fragments"][0]["kind"], kind)
                blocked = plan_batches(self.repo, [path], max_input_bytes=1)
                self.assertEqual(blocked["status"], "blocked")
                self.assertEqual(blocked["batches"], [])
