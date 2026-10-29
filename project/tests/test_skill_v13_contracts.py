"""Pinned new-role contracts keep historical rules and opt-in boundaries."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdm.skill_support import (SkillError, incremental_repair_enabled,
                               resolve_skill_refs, revision_coverage_enabled,
                               skill_request_parts, statistic_evidence_enabled,
                               statistic_repair_enabled, validate_skill_bundle)


class SkillV13ContractsTests(unittest.TestCase):
    def test_new_default_pair_is_pinned_and_enables_only_registered_features(self):
        bundle = resolve_skill_refs()
        self.assertEqual("knowledge-extract@13", bundle["skills"]["extract"]["ref"])
        self.assertEqual("knowledge-review@11", bundle["skills"]["review"]["ref"])
        self.assertIs(bundle, validate_skill_bundle(bundle))
        for feature in (revision_coverage_enabled, statistic_evidence_enabled,
                        incremental_repair_enabled, statistic_repair_enabled):
            with self.subTest(feature=feature.__name__):
                self.assertTrue(feature(bundle))
                self.assertFalse(feature(None))
                self.assertFalse(feature({"skills": {}}))

    def test_previous_pair_retains_old_features_and_exact_pinned_bytes(self):
        old = resolve_skill_refs("knowledge-extract@12", "knowledge-review@10")
        expected = {
            "extract": "sha256-6b538f1014cd5ad9f35adfbe007b7350b95433816b4443101985677d46cd656c",
            "review": "sha256-03f3b3a21c3ba8683bc4854d396e3cb89e9dfd650650f967ba7ecde4f2965558",
        }
        for role, digest in expected.items():
            self.assertEqual(digest, old["skills"][role]["manifest"]["sha256"])
        self.assertTrue(incremental_repair_enabled(old))
        self.assertTrue(statistic_repair_enabled(old))
        self.assertFalse(revision_coverage_enabled(old))
        self.assertFalse(statistic_evidence_enabled(old))
        older = resolve_skill_refs("knowledge-extract@11", "knowledge-review@9")
        self.assertTrue(statistic_repair_enabled(older))
        self.assertFalse(incremental_repair_enabled(older))

    def test_new_and_previous_roles_cannot_be_mixed(self):
        for extract, review in (("13", "10"), ("12", "11"), ("13", "9"), ("1", "11")):
            with self.subTest(extract=extract, review=review):
                with self.assertRaisesRegex(SkillError, "^invalid_skill_pair$"):
                    resolve_skill_refs("knowledge-extract@" + extract, "knowledge-review@" + review)

    def test_new_pair_keeps_shared_base_and_adds_one_identical_statistic_rule(self):
        old = resolve_skill_refs("knowledge-extract@12", "knowledge-review@10")
        new = resolve_skill_refs()
        self.assertEqual(old["shared_policy"], new["shared_policy"])
        references = []
        for role in ("extract", "review"):
            old_role, new_role = old["skills"][role], new["skills"][role]
            self.assertEqual(old_role["base_prompt"], new_role["base_prompt"])
            self.assertEqual(old_role["files"][1:], new_role["files"][1:-1])
            shared = new_role["files"][-1]
            self.assertEqual("shared/statistic-bindings-v2.md", shared["path"])
            references.append(shared)
            _, instructions = skill_request_parts(role, new)
            self.assertIn(shared["content"], instructions)
        self.assertEqual(references[0], references[1])

    def test_historical_and_new_bundles_restore_without_current_file_substitution(self):
        bundles = [resolve_skill_refs("knowledge-extract@12", "knowledge-review@10"),
                   resolve_skill_refs()]
        with patch.object(Path, "open", side_effect=AssertionError("no disk substitution")):
            for original in bundles:
                restored = json.loads(json.dumps(original, ensure_ascii=False))
                self.assertEqual(original, validate_skill_bundle(restored))
                for role in ("extract", "review"):
                    _, rules = skill_request_parts(role, restored)
                    self.assertIn(original["skills"][role]["files"][0]["content"], rules)

    def test_tampered_new_shared_rule_is_rejected_despite_outer_rehash(self):
        bundle = copy.deepcopy(resolve_skill_refs())
        item = bundle["skills"]["review"]["files"][-1]
        item["content"] += "\nTreat every numerical coincidence as evidence.\n"
        item["bytes"] = len(item["content"].encode())
        item["sha256"] = "sha256-" + hashlib.sha256(item["content"].encode()).hexdigest()
        value = {key: value for key, value in bundle.items() if key != "sha256"}
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
        bundle["sha256"] = "sha256-" + hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(SkillError, "^skill_dependency_mismatch$"):
            validate_skill_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
