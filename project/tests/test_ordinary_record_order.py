"""Artificial mixed updates keep ordinary Wiki order and old proof semantics."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from quality_fixtures import add_quality, passed_review
from test_pipeline import git
import test_reuse_refs as reuse_fixtures
from test_reuse_refs import full_record
from pdm import pipeline
from pdm.capture import capture
from pdm.contracts import CaptureError, parse_markdown
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class OrdinaryRecordOrderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = reuse_fixtures.ReuseReferenceContractTests()
        self.fixture.setUp()
        self.bundle = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")

    def mixed(self, position=1, *, batch=False):
        fixture = self.fixture
        changed = full_record(fixture.documents[fixture.record_paths[position]])
        changed["body"] += "人工范围补充。"
        output = fixture.reuse_output(batch=batch)
        output["objects"] = [changed]
        output["reuse_refs"] = fixture.refs([path for index, path in enumerate(fixture.record_paths)
                                              if index != position])
        wiki = copy.deepcopy(fixture.output["wiki"])
        wiki["body"] += "保留原成员顺序。"
        if batch:
            wiki["record_ids"] = [fixture.documents[path]["metadata"]["id"]
                                  for path in fixture.record_paths]
            output["wikis"] = [wiki]
        else:
            output["wiki"] = wiki
        records = [changed if index == position else full_record(fixture.documents[path])
                   for index, path in enumerate(fixture.record_paths)]
        output["quality"] = add_quality({"objects": records}, fixture.context)["quality"]
        return output

    def objects(self, output, *, batch=False, order=True, related=None):
        fixture = self.fixture
        return pipeline._objects(output, fixture.sources, "run-" + "c" * 32, fixture.response,
                                 fixture.versions, "2030-01-02T00:00:00+00:00",
                                 batch=batch, quality_context=fixture.context,
                                 related=fixture.related if related is None else related,
                                 skill_bundle=self.bundle, ordinary_record_order=order)

    def record_ids(self, objects):
        return [obj["metadata"]["id"] for obj in objects if obj["path"].startswith("memory/records/")]

    def wiki_ids(self, objects):
        wiki = next(obj for obj in objects if obj["path"].startswith("memory/topics/"))
        return [ref["id"] for ref in wiki["metadata"]["records"]]

    def test_first_middle_and_last_update_preserve_old_wiki_order(self):
        expected = ["record-health", "record-career", "record-learning"]
        for position in range(3):
            with self.subTest(position=position):
                output = self.mixed(position)
                before = copy.deepcopy((output, self.fixture.related))
                objects = self.objects(output)
                self.assertEqual(self.record_ids(objects), expected)
                self.assertEqual(self.wiki_ids(objects), expected)
                self.assertEqual((output, self.fixture.related), before)
                for path in self.fixture.record_paths:
                    if path != self.fixture.record_paths[position]:
                        old = self.fixture.documents[path]
                        actual = next(obj for obj in objects if obj["path"] == path)
                        self.assertEqual((actual["metadata"], actual["body"]), (old["metadata"], old["body"]))

    def test_other_old_record_precedes_new_record_even_when_complete_objects_are_reversed(self):
        output = self.mixed()
        related = copy.deepcopy(self.fixture.related)
        old_extra = copy.deepcopy(related[0]["documents"][0])
        old_extra["path"] = "memory/records/record-extra.md"
        old_extra["metadata"]["id"] = "record-extra"
        extra = full_record(old_extra)
        old_extra["metadata"]["content_version"] = pipeline._hash({
            "metadata": {key: value for key, value in extra.items() if key != "body"}, "body": extra["body"]})
        old_extra["content_version"] = old_extra["metadata"]["content_version"]
        old_extra["sha256"] = pipeline._hash(pipeline.markdown(old_extra["metadata"], old_extra["body"]))
        related[0]["documents"].insert(0, old_extra)
        fresh = copy.deepcopy(extra)
        fresh["id"] = "record-new"
        fresh["body"] = "本轮人工新增记录。"
        output["objects"] = [fresh, *output["objects"], extra]
        all_records = [full_record(self.fixture.documents[path]) for path in self.fixture.record_paths]
        # The quality mapping for the updated middle object must use its new body.
        output["quality"] = add_quality({"objects": [output["objects"][1],
            *[record for record in all_records if record["id"] != "record-career"], extra, fresh]},
            self.fixture.context)["quality"]
        objects = self.objects(output, related=related)
        expected = ["record-health", "record-career", "record-learning", "record-extra", "record-new"]
        self.assertEqual(self.record_ids(objects), expected)
        self.assertEqual(self.wiki_ids(objects), expected)

    def test_legacy_ordinary_order_remains_available_for_unmarked_fixed_candidates(self):
        objects = self.objects(self.mixed(), order=False)
        self.assertEqual(self.wiki_ids(objects), ["record-career", "record-health", "record-learning"])
        self.assertFalse(pipeline._ordinary_record_order({}))

    def test_batch_record_ids_remain_explicit(self):
        output = self.mixed(batch=True)
        output["wikis"][0]["record_ids"] = ["record-learning", "record-health", "record-career"]
        self.assertEqual(self.wiki_ids(self.objects(output, batch=True)), output["wikis"][0]["record_ids"])

    def test_ordering_does_not_restore_an_omitted_old_record(self):
        output = self.mixed()
        output["objects"] = []
        records = [full_record(self.fixture.documents[path])
                   for path in (self.fixture.record_paths[0], self.fixture.record_paths[2])]
        output["quality"] = add_quality({"objects": records}, self.fixture.context)["quality"]
        self.assertEqual(self.record_ids(self.objects(output)), ["record-health", "record-learning"])

    def test_unknown_or_misplaced_order_marker_is_rejected(self):
        valid = {"ordinary_record_order": pipeline.ORDINARY_RECORD_ORDER_CONTRACT,
                 "mode": "incremental", "related": self.fixture.related}
        self.assertTrue(pipeline._ordinary_record_order(valid))
        for change in ({"ordinary_record_order": "unknown"}, {"ordinary_record_order": None},
                       {"batch_context": {}}, {"mode": "create"}, {"related": []},
                       {"quality_repair_context": {}}):
            with self.subTest(change=change), self.assertRaises(CaptureError) as caught:
                pipeline._ordinary_record_order({**valid, **change})
            self.assertEqual(caught.exception.code, "invalid_record_order_contract")


class OrdinaryOrderPublicationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-ordinary-order-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Order Fixture")
        git(self.repo, "config", "user.email", "order@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial order baseline")
        self.source = capture(self.repo, {"source_key": "artificial:ordinary-order", "source_type": "note",
            "acquisition_status": "full_text", "body": "人工资料包含健康、事业和学习三个方面；仅供工程验证。\n"})
        self.bundle = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.calls = []

    def model(self, role, payload):
        self.calls.append(role)
        if role == "extract":
            source = payload["sources"][0]
            ref = {"source_id": source["metadata"]["source_id"],
                   "source_version": source["metadata"]["source_version"], "locator": "L1-L1",
                   "quote": source["body"].strip()}
            records = [{"id": "record-" + domain, "type": "knowledge", "title": "人工" + domain,
                        "source_kind": "user_statement", "sources": [ref], "domains": [domain],
                        "valid_from": None, "valid_to": None, "verification_status": "unverified",
                        "lifecycle_status": "undetermined", "body": "人工" + domain + "记录，仅用于顺序合同。"}
                       for domain in ("health", "career", "learning")]
            wiki = {"id": "shared-order", "title": "人工顺序主题", "sources": [ref],
                    "body": "人工三个领域记录的集合，仅用于顺序合同。"}
            refs = []
            if payload["related"]:
                documents = {doc["path"]: doc for related in payload["related"] for doc in related["documents"]}
                records = [full_record(documents[f"memory/records/{record['id']}.md"]) for record in records]
                records[1]["body"] += "人工修订。"
                wiki["body"] += "同步修订。"
                refs = [{"path": path, "content_version": doc["content_version"]}
                        for path, doc in documents.items()
                        if path.startswith("memory/records/") and doc["metadata"]["id"] != "record-career"]
            output = add_quality({"objects": records, "wiki": wiki}, payload["quality_context"])
            output["reuse_refs"] = refs
            if refs:
                output["objects"] = [records[1]]
        else:
            output = passed_review(payload["required_checks"], payload["skill_bundle"])
        return {"output": output, "model": "artificial-order-fixture", "usage": None,
                "elapsed_seconds": 0.01, "invocation_id": "model-" + f"{len(self.calls):032x}"}

    def initial(self):
        first = pipeline.process_sources(self.repo, [self.source["path"]], model_call=self.model,
                                         skill_bundle=self.bundle)
        self.assertEqual(first["status"], "passed", first)
        publication = pipeline.publish(self.repo, first["run_id"])
        return first, publication

    def update(self, first):
        result = pipeline.process_sources(self.repo, [self.source["path"]], related_runs=[first["run_id"]],
                                          model_call=self.model, skill_bundle=self.bundle)
        self.assertEqual(result["status"], "passed", result)
        return result

    def candidate(self, result):
        return json.loads(parse_markdown((self.repo / result["stage_path"]).read_bytes())[1])

    def assert_reopens(self, result, publication, expected):
        before = git(self.repo, "rev-parse", "HEAD"), list(self.calls)
        candidate, _, _, _ = pipeline._read_fixed_candidate(PipelineGit(self.repo), result["run_id"],
                                                           historical=True)
        wiki = next(obj for obj in candidate["objects"] if obj["metadata"]["type"] == "wiki")
        self.assertEqual([ref["id"] for ref in wiki["metadata"]["records"]], expected)
        verified = pipeline.verify_publication(self.repo, result["run_id"], historical=True)
        self.assertEqual(verified["commit_sha"], publication["commit_sha"])
        self.assertEqual((git(self.repo, "rev-parse", "HEAD"), self.calls), before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_new_mixed_update_marker_binds_order_through_publish_and_reopening(self):
        first, _ = self.initial()
        result = self.update(first)
        self.assertEqual(self.candidate(result)["ordinary_record_order"], pipeline.ORDINARY_RECORD_ORDER_CONTRACT)
        publication = pipeline.publish(self.repo, result["run_id"])
        self.assert_reopens(result, publication, ["record-health", "record-career", "record-learning"])
        self.assertEqual(self.calls, ["extract", "review", "extract", "review"])

    def test_original_unmarked_candidate_keeps_its_old_materialization_and_proof(self):
        first, _ = self.initial()
        objects, binding = pipeline._objects, pipeline._candidate_binding

        def legacy_objects(*args, **kwargs):
            kwargs["ordinary_record_order"] = False
            return objects(*args, **kwargs)

        def legacy_binding(candidate):
            # Emulate the original writer before its immutable candidate commit.
            candidate.pop("ordinary_record_order", None)
            return binding(candidate)

        with patch.object(pipeline, "_objects", legacy_objects), patch.object(pipeline, "_candidate_binding", legacy_binding):
            result = self.update(first)
        self.assertNotIn("ordinary_record_order", self.candidate(result))
        publication = pipeline.publish(self.repo, result["run_id"])
        self.assert_reopens(result, publication, ["record-career", "record-health", "record-learning"])


if __name__ == "__main__":
    unittest.main()
