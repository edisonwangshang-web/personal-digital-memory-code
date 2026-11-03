"""Reviewed classification policy with disposable sources and fake model calls."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import test_pipeline
from pdm.capture import capture
from pdm.contracts import CaptureError, parse_markdown
from pdm.domains import DOMAIN_LABELS, DOMAIN_POLICY_VERSION, DOMAIN_POLICY_VERSIONS, domain_ids, domain_policy
from pdm.evidence import build_current_evidence
from pdm.pipeline import execution_versions, process_sources, publish, verify_publication
from pdm.transaction import PipelineGit
from pdm import pipeline


class DomainPolicyTests(unittest.TestCase):
    def test_registered_v1_bytes_survive_the_new_default(self):
        self.assertEqual(DOMAIN_POLICY_VERSION, "three-domains-v2")
        self.assertEqual(DOMAIN_POLICY_VERSIONS, ("three-domains-v1", "three-domains-v2"))
        self.assertEqual(pipeline._hash(domain_policy("three-domains-v1")),
                         "sha256-630587eb95be41d7effcdbe5d6e90b47b5921bf9341c8a17c91f1991dd1892eb")
        self.assertNotEqual(domain_policy(), domain_policy("three-domains-v1"))
        for version in DOMAIN_POLICY_VERSIONS:
            policy = domain_policy(version)
            versions = {"domain_policy_version": version, "domain_policy_sha256": pipeline._hash(policy)}
            self.assertTrue(pipeline._validate_domain_policy(versions))
            self.assertEqual(pipeline._domain_policy_for_versions(versions), policy)
            policy["rules"].append("Artificial mutation must not change registry values.")
            self.assertNotEqual(policy, domain_policy(version))

    def test_unknown_versions_and_mixed_policy_digests_are_rejected(self):
        for version in ("", "unknown", False, 1, [], {}):
            with self.subTest(version=version), self.assertRaises(CaptureError) as caught:
                domain_policy(version)
            self.assertEqual(caught.exception.code, "invalid_domain_policy")
        for version in DOMAIN_POLICY_VERSIONS:
            other = next(item for item in DOMAIN_POLICY_VERSIONS if item != version)
            versions = {"domain_policy_version": version, "domain_policy_sha256": pipeline._hash(domain_policy(other))}
            with self.subTest(version=version), self.assertRaises(CaptureError) as caught:
                pipeline._domain_policy_for_versions(versions)
            self.assertEqual(caught.exception.code, "invalid_domain_policy")
        self.assertEqual(pipeline._domain_policy_for_versions({}), domain_policy("three-domains-v1"))

    def test_historical_embedded_engine_selects_its_own_literal_policy_version(self):
        from engine_fixtures import isolated_engine
        from pdm.quality_completed_extraction import _versions_at
        engine = isolated_engine(self)
        path = engine / "project/src/pdm/domains.py"
        current = path.read_text()
        path.write_text(current.replace('DOMAIN_POLICY_VERSION = "three-domains-v2"',
                                        'DOMAIN_POLICY_VERSION = "three-domains-v1"'))
        test_pipeline.git(engine, "add", ".")
        test_pipeline.git(engine, "commit", "-m", "artificial original v1 policy default")
        old_commit = test_pipeline.git(engine, "rev-parse", "HEAD")
        old = _versions_at(PipelineGit(engine), old_commit)
        path.write_text(current)
        test_pipeline.git(engine, "add", ".")
        test_pipeline.git(engine, "commit", "-m", "artificial v2 policy default")
        new_commit = test_pipeline.git(engine, "rev-parse", "HEAD")
        self.assertEqual(_versions_at(PipelineGit(engine), old_commit), old)
        self.assertEqual(old["domain_policy_version"], "three-domains-v1")
        self.assertEqual(old["domain_policy_sha256"], pipeline._hash(domain_policy("three-domains-v1")))
        new = _versions_at(PipelineGit(engine), new_commit)
        self.assertEqual(new["domain_policy_version"], "three-domains-v2")
        self.assertEqual(old["rules_version"], pipeline._rules_version_for_domain_policy("three-domains-v1"))
        self.assertEqual(new["rules_version"], pipeline._rules_version_for_domain_policy("three-domains-v2"))
        self.assertNotEqual(old["rules_version"], new["rules_version"])

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

    def test_reference_material_purpose_reaches_both_roles_without_becoming_a_user_goal(self):
        material_rule = ("用户已明确相关资料的归档领域且内容与之相关时，应在资料的实质知识记录中体现这一领域，"
                         "必要时与其他领域并列；不能只给新增用途或收藏记录打标而让相关内容继续缺少该领域。")
        cases = (
            ("health", "请作为训练管理的参考资料整理。", "人工材料介绍通用负荷规划，没有用户已经训练的记录。"),
            ("career", "请作为业务方案的参考资料整理。", "人工材料介绍机构研究流程，没有用户已经采用的记录。"),
            ("learning", "请作为自学课程的参考资料整理。", "人工材料介绍练习安排，没有用户已经掌握的记录。"),
        )
        for domain, purpose, material in cases:
            with self.subTest(domain=domain):
                source = capture(self.repo, {"source_key": "artificial:reference-purpose:" + domain,
                    "source_type": "note", "acquisition_status": "full_text", "body": purpose + "\n" + material + "\n"})
                self.labels = [domain]
                def model(role, payload):
                    self.assertEqual(payload["domain_policy"], domain_policy("three-domains-v2"))
                    self.assertIn(material_rule, payload["domain_policy"]["rules"], role)
                    self.assertEqual(payload["sources"][0]["body"].splitlines()[0], purpose)
                    response = self.model(role, payload)
                    if role == "extract":
                        record = response["output"]["objects"][0]
                        record.update(id="artificial-reference-" + domain, type="knowledge",
                                      title="人工参考材料", body=material)
                        record["sources"][0].update(locator="L2-L2", quote=material)
                        response["output"]["wiki"].update(id="artificial-topic-" + domain, body=material)
                    else:
                        self.assertEqual(payload["candidate"]["objects"][0]["body"], material)
                    return response
                calls = self.number
                result = process_sources(self.repo, [source["path"]], model_call=model)
                self.assertEqual(result["status"], "passed", result)
                published = publish(self.repo, result["run_id"])
                record_path = next(path for path in published["files"] if path.startswith("memory/records/"))
                metadata, body = parse_markdown((self.repo / record_path).read_bytes())
                self.assertEqual((metadata["type"], metadata["domains"], body.strip()), ("knowledge", [domain], material))
                self.assertEqual(self.number - calls, 2)

    def test_v1_review_payload_and_publication_remain_exact_under_v2_default(self):
        self.labels = ["health"]
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            result = self.process()
            self.assertEqual(result["status"], "passed", result)
            publication = publish(self.repo, result["run_id"])
        self.assertEqual(domain_policy()["version"], "three-domains-v2")
        self.assertEqual([context["domain_policy"] for context in self.contexts],
                         [domain_policy("three-domains-v1")] * 2)
        before = {path: (self.repo / path).read_bytes() for path in publication["files"]}
        checkpoint = PipelineGit(self.repo).head(), self.number
        self.assertEqual(verify_publication(self.repo, result["run_id"])["commit_sha"], publication["commit_sha"])
        self.assertEqual(verify_publication(self.repo, result["run_id"], historical=True)["commit_sha"], publication["commit_sha"])
        self.assertEqual((PipelineGit(self.repo).head(), self.number), checkpoint)
        self.assertEqual(before, {path: (self.repo / path).read_bytes() for path in before})

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


class DomainPolicyRecoveryTests(unittest.TestCase):
    def test_old_known_request_authenticates_before_cross_policy_recovery_is_refused(self):
        import test_known_extraction
        case = test_known_extraction.KnownExtractionTests(
            "test_known_response_proof_is_zero_call_read_only_and_keeps_original_output")
        self.addCleanup(case.doCleanups)
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            case.setUp()
            fixture = case.completed()
        before = fixture["git"].head(), fixture["model_call"].ledger.path.read_bytes(), case.number
        # Original bytes authenticate first; the existing cross-rule recovery
        # gate still refuses adopting this output under today's v2 policy.
        with self.assertRaises(CaptureError) as caught:
            case.verify_completed(fixture)
        self.assertEqual(caught.exception.code, "completed_extraction_rules_changed")
        self.assertEqual(fixture["original_payload"]["domain_policy"], domain_policy("three-domains-v1"))
        self.assertEqual((fixture["git"].head(), fixture["model_call"].ledger.path.read_bytes(), case.number), before)

    def test_old_quality_cache_authenticates_before_cross_policy_recovery_is_refused(self):
        import test_quality_completed_extraction
        case = test_quality_completed_extraction.QualityCompletedExtractionTests(
            "test_preparation_and_runtime_validation_are_zero_call_and_preserve_original_proof")
        self.addCleanup(case.doCleanups)
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            case.setUp()
        before = case.git.head(), case.ledger_path.read_bytes(), case.number
        prepared = case.prepare()
        with self.assertRaises(CaptureError) as caught:
            case.verify(prepared)
        self.assertEqual(caught.exception.code, "quality_completed_context_changed")
        self.assertEqual(prepared["original_payload"]["domain_policy"], domain_policy("three-domains-v1"))
        self.assertEqual(prepared["original_payload"], case.payload)
        self.assertEqual((case.git.head(), case.ledger_path.read_bytes(), case.number), before)
