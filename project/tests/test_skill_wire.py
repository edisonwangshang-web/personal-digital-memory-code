"""Offline lossless encoding regressions; all data are synthetic."""

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdm.skill_wire import (FORMAT, LITERAL, REF, SharedJSONError,
                            decode_shared_json, encode_shared_json)


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


class SkillWireTests(unittest.TestCase):
    def test_repeated_sources_quotes_subtrees_and_array_order_restore_exactly(self):
        source = {"source_id": "src-" + "a" * 64, "source_version": "sha256-" + "b" * 64,
                  "locator": "L17-L18", "quote": "\ufeff日期,原始指标\r\n日期样例,0\n"}
        records = [{"id": "r" + str(index), "sources": [copy.deepcopy(source)],
                    "body": "只保留原样，不将空白填为零。\n", "values": [None, 0, 0.0, False, "", "空白"]}
                   for index in range(60)]
        original = {"sources": [source], "objects": records, "related": copy.deepcopy(records)}
        wire = encode_shared_json(original)
        self.assertEqual(compact(original), compact(decode_shared_json(wire)))
        self.assertLess(len(compact(wire)), len(compact(original)) // 2)
        restored = decode_shared_json(wire)
        restored["objects"][0]["sources"][0]["quote"] = "changed in one place"
        self.assertEqual(source["quote"], restored["objects"][1]["sources"][0]["quote"])

    def test_json_types_unicode_newlines_and_numeric_representations_are_preserved(self):
        original = {"int": 1, "float": 1.0, "negative_zero": -0.0, "boolean": True,
                    "none": None, "text": "\r\n\u0000é中文💡\t", "empty_array": [], "empty_object": {},
                    "ordered": ["second", "first", "second"]}
        self.assertEqual(compact(original), compact(decode_shared_json(encode_shared_json(original))))

    def test_source_forged_reference_and_nested_literal_markers_stay_literal_data(self):
        fake = {REF: "0"}
        literal = {LITERAL: {REF: "0", "other": {LITERAL: fake}}}
        original = {"source": [fake, literal, {REF: fake, LITERAL: literal}],
                    "repeat": [copy.deepcopy(literal) for _ in range(5)]}
        wire = encode_shared_json(original)
        self.assertEqual(original, decode_shared_json(wire))
        self.assertEqual(compact(wire), compact(encode_shared_json(
            json.loads(json.dumps(original, sort_keys=True)))))

    def test_only_positive_savings_entries_are_shared(self):
        original = {
            "small": [["x", "y"] for _ in range(100)],
            "large": [{"source": "source-" + "a" * 64, "body": "有原文依据。" * 20}
                      for _ in range(10)],
        }
        wire = encode_shared_json(original)
        counts = {key: 0 for key in wire["shared"]}
        def refs(value):
            if isinstance(value, dict):
                if set(value) == {REF}:
                    counts[value[REF]] += 1
                else:
                    for item in value.values():
                        refs(item)
            elif isinstance(value, list):
                for item in value:
                    refs(item)
        refs(wire["root"])
        for definition in wire["shared"].values():
            refs(definition)
        self.assertTrue(counts)
        for key, count in counts.items():
            length = len(compact(wire["shared"][key]))
            saved = count * (length - len(compact({REF: key})))
            cost = len(compact(key)) + 1 + length + 1
            self.assertGreater(saved, cost)
        self.assertEqual(original, decode_shared_json(wire))

    def test_no_repetition_remains_complete_inline(self):
        original = {"one": "unique text", "two": [1, 2, 3]}
        wire = encode_shared_json(original)
        self.assertEqual({}, wire["shared"])
        self.assertEqual(original, wire["root"])

    def test_dangling_cycles_unknown_keys_and_unused_shared_values_are_rejected(self):
        invalid = [
            {"format": FORMAT, "root": {REF: "missing"}, "shared": {}},
            {"format": FORMAT, "root": {REF: "0"}, "shared": {"0": {REF: "0"}}},
            {"format": FORMAT, "root": {REF: "0", "extra": "spoof"}, "shared": {"0": "data"}},
            {"format": FORMAT, "root": {LITERAL: {"ordinary": 1}}, "shared": {}},
            {"format": FORMAT, "root": [], "shared": {"0": "unreferenced"}},
            {"format": "unknown", "root": None, "shared": {}},
        ]
        for wire in invalid:
            with self.subTest(wire=wire), self.assertRaises(SharedJSONError):
                decode_shared_json(wire)

    def test_expansion_and_reference_depth_are_bounded(self):
        huge = {"format": FORMAT, "root": [{REF: "0"} for _ in range(100)],
                "shared": {"0": "x" * 64_000}}
        with self.assertRaisesRegex(SharedJSONError, "^shared_json_limit$"):
            decode_shared_json(huge)
        chain = {str(index): {REF: str(index + 1)} for index in range(100)}
        chain["100"] = "terminal"
        with self.assertRaisesRegex(SharedJSONError, "^shared_json_limit$"):
            decode_shared_json({"format": FORMAT, "root": {REF: "0"}, "shared": chain})

    def test_input_cycles_non_json_types_and_non_finite_numbers_are_rejected(self):
        cyclic = []
        cyclic.append(cyclic)
        for value in (cyclic, {"bad": {1, 2}}, {1: "non-string key"}, float("inf"), float("nan")):
            with self.subTest(kind=type(value).__name__), self.assertRaises(SharedJSONError):
                encode_shared_json(value)


if __name__ == "__main__":
    unittest.main()
