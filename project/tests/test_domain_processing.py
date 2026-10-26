"""Reviewed classification policy with disposable sources and fake model calls."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_pipeline
from pdm.capture import capture
from pdm.contracts import CaptureError, parse_markdown
from pdm.domains import DOMAIN_LABELS, DOMAIN_POLICY_VERSION, domain_ids, domain_policy
from pdm.evidence import build_current_evidence
from pdm.pipeline import execution_versions, process_sources, publish, verify_publication
from pdm.transaction import PipelineGit
from pdm import pipeline


class DomainPolicyTests(unittest.TestCase):
    def test_policy_is_json_only_and_returns_independent_values(self):
        policy = domain_policy()
        self.assertEqual(policy["version"], DOMAIN_POLICY_VERSION)
        self.assertEqual([item["id"] for item in policy["domains"]], list(DOMAIN_LABELS))
        self.assertEqual(json.loads(json.dumps(policy)), policy)
        policy["domains"][0]["definition"] = "changed"
        policy["legacy_aliases"]["数据质量"] = "health"
        self.assertNotEqual(domain_policy(), policy)

    def test_display_aliases_are_conservative_ordered_and_do_not_infer(self):
        self.assertEqual(domain_ids(["learning", "事业", "健康", "health", "学习"]), list(DOMAIN_LABELS))
        self.assertEqual(domain_ids(["数据质量", "睡眠", "事业规划", "工作", "运动"]), [])
        self.assertEqual(domain_ids(["健康", "career"], allow_legacy=False), ["career"])
        for labels in (None, "health", [1]):
            with self.subTest(labels=labels), self.assertRaises(CaptureError) as caught:
                domain_ids(labels)
            self.assertEqual(caught.exception.code, "invalid_domains")


class DomainProcessingTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    assert_error = test_pipeline.PipelineTests.assert_error

    def model(self, role, payload):
        response = test_pipeline.PipelineTests.model(self, role, payload)
        if role == "extract":
            response["output"]["objects"][0]["domains"] = list(self.labels)
            if getattr(self, "reuse", False):
                documents = {item["path"]: item for related in payload["related"] for item in related["documents"]}
                record = documents["memory/records/goal-isolated.md"]
                fields = set(response["output"]["objects"][0]) - {"body"}
                response["output"]["objects"] = [{**{key: deepcopy(record["metadata"][key]) for key in fields},
                                                  "body": record["body"]}]
                wiki = documents["memory/topics/topic-isolated.md"]
                response["output"]["wiki"] = {**{key: deepcopy(wiki["metadata"][key]) for key in ("id", "title", "sources")},
                                               "body": wiki["body"]}
        return response

    def process(self, *, paths=None, related=None):
        return process_sources(self.repo, paths or [self.saved["path"]], related_runs=related or [], model_call=self.model)

    def document(self, path):
        metadata, body = parse_markdown((self.repo / path).read_bytes())
        return metadata, json.loads(body)

    def extra_source(self):
        return capture(self.repo, {"source_key": "fixture:domain-extra", "source_type": "note",
                       "acquisition_status": "full_text", "body": "人工补充资料，尚未决定。\n"})["path"]

    def legacy_publication(self):
        # Synthetic legacy-shape version metadata. This fixture tests compatibility
        # without relying on a historical checkout or real engine identifiers.
        self.labels = ["健康", "旧自由标签"]
        versions = {
            "program_sha256": "sha256-1111111111111111111111111111111111111111111111111111111111111111",
            "rules_version": "sha256-2222222222222222222222222222222222222222222222222222222222222222",
            "program_commit": "3333333333333333333333333333333333333333",
        }
        with patch("pdm.pipeline.execution_versions", return_value=versions), \
                patch("pdm.engine_provenance.reservation_file", return_value={}):
            result = self.process()
            self.assertEqual(result["status"], "passed")
            publication = publish(self.repo, result["run_id"])
        return result, publication

    def test_three_domains_cross_domain_and_unclassified_use_two_calls(self):
        for labels in (["health"], ["career"], ["learning"], ["learning", "health", "career"], []):
            with self.subTest(labels=labels):
                # New sources give independent stable input identities; new
                # record IDs keep these cases in the same disposable repo.
                source = capture(self.repo, {"source_key": "fixture:domain:" + str(labels), "source_type": "note",
                                 "acquisition_status": "full_text", "body": "人工跨领域想法，尚未执行。\n仅为隔离资料。\n"})
                self.labels = labels
                def model(role, payload):
                    response = self.model(role, payload)
                    if role == "extract":
                        response["output"]["objects"][0]["id"] += f"-{self.number}"
                        response["output"]["wiki"]["id"] += f"-{self.number}"
                    return response
                before = self.number
                result = process_sources(self.repo, [source["path"]], related_runs=[], model_call=model)
                self.assertEqual(result["status"], "passed")
                self.assertEqual(self.number - before, 2)
                self.assertEqual(self.roles[-2:], ["extract", "review"])
                self.assertEqual(self.contexts[-2]["domain_policy"], self.contexts[-1]["domain_policy"])
                _, candidate = self.document(result["stage_path"])
                self.assertEqual(candidate["versions"]["domain_policy_version"], DOMAIN_POLICY_VERSION)
                publication = publish(self.repo, result["run_id"])
                record = next(path for path in publication["files"] if path.startswith("memory/records/"))
                metadata, _ = parse_markdown((self.repo / record).read_bytes())
                self.assertEqual(metadata["domains"], labels)
                self.assertEqual(len(publication["files"]), 2)
                self.assertEqual(self.number - before, 2)

    def test_invalid_duplicate_and_noncanonical_domains_fail_before_review(self):
        for labels in (["health", "health"], ["健康"], ["other"], ["health", "数据质量"]):
            with self.subTest(labels=labels):
                self.labels = labels
                calls = self.number
                result = self.process()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"], "invalid_domains")
                self.assertEqual(self.number - calls, 1)
                _, failure = self.document(result["failure_path"])
                self.assertEqual(failure["attempted_calls"], 1)
                self.assertEqual(failure["call"]["invocation_id"], "model-" + f"{self.number:032x}")
                self.assertFalse((self.repo / "memory/records").exists())
                self.assertFalse((self.repo / f"memory/staging/{result['run_id']}.md").exists())
                self.assert_error("git_failed", lambda: publish(self.repo, result["run_id"]))

    def test_classification_remains_subject_to_independent_review(self):
        self.labels = ["health"]
        self.verdict = "needs_revision"
        result = self.process()
        self.assertEqual(result["status"], "needs_revision")
        self.assertEqual(self.roles, ["extract", "review"])
        self.assert_error("review_not_passed", lambda: publish(self.repo, result["run_id"]))
        self.assertFalse((self.repo / "memory/records").exists())

    def test_legacy_publication_and_exact_reuse_keep_all_formal_bytes(self):
        old, publication = self.legacy_publication()
        before = {path: (self.repo / path).read_bytes() for path in publication["files"]}
        self.assertEqual(verify_publication(self.repo, old["run_id"])["commit_sha"], publication["commit_sha"])
        self.assertEqual(verify_publication(self.repo, old["run_id"], historical=True)["commit_sha"], publication["commit_sha"])
        paths = [self.saved["path"], self.extra_source()]
        self.reuse = True
        result = self.process(paths=paths, related=[old["run_id"]])
        self.assertEqual(result["status"], "passed")
        newer = publish(self.repo, result["run_id"])
        self.assertTrue(all(operation["action"] == "reuse" for operation in newer["operations"].values()))
        self.assertEqual(before, {path: (self.repo / path).read_bytes() for path in before})
        current = build_current_evidence(self.repo, paths, [result["run_id"]])
        record = next(document for document in current["publications"][0]["documents"] if document["metadata"]["type"] != "wiki")
        self.assertEqual(record["metadata"]["domains"], ["健康", "旧自由标签"])
        calls = self.number
        repeated = self.process(paths=paths, related=[old["run_id"]])
        self.assertEqual(repeated["status"], "already_processed")
        self.assertEqual(self.number, calls)
        self.assertEqual({path: (self.repo / path).read_bytes() for path in before}, before)

    def test_changed_legacy_record_does_not_get_reuse_domain_exemption(self):
        old, publication = self.legacy_publication()
        before = {path: (self.repo / path).read_bytes() for path in publication["files"]}
        self.labels = ["健康"]
        result = self.process(paths=[self.saved["path"], self.extra_source()], related=[old["run_id"]])
        self.assertEqual((result["status"], result["error"]), ("failed", "invalid_domains"))
        self.assertEqual(before, {path: (self.repo / path).read_bytes() for path in before})

    def test_policy_digest_change_rejects_old_review_and_changes_processing_key(self):
        self.labels = ["health"]
        result = self.process()
        original_head = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        original = execution_versions(PipelineGit(self.repo))
        changed_policy = domain_policy()
        changed_policy["domains"][0]["definition"] += "changed"
        source_fields = {"source": {"source_id": "artificial", "source_version": "version"}}
        with patch("pdm.pipeline.domain_policy", return_value=changed_policy):
            changed = execution_versions(PipelineGit(self.repo))
            self.assertNotEqual(changed["rules_version"], original["rules_version"])
            self.assertNotEqual(pipeline._processing_key(source_fields, original, "controls"),
                                pipeline._processing_key(source_fields, changed, "controls"))
            self.assert_error("invalid_domain_policy", lambda: publish(self.repo, result["run_id"]))
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), original_head)
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertFalse((self.repo / "memory/records").exists())
        self.assertEqual(publish(self.repo, result["run_id"])["status"], "published_local")

    def test_unknown_policy_marker_and_forged_reuse_are_rejected(self):
        self.labels = ["health"]
        result = self.process()
        _, candidate = self.document(result["stage_path"])
        for versions in ({**candidate["versions"], "domain_policy_version": "unknown"},
                         {key: value for key, value in candidate["versions"].items() if key != "domain_policy_sha256"}):
            with self.subTest(versions=versions):
                self.assert_error("invalid_domain_policy", lambda: pipeline._validate_domain_policy(versions))
        obj = candidate["objects"][0]
        old = {"path": obj["path"], "metadata": deepcopy(obj["metadata"]), "body": obj["body"],
               "content_version": obj["metadata"]["content_version"]}
        old["metadata"]["domains"] = ["career"]
        self.assert_error("invalid_domain_reuse", lambda: pipeline._validate_domains(
            [obj], [{"documents": [old]}], {obj["path"]: {"action": "reuse"}}, candidate["versions"]))

    def test_removing_policy_markers_cannot_reuse_the_original_review(self):
        self.labels = ["health"]
        result = self.process()
        _, candidate = self.document(result["stage_path"])
        original_stage = (self.repo / result["stage_path"]).read_bytes()
        stage_commit = test_pipeline.git(self.repo, "log", "-1", "--format=%H", "--", result["stage_path"])
        original_review = (self.repo / result["review_path"]).read_bytes()
        review_commit = test_pipeline.git(self.repo, "log", "-1", "--format=%H", "--", result["review_path"])
        review_parent = test_pipeline.git(self.repo, "rev-parse", review_commit + "^")
        self.assertEqual(review_parent, stage_commit)
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", stage_commit + "^"), candidate["base_head"])
        candidate["versions"].pop("domain_policy_version")
        candidate["versions"].pop("domain_policy_sha256")
        candidate["binding"] = pipeline._candidate_binding(candidate)
        stage = self.repo / result["stage_path"]
        stage.write_bytes(pipeline._document("candidate", candidate, "0.4"))
        test_pipeline.git(self.repo, "add", result["stage_path"])
        test_pipeline.git(self.repo, "commit", "-m", "artificial candidate marker removal")
        tampered_head = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        before = {str(path.relative_to(self.repo)): path.read_bytes()
                  for path in (self.repo / "memory").rglob("*") if path.is_file()}
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", tampered_head + "^"), review_commit)
        self.assertEqual(test_pipeline.git(self.repo, "status", "--porcelain"), "")
        # The fixed-candidate verifier now rejects the changed stage parent
        # before the original Review can be considered for publication.
        self.assert_error("candidate_mismatch", lambda: publish(self.repo, result["run_id"]))
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), tampered_head)
        self.assertEqual(test_pipeline.git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(before, {str(path.relative_to(self.repo)): path.read_bytes()
                                 for path in (self.repo / "memory").rglob("*") if path.is_file()})
        self.assertEqual(PipelineGit(self.repo).read(stage_commit, result["stage_path"]), original_stage)
        self.assertEqual(test_pipeline.git(self.repo, "log", "-1", "--format=%H", "--", result["stage_path"]), tampered_head)
        self.assertEqual((self.repo / result["review_path"]).read_bytes(), original_review)
        self.assertEqual(test_pipeline.git(self.repo, "log", "-1", "--format=%H", "--", result["review_path"]), review_commit)
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", review_commit + "^"), review_parent)
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertFalse((self.repo / "memory/records").exists())
        self.assertFalse((self.repo / "memory/topics").exists())
        for name in ("manifest", "receipt"):
            self.assertFalse((self.repo / pipeline._run_path(result["run_id"], name)).exists())
