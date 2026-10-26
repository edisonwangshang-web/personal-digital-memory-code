"""Exact incremental references over artificial health/career/learning data only."""

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from quality_fixtures import add_quality, passed_review
from test_pipeline import git
from pdm import compatibility, pipeline
from pdm.batching import plan_batches, project_batch_sources
from pdm.capture import capture
from pdm.contracts import CaptureError, markdown, parse_markdown
from pdm.domains import DOMAIN_POLICY_VERSION, domain_policy
from pdm.knowledge_quality import prepare_quality
from pdm.model_calls import _valid, output_schema, request_binding
from pdm.pipeline import (_load, _objects, _read_fixed_candidate, process_sources, publish,
                          resume_decomposed_processing, verify_publication)
from pdm.skill_support import SkillError, resolve_skill_refs, validate_skill_bundle
from pdm.transaction import PipelineGit


RECORD_FIELDS = ("id", "type", "title", "source_kind", "sources", "domains", "valid_from", "valid_to",
                 "verification_status", "lifecycle_status")


def full_record(document):
    return {**{key: copy.deepcopy(document["metadata"][key]) for key in RECORD_FIELDS},
            "body": document["body"]}


class ReuseReferenceContractTests(unittest.TestCase):
    def setUp(self):
        self.bundle = resolve_skill_refs("knowledge-extract@9", "knowledge-review@7")
        self.old_bundle = resolve_skill_refs("knowledge-extract@8", "knowledge-review@6")
        self.source = {"source_id": "src-" + "1" * 64, "source_version": "sha256-" + "2" * 64,
                       "path": "memory/sources/inbox/src-" + "1" * 64 + "/sha256-" + "2" * 64 + ".md",
                       "metadata": {"source_type": "note", "acquisition_status": "full_text", "original_time": None},
                       "body": "人工资料：运动记录、职业记录和学习记录，均仅用于隔离测试。\n"}
        self.sources = [self.source]
        self.context = prepare_quality(self.sources)
        self.response = {"model": "artificial-fixture"}
        self.versions = {"rules_version": "sha256-" + "3" * 64,
                         "domain_policy_version": DOMAIN_POLICY_VERSION,
                         "domain_policy_sha256": pipeline._hash(domain_policy())}
        self.output, self.related = self.seed()
        self.documents = {doc["path"]: doc for doc in self.related[0]["documents"]}
        self.record_paths = [path for path in self.documents if path.startswith("memory/records/")]
        self.wiki_path = next(path for path in self.documents if path.startswith("memory/topics/"))

    def seed(self, labels=None):
        records = [{"id": "record-" + domain, "type": "knowledge", "title": "人工" + domain,
                    "source_kind": "user_statement", "sources": [{"source_id": self.source["source_id"],
                    "source_version": self.source["source_version"], "locator": "L1-L1",
                    "quote": self.source["body"].strip()}],
                    "domains": list(labels if labels is not None and index == 0 else [domain]),
                    "valid_from": None, "valid_to": None, "verification_status": "unverified",
                    "lifecycle_status": "undetermined", "body": "人工" + domain + "资料范围与限制。"}
                   for index, domain in enumerate(("health", "career", "learning"))]
        output = add_quality({"objects": records, "wiki": {"id": "shared-topic", "title": "人工共同主题",
            "body": "人工资料概览，分清各记录的范围与限制。", "sources": copy.deepcopy(records[0]["sources"])}}, self.context)
        objects = _objects(output, self.sources, "run-" + "a" * 32, self.response,
                           self.versions, "2026-01-01T00:00:00+00:00", quality_context=self.context,
                           skill_bundle=self.old_bundle)
        documents = []
        for obj in objects:
            obj["metadata"].update(review_status="passed", review_ref="memory/runs/processes/run-" + "a" * 32 + "/review.md")
            documents.append({**obj, "content_version": obj["metadata"]["content_version"],
                              "sha256": pipeline._hash(markdown(obj["metadata"], obj["body"]))})
        return output, [{"run_id": "run-" + "a" * 32, "commit_sha": "b" * 40,
                         "manifest_path": "memory/runs/processes/run-" + "a" * 32 + "/manifest.md", "documents": documents}]

    def refs(self, paths=None):
        return [{"path": path, "content_version": self.documents[path]["content_version"]}
                for path in (list(self.documents) if paths is None else paths)]

    def reuse_output(self, *, batch=False):
        return {"objects": [], "wikis" if batch else "wiki": [] if batch else None,
                "quality": copy.deepcopy(self.output["quality"]), "reuse_refs": self.refs()}

    def objects(self, output, *, batch=False, related=None, bundle=None, sources=None):
        return _objects(output, self.sources if sources is None else sources,
                        "run-" + "c" * 32, self.response, self.versions, "2026-02-01T00:00:00+00:00",
                        batch=batch, quality_context=self.context,
                        related=self.related if related is None else related,
                        skill_bundle=self.bundle if bundle is None else bundle)

    def assert_code(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_new_skill_pair_loads_and_keeps_original_common_dependencies(self):
        self.assertIs(validate_skill_bundle(self.bundle), self.bundle)
        for role in ("extract", "review"):
            new = self.bundle["skills"][role]
            old = self.old_bundle["skills"][role]
            self.assertEqual(new["files"][1:], old["files"][1:])
            self.assertEqual(new["base_prompt"], old["base_prompt"])
            self.assertIn("reuse_refs", new["files"][0]["content"])
        for extract, review in (("knowledge-extract@9", "knowledge-review@6"),
                                ("knowledge-extract@8", "knowledge-review@7")):
            with self.subTest(extract=extract, review=review), self.assertRaisesRegex(SkillError, "^invalid_skill_pair$"):
                resolve_skill_refs(extract, review)

    def test_all_references_materialize_exact_records_and_wiki_without_rewriting_output(self):
        for batch in (False, True):
            with self.subTest(batch=batch):
                output = self.reuse_output(batch=batch)
                before = copy.deepcopy(output)
                materialized = self.objects(output, batch=batch)
                self.assertEqual({obj["path"] for obj in materialized}, set(self.documents))
                for obj in materialized:
                    old = self.documents[obj["path"]]
                    self.assertEqual((obj["metadata"], obj["body"]), (old["metadata"], old["body"]))
                self.assertEqual(output, before)

    def test_mixed_update_and_references_materialize_complete_review_scope(self):
        output = self.reuse_output()
        changed = full_record(self.documents[self.record_paths[0]])
        changed["body"] += "人工新增的范围说明。"
        output["objects"] = [changed]
        output["reuse_refs"] = self.refs(self.record_paths[1:])
        output["wiki"] = copy.deepcopy(self.output["wiki"])
        output["wiki"]["body"] += "同步保留人工更新说明。"
        shadow = {"objects": [changed, *[full_record(self.documents[p]) for p in self.record_paths[1:]]]}
        output["quality"] = add_quality(shadow, self.context)["quality"]
        materialized = {obj["path"]: obj for obj in self.objects(output)}
        self.assertEqual(set(materialized), set(self.documents))
        self.assertNotEqual(materialized[self.record_paths[0]]["metadata"]["content_version"],
                            self.documents[self.record_paths[0]]["content_version"])
        for path in self.record_paths[1:]:
            self.assertEqual(materialized[path]["body"], self.documents[path]["body"])
            self.assertEqual(materialized[path]["metadata"], self.documents[path]["metadata"])
        members = materialized[self.wiki_path]["metadata"]["records"]
        self.assertEqual(members, [{"id": obj["metadata"]["id"], "content_version": obj["metadata"]["content_version"]}
                                  for obj in materialized.values() if obj["path"].startswith("memory/records/")])

    def test_wrong_version_missing_context_and_path_impersonation_are_rejected(self):
        for mutation in ("version", "unknown", "traversal", "extra_field", "wrong_type"):
            with self.subTest(mutation=mutation):
                output = self.reuse_output()
                ref = output["reuse_refs"][0]
                if mutation == "version":
                    ref["content_version"] = "sha256-" + "f" * 64
                elif mutation == "unknown":
                    ref["path"] = "memory/records/unprovided.md"
                elif mutation == "traversal":
                    ref["path"] = "memory/records/../record-health.md"
                elif mutation == "extra_field":
                    ref["body"] = "A ref cannot supply replacement text."
                else:
                    ref["content_version"] = 1
                self.assert_code("invalid_reuse_reference", lambda: self.objects(output))
        self.assert_code("invalid_reuse_reference", lambda: self.objects(self.reuse_output(), related=[]))
        supplied = copy.deepcopy(self.related)
        supplied[0]["documents"][0]["metadata"]["id"] = "different-id"
        self.assert_code("invalid_reuse_reference", lambda: self.objects(self.reuse_output(), related=supplied))

    def test_duplicate_reference_and_full_reference_conflict_are_rejected(self):
        duplicate = self.reuse_output()
        duplicate["reuse_refs"].append(copy.deepcopy(duplicate["reuse_refs"][0]))
        self.assert_code("invalid_reuse_reference", lambda: self.objects(duplicate))
        conflict = self.reuse_output()
        conflict["objects"] = [full_record(self.documents[self.record_paths[0]])]
        self.assert_code("invalid_object_id", lambda: self.objects(conflict))

    def test_reference_cannot_authorize_sources_or_omit_quality_mapping(self):
        self.assert_code("source_reference_mismatch", lambda: self.objects(self.reuse_output(), sources=[]))
        no_quality = self.reuse_output()
        no_quality["quality"]["coverage_disposition"] = []
        self.assert_code("quality_coverage_mismatch", lambda: self.objects(no_quality))
        wrong_excerpt = self.reuse_output()
        wrong_excerpt["quality"]["coverage_disposition"][0]["body_evidence"][0]["text"] = "Absent materialized body text."
        self.assert_code("quality_body_mapping_mismatch", lambda: self.objects(wrong_excerpt))

    def test_stale_wiki_reference_is_rejected_after_member_update(self):
        output = self.reuse_output()
        changed = full_record(self.documents[self.record_paths[0]])
        changed["body"] += "人工实质更新。"
        output["objects"] = [changed]
        output["reuse_refs"] = self.refs([*self.record_paths[1:], self.wiki_path])
        output["quality"] = add_quality({"objects": [changed, *[full_record(self.documents[p]) for p in self.record_paths[1:]]]}, self.context)["quality"]
        self.assert_code("invalid_wiki_dependency", lambda: self.objects(output))

    def test_missing_and_duplicate_wiki_members_are_rejected(self):
        missing = self.reuse_output()
        missing["reuse_refs"] = self.refs([*self.record_paths[1:], self.wiki_path])
        self.assert_code("invalid_wiki_dependency", lambda: self.objects(missing))
        duplicated = copy.deepcopy(self.related)
        wiki = next(doc for doc in duplicated[0]["documents"] if doc["path"] == self.wiki_path)
        wiki["metadata"]["records"].append(copy.deepcopy(wiki["metadata"]["records"][0]))
        self.assert_code("invalid_wiki_dependency", lambda: self.objects(self.reuse_output(), related=duplicated))

    def test_reuse_preserves_legacy_and_cross_domain_labels_without_reclassification(self):
        _, related = self.seed(["健康", "旧自由标签", "career"])
        documents = {doc["path"]: doc for doc in related[0]["documents"]}
        output = self.reuse_output()
        output["reuse_refs"] = [{"path": path, "content_version": doc["content_version"]} for path, doc in documents.items()]
        shadow = {"objects": [full_record(doc) for doc in documents.values() if doc["path"].startswith("memory/records/")]}
        output["quality"] = add_quality(shadow, self.context)["quality"]
        objects = self.objects(output, related=related)
        operations = {obj["path"]: {"action": "reuse"} for obj in objects}
        pipeline._validate_domains(objects, related, operations, self.versions)
        first = next(obj for obj in objects if obj["path"] == self.record_paths[0])
        self.assertEqual(first["metadata"]["domains"], ["健康", "旧自由标签", "career"])
        self.assertEqual({d for obj in objects if obj["metadata"]["type"] != "wiki" for d in obj["metadata"]["domains"]},
                         {"健康", "旧自由标签", "career", "learning"})

    def test_only_new_contract_schema_accepts_refs_and_nullable_plain_wiki(self):
        for bundle, accepted in ((self.bundle, True), (self.old_bundle, False)):
            payload = {"skill_bundle": bundle, "quality_contract": bundle["quality_contract"], "quality_context": self.context}
            schema = output_schema("extract", payload)
            self.assertEqual(_valid(self.reuse_output(), schema), accepted)
        self.assert_code("invalid_model_output", lambda: self.objects(self.reuse_output(), bundle=self.old_bundle))
        missing = self.reuse_output()
        del missing["reuse_refs"]
        self.assert_code("invalid_model_output", lambda: self.objects(missing))


class ReuseReferencePublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-reuse-ref-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Reuse Fixture")
        git(self.repo, "config", "user.email", "reuse@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial reuse baseline")
        self.saved = capture(self.repo, {"source_key": "artificial:reuse", "source_type": "note",
            "acquisition_status": "full_text", "body": "人工学习资料：每周整理一次笔记，执行情况未知。\n"})
        self.old_bundle = resolve_skill_refs("knowledge-extract@8", "knowledge-review@6")
        self.bundle = resolve_skill_refs("knowledge-extract@9", "knowledge-review@7")
        self.calls = []
        self.extract_outputs = []
        self.reuse = False

    def model(self, role, payload):
        self.calls.append((role, copy.deepcopy(payload)))
        if role == "extract":
            source = payload["sources"][0]
            quote = (source["body"].strip() if "body" in source
                     else source["fragments"][0]["lines"][0]["text"])
            ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                   "locator": "L1-L1", "quote": quote}
            record = {"id": "learning-note", "type": "knowledge", "title": "人工学习笔记",
                      "source_kind": "user_statement", "sources": [ref], "domains": ["learning"],
                      "valid_from": None, "valid_to": None, "verification_status": "unverified",
                      "lifecycle_status": "undetermined", "body": "人工资料描述每周整理笔记，执行情况未知。"}
            output = add_quality({"objects": [record], "wiki": {"id": "learning-topic", "title": "人工学习主题",
                "body": "本人工来源描述笔记安排，执行情况未知。", "sources": [ref]}}, payload["quality_context"])
            batch = payload.get("output_contract") == "batch-v1"
            if batch:
                output["wikis"] = [{**output.pop("wiki"), "record_ids": [record["id"]]}]
            if self.reuse:
                documents = [doc for related in payload["related"] for doc in related["documents"]]
                shadow = {"objects": [full_record(doc) for doc in documents if doc["path"].startswith("memory/records/")]}
                output = {"objects": [], "wikis" if batch else "wiki": [] if batch else None, "reuse_refs": [
                    {"path": doc["path"], "content_version": doc["content_version"]} for doc in documents],
                    "quality": add_quality(shadow, payload["quality_context"])["quality"]}
            self.extract_outputs.append(copy.deepcopy(output))
        else:
            output = passed_review(payload["required_checks"])
        return {"output": output, "model": "artificial-fixture", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{len(self.calls):032x}"}

    def test_real_git_incremental_ref_output_survives_review_publish_and_historical_recheck(self):
        old = process_sources(self.repo, [self.saved["path"]], model_call=self.model, skill_bundle=self.old_bundle)
        self.assertEqual(old["status"], "passed", old)
        published = publish(self.repo, old["run_id"])
        formal_before = {path: (self.repo / path).read_bytes() for path in published["files"]}
        self.reuse = True
        new = process_sources(self.repo, [self.saved["path"]], related_runs=[old["run_id"]],
                              model_call=self.model, skill_bundle=self.bundle)
        self.assertEqual(new["status"], "passed", new)
        self.assertEqual([role for role, _ in self.calls], ["extract", "review", "extract", "review"])
        _, text = parse_markdown((self.repo / new["stage_path"]).read_bytes())
        candidate = json.loads(text)
        self.assertEqual(candidate["extraction_output"], self.extract_outputs[-1])
        self.assertEqual(candidate["extraction_output"]["objects"], [])
        self.assertIsNone(candidate["extraction_output"]["wiki"])
        self.assertEqual(candidate["extraction_request_binding"], request_binding(*self.calls[-2]))
        materialized = candidate["objects"]
        self.assertEqual(len(materialized), 2)
        self.assertEqual({op["action"] for op in candidate["operations"].values()}, {"reuse"})
        reviewed_objects = self.calls[-1][1]["candidate"]["objects"]
        self.assertEqual(reviewed_objects, materialized)
        self.assertTrue(all(obj["body"] for obj in reviewed_objects))
        self.assertTrue(self.calls[-1][1]["required_checks"])
        final = publish(self.repo, new["run_id"])
        self.assertEqual(final["status"], "published_local")
        self.assertEqual({path: (self.repo / path).read_bytes() for path in formal_before}, formal_before)
        head = git(self.repo, "rev-parse", "HEAD")
        reread, _, _, _ = _read_fixed_candidate(PipelineGit(self.repo), new["run_id"],
                                              source_paths=[self.saved["path"]], historical=True)
        self.assertEqual(reread["extraction_output"], self.extract_outputs[-1])
        self.assertEqual(verify_publication(self.repo, new["run_id"], historical=True)["commit_sha"], final["commit_sha"])
        self.assertEqual(verify_publication(self.repo, old["run_id"], historical=True)["commit_sha"], published["commit_sha"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_new_family_decomposed_refs_bind_every_node_and_reopen_without_calls(self):
        old = process_sources(self.repo, [self.saved["path"]], model_call=self.model, skill_bundle=self.old_bundle)
        self.assertEqual(old["status"], "passed", old)
        publish(self.repo, old["run_id"])
        paths = [self.saved["path"]]
        plan = plan_batches(self.repo, paths, max_input_bytes=128000, payload_overhead_bytes=16000)
        self.assertEqual(len(plan["batches"]), 1)
        projection = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=paths)
        self.reuse = True
        call_start = len(self.calls)
        result = process_sources(self.repo, paths, related_runs=[old["run_id"]], model_call=self.model,
            skill_bundle=self.bundle, batch_context=projection["batch_context"], source_views=projection["source_views"],
            review_strategy="decomposed-v1")
        self.assertEqual(result["status"], "passed", result)
        requests = self.calls[call_start:]
        self.assertEqual([role for role, _ in requests], ["extract"] + ["review"] * 4)
        self.assertEqual([request["review_scope"]["kind"] for _, request in requests[1:]],
                         ["object", "object", "coverage", "global"])
        review = _load((self.repo / result["review_path"]).read_bytes(), "review")
        proof = review["decomposed_proof"]
        self.assertEqual(proof["schema_version"], "0.11")
        checked_paths = [result["stage_path"], result["review_path"], proof["plan_path"],
                         f"memory/runs/processes/{result['run_id']}/started.md"]
        for node in proof["nodes"]:
            checked_paths.append(node["path"])
            saved = _load((self.repo / node["path"]).read_bytes(), "decomposed_review_result")
            self.assertEqual(saved["schema_version"], "0.11")
            checked_paths.append(saved["reservation"]["path"])
        for path in checked_paths:
            self.assertEqual(parse_markdown((self.repo / path).read_bytes())[0]["schema_version"], "0.11", path)
        before = git(self.repo, "rev-parse", "HEAD"), len(self.calls)
        reopened = resume_decomposed_processing(self.repo, result["run_id"], allowed_source_paths=paths, model_call=self.model)
        self.assertEqual(reopened["status"], "passed", reopened)
        self.assertEqual(reopened["attempted_calls"], 0)
        self.assertEqual((git(self.repo, "rev-parse", "HEAD"), len(self.calls)), before)
        published = publish(self.repo, result["run_id"])
        self.assertEqual(verify_publication(self.repo, result["run_id"], historical=True)["commit_sha"], published["commit_sha"])
        candidate, _, _, _ = _read_fixed_candidate(PipelineGit(self.repo), result["run_id"], source_paths=paths, historical=True)
        self.assertEqual(candidate["extraction_output"], self.extract_outputs[-1])
        self.assertEqual(candidate["extraction_output"]["wikis"], [])
        self.assertEqual(len(candidate["extraction_output"]["reuse_refs"]), 2)

    def test_old_schema_guard_rejects_new_family_before_capture_writes(self):
        # This emulates the old metadata-only writer's known schema set; an
        # independent fixed-old-source check may additionally verify the same.
        target = self.repo / "memory/staging/artificial-new-family.md"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(markdown({"schema_version": "0.11", "kind": "candidate"}, "Artificial body must not be read.\n"))
        git(self.repo, "add", str(target.relative_to(self.repo)))
        git(self.repo, "commit", "-m", "artificial new schema marker")
        before = git(self.repo, "rev-parse", "HEAD")
        supported = tuple(v for v in compatibility.SUPPORTED_SCHEMA_VERSIONS if v != "0.11")
        known = frozenset(v for v in compatibility._KNOWN_VERSIONS if v != "0.11")
        with patch.object(compatibility, "SUPPORTED_SCHEMA_VERSIONS", supported), patch.object(compatibility, "_KNOWN_VERSIONS", known):
            inspected = compatibility.inspect_memory_schema(self.repo)
            self.assertFalse(inspected["writable"])
            self.assertEqual(inspected["unknown_files"], 1)
            with self.assertRaises(CaptureError):
                capture(self.repo, {"source_key": "artificial:old-writer", "source_type": "note",
                    "acquisition_status": "full_text", "body": "Artificial input rejected before write.\n"})
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")


if __name__ == "__main__":
    unittest.main()
