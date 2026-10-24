"""Synthetic columnar-wire safety and exact historical-material regressions."""

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdm import skill_wire as v1, skill_wire_v2 as v2, skill_wire_v3 as v3


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def envelope(root):
    return {"format": v3.FORMAT, "value": {
        "format": v2.FORMAT, "keys": {}, "value": {
            "format": v1.FORMAT, "root": root, "shared": {}}}}


class SkillWireV3Tests(unittest.TestCase):
    def test_same_complete_key_sets_save_bytes_and_restore_every_column_value_and_row(self):
        original = {"rows": [{"first_measurement": i, "second_measurement": i + 1,
                              "third_measurement": i + 2, "fourth_measurement": i + 3}
                             for i in range(700)]}
        before, after = v2.encode_shared_json_v2(original), v3.encode_shared_json_v3(original)
        self.assertGreater(len(compact(before)) - len(compact(after)), 10_000)
        self.assertEqual(compact(before), compact(v3._restore_v2(after)))
        self.assertEqual(compact(original), compact(v3.decode_shared_json_v3(after)))
        self.assertEqual(compact(original), compact(v1.decode_shared_json(after)))
        self.assertEqual(compact(after), compact(v3.encode_shared_json_v3(
            json.loads(json.dumps(original, sort_keys=True)))))

    def test_empty_rows_missing_fields_null_and_numeric_types_remain_distinct(self):
        original = {"heterogeneous": [{"a": None}, {}, {"a": 0}, {"a": False, "b": 1}],
                    "empty": [], "empty_objects": [{}, {}, {}],
                    "numbers": [{"a": value, "b": [None, "", "原文\r\n💡"]}
                                for value in (1, 1.0, -0.0, False, None)]}
        self.assertEqual(compact(original), compact(v1.decode_shared_json(v3.encode_shared_json_v3(original))))
        self.assertEqual(original["heterogeneous"], v3._columnar(original["heterogeneous"]))
        self.assertEqual(original["empty_objects"], v3._columnar(original["empty_objects"]))

    def test_reserved_keys_and_marker_strings_are_literal_data_at_every_layer(self):
        original = {
            v3.TABLE: 1, "keys": ["source_field"], "rows": [["source-value"]],
            v3.LITERAL: {v3.TABLE: "data", "~0": v3.TABLE},
            "nested": [{v3.TABLE: i, v3.LITERAL: {"$pdm_ref": "0"},
                        "$pdm_literal": {"~literal": v3.LITERAL}} for i in range(25)],
        }
        self.assertEqual(compact(original), compact(v1.decode_shared_json(v3.encode_shared_json_v3(original))))
        # A restored row or literal dictionary is data, not a second table instruction.
        table = {v3.TABLE: 1, "keys": [v3.TABLE, "keys", "rows"],
                 "rows": [[1, ["x"], [[1]]], [2, [], []]]}
        expected = [{v3.TABLE: 1, "keys": ["x"], "rows": [[1]]},
                    {v3.TABLE: 2, "keys": [], "rows": []}]
        self.assertEqual(expected, v1.decode_shared_json(envelope(table)))
        literal = {v3.LITERAL: {v3.TABLE: "ordinary source", "string": v3.LITERAL}}
        self.assertEqual(literal[v3.LITERAL], v1.decode_shared_json(envelope(literal)))

    def test_nested_tables_keep_independent_mutable_rows_and_value_order(self):
        nested = {v3.TABLE: 1, "keys": ["a", "b"],
                  "rows": [[i, [None, False, 0.0, "原文"]] for i in range(5)]}
        table = {v3.TABLE: 1, "keys": ["child"],
                 "rows": [[copy.deepcopy(nested)], [copy.deepcopy(nested)]]}
        restored = v1.decode_shared_json(envelope(table))
        self.assertEqual(restored[0], restored[1])
        restored[0]["child"][0]["b"][0] = "changed"
        self.assertIsNone(restored[1]["child"][0]["b"][0])

    def test_table_shape_key_uniqueness_and_row_arity_are_strict(self):
        bad = [
            {v3.TABLE: True, "keys": ["a"], "rows": [[1]]},
            {v3.TABLE: 2, "keys": ["a"], "rows": [[1]]},
            {v3.TABLE: 1, "keys": ["a"], "rows": [[1]], "extra": 1},
            {v3.TABLE: 1, "keys": ["a", "a"], "rows": [[1, 2]]},
            {v3.TABLE: 1, "keys": [1], "rows": [[1]]},
            {v3.TABLE: 1, "keys": ["a"], "rows": [[1, 2]]},
            {v3.TABLE: 1, "keys": ["a"], "rows": [[]]},
            {v3.TABLE: 1, "keys": ["a"], "rows": [{"a": 1}]},
            {v3.TABLE: 1, "keys": ["a"], "rows": None},
            {v3.LITERAL: {"ordinary": 1}},
            {v3.LITERAL: {v3.TABLE: 1}, "extra": 2},
        ]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(v1.SharedJSONError):
                v3.decode_shared_json_v3(envelope(item))

    def test_budget_applies_to_materialized_rows_keys_values_and_depth(self):
        huge = {v3.TABLE: 1, "keys": ["x" * 100_000], "rows": [[i] for i in range(50)]}
        with self.assertRaises(v1.SharedJSONError):
            v3.decode_shared_json_v3(envelope(huge))
        node_heavy = {v3.TABLE: 1, "keys": ["a"], "rows": [[i] for i in range(30)]}
        with patch.object(v3, "MAX_NODES", 20), self.assertRaises(v1.SharedJSONError):
            v3.decode_shared_json_v3(envelope(node_heavy))
        deep = None
        for _ in range(200):
            deep = [deep]
        with self.assertRaises(v1.SharedJSONError):
            v3.decode_shared_json_v3(envelope(deep))

    def test_cycles_bad_scalars_and_outer_envelopes_fail_with_safe_errors(self):
        cyclic = []
        cyclic.append(cyclic)
        for value in (cyclic, {1: "non-string"}, float("inf"), float("nan"), {"x": {1, 2}}):
            with self.subTest(kind=type(value).__name__), self.assertRaises(v1.SharedJSONError):
                v3.encode_shared_json_v3(value)
        for invalid in ({"format": v3.FORMAT, "value": {}, "extra": None},
                        {"format": "unknown", "value": {}}, envelope(cyclic)):
            with self.assertRaises(v1.SharedJSONError):
                v3.decode_shared_json_v3(invalid)


if __name__ == "__main__":
    unittest.main()
