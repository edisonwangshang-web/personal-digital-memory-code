"""Exact old claims may be materialized; synthetic Git authority stays required."""

from copy import deepcopy
import hashlib
import unittest
from unittest.mock import patch

import test_reuse_quality as legacy_fixture
from test_pipeline import git
from pdm.capture import capture
from pdm.contracts import CaptureError, canonical, markdown
from pdm.controls import read_controls, record_control
from pdm.knowledge_quality import prepare_quality, validate_quality_output
from pdm.pipeline import load_source
from pdm.reuse_quality import (INHERITANCE_CONTRACT, materialize_reuse_statistic_claims,
                               validate_reuse_quality)


class ReuseInheritanceTests(unittest.TestCase):
    # Reuse fixture methods without discovering its unrelated TestCase methods.
    setUp = legacy_fixture.ReuseQualityTests.setUp
    model = legacy_fixture.ReuseQualityTests.model
    refresh = legacy_fixture.ReuseQualityTests.refresh
    assert_error = legacy_fixture.ReuseQualityTests.assert_error

    def materialize(self, output=None, **changes):
        args = {"git": self.git, "related": self.related, "sources": self.sources,
                "context": self.context, "reused_objects": self.reused,
                "output": self.output if output is None else output,
                "controls_digest": read_controls(self.repo)["digest"]}
        args.update(changes)
        return materialize_reuse_statistic_claims(**args)

    def missing(self):
        output = deepcopy(self.output)
        output["quality"]["statistic_claims"] = []
        return output

    def full_output(self, materialized):
        return {"objects": [{**obj["metadata"], "body": obj["body"]}
                            for obj in self.reused if obj["metadata"]["type"] != "wiki"],
                "quality": materialized["quality"]}

    def test_omitted_claims_are_copied_from_verified_authority_without_changing_raw(self):
        output = self.missing()
        before = deepcopy(output)
        head = git(self.repo, "rev-parse", "HEAD")
        materialized, proof = self.materialize(output)
        self.assertEqual(output, before)
        self.assertEqual(materialized["quality"]["statistic_claims"],
                         sorted(self.output["quality"]["statistic_claims"],
                                key=lambda value: value["stat_id"]))
        self.assertEqual(proof["contract"], INHERITANCE_CONTRACT)
        self.assertEqual(proof["controls_digest"], read_controls(self.repo)["digest"])
        self.assertEqual(len(proof["claims"]), 2)
        for item in proof["claims"]:
            claim = next(value for value in self.output["quality"]["statistic_claims"]
                         if value["stat_id"] == item["stat_id"])
            self.assertEqual(item["record_id"], self.record_id)
            self.assertEqual(item["origin"]["stage_path"], self.old["stage_path"])
            saved = self.git.read(item["origin"]["stage_commit"], item["origin"]["stage_path"])
            self.assertEqual(item["origin"]["stage_sha256"], "sha256-" + hashlib.sha256(saved).hexdigest())
            self.assertEqual(item["claim_sha256"], "sha256-" + hashlib.sha256(canonical(claim)).hexdigest())
        validate_quality_output(self.full_output(materialized), self.context)
        self.assertEqual(len(validate_reuse_quality(self.git, self.related, self.sources,
                                                   self.context, self.reused, materialized)), 2)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_existing_wrong_model_claim_is_not_overwritten_or_silently_repaired(self):
        output = deepcopy(self.output)
        output["quality"]["statistic_claims"][0]["value"] = "999"
        output["quality"]["statistic_claims"] = output["quality"]["statistic_claims"][:1]
        before = deepcopy(output)
        materialized, proof = self.materialize(output)
        self.assertEqual(output, before)
        self.assertEqual(materialized["quality"]["statistic_claims"][0]["value"], "999")
        self.assertEqual(len(proof["claims"]), 1)
        self.assert_error("quality_statistic_mismatch",
                          lambda: validate_quality_output(self.full_output(materialized), self.context))

    def test_fully_declared_output_stays_exact_with_no_inheritance_entries(self):
        before = deepcopy(self.output)
        materialized, proof = self.materialize()
        self.assertEqual(materialized, before)
        self.assertEqual(self.output, before)
        self.assertEqual(proof["claims"], [])

    def test_changed_statistical_definition_is_not_inherited_even_with_same_value(self):
        context = deepcopy(self.context)
        context["statistic_definitions"]["operations"]["mean"] = "Different artificial method."
        materialized, proof = self.materialize(self.missing(), context=context)
        inherited = materialized["quality"]["statistic_claims"]
        self.assertEqual(len(inherited), 1)
        self.assertEqual(len(proof["claims"]), 1)
        row_count = next(stat for stat in context["statistics"] if stat["operation"] == "row_count")
        self.assertEqual(inherited[0]["stat_id"], row_count["stat_id"])

    def test_changed_source_scope_is_rejected_before_publication_verification(self):
        with patch("pdm.pipeline.verify_publication", side_effect=AssertionError("unselected source")) as verifier:
            self.assert_error("reuse_quality_source_scope",
                              lambda: self.materialize(self.missing(), sources=[]))
        verifier.assert_not_called()

    def test_changed_current_formal_bytes_are_not_covered_by_historical_authority(self):
        record = next(obj for obj in self.reused if obj["metadata"]["type"] != "wiki")
        relative = record["path"]
        (self.repo / relative).write_bytes(markdown(record["metadata"], record["body"] + "\nArtificial later text."))
        git(self.repo, "add", relative)
        git(self.repo, "commit", "-m", "artificial later formal change")
        materialized, proof = self.materialize(self.missing())
        self.assertEqual(materialized["quality"]["statistic_claims"], [])
        self.assertEqual(proof["claims"], [])

    def test_past_materialization_reconstructs_from_its_base_after_a_later_formal_update(self):
        original_head = git(self.repo, "rev-parse", "HEAD")
        output = self.missing()
        original, original_proof = self.materialize(output)
        record = next(obj for obj in self.reused if obj["metadata"]["type"] != "wiki")
        (self.repo / record["path"]).write_bytes(markdown(record["metadata"], record["body"] + "\nLater text."))
        git(self.repo, "add", record["path"])
        git(self.repo, "commit", "-m", "artificial later formal snapshot")
        restored, restored_proof = self.materialize(output, at_commit=original_head)
        self.assertEqual(restored, original)
        self.assertEqual(restored_proof, original_proof)

    def test_current_control_snapshot_mismatch_fails_before_opening_old_authority(self):
        with patch("pdm.reuse_quality._verified_collection", side_effect=AssertionError("old body read")) as collect:
            self.assert_error("reuse_quality_controls_changed",
                              lambda: self.materialize(self.missing(), controls_digest="sha256-" + "0" * 64))
        collect.assert_not_called()

    def test_historical_reconstruction_cannot_bypass_a_current_source_deletion(self):
        original_head = git(self.repo, "rev-parse", "HEAD")
        record_control(self.repo, {"request_key": "artificial:delete-statistic-source", "action": "delete",
            "source_paths": [self.saved["path"]], "object_paths": sorted(self.publication["files"])})
        with self.assertRaises(CaptureError):
            self.materialize(self.missing(), at_commit=original_head,
                             controls_digest=None)

    def test_a_corrected_source_cannot_gain_automatic_claim_inheritance(self):
        replacement = capture(self.repo, {"source_key": "artificial:replacement-table", "source_type": "note",
            "acquisition_status": "full_text", "body": "date,load_kg\n2025-01-01,11\n2025-01-02,21\n"})
        record_control(self.repo, {"request_key": "artificial:correct-old-statistics", "action": "corrects",
            "source_paths": [self.saved["path"]], "replacement_source_paths": [replacement["path"]],
            "object_paths": sorted(self.publication["files"])})
        sources = [load_source(self.repo, self.saved["path"], purpose="history"),
                   load_source(self.repo, replacement["path"], purpose="history")]
        context = prepare_quality(sources)
        materialized, proof = self.materialize(self.missing(), sources=sources, context=context)
        self.assertEqual(materialized["quality"]["statistic_claims"], [])
        self.assertEqual(proof["claims"], [])


if __name__ == "__main__":
    unittest.main()
