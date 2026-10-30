"""Frozen ordinary short-claim contracts do not rewrite historical bundles."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdm.contracts import QUALITY_SCHEMA_VERSIONS, quality_schema_version
from pdm.skill_support import (SkillError, compact_statistics_enabled,
                               complete_feedback_enabled, csv_bom_normalization_enabled,
                               incremental_repair_enabled, pending_coverage_priority_enabled,
                               resolve_skill_refs, revision_coverage_enabled,
                               skill_request_parts, statistic_evidence_enabled,
                               statistic_repair_enabled, validate_skill_bundle)


class SkillV15ContractsTests(unittest.TestCase):
    def test_pinned_pair_selects_short_generation_with_existing_guards(self):
        bundle = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.assertEqual("knowledge-extract@15", bundle["skills"]["extract"]["ref"])
        self.assertEqual("knowledge-review@13", bundle["skills"]["review"]["ref"])
        self.assertEqual("0.22", quality_schema_version(bundle))
        self.assertIn("0.22", QUALITY_SCHEMA_VERSIONS)
        self.assertIs(bundle, validate_skill_bundle(bundle))
        for feature in (compact_statistics_enabled, complete_feedback_enabled,
                        csv_bom_normalization_enabled, pending_coverage_priority_enabled,
                        revision_coverage_enabled, statistic_evidence_enabled,
                        incremental_repair_enabled, statistic_repair_enabled):
            with self.subTest(feature=feature.__name__):
                self.assertTrue(feature(bundle))
                self.assertFalse(feature(None))
                self.assertFalse(feature({"skills": {}}))

    def test_previous_pair_keeps_original_policy_schema_and_manifest_bytes(self):
        old = resolve_skill_refs("knowledge-extract@14", "knowledge-review@12")
        self.assertEqual("0.21", quality_schema_version(old))
        self.assertEqual("knowledge-quality-v1", old["shared_policy"]["id"])
        self.assertEqual("sha256-9e384d9389642ac6287aa067a1ca3348f149bbb419a5becc7b24e4265c1f3d0c",
                         old["skills"]["extract"]["manifest"]["sha256"])
        self.assertEqual("sha256-71085225c3977f7ae9c2be19c615f5e3bab65057bf23c8aa264c985cb12930aa",
                         old["skills"]["review"]["manifest"]["sha256"])
        self.assertTrue(csv_bom_normalization_enabled(old))
        self.assertTrue(pending_coverage_priority_enabled(old))
        self.assertFalse(complete_feedback_enabled(old))
        self.assertFalse(compact_statistics_enabled(old))

    def test_older_pairs_do_not_opt_into_short_statistics_or_complete_feedback(self):
        for extract, review in (("13", "11"), ("12", "10"), ("11", "9"), ("10", "8")):
            with self.subTest(extract=extract):
                bundle = resolve_skill_refs("knowledge-extract@" + extract, "knowledge-review@" + review)
                self.assertFalse(compact_statistics_enabled(bundle))
                self.assertFalse(complete_feedback_enabled(bundle))

    def test_new_roles_cannot_mix_with_previous_role_or_policy(self):
        for extract, review in (("15", "12"), ("14", "13"), ("15", "11"), ("1", "13")):
            with self.subTest(extract=extract, review=review):
                with self.assertRaisesRegex(SkillError, "^invalid_skill_pair$"):
                    resolve_skill_refs("knowledge-extract@" + extract, "knowledge-review@" + review)

    def test_new_shared_policy_is_pinned_but_other_dependencies_and_base_stay(self):
        old = resolve_skill_refs("knowledge-extract@14", "knowledge-review@12")
        new = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.assertEqual("knowledge-quality-v2", new["shared_policy"]["id"])
        self.assertEqual("shared/knowledge-quality-v2.md", new["shared_policy"]["path"])
        self.assertNotEqual(old["shared_policy"], new["shared_policy"])
        for role in ("extract", "review"):
            with self.subTest(role=role):
                self.assertEqual(old["skills"][role]["base_prompt"], new["skills"][role]["base_prompt"])
                self.assertEqual(old["skills"][role]["files"][2:], new["skills"][role]["files"][2:])
                self.assertEqual(new["shared_policy"]["sha256"], new["skills"][role]["files"][1]["sha256"])

    def test_snapshot_restore_uses_original_policy_and_rules_without_disk(self):
        bundles = [resolve_skill_refs("knowledge-extract@15", "knowledge-review@13"), resolve_skill_refs("knowledge-extract@14", "knowledge-review@12")]
        with patch.object(Path, "open", side_effect=AssertionError("no disk substitution")):
            for original in bundles:
                restored = json.loads(json.dumps(original, ensure_ascii=False))
                self.assertEqual(original, validate_skill_bundle(restored))
                for role in ("extract", "review"):
                    _, rules = skill_request_parts(role, restored)
                    self.assertIn(original["skills"][role]["files"][1]["content"], rules)

    def test_outer_rehash_cannot_replace_old_shared_policy_with_new_policy(self):
        old = copy.deepcopy(resolve_skill_refs("knowledge-extract@14", "knowledge-review@12"))
        old["shared_policy"] = copy.deepcopy(resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")["shared_policy"])
        value = {key: value for key, value in old.items() if key != "sha256"}
        old["sha256"] = "sha256-" + hashlib.sha256(json.dumps(value, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        with self.assertRaisesRegex(SkillError, "^shared_policy_mismatch$"):
            validate_skill_bundle(old)

    def test_mutated_new_shared_policy_is_rejected_after_outer_rehash(self):
        bundle = copy.deepcopy(resolve_skill_refs("knowledge-extract@15", "knowledge-review@13"))
        item = bundle["skills"]["extract"]["files"][1]
        item["content"] += "\nIncomplete available data may be published.\n"
        item["bytes"] = len(item["content"].encode())
        item["sha256"] = "sha256-" + hashlib.sha256(item["content"].encode()).hexdigest()
        value = {key: value for key, value in bundle.items() if key != "sha256"}
        bundle["sha256"] = "sha256-" + hashlib.sha256(json.dumps(value, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        with self.assertRaisesRegex(SkillError, "^skill_dependency_mismatch$"):
            validate_skill_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
