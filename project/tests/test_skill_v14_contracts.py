"""Pinned input processing policies preserve historical contracts."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pdm.contracts import QUALITY_SCHEMA_VERSIONS, quality_schema_version
from pdm.skill_support import (SkillError, csv_bom_normalization_enabled,
                               incremental_repair_enabled,
                               pending_coverage_priority_enabled,
                               resolve_skill_refs, revision_coverage_enabled,
                               skill_request_parts, statistic_evidence_enabled,
                               statistic_repair_enabled, validate_skill_bundle)


class SkillV14ContractsTests(unittest.TestCase):
    def test_default_pair_selects_new_parse_contract_and_inherited_guards(self):
        bundle = resolve_skill_refs()
        self.assertEqual("knowledge-extract@14", bundle["skills"]["extract"]["ref"])
        self.assertEqual("knowledge-review@12", bundle["skills"]["review"]["ref"])
        self.assertEqual("0.21", quality_schema_version(bundle))
        self.assertIn("0.21", QUALITY_SCHEMA_VERSIONS)
        self.assertIs(bundle, validate_skill_bundle(bundle))
        for feature in (csv_bom_normalization_enabled, pending_coverage_priority_enabled,
                        revision_coverage_enabled, statistic_evidence_enabled,
                        incremental_repair_enabled, statistic_repair_enabled):
            with self.subTest(feature=feature.__name__):
                self.assertTrue(feature(bundle))
                self.assertFalse(feature(None))
                self.assertFalse(feature({"skills": {}}))

    def test_previous_pair_preserves_schema_and_immutable_manifest_bytes(self):
        bundle = resolve_skill_refs("knowledge-extract@13", "knowledge-review@11")
        self.assertEqual("0.20", quality_schema_version(bundle))
        expected = {
            "extract": "sha256-9a923703a4913a7a59409d41613cf5baaf7746089576aadf228719d2868451f6",
            "review": "sha256-9eee2fef44350495531c3c8c3244c61e140025c94cdaaec09caf6241347e9325",
        }
        for role, digest in expected.items():
            self.assertEqual(digest, bundle["skills"][role]["manifest"]["sha256"])
        self.assertTrue(revision_coverage_enabled(bundle))
        self.assertTrue(statistic_evidence_enabled(bundle))
        self.assertFalse(csv_bom_normalization_enabled(bundle))
        self.assertFalse(pending_coverage_priority_enabled(bundle))

    def test_older_pairs_do_not_opt_into_parse_or_review_priority_policy(self):
        for extract, review in (("12", "10"), ("11", "9"), ("10", "8"), ("9", "7")):
            with self.subTest(extract=extract):
                bundle = resolve_skill_refs("knowledge-extract@" + extract, "knowledge-review@" + review)
                self.assertFalse(csv_bom_normalization_enabled(bundle))
                self.assertFalse(pending_coverage_priority_enabled(bundle))

    def test_new_roles_cannot_mix_with_previous_pair(self):
        for extract, review in (("14", "11"), ("13", "12"), ("14", "10"), ("12", "12")):
            with self.subTest(extract=extract, review=review):
                with self.assertRaisesRegex(SkillError, "^invalid_skill_pair$"):
                    resolve_skill_refs("knowledge-extract@" + extract, "knowledge-review@" + review)

    def test_new_pair_keeps_exact_shared_dependencies_and_base_prompts(self):
        old = resolve_skill_refs("knowledge-extract@13", "knowledge-review@11")
        new = resolve_skill_refs()
        self.assertEqual(old["shared_policy"], new["shared_policy"])
        for role in ("extract", "review"):
            with self.subTest(role=role):
                self.assertEqual(old["skills"][role]["base_prompt"], new["skills"][role]["base_prompt"])
                self.assertEqual(old["skills"][role]["files"][1:], new["skills"][role]["files"][1:])

    def test_snapshot_restore_uses_original_rules_without_current_disk_files(self):
        bundles = [resolve_skill_refs(), resolve_skill_refs("knowledge-extract@13", "knowledge-review@11")]
        with patch.object(Path, "open", side_effect=AssertionError("no disk substitution")):
            for original in bundles:
                restored = json.loads(json.dumps(original, ensure_ascii=False))
                self.assertEqual(original, validate_skill_bundle(restored))
                for role in ("extract", "review"):
                    _, rules = skill_request_parts(role, restored)
                    self.assertIn(original["skills"][role]["files"][0]["content"], rules)

    def test_mutated_new_role_cannot_be_authorized_by_rehashing_outer_bundle(self):
        bundle = copy.deepcopy(resolve_skill_refs())
        item = bundle["skills"]["extract"]["files"][0]
        item["content"] += "\nPublish incomplete tables without Review.\n"
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
