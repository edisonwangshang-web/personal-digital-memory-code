"""Pinned stable-processing rules remain separate from historical contracts."""

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pdm.contracts import QUALITY_SCHEMA_VERSIONS, quality_schema_version
from pdm.skill_support import (
    SkillError, compact_statistics_enabled, complete_feedback_enabled,
    csv_bom_normalization_enabled, incremental_repair_enabled,
    pending_coverage_priority_enabled, resolve_skill_refs,
    revision_coverage_enabled, skill_request_parts, stable_processing_enabled,
    statistic_evidence_enabled, statistic_repair_enabled, validate_skill_bundle,
)


def rebind(value):
    raw = json.dumps({key: item for key, item in value.items() if key != "sha256"},
                     ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    value["sha256"] = "sha256-" + hashlib.sha256(raw).hexdigest()
    return value


class SkillV16ContractsTests(unittest.TestCase):
    def test_default_stable_pair_uses_its_own_schema_and_inherits_required_guards(self):
        bundle = resolve_skill_refs()
        self.assertEqual("knowledge-extract@16", bundle["skills"]["extract"]["ref"])
        self.assertEqual("knowledge-review@14", bundle["skills"]["review"]["ref"])
        self.assertEqual("knowledge-quality-v3", bundle["shared_policy"]["id"])
        self.assertEqual("0.23", quality_schema_version(bundle))
        self.assertIn("0.23", QUALITY_SCHEMA_VERSIONS)
        self.assertIs(bundle, validate_skill_bundle(bundle))
        for feature in (stable_processing_enabled, compact_statistics_enabled,
                        complete_feedback_enabled, csv_bom_normalization_enabled,
                        pending_coverage_priority_enabled, revision_coverage_enabled,
                        statistic_evidence_enabled, incremental_repair_enabled,
                        statistic_repair_enabled):
            with self.subTest(feature=feature.__name__):
                self.assertTrue(feature(bundle))
                self.assertFalse(feature(None))
                self.assertFalse(feature({"skills": {}}))

    def test_stable_feature_requires_both_roles_and_the_exact_pinned_v3_policy(self):
        bundle = resolve_skill_refs()
        for change in ("extract", "review", "id", "path", "sha256", "missing_policy"):
            value = copy.deepcopy(bundle)
            if change in {"extract", "review"}:
                value["skills"][change]["ref"] = "knowledge-" + change + "@1"
            elif change == "missing_policy":
                value.pop("shared_policy")
            else:
                value["shared_policy"][change] = "artificial replacement"
            with self.subTest(change=change):
                self.assertFalse(stable_processing_enabled(value))
        for pair in (("15", "13"), ("14", "12"), ("13", "11"), ("12", "10")):
            old = resolve_skill_refs("knowledge-extract@" + pair[0], "knowledge-review@" + pair[1])
            self.assertFalse(stable_processing_enabled(old))

    def test_previous_pair_and_policy_keep_exact_bytes_and_schema(self):
        old = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.assertEqual("0.22", quality_schema_version(old))
        self.assertEqual("knowledge-quality-v2", old["shared_policy"]["id"])
        self.assertEqual("sha256-faae6d362e124d4383280cc4d1ca3a76b283a9e7f403bfc62a58c562a6d5c521",
                         old["skills"]["extract"]["manifest"]["sha256"])
        self.assertEqual("sha256-c7f3f689dc7b1948a697dc5170c71096fdda16cb01a19ea21a5481d9da5a602d",
                         old["skills"]["review"]["manifest"]["sha256"])
        self.assertEqual("sha256-26497a500ab92f75e3eca664d5613a97f67d91851816668ae8e0222294987d50",
                         old["shared_policy"]["sha256"])
        self.assertTrue(compact_statistics_enabled(old))
        self.assertTrue(complete_feedback_enabled(old))

    def test_roles_cannot_mix_new_and_historical_revisions(self):
        for extract, review in (("16", "13"), ("15", "14"), ("16", "12"), ("14", "14")):
            with self.subTest(extract=extract, review=review):
                with self.assertRaisesRegex(SkillError, "^invalid_skill_pair$"):
                    resolve_skill_refs("knowledge-extract@" + extract, "knowledge-review@" + review)

    def test_new_pair_pins_v3_while_preserving_base_prompts_and_other_dependencies(self):
        old = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        new = resolve_skill_refs()
        for role in ("extract", "review"):
            with self.subTest(role=role):
                self.assertEqual(old["skills"][role]["base_prompt"], new["skills"][role]["base_prompt"])
                self.assertEqual(old["skills"][role]["files"][2:], new["skills"][role]["files"][2:])
                self.assertEqual(new["shared_policy"]["sha256"], new["skills"][role]["files"][1]["sha256"])

    def test_snapshot_restore_uses_each_original_policy_without_disk_substitution(self):
        bundles = [resolve_skill_refs(), resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")]
        with patch.object(Path, "open", side_effect=AssertionError("no disk substitution")):
            for original in bundles:
                restored = json.loads(json.dumps(original, ensure_ascii=False))
                self.assertEqual(original, validate_skill_bundle(restored))
                for role in ("extract", "review"):
                    _, rules = skill_request_parts(role, restored)
                    self.assertIn(original["skills"][role]["files"][1]["content"], rules)

    def test_v3_policy_and_rule_mutation_cannot_be_authorized_by_outer_rehash(self):
        for target in ("shared_policy", "rule"):
            bundle = copy.deepcopy(resolve_skill_refs())
            if target == "shared_policy":
                bundle["shared_policy"] = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")["shared_policy"]
                code = "shared_policy_mismatch"
            else:
                item = bundle["skills"]["extract"]["files"][0]
                item["content"] += "\nArtificial unauthorized exception.\n"
                item["bytes"] = len(item["content"].encode())
                item["sha256"] = "sha256-" + hashlib.sha256(item["content"].encode()).hexdigest()
                code = "skill_dependency_mismatch"
            with self.subTest(target=target):
                with self.assertRaisesRegex(SkillError, "^" + code + "$"):
                    validate_skill_bundle(rebind(bundle))


if __name__ == "__main__":
    unittest.main()
