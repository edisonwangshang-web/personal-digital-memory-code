"""Bounded artificial source sets; no personal data or external model calls."""

import copy
import json
import unittest
from unittest.mock import patch

import test_pipeline
from pdm.capture import capture
from pdm.contracts import CaptureError, markdown, parse_markdown
from pdm.model_calls import ModelCallError
from pdm.pipeline import load_source, normalize_source_paths, process, process_sources, publish, verify_publication
from pdm.transaction import PipelineGit


class MultisourcePipelineTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    assert_error = test_pipeline.PipelineTests.assert_error
    commit_change = test_pipeline.PipelineTests.commit_change

    def source(self, key="article", body="文章作者：先比较方案，再选择。\n", *, source_type="article", link=False):
        return capture(self.repo, {"source_key": "fixture:" + key, "source_type": source_type,
            "platform": "isolated", "original_url": "https://example.invalid/" + key,
            "acquisition_status": "link_only" if link else "full_text", "original_time": None,
            "body": "" if link else body})

    def selected(self):
        second = self.source()
        return [second["path"], self.saved["path"]]

    def model(self, role, payload):
        self.roles.append(role)
        self.contexts.append(copy.deepcopy(payload))
        self.number += 1
        if role == "extract":
            refs = [{"source_id": source["metadata"]["source_id"],
                     "source_version": source["metadata"]["source_version"],
                     "locator": "L1-L1", "quote": source["body"].splitlines()[0]}
                    for source in payload["sources"] if source["body"]]
            output = {"objects": [{"id": "knowledge-shared", "type": "knowledge", "title": "共同主题",
                "source_kind": "model_inference", "sources": refs, "domains": ["career", "health", "learning"],
                "valid_from": None, "valid_to": None, "verification_status": "unverified",
                "lifecycle_status": "undetermined", "body": "文章观点、对话目标和个人想法分别保留依据。"}],
                "wiki": {"id": "topic-shared", "title": "共同主题", "body": "综合同批选定来源。", "sources": refs}}
        else:
            output = {"verdict": self.verdict, "reasons": ["只采用选定来源，区分各方表达。"]}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def read(self, path):
        metadata, body = parse_markdown((self.repo / path).read_bytes())
        return metadata, json.loads(body)

    def run_selected(self, paths=None, model=None):
        return process_sources(self.repo, paths or self.selected(), model_call=model or self.model)

    def test_three_sources_share_one_record_and_reviewed_publication(self):
        paths = self.selected() + [self.source("thought", "用户：这是想法，尚未选择。\n", source_type="note")["path"]]
        result = self.run_selected(paths)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(self.roles, ["extract", "review"])
        ordered = sorted(paths)
        self.assertEqual([source["path"] for source in self.contexts[0]["sources"]], ordered)
        self.assertEqual(self.contexts[0]["sources"], self.contexts[1]["sources"])
        metadata, candidate = self.read(result["stage_path"])
        self.assertEqual(metadata["schema_version"], "0.3")
        self.assertEqual([binding["path"] for binding in [candidate["source"], *candidate["additional_sources"]]], ordered)
        self.assertEqual(self.read(result["review_path"])[0]["schema_version"], "0.3")
        published = publish(self.repo, result["run_id"], result["head"])
        self.assertEqual(published["source"], candidate["source"])
        self.assertEqual(published["additional_sources"], candidate["additional_sources"])
        for field in ("manifest_path", "receipt_path"):
            self.assertEqual(self.read(published[field])[0]["schema_version"], "0.3")
        records = list((self.repo / "memory/records").glob("*.md"))
        self.assertEqual(len(records), 1)
        record, _ = parse_markdown(records[0].read_bytes())
        self.assertEqual(record["schema_version"], "0.2")
        self.assertEqual(len(record["sources"]), 3)
        self.assertEqual(record["domains"], ["career", "health", "learning"])
        self.assertEqual(verify_publication(self.repo, result["run_id"])["additional_sources"], candidate["additional_sources"])
        self.assertEqual(publish(self.repo, result["run_id"])["head"], published["head"])
        self.assertEqual(test_pipeline.git(self.repo, "status", "--porcelain"), "")

    def test_selection_rejects_duplicates_conflicting_versions_and_more_than_eight(self):
        updated = self.source("one", "用户：同一来源的新版本。\n", source_type="conversation")
        paths = [f"memory/sources/inbox/src-{number:064x}/sha256-{number:064x}.md" for number in range(9)]
        cases = [("duplicate_source", [self.saved["path"], self.saved["path"]]),
                 ("conflicting_source_versions", [self.saved["path"], updated["path"]]),
                 ("too_many_sources", paths), ("invalid_source_selection", []),
                 ("invalid_source_selection", self.saved["path"]),
                 ("path_rejected", ["../../memory/sources/escape.md"])]
        head = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        for code, selection in cases:
            with self.subTest(code=code):
                self.assert_error(code, lambda: process_sources(self.repo, selection, model_call=self.model))
        self.assertEqual(self.roles, [])
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(normalize_source_paths(tuple(paths[:8])), sorted(paths[:8]))

    def test_eight_distinct_sources_can_be_processed(self):
        paths = [self.saved["path"]] + [self.source("extra-" + str(number))["path"] for number in range(7)]
        result = self.run_selected(paths)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.contexts[1]["sources"]), 8)
        self.assertEqual(len(self.read(result["stage_path"])[1]["additional_sources"]), 7)

    def test_second_source_and_receipt_are_validated_before_any_model_call(self):
        selected = self.selected()
        second = selected[0]
        metadata, body = parse_markdown((self.repo / second).read_bytes())
        self.commit_change(second, markdown(metadata, body + "篡改正文。\n"))
        self.assert_error("source_mismatch", lambda: self.run_selected(selected))
        self.assertEqual(self.roles, [])

    def test_additional_source_capture_receipt_cannot_be_tampered(self):
        second = self.source()
        metadata, _ = parse_markdown((self.repo / second["receipt_path"]).read_bytes())
        metadata["save_result"] = "invented"
        self.commit_change(second["receipt_path"], markdown(metadata))
        self.assert_error("receipt_mismatch", lambda: self.run_selected([self.saved["path"], second["path"]]))
        self.assertEqual(self.roles, [])

    def test_unreferenced_additional_source_receipt_change_invalidates_publication(self):
        paths = self.selected()
        def primary_only(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                for value in [*response["output"]["objects"], response["output"]["wiki"]]:
                    value["sources"] = value["sources"][:1]
            return response
        result = self.run_selected(paths, primary_only)
        candidate = self.read(result["stage_path"])[1]
        receipt_path = candidate["additional_sources"][0]["receipt_path"]
        metadata, _ = parse_markdown((self.repo / receipt_path).read_bytes())
        metadata["save_result"] = "invented"
        self.commit_change(receipt_path, markdown(metadata))
        self.assert_error("receipt_mismatch", lambda: publish(self.repo, result["run_id"]))
        self.assertFalse((self.repo / "memory/records").exists())

    def test_second_source_change_after_publication_rejects_readback(self):
        result = self.run_selected()
        published = publish(self.repo, result["run_id"])
        path = published["additional_sources"][0]["path"]
        metadata, body = parse_markdown((self.repo / path).read_bytes())
        self.commit_change(path, markdown(metadata, body + "篡改正文。\n"))
        self.assert_error("source_mismatch", lambda: verify_publication(self.repo, result["run_id"]))

    def test_independent_review_rejects_same_invocation_and_saves_schema_three_failure(self):
        def reused(role, payload):
            response = self.model(role, payload)
            response["invocation_id"] = "model-" + "a" * 32
            return response
        result = self.run_selected(model=reused)
        self.assertEqual(result["error"], "review_not_independent")
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(self.read(result["failure_path"])[0]["schema_version"], "0.3")
        self.assertTrue((self.repo / result["stage_path"]).is_file())
        self.assertFalse((self.repo / "memory/records").exists())

    def test_confirmation_is_open_and_never_published(self):
        self.verdict = "needs_confirmation"
        result = self.run_selected()
        metadata, pending = self.read(result["pending_path"])
        self.assertEqual(metadata["schema_version"], "0.3")
        self.assertEqual(pending["status"], "open")
        self.assertIsNone(pending["user_reply"])
        self.assert_error("review_not_passed", lambda: publish(self.repo, result["run_id"]))

    def test_extraction_failure_is_not_automatically_retried(self):
        roles = []
        def timed_out(role, payload):
            roles.append(role)
            raise ModelCallError("model_timeout", "model-" + "a" * 32)
        result = self.run_selected(model=timed_out)
        metadata, failure = self.read(result["failure_path"])
        self.assertEqual(metadata["schema_version"], "0.3")
        self.assertEqual(failure["attempted_calls"], 1)
        self.assertEqual(roles, ["extract"])

    def test_only_links_never_call_model(self):
        paths = [self.source("link-one", link=True)["path"], self.source("link-two", link=True)["path"]]
        head = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        self.assert_error("source_has_no_text", lambda: self.run_selected(paths))
        self.assertEqual(self.roles, [])
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), head)

    def test_link_only_context_is_present_without_fabricated_quote(self):
        link = self.source("link", link=True)
        result = self.run_selected([self.saved["path"], link["path"]])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.contexts[0]["sources"]), 2)
        link_source = next(source for source in self.contexts[1]["sources"] if source["path"] == link["path"])
        self.assertEqual(link_source["body"], "")
        self.assertEqual(link_source["metadata"]["acquisition_status"], "link_only")
        self.assertEqual(len(self.read(result["stage_path"])[1]["objects"][0]["metadata"]["sources"]), 1)

    def test_link_only_fabricated_reference_fails_before_review(self):
        link = self.source("link", link=True)
        def invented(role, payload):
            response = self.model(role, payload)
            link_source = next(source for source in payload["sources"] if not source["body"])
            response["output"]["objects"][0]["sources"].append({
                "source_id": link_source["metadata"]["source_id"],
                "source_version": link_source["metadata"]["source_version"],
                "locator": "L1-L1", "quote": "未取得的文章内容"})
            return response
        result = self.run_selected([self.saved["path"], link["path"]], invented)
        self.assertEqual(result["error"], "invalid_quote")
        self.assertEqual(self.roles, ["extract"])

    def test_foreign_or_mixed_source_reference_fails_before_review(self):
        paths = self.selected()
        def mixed(role, payload):
            response = self.model(role, payload)
            refs = response["output"]["objects"][0]["sources"]
            refs[1]["source_version"] = refs[0]["source_version"]
            return response
        result = self.run_selected(paths, mixed)
        self.assertEqual(result["error"], "source_reference_mismatch")
        self.assertEqual(self.roles, ["extract"])

    def test_model_payload_mutation_does_not_change_source_or_saved_candidate(self):
        def mutating(role, payload):
            response = self.model(role, payload)
            payload["sources"][0]["metadata"]["source_id"] = "invented"
            if role == "review":
                payload["candidate"]["objects"][0]["body"] = "invented"
            return response
        result = self.run_selected(model=mutating)
        self.assertEqual(result["status"], "passed")
        self.assertNotEqual(self.contexts[1]["sources"][0]["metadata"]["source_id"], "invented")
        self.assertEqual(publish(self.repo, result["run_id"])["status"], "published_local")

    def test_existing_target_remains_create_only(self):
        result = self.run_selected()
        publish(self.repo, result["run_id"])
        before = (self.repo / "memory/records/knowledge-shared.md").read_bytes()
        self.assert_error("target_exists_or_deleted", lambda: self.run_selected([self.saved["path"], self.source("new")["path"]]))
        self.assertEqual((self.repo / "memory/records/knowledge-shared.md").read_bytes(), before)

    def test_missing_multisource_receipt_is_repaired_without_republication(self):
        result = self.run_selected()
        original = PipelineGit.write_files
        def interrupted(adapter, files, head, message):
            if any(path.endswith("/receipt.md") for path in files):
                raise CaptureError("isolated_failure", "isolated receipt interruption")
            return original(adapter, files, head, message)
        with patch.object(PipelineGit, "write_files", interrupted):
            self.assert_error("isolated_failure", lambda: publish(self.repo, result["run_id"]))
        first = verify_publication(self.repo, result["run_id"])
        self.assertFalse(first["receipt_saved"])
        restored = publish(self.repo, result["run_id"])
        self.assertEqual(restored["commit_sha"], first["commit_sha"])
        self.assertEqual(self.read(restored["receipt_path"])[0]["schema_version"], "0.3")

    def reject_downgrade(self, field):
        result = self.run_selected()
        published = publish(self.repo, result["run_id"])
        path = result.get(field) or published[field]
        metadata, body = parse_markdown((self.repo / path).read_bytes())
        metadata["schema_version"] = "0.2"
        self.commit_change(path, markdown(metadata, body))
        self.assert_error("invalid_pipeline_data", lambda: verify_publication(self.repo, result["run_id"]))

    def test_candidate_schema_cannot_be_downgraded(self):
        self.reject_downgrade("stage_path")

    def test_review_schema_cannot_be_downgraded(self):
        self.reject_downgrade("review_path")

    def test_manifest_schema_cannot_be_downgraded(self):
        self.reject_downgrade("manifest_path")

    def test_receipt_schema_cannot_be_downgraded(self):
        self.reject_downgrade("receipt_path")

    def test_single_source_serialized_contract_remains_schema_two(self):
        result = process(self.repo, self.saved["path"], model_call=self.model)
        metadata, candidate = self.read(result["stage_path"])
        self.assertEqual(metadata["schema_version"], "0.2")
        self.assertNotIn("additional_sources", candidate)
        published = publish(self.repo, result["run_id"])
        self.assertNotIn("additional_sources", published)
        for field in ("manifest_path", "receipt_path"):
            self.assertEqual(self.read(published[field])[0]["schema_version"], "0.2")
        self.assertNotIn("additional_sources", self.read(published["manifest_path"])[1])

    def test_mismatched_publication_scope_loads_no_source_bodies(self):
        paths = self.selected()
        other = self.source("outside")["path"]
        result = self.run_selected(paths)
        publish(self.repo, result["run_id"])
        for scope in ([self.saved["path"]], [self.saved["path"], other]):
            with self.subTest(scope=scope), patch("pdm.pipeline.load_source", wraps=load_source) as loader:
                self.assert_error("evidence_source_mismatch", lambda: verify_publication(
                    self.repo, result["run_id"], source_paths=scope))
                loader.assert_not_called()

    def test_matching_publication_scope_revalidates_every_source_and_receipt(self):
        paths = self.selected()
        result = self.run_selected(paths)
        publish(self.repo, result["run_id"])
        with patch("pdm.pipeline.load_source", wraps=load_source) as loader:
            verified = verify_publication(self.repo, result["run_id"], source_paths=list(reversed(paths)))
            self.assertEqual([call.args[1] for call in loader.call_args_list], sorted(paths))
            self.assertEqual(verified["status"], "published_local")
        receipt_path = verified["additional_sources"][0]["receipt_path"]
        metadata, _ = parse_markdown((self.repo / receipt_path).read_bytes())
        metadata["save_result"] = "invented"
        self.commit_change(receipt_path, markdown(metadata))
        self.assert_error("receipt_mismatch", lambda: verify_publication(
            self.repo, result["run_id"], source_paths=paths))

    def test_manifest_claim_cannot_hide_candidate_source_outside_scope(self):
        paths = self.selected()
        other = self.source("outside")["path"]
        result = self.run_selected(paths)
        published = publish(self.repo, result["run_id"])
        metadata, manifest = self.read(published["manifest_path"])
        manifest["additional_sources"][0]["path"] = other
        self.commit_change(published["manifest_path"], markdown(metadata, json.dumps(manifest)))
        with patch("pdm.pipeline.load_source", wraps=load_source) as loader:
            self.assert_error("evidence_source_mismatch", lambda: verify_publication(
                self.repo, result["run_id"], source_paths=[self.saved["path"], other]))
            loader.assert_not_called()

    def test_single_source_publication_accepts_only_its_exact_scope(self):
        other = self.source("outside")["path"]
        result = process(self.repo, self.saved["path"], model_call=self.model)
        publish(self.repo, result["run_id"])
        with patch("pdm.pipeline.load_source", wraps=load_source) as loader:
            self.assertEqual(verify_publication(self.repo, result["run_id"],
                source_paths=[self.saved["path"]])["status"], "published_local")
            self.assertEqual(loader.call_count, 1)
        with patch("pdm.pipeline.load_source", wraps=load_source) as loader:
            self.assert_error("evidence_source_mismatch", lambda: verify_publication(
                self.repo, result["run_id"], source_paths=[self.saved["path"], other]))
            loader.assert_not_called()
