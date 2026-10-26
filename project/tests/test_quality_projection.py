"""Artificial-only quality model views; no repository or model is contacted."""

import copy
import hashlib
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import prepare_quality, project_quality
from pdm.model_calls import render_model_request
from pdm.quality_projection import (
    CONTRACT, MARKER, project_quality_payload, restore_quality_payload,
)
from pdm.skill_support import resolve_skill_refs
from pdm.skill_wire_v3 import decode_shared_json_v3, encode_shared_json_v3


def source(body, name="artificial"):
    version = "sha256-" + hashlib.sha256(body.encode()).hexdigest()
    return {"path": "memory/sources/" + name + ".md", "body": body,
            "metadata": {"source_id": "src-" + name, "source_version": version,
                         "source_type": "note", "original_time": None}}


def payload(*sources):
    return {"sources": list(sources), "quality_contract": "skill-quality-v1",
            "quality_context": prepare_quality(list(sources)),
            "objects": [{"body": "明确标记为人工样例的完整候选正文。"}],
            "review_record": {"output": {"checks": [
                {"check_id": "artificial-check-" + str(i), "reason": "保留此项人工审核意见。"}
                for i in range(73)], "verdict": "needs_revision"}}}


class QualityProjectionTests(unittest.TestCase):
    def assert_rejected(self, action):
        with self.assertRaises(CaptureError):
            action()

    def test_roundtrip_keeps_exact_bom_crlf_blank_lines_statistics_and_all_feedback(self):
        first = source("\ufeff人工标题\r\n\r\n原始说明。\r\n最后一行\r\n", "prose")
        second = source("日期,steps\n" + "".join(
            f"2025-01-{day:02},{day}\n" for day in range(1, 9)), "table")
        original = payload(first, second)
        frozen = copy.deepcopy(original)
        projected = project_quality_payload(original)
        self.assertEqual(projected[MARKER]["contract"], CONTRACT)
        self.assertEqual(original, frozen)
        self.assertEqual(projected["sources"], original["sources"])
        self.assertEqual(projected["quality_context"]["targets"], original["quality_context"]["targets"])
        self.assertEqual(projected["quality_context"]["statistics"], original["quality_context"]["statistics"])
        self.assertTrue(projected["quality_context"]["statistics"])
        self.assertEqual(projected["review_record"], original["review_record"])
        self.assertEqual(projected["objects"], original["objects"])
        for old, view in zip(original["quality_context"]["sources"], projected["quality_context"]["sources"]):
            self.assertNotIn("line_sha256", view)
            self.assertEqual(view["line_integrity"]["line_count"], len(old["line_sha256"]))
            self.assertEqual(view["evidence_id"], old["evidence_id"])
        restored_wire = decode_shared_json_v3(encode_shared_json_v3(projected))
        self.assertEqual(restore_quality_payload(restored_wire), original)

    def test_nested_revision_quality_and_shared_python_objects_are_all_projected(self):
        original = payload(source("人工第一行\n人工第二行\n"))
        context = original["quality_context"]
        original["revision_context"] = {"candidate": {
            "quality_contract": "skill-quality-v1", "quality_context": context,
            "objects": copy.deepcopy(original["objects"])}, "model_context": {"candidate": {
                "quality_provenance": {"quality_contract": "skill-quality-v1", "quality_context": context}}},
            "review_record": copy.deepcopy(original["review_record"])}
        projected = project_quality_payload(original)
        contexts = [projected["quality_context"], projected["revision_context"]["candidate"]["quality_context"],
                    projected["revision_context"]["model_context"]["candidate"]["quality_provenance"]["quality_context"]]
        self.assertTrue(all("line_integrity" in value["sources"][0] for value in contexts))
        self.assertIn("line_sha256", context["sources"][0])
        self.assertEqual(restore_quality_payload(projected), original)

    def test_changed_source_line_bom_version_and_conflicting_fragments_are_rejected(self):
        original = payload(source("\ufeff人工标题\n\n尾行\n"))
        for change in ("line", "bom", "version", "path", "missing", "fragment"):
            value = copy.deepcopy(original)
            item = value["sources"][0]
            if change == "line": item["body"] += "额外实际行\n"
            elif change == "bom": item["body"] = item["body"].lstrip("\ufeff")
            elif change == "version": item["metadata"]["source_version"] = "sha256-" + "0" * 64
            elif change == "path": item["path"] = "memory/sources/different.md"
            elif change == "missing": item["body"] = item["body"].splitlines()[0]
            else: item["fragments"] = [{"fragment_id": "conflict", "locator": "L1-L1",
                                        "lines": [{"line": 1, "text": "冲突文本"}]}]
            with self.subTest(change=change):
                self.assert_rejected(lambda: project_quality_payload(value))

    def test_profile_map_and_computed_statistic_tampering_fail_before_projection(self):
        original = payload(source("日期,steps\n2025-01-01,1\n2025-01-02,2\n"))
        for change in ("hash", "extra", "statistic"):
            value = copy.deepcopy(original)
            profile = value["quality_context"]["sources"][0]
            if change == "hash": profile["line_sha256"]["1"] = "0" * 64
            elif change == "extra": profile["line_sha256"]["99"] = "0" * 64
            else: value["quality_context"]["statistics"][0]["value"] = "999"
            with self.subTest(change=change):
                self.assert_rejected(lambda: project_quality_payload(value))

    def test_scope_never_loads_missing_lines_and_retains_unreconstructable_map(self):
        item = source("人工第一行\n人工第二行\n人工第三行\n")
        original = payload(item)
        original["review_scope"] = {"kind": "object"}
        original["sources"] = [{"path": item["path"], "metadata": item["metadata"], "fragments": [
            {"fragment_id": "only-second", "locator": "L2-L2",
             "lines": [{"line": 2, "text": "人工第二行"}]}]}]
        projected = project_quality_payload(original)
        self.assertEqual(projected["sources"], original["sources"])
        self.assertEqual(projected["quality_context"], original["quality_context"])
        self.assertEqual(restore_quality_payload(projected), original)
        bad = copy.deepcopy(original)
        bad["sources"][0]["fragments"][0]["lines"][0]["text"] = "不是原行"
        self.assert_rejected(lambda: project_quality_payload(bad))
        del bad["review_scope"]
        self.assert_rejected(lambda: project_quality_payload(bad))

    def test_actual_decomposed_object_and_global_quality_views_restore_exactly(self):
        item = source("日期,steps\n2025-01-01,1\n2025-01-02,2\n")
        fragments = [{"fragment_id": "part-" + str(number), "locator": f"L{number}-L{number}",
                      "lines": [{"line": number, "text": text}]}
                     for number, text in enumerate(item["body"].splitlines(), 1)]
        full_view = {"path": item["path"], "metadata": item["metadata"], "fragments": fragments}
        context = prepare_quality([item], [full_view])
        for kind, selected in (("object", ["part-1", "part-2"]), ("global", [])):
            scope = {"kind": kind, "fragment_ids": selected, "object_paths": []}
            material = {"quality_contract": "skill-quality-v1", "review_scope": scope,
                        "quality_context": project_quality(context, scope), "sources": []}
            if selected:
                material["sources"] = [{**full_view, "fragments": [fragment for fragment in fragments
                                           if fragment["fragment_id"] in selected]}]
            with self.subTest(kind=kind):
                projected = project_quality_payload(material)
                self.assertEqual(restore_quality_payload(projected), material)
                self.assertEqual(projected["sources"], material["sources"])

    def test_projected_descriptor_and_entire_material_are_bound_on_restore(self):
        original = payload(source("人工行一\r\n\r\n人工行三\r\n"))
        projected = project_quality_payload(original)
        for change in ("digest", "count", "contract", "resolution", "line", "crlf", "object", "feedback"):
            value = copy.deepcopy(projected)
            descriptor = value["quality_context"]["sources"][0]["line_integrity"]
            if change == "digest": descriptor["sha256"] = "sha256-" + "0" * 64
            elif change == "count": descriptor["line_count"] += 1
            elif change == "contract": descriptor["contract"] = "unknown"
            elif change == "resolution": descriptor["resolution"] = "guess"
            elif change == "line": value["sources"][0]["body"] += "新行\n"
            elif change == "crlf": value["sources"][0]["body"] = value["sources"][0]["body"].replace("\r\n", "\n")
            elif change == "object": value["objects"][0]["body"] = "篡改人工正文"
            else: value["review_record"]["output"]["checks"].pop()
            with self.subTest(change=change):
                self.assert_rejected(lambda: restore_quality_payload(value))

    def test_unknown_version_double_projection_and_partial_opt_in_are_rejected(self):
        original = payload(source("人工说明\n"))
        self.assert_rejected(lambda: project_quality_payload(original, contract="unknown"))
        projected = project_quality_payload(original)
        self.assert_rejected(lambda: project_quality_payload(projected))
        self.assert_rejected(lambda: restore_quality_payload(original))
        projected[MARKER]["contract"] = "unknown"
        self.assert_rejected(lambda: restore_quality_payload(projected))
        del original["quality_contract"]
        self.assert_rejected(lambda: project_quality_payload(original))

    def test_source_metadata_and_review_feedback_are_opaque_not_computation_structures(self):
        original = payload(source("人工说明\n"))
        opaque = {"quality_context": {"line_sha256": "原资料同名字段不是程序契约"}}
        original["sources"][0]["metadata"]["quoted_object"] = copy.deepcopy(opaque)
        original["review_record"]["untrusted_feedback"] = copy.deepcopy(opaque)
        self.assertEqual(restore_quality_payload(project_quality_payload(original)), original)

    def test_minimal_rule_overhead_template_can_be_projected_without_sources(self):
        original = {"quality_contract": "skill-quality-v1", "skill_bundle": {"synthetic": True}}
        self.assertEqual(restore_quality_payload(project_quality_payload(original)), original)

    def test_mixed_link_only_empty_source_keeps_empty_scope_and_map(self):
        empty = source("", "link")
        empty["metadata"]["source_type"] = "link_only"
        original = payload(empty, source("人工非空来源。\n", "note"))
        projected = project_quality_payload(original)
        profile = projected["quality_context"]["sources"][0]
        self.assertEqual(profile["line_sha256"], {})
        self.assertEqual(profile["fragments"][0]["locators"], [])
        self.assertEqual(profile["evidence_id"], original["quality_context"]["sources"][0]["evidence_id"])
        self.assertEqual(restore_quality_payload(projected), original)

    def test_frozen_nine_seven_requests_have_exact_pre_projection_hashes(self):
        original = {"quality_contract": "skill-quality-v1", "sources": [],
                    "skill_bundle": resolve_skill_refs("knowledge-extract@9", "knowledge-review@7")}
        expected = {"extract": "3e20c2e6ebdebe3b976985fbc21b7cc91352a2d46879ea32a6d1854e97839b00",
                    "review": "68275b813af40778af19824e97a2c859a838331ed586f1b3a87c96bd8554a0f9"}
        for role in expected:
            self.assertEqual(hashlib.sha256(render_model_request(role, original)).hexdigest(), expected[role])


if __name__ == "__main__":
    unittest.main()
