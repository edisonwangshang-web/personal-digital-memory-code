"""Pending resolution through schema 0.6 ordinary and group publications."""

from unittest.mock import patch
import unittest

import test_pending
from pdm.batching import merge_batch_sources, plan_batches
from pdm.batch_groups import publish_final_group
from pdm.contracts import CaptureError, canonical, markdown, parse_markdown
from pdm.evidence import build_current_evidence
from pdm.pending import (_publication_source_refs, answer_pending, read_registry,
                         resolve_pending)
from pdm.context import plan_answered_pending
from pdm.pipeline import _committed, _load, process_sources, publish, verify_publication
from pdm.production import prepare_production, run_production
from pdm.transaction import PipelineGit


class PendingGroupTests(unittest.TestCase):
    setUp = test_pending.PendingTests.setUp
    capture = test_pending.PendingTests.capture
    ref = staticmethod(test_pending.PendingTests.ref)
    assert_error = test_pending.PendingTests.assert_error
    commit = test_pending.PendingTests.commit

    def model(self, role, payload):
        self.invocations += 1
        if role == "extract":
            refs = []
            for source in payload["sources"]:
                line = (source["fragments"][0]["lines"][0] if "fragments" in source
                        else {"line": 1, "text": source["body"].splitlines()[0]})
                refs.append({"source_id": source["metadata"]["source_id"],
                             "source_version": source["metadata"]["source_version"],
                             "locator": f"L{line['line']}-L{line['line']}", "quote": line["text"]})
            record_refs = refs
            if not getattr(self, "apply_reply", True):
                record_refs = [ref for ref in refs if ref["source_id"] == self.source["source_id"]]
            record = {"id": "group-confirmed-date", "type": "knowledge", "title": "人工日期",
                      "source_kind": "user_statement", "sources": record_refs, "domains": ["learning"],
                      "valid_from": None, "valid_to": None, "verification_status": "unverified",
                      "lifecycle_status": "undetermined", "body": "人工来源及答复保留为计划，不推断执行。"}
            topic = {"id": "group-confirmed-topic", "title": "人工主题", "sources": refs,
                     "body": "只在人工明确范围汇总。"}
            if "fragments" in payload["sources"][0]:
                topic["record_ids"] = [record["id"]]
                output = {"objects": [record], "wikis": [topic]}
            else:
                output = {"objects": [record], "wiki": topic}
        else:
            output = {"verdict": getattr(self, "verdict", "passed"), "reasons": ["人工计划具体从哪天开始？"]}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.invocations:032x}"}

    def origin_and_reply(self, *, associate=True):
        self.verdict = "needs_confirmation"
        self.original = process_sources(self.repo, [self.source["path"]], related_runs=[], model_call=self.model)
        self.assertEqual(self.original["status"], "needs_confirmation")
        self.item = read_registry(self.repo)["items"][0]
        self.reply = self.capture("group-reply", "用户：计划从2030年1月1日开始。\n")
        if associate:
            answer_pending(self.repo, self.item["item_id"], self.reply["path"])
        self.verdict = "passed"
        return [self.source["path"], self.reply["path"]]

    def publication(self, paths, *, group=True):
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        projection = merge_batch_sources(self.repo, plan, [batch["batch_id"] for batch in plan["batches"]],
                                         source_paths=paths, max_bytes=100_000)
        reviewed = process_sources(self.repo, paths, related_runs=[], model_call=self.model,
                                   batch_context=projection["batch_context"], source_views=projection["source_views"])
        self.assertEqual(reviewed["status"], "passed", reviewed)
        if group:
            candidate = _load(_committed(PipelineGit(self.repo), reviewed["stage_path"]), "candidate")
            result = publish_final_group(self.repo, plan, candidate["objects"], [reviewed["run_id"]], source_paths=paths)
        else:
            result = publish(self.repo, reviewed["run_id"])
        verify_publication(self.repo, result["run_id"], source_paths=paths)
        return result

    def test_production_group_resolves_and_current_pending_proof_is_verified(self):
        paths = self.origin_and_reply()
        prepare_production(self.repo, "answered-production", paths)
        produced = run_production(self.repo, "answered-production", model_call=self.model)
        self.assertEqual(produced["status"], "published", produced)
        publication = produced["publication"]
        self.assertEqual(publication["mode"], "batch_group")
        resolved = resolve_pending(self.repo, self.item["item_id"], publication["run_id"])
        self.assertEqual(resolved["pending_status"], "resolved")
        evidence = build_current_evidence(self.repo, paths, [publication["run_id"]])
        proof = evidence["pending"]["items"][0]["resolution_verification"]
        self.assertEqual(proof["status"], "verified_history")
        self.assertTrue(proof["reply_record_references_verified"])
        self.assertTrue(proof["reply_association_verified"])
        head, calls = PipelineGit(self.repo).head(), self.invocations
        self.assertFalse(resolve_pending(self.repo, self.item["item_id"], publication["run_id"])["changed"])
        self.assertEqual(PipelineGit(self.repo).head(), head)
        self.assertEqual(self.invocations, calls)

    def test_production_first_review_registers_06_candidate_then_answer_is_plannable(self):
        self.verdict = "needs_confirmation"
        paths = [self.source["path"]]
        prepare_production(self.repo, "original-production", paths, review_strategy="legacy-v1")
        original = run_production(self.repo, "original-production", model_call=self.model)
        self.assertEqual(original["status"], "needs_confirmation", original)
        item = read_registry(self.repo)["items"][0]
        original_run = item["run_ids"][0]
        stage = self.repo / f"memory/staging/{original_run}.md"
        self.assertEqual(parse_markdown(stage.read_bytes())[0]["schema_version"], "0.6")
        reply = self.capture("native-group-reply", "用户：计划从2030年1月1日开始。\n")
        answer_pending(self.repo, item["item_id"], reply["path"])
        paths.append(reply["path"])
        planned = plan_answered_pending(self.repo, allowed_source_paths=paths, allowed_run_ids=[original_run])
        self.assertEqual(len(planned["jobs"]), 1)
        self.verdict = "passed"
        prepare_production(self.repo, "native-answered-production", paths, review_strategy="legacy-v1")
        produced = run_production(self.repo, "native-answered-production", model_call=self.model)
        self.assertEqual(produced["status"], "published", produced)
        self.assertEqual(resolve_pending(self.repo, item["item_id"], produced["publication"]["run_id"])["pending_status"], "resolved")

    def test_schema_06_ordinary_manifest_still_requires_real_reply_reference(self):
        paths = self.origin_and_reply()
        publication = self.publication(paths, group=False)
        metadata, _ = parse_markdown((self.repo / publication["manifest_path"]).read_bytes())
        self.assertEqual((metadata["schema_version"], metadata["kind"]), ("0.6", "manifest"))
        self.assertEqual(resolve_pending(self.repo, self.item["item_id"], publication["run_id"])["pending_status"], "resolved")

    def test_group_with_reply_only_in_wiki_cannot_resolve(self):
        paths = self.origin_and_reply()
        self.apply_reply = False
        publication = self.publication(paths)
        self.assert_error("pending_reply_not_applied", lambda: resolve_pending(self.repo, self.item["item_id"], publication["run_id"]))
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")

    def test_group_scope_rejects_unassociated_source_before_opening_any_body(self):
        paths = self.origin_and_reply()
        unrelated = self.capture("group-unrelated", "用户：这条其他信息未关联此确认事项。\n")
        publication = self.publication([*paths, unrelated["path"]])
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("scope must precede body reads")):
            self.assert_error("pending_publication_mismatch", lambda: resolve_pending(self.repo, self.item["item_id"], publication["run_id"]))
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")

    def test_group_published_before_actual_reply_association_cannot_resolve(self):
        paths = self.origin_and_reply(associate=False)
        publication = self.publication(paths)
        answer_pending(self.repo, self.item["item_id"], self.reply["path"])
        self.assert_error("pending_publication_mismatch", lambda: resolve_pending(self.repo, self.item["item_id"], publication["run_id"]))
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")

    def test_forged_group_manifest_is_not_publication_proof(self):
        paths = self.origin_and_reply()
        publication = self.publication(paths)
        target = publication["manifest_path"]
        metadata, body = parse_markdown((self.repo / target).read_bytes())
        import json
        manifest = json.loads(body)
        manifest["parent_head"] = "f" * 40
        self.commit(target, markdown(metadata, canonical(manifest).decode() + "\n"))
        # Metadata parsing is bounded and opens no source bodies, but is never
        # treated as proof: the original verifier still rejects the fake chain.
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("metadata-only helper opened text")):
            refs = _publication_source_refs(PipelineGit(self.repo), publication["run_id"])
        self.assertEqual({ref["path"] for ref in refs}, set(paths))
        with self.assertRaises(CaptureError):
            resolve_pending(self.repo, self.item["item_id"], publication["run_id"])
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")

