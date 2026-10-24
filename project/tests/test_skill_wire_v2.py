"""Synthetic regressions for reversible field names layered over frozen v1."""

import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdm import skill_wire as v1
from pdm.skill_wire_v2 import FORMAT, decode_shared_json_v2, encode_shared_json_v2


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode()


def envelope(root, keys=None, shared=None):
    return {"format": FORMAT, "keys": keys or {}, "value": {
        "format": v1.FORMAT, "root": root, "shared": shared or {}}}


class SkillWireV2Tests(unittest.TestCase):
    def test_all_keys_values_types_quotes_and_order_roundtrip_without_alias_substitution_in_values(self):
        source = {"source_id": "src-" + "a" * 64, "source_version": "sha256-" + "b" * 64,
                  "locator": "L1-L2", "quote": "\ufeff原字段,值\r\n原文字段,0\n"}
        original = {"~0": "~0", "~~literal": {"$pdm_ref": "not-a-reference"},
                    "$pdm_literal": {"~key": "$pdm_literal"},
                    "rows": [{"long_repeated_metric_name": index, "sources": [source],
                              "ordered": [False, None, 0, 0.0, -0.0, "", "~0", "中文💡"]}
                             for index in range(60)]}
        wire = encode_shared_json_v2(original)
        self.assertTrue(wire["keys"])
        self.assertEqual(compact(original), compact(decode_shared_json_v2(wire)))
        self.assertEqual(compact(original), compact(v1.decode_shared_json(wire)))
        restored = v1.decode_shared_json(wire)
        restored["rows"][0]["sources"][0]["quote"] = "changed"
        self.assertEqual(source["quote"], restored["rows"][1]["sources"][0]["quote"])
        self.assertEqual("~0", decode_shared_json_v2(envelope({"~0": "~0"}, {"~0": "field"}))["field"])

    def test_repeated_field_names_save_capacity_without_changing_any_value(self):
        original = {"rows": [
            {"descriptive_observation_measurement_field": index,
             "corresponding_observation_source_line_number": index + 1,
             "unmodified_raw_observation_text": "原文-" + str(index)}
            for index in range(600)]}
        old, new = v1.encode_shared_json(original), encode_shared_json_v2(original)
        self.assertGreater(len(compact(old)) - len(compact(new)), 18_000)
        self.assertEqual(compact(original), compact(v1.decode_shared_json(new)))

    def test_deterministic_dictionary_and_historical_v1_decoder_still_work(self):
        original = {"rows": [{"a_long_metric_name": index, "another_long_name": "~literal"}
                             for index in range(20)], "~source_key": "value"}
        first = encode_shared_json_v2(original)
        reordered = json.loads(json.dumps(original, sort_keys=True))
        self.assertEqual(compact(first), compact(encode_shared_json_v2(reordered)))
        old = v1.encode_shared_json(original)
        self.assertEqual(v1.FORMAT, old["format"])
        self.assertEqual(original, v1.decode_shared_json(old))
        self.assertEqual(original, v1._decode_shared_json_v1(old))

    def test_literal_alias_names_and_reference_markers_are_not_interpreted_as_instructions(self):
        original = {"~0": {"~~0": {"~": {"$pdm_ref": "0"}}},
                    "$pdm_literal": {"$pdm_literal": {"$pdm_ref": "~0"}},
                    "keys": {"~0": "replace-source"}, "value": ["~0", "$pdm_ref", "$pdm_literal"]}
        self.assertEqual(original, decode_shared_json_v2(encode_shared_json_v2(original)))

    def test_undefined_unused_duplicate_and_colliding_key_definitions_are_rejected(self):
        cases = [
            envelope({"~0": 1}),
            envelope({"field": 1}, {"~0": "unused"}),
            envelope({"~0": 1, "~1": 2}, {"~0": "same", "~1": "same"}),
            envelope({"~0": 1, "field": 2}, {"~0": "field"}),
            envelope({"~0": 1, "~~field": 2}, {"~0": "~field"}),
            envelope({"field": 1}, {"not-an-alias": "field"}),
            envelope({"field": 1}, {"~~0": "field"}),
            envelope({"field": 1}, {"~0": 7}),
        ]
        for case in cases:
            with self.subTest(keys=case["keys"]), self.assertRaises(v1.SharedJSONError):
                decode_shared_json_v2(case)

    def test_bad_envelopes_and_inner_v1_reference_errors_are_rejected(self):
        cases = [
            {"format": FORMAT, "keys": {}, "value": {}, "extra": 1},
            {"format": FORMAT, "keys": [], "value": {}},
            {"format": "unknown", "keys": {}, "value": {}},
            envelope({v1.REF: "missing"}),
            envelope({v1.REF: "0"}, shared={"0": {v1.REF: "0"}}),
            envelope({}, shared={"0": "unused"}),
            envelope({v1.LITERAL: {"ordinary": 1}}),
        ]
        for case in cases:
            with self.subTest(format=case["format"]), self.assertRaises(v1.SharedJSONError):
                v1.decode_shared_json(case)

    def test_limits_cover_key_expansion_deep_data_and_v1_reference_chains(self):
        cases = [envelope([{"~0": i} for i in range(50)], {"~0": "x" * 100_000})]
        nested = None
        for _ in range(200):
            nested = [nested]
        cases.append(envelope(nested))
        shared = {str(i): {v1.REF: str(i + 1)} for i in range(1500)}
        shared["1500"] = "end"
        cases.append(envelope({v1.REF: "0"}, shared=shared))
        for case in cases:
            with self.assertRaises(v1.SharedJSONError):
                decode_shared_json_v2(case)

    def test_cycles_non_json_keys_and_non_finite_values_fail_safely(self):
        cyclic = []
        cyclic.append(cyclic)
        for value in (cyclic, {1: "key"}, {"x": {1, 2}}, float("inf"), float("nan")):
            with self.subTest(kind=type(value).__name__), self.assertRaises(v1.SharedJSONError):
                encode_shared_json_v2(value)
        malformed = envelope({})
        malformed["value"]["root"] = malformed["value"]
        with self.assertRaises(v1.SharedJSONError):
            decode_shared_json_v2(malformed)


if __name__ == "__main__":
    unittest.main()
