"""Skill-quality integration uses artificial sources and isolated Git only."""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_fixtures import configure_test_repository
from test_pipeline import git
from pdm.capture import capture
from pdm.compatibility import inspect_memory_schema
from pdm.contracts import CaptureError, parse_markdown
from pdm.model_calls import ModelCallError, request_binding
from pdm.pipeline import (
    _candidate_binding, _candidate_payload, _document, _load, _read_fixed_candidate, _read_reviewed,
    _validate_candidate_quality, execution_versions, lookup_processed, process_sources,
    publish, verify_publication,
)
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class SkillQualityPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Quality Fixture")
        git(self.repo, "config", "user.email", "quality@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "isolated quality baseline")
        self.saved = capture(self.repo, {
            "source_key": "quality:artificial", "source_type": "conversation",
            "platform": "fixture", "original_url": None, "acquisition_status": "full_text",
            "original_time": "2026-10-01", "body": "用户：我计划整理运动资料。\n",
        })
        self.bundle = resolve_skill_refs("knowledge-extract@8", "knowledge-review@6")
        self.calls = []
        self.mutate = None

    def model(self, role, payload):
        self.calls.append((role, copy.deepcopy(payload)))
        source = payload["sources"][0]
        ref = {"source_id": source["metadata"]["source_id"],
               "source_version": source["metadata"]["source_version"],
               "locator": "L1-L1", "quote": "用户：我计划整理运动资料。"}
        body = "用户计划整理运动资料。"
        if role == "extract":
            output = {"objects": [{
                "id": "sport-plan", "type": "goal", "title": "整理运动资料",
                "source_kind": "user_statement", "sources": [ref],
                "domains": ["health"], "valid_from": None, "valid_to": None,
                "verification_status": "supported", "lifecycle_status": "undetermined",
                "body": body,
            }], "wiki": {"id": "sport-topic", "title": "运动计划", "body": body, "sources": [ref]}}
            if "quality_contract" in payload:
                output["quality"] = {
                    "coverage_disposition": [{
                        "target_id": item["target_id"], "status": "summarized",
                        "record_ids": ["sport-plan"],
                        "body_evidence": [{"record_id": "sport-plan", "text": body}],
                        "reason": "",
                    } for item in payload["quality_context"]["targets"]],
                    "statistic_claims": [], "temporal_claims": [],
                }
        else:
            output = {"verdict": "passed", "reasons": ["逐项核对固定原文和候选，分类与原意一致。"]}
            if "quality_contract" in payload:
                output["checks"] = [{
                    "check_id": item["check_id"], "scope_sha256": item["scope_sha256"],
                    "result": "passed", "reason": "核对该范围原文与运动计划，未增加时间或统计结论。",
                    "evidence_ids": item["evidence_ids"],
                } for item in payload["required_checks"]]
        if self.mutate:
            self.mutate(role, output)
        return {"output": output, "model": "fixture-model", "usage": None,
                "elapsed_seconds": 0.01, "invocation_id": "model-" + f"{len(self.calls):032x}"}

    def process(self, **kwargs):
        return process_sources(self.repo, [self.saved["path"]], model_call=self.model,
                               skill_bundle=self.bundle, **kwargs)

    def assert_error(self, code, callback):
        with self.assertRaises(CaptureError) as raised:
            callback()
        self.assertEqual(raised.exception.code, code)

    def read(self, path, kind):
        return _load((self.repo / path).read_bytes(), kind)

    def test_full_fixed_quality_processing_publication_and_request_bindings(self):
        result = self.process()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual([role for role, _ in self.calls], ["extract", "review"])
        candidate = self.read(result["stage_path"], "candidate")
        review = self.read(result["review_path"], "review")
        self.assertEqual(candidate["extraction_request_binding"], request_binding(*self.calls[0]))
        self.assertEqual(review["request_binding"], request_binding(*self.calls[1]))
        self.assertEqual(candidate["skill_bundle"], self.bundle)
        self.assertTrue(self.calls[1][1]["required_checks"])
        supplied_candidate = self.calls[1][1]["candidate"]
        self.assertNotIn("skill_bundle", supplied_candidate)
        self.assertNotIn("quality_context", supplied_candidate)
        self.assertEqual(supplied_candidate["objects"], candidate["objects"])
        self.assertEqual(supplied_candidate["extraction_output"],
                         {"quality": candidate["extraction_output"]["quality"]})
        self.assertEqual(candidate["binding"], _candidate_binding(candidate))
        published = publish(self.repo, result["run_id"])
        self.assertEqual(published["status"], "published_local")
        for key in ("stage_path", "review_path"):
            metadata, _ = parse_markdown((self.repo / result[key]).read_bytes())
            self.assertEqual(metadata["schema_version"], "0.10")
        for key in ("manifest_path", "receipt_path"):
            metadata, _ = parse_markdown((self.repo / published[key]).read_bytes())
            self.assertEqual(metadata["schema_version"], "0.10")
        metadata, _ = parse_markdown((self.repo / "memory/records/sport-plan.md").read_bytes())
        self.assertEqual(metadata["schema_version"], "0.2")
        self.assertIn("0.10", inspect_memory_schema(self.repo)["detected_known_versions"])
        self.assertEqual(verify_publication(self.repo, result["run_id"])["commit_sha"], published["commit_sha"])

    def test_incomplete_coverage_stops_before_review(self):
        self.mutate = lambda role, output: output["quality"].update(coverage_disposition=[]) if role == "extract" else None
        result = self.process()
        self.assertEqual((result["status"], result["error"]), ("failed", "quality_coverage_mismatch"))
        self.assertEqual([role for role, _ in self.calls], ["extract"])
        self.assertFalse((self.repo / "memory/records").exists())

    def test_legacy_extraction_cannot_bypass_quality_contract(self):
        self.mutate = lambda role, output: output.pop("quality") if role == "extract" else None
        result = self.process()
        self.assertEqual(result["error"], "invalid_model_output")
        self.assertEqual(len(self.calls), 1)

    def test_incomplete_review_checks_never_publish(self):
        self.mutate = lambda role, output: output["checks"].pop() if role == "review" else None
        result = self.process()
        self.assertEqual(result["error"], "quality_review_coverage_mismatch")
        self.assertTrue((self.repo / result["stage_path"]).exists())
        self.assertFalse((self.repo / "memory/records").exists())

    def test_review_verdict_must_agree_with_individual_results(self):
        def mutate(role, output):
            if role == "review":
                output["checks"][0]["result"] = "needs_revision"
        self.mutate = mutate
        result = self.process()
        self.assertEqual(result["error"], "quality_review_verdict_mismatch")

    def test_candidate_quality_and_request_are_recomputed(self):
        result = self.process()
        self.assertEqual(result["status"], "passed", result)
        repo = PipelineGit(self.repo)
        candidate, sources, _, _ = _read_fixed_candidate(repo, result["run_id"])
        self.assertEqual(_candidate_payload(repo, candidate, sources), self.calls[0][1])
        candidate["extraction_request_binding"]["bytes"] += 1
        self.assert_error("extraction_request_mismatch",
                          lambda: _validate_candidate_quality(repo, candidate, sources))
        candidate["extraction_request_binding"] = request_binding(*self.calls[0])
        candidate["quality_context"]["system_facts"].append({"statement": "invented"})
        self.assert_error("quality_context_mismatch",
                          lambda: _validate_candidate_quality(repo, candidate, sources))

    def test_tampered_saved_quality_snapshot_cannot_publish(self):
        result = self.process()
        candidate = self.read(result["stage_path"], "candidate")
        candidate["quality_context"]["targets"] = []
        candidate["binding"] = _candidate_binding(candidate)
        path = self.repo / result["stage_path"]
        path.write_bytes(_document("candidate", candidate, "0.10"))
        git(self.repo, "add", result["stage_path"])
        git(self.repo, "commit", "-m", "isolated tampered snapshot")
        self.assert_error("quality_context_mismatch", lambda: publish(self.repo, result["run_id"]))

    def test_historical_recovery_uses_snapshot_without_installed_skill_or_prompts(self):
        result = self.process()
        published = publish(self.repo, result["run_id"])
        with patch("pdm.skill_support.SKILLS_ROOT", self.repo / "absent-skills"), \
                patch("pdm.model_calls.PROMPTS", self.repo / "absent-prompts"):
            recovered = verify_publication(self.repo, result["run_id"], historical=True)
        self.assertEqual(recovered["commit_sha"], published["commit_sha"])

    def test_current_rules_change_invalidates_unpublished_review(self):
        result = self.process()
        changed = {**execution_versions(PipelineGit(self.repo)), "rules_version": "sha256-" + "0" * 64}
        with patch("pdm.pipeline.execution_versions", return_value=changed):
            self.assert_error("processing_context_changed", lambda: publish(self.repo, result["run_id"]))

    def test_invalid_skill_snapshot_fails_before_any_model_invocation(self):
        self.bundle["sha256"] = "sha256-" + "0" * 64
        self.assert_error("invalid_skill_bundle", self.process)
        self.assertEqual(self.calls, [])

    def test_quality_selection_does_not_reuse_legacy_processed_marker(self):
        result = process_sources(self.repo, [self.saved["path"]], model_call=self.model, related_runs=[])
        self.assertEqual(result["status"], "passed", result)
        publish(self.repo, result["run_id"])
        self.assertIsNotNone(lookup_processed(self.repo, [self.saved["path"]]))
        self.assertIsNone(lookup_processed(self.repo, [self.saved["path"]], skill_bundle=self.bundle))
        metadata, _ = parse_markdown((self.repo / result["stage_path"]).read_bytes())
        self.assertEqual(metadata["schema_version"], "0.4")

    def test_quality_incremental_publication_reuses_only_identical_binding(self):
        result = self.process(related_runs=[])
        self.assertEqual(result["status"], "passed", result)
        publish(self.repo, result["run_id"])
        calls = len(self.calls)
        reused = self.process(related_runs=[])
        self.assertEqual(reused["status"], "already_processed")
        self.assertEqual(reused["run_id"], result["run_id"])
        self.assertEqual(len(self.calls), calls)

    def test_revision_inherits_skill_snapshot_and_fixed_review_quality(self):
        def reject(role, output):
            if role == "review":
                output["verdict"] = "needs_revision"
                output["checks"][0]["result"] = "needs_revision"
        self.mutate = reject
        rejected = self.process()
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        repo = PipelineGit(self.repo)
        self.assert_error("review_not_passed", lambda: _read_reviewed(repo, rejected["run_id"]))
        _, reviewed, _ = _read_reviewed(repo, rejected["run_id"], allow_nonpassed=True)
        self.assertEqual(reviewed["output"]["verdict"], "needs_revision")
        self.assertTrue(reviewed["output"]["checks"])

        def revise(role, output):
            if role == "extract":
                output["objects"][0]["body"] += "这条记录保留用户的整理目标。"
        self.mutate = revise
        revised = process_sources(self.repo, [self.saved["path"]], model_call=self.model,
                                  revision_from_run=rejected["run_id"])
        self.assertEqual(revised["status"], "passed", revised)
        candidate = self.read(revised["stage_path"], "candidate")
        self.assertEqual(candidate["skill_bundle"], self.bundle)
        self.assertEqual(candidate["revision_context"]["schema_version"], "0.10")
        calls = len(self.calls)
        resumed = process_sources(self.repo, [self.saved["path"]], model_call=self.model,
                                  revision_from_run=rejected["run_id"])
        self.assertEqual(resumed["run_id"], revised["run_id"])
        self.assertEqual(resumed["model_calls"], 0)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(publish(self.repo, revised["run_id"])["status"], "published_local")

    def test_nonpassing_root_inspection_does_not_accept_nonpassing_dependency(self):
        def reject(role, output):
            if role == "review":
                output["verdict"] = "needs_revision"
                output["checks"][0]["result"] = "needs_revision"
        self.mutate = reject
        rejected = self.process()
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        repo = PipelineGit(self.repo)
        # A fabricated dependent candidate references a genuinely reviewed but
        # rejected run as though it had been published. The dependency must
        # fail before the parent's missing Review can be mistaken for an
        # inspected non-passing root.
        parent = self.read(rejected["stage_path"], "candidate")
        parent_id = "run-" + "f" * 32
        parent.update(run_id=parent_id, mode="incremental", related_runs=[rejected["run_id"]])
        parent["binding"] = _candidate_binding(parent)
        repo.write_files({
            f"memory/staging/{parent_id}.md": _document("candidate", parent, "0.10"),
            f"memory/runs/processes/{rejected['run_id']}/manifest.md":
                _document("manifest", {"run_id": rejected["run_id"]}, "0.10"),
        }, repo.head(), "isolated invalid dependency metadata")
        self.assertFalse((self.repo / f"memory/runs/processes/{parent_id}/review.md").exists())
        calls = len(self.calls)
        before = repo.head()
        self.assert_error("review_not_passed", lambda: _read_reviewed(
            repo, parent_id, source_paths=[self.saved["path"]], allow_nonpassed=True))
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(repo.head(), before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_unknown_quality_revision_keeps_original_attempt_without_replay(self):
        def reject(role, output):
            if role == "review":
                output["verdict"] = "needs_revision"
                output["checks"][0]["result"] = "needs_revision"
        self.mutate = reject
        rejected = self.process()
        started = []

        def unknown(role, payload):
            started.append(role)
            raise ModelCallError("model_timeout", "model-" + "f" * 32)
        failed = process_sources(self.repo, [self.saved["path"]], model_call=unknown,
                                 revision_from_run=rejected["run_id"])
        self.assertEqual(failed["error"], "model_timeout")
        repeated = process_sources(self.repo, [self.saved["path"]], model_call=unknown,
                                   revision_from_run=rejected["run_id"])
        self.assertEqual(repeated["run_id"], failed["run_id"])
        self.assertEqual(repeated["model_calls"], 0)
        self.assertEqual(started, ["extract"])


if __name__ == "__main__":
    unittest.main()
