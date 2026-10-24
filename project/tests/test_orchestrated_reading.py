"""Read schema 0.7 ordinary batch publications without expanding source scope."""

from copy import deepcopy
import hashlib
import json
from unittest.mock import patch
import unittest

import test_pending
from pdm.batching import merge_batch_sources, plan_batches
from pdm.context import plan_answered_pending
from pdm.contracts import CaptureError, canonical, markdown, parse_markdown
from pdm.domain_catalog import build_domain_catalog
from pdm.evidence import build_current_evidence
from pdm.pending import answer_pending, read_registry
from pdm.pipeline import process_sources, publish
from pdm.portable import prepare_reading_package, verify_reading_package
from pdm.transaction import PipelineGit


class OrchestratedReadingTests(unittest.TestCase):
    setUp = test_pending.PendingTests.setUp
    capture = test_pending.PendingTests.capture
    ref = staticmethod(test_pending.PendingTests.ref)
    commit = test_pending.PendingTests.commit
    assert_error = test_pending.PendingTests.assert_error

    def model(self, role, payload):
        self.invocations += 1
        if role == "review":
            output = {"verdict": self.verdict, "reasons": ["是否计划从2030年1月1日开始？"]}
        else:
            refs = []
            for source in payload["sources"]:
                line = (source["fragments"][0]["lines"][0] if "fragments" in source
                        else {"line": 1, "text": source["body"].splitlines()[0]})
                refs.append({"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                             "locator": f"L{line['line']}-L{line['line']}", "quote": line["text"]})
            objects = [{"id": "reading-record-" + str(number), "type": "knowledge", "title": "人工跨类计划",
                        "source_kind": "user_statement", "sources": refs, "domains": ["health", "career", "learning"],
                        "valid_from": None, "valid_to": None, "verification_status": "unverified",
                        "lifecycle_status": "undetermined", "body": "只根据人工来源保留计划及其未知范围。"}
                       for number in range(self.object_count)]
            if "fragments" in payload["sources"][0]:
                wikis = [{"id": "reading-topic-" + str(number), "title": "人工主题", "sources": refs,
                          "record_ids": [record["id"]], "body": "人工主题依赖受审记录。"} for number, record in enumerate(objects)]
                output = {"objects": objects, "wikis": wikis}
            else:
                output = {"objects": objects, "wiki": {"id": "reading-topic", "title": "人工主题",
                          "sources": refs, "body": "人工资料所示计划。"}}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.invocations:032x}"}

    def context_publication(self, origin_count=1):
        paths = [self.source["path"], *[self.capture("reading-original-" + str(index))["path"]
                                       for index in range(1, origin_count)]]
        self.verdict, self.object_count = "needs_confirmation", 1
        original = process_sources(self.repo, paths, related_runs=[], model_call=self.model)
        self.assertEqual(original["status"], "needs_confirmation", original)
        item = read_registry(self.repo)["items"][0]
        reply = self.capture("reading-yes", "用户：对。\n")
        answer_pending(self.repo, item["item_id"], reply["path"])
        paths.append(reply["path"])
        job = plan_answered_pending(self.repo, allowed_source_paths=paths, allowed_run_ids=[original["run_id"]],
                                    max_sources=256, max_runs=256)["jobs"][0]
        context = {key: job[key] for key in ("job_id", "parent_item_id", "question", "parent_run_ids",
                                            "source_refs", "object_refs", "reply_sources")}
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        projection = merge_batch_sources(self.repo, plan, [batch["batch_id"] for batch in plan["batches"]],
                                         source_paths=paths, max_bytes=100_000)
        self.verdict, self.object_count = "passed", 12
        result = process_sources(self.repo, paths, related_runs=[], model_call=self.model,
            source_views=projection["source_views"], batch_context=projection["batch_context"], continuation_context=context)
        self.assertEqual(result["status"], "passed", result)
        publication = publish(self.repo, result["run_id"])
        metadata, _ = parse_markdown((self.repo / publication["manifest_path"]).read_bytes())
        self.assertEqual((metadata["schema_version"], metadata["kind"]), ("0.7", "manifest"))
        self.assertEqual(len(publication["files"]), 24)
        return paths, result, publication, projection

    def package(self, paths, run, name, **options):
        output = self.repo.parent / (self.repo.name + "-" + name + ".json")
        self.addCleanup(lambda: output.unlink(missing_ok=True))
        result = prepare_reading_package(self.repo, paths, ["人工计划有哪些已知范围？"], [run], output=output, **options)
        self.assertEqual(verify_reading_package(output, expected_sha256=result["sha256"])["status"], "integrity_verified")
        return output, result

    def snapshot(self):
        return (PipelineGit(self.repo).head(),
                {str(path.relative_to(self.repo)): path.read_bytes() for path in (self.repo / "memory").rglob("*.md")})

    def test_24_formal_objects_are_current_catalogued_and_exported_with_legacy_whole_body_contract(self):
        paths, result, publication, _ = self.context_publication()
        before, calls = self.snapshot(), self.invocations
        current = build_current_evidence(self.repo, paths, [result["run_id"]])
        self.assertEqual(len(current["publications"][0]["documents"]), 24)
        self.assertTrue(current["coverage"]["current_complete"])
        catalog = build_domain_catalog(self.repo, paths, [result["run_id"]])
        self.assertTrue(catalog["catalog_complete_within_selected_scope"])
        self.assertTrue(all(len(group["records"]) == 12 and len(group["topics"]) == 12 for group in catalog["domains"].values()))
        output, _ = self.package(paths, result["run_id"], "whole")
        payload = json.loads(output.read_bytes())["payload"]
        self.assertEqual(payload["schema_version"], "0.4")
        self.assertEqual(len(payload["evidence"]["publications"][0]["documents"]), len(publication["files"]))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.invocations, calls)

    def test_nine_explicit_sources_and_24_objects_remain_readable_in_whole_and_fragment_packages(self):
        paths, result, _, projection = self.context_publication(origin_count=8)
        before, calls = self.snapshot(), self.invocations
        current = build_current_evidence(self.repo, paths, [result["run_id"]])
        self.assertEqual(len(current["sources"]), 9)
        self.assertEqual(len(current["publications"][0]["documents"]), 24)
        output, _ = self.package(paths, result["run_id"], "nine-whole")
        self.assertEqual(json.loads(output.read_bytes())["payload"]["schema_version"], "0.6")
        fragment, _ = self.package(paths, result["run_id"], "nine-fragment",
            source_views=projection["source_views"], batch_context=projection["batch_context"])
        self.assertEqual(len(json.loads(fragment.read_bytes())["payload"]["evidence"]["publications"][0]["documents"]), 24)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.invocations, calls)

    def test_missing_source_scope_is_rejected_before_verifier_or_unselected_body(self):
        paths, result, _, _ = self.context_publication(origin_count=8)
        from pdm import evidence as evidence_module
        original = evidence_module.load_source
        chosen = paths[:-1]
        def selected_only(repo, path, *args, **kwargs):
            self.assertIn(path, chosen)
            return original(repo, path, *args, **kwargs)
        before, calls = self.snapshot(), self.invocations
        with patch("pdm.evidence.load_source", side_effect=selected_only), patch(
                "pdm.evidence.verify_publication", side_effect=AssertionError("scope precedes full verification")):
            self.assert_error("evidence_source_mismatch", lambda: build_current_evidence(self.repo, chosen, [result["run_id"]]))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.invocations, calls)

    def test_manifest_cannot_hide_a_source_declared_by_its_actual_batch_candidate(self):
        paths, result, publication, _ = self.context_publication(origin_count=8)
        path = publication["manifest_path"]
        metadata, body = parse_markdown((self.repo / path).read_bytes())
        manifest = json.loads(body)
        manifest["additional_sources"].pop()
        self.commit(path, markdown(metadata, canonical(manifest).decode() + "\n"))
        with patch("pdm.evidence.verify_publication", side_effect=AssertionError("hidden scope rejected before verifier")):
            self.assert_error("publication_mismatch", lambda: build_current_evidence(self.repo, paths, [result["run_id"]]))

    def test_legacy_manifest_cannot_expand_limits_by_claiming_schema_07(self):
        self.verdict, self.object_count = "passed", 12
        result = process_sources(self.repo, [self.source["path"]], related_runs=[], model_call=self.model)
        publication = publish(self.repo, result["run_id"])
        path = publication["manifest_path"]
        metadata, body = parse_markdown((self.repo / path).read_bytes())
        metadata["schema_version"] = "0.7"
        self.commit(path, markdown(metadata, body))
        with patch("pdm.evidence.verify_publication", side_effect=AssertionError("fake batch rejected before verifier")):
            with self.assertRaises(CaptureError):
                build_current_evidence(self.repo, [self.source["path"]], [result["run_id"]])

    def test_self_rehashed_packages_cannot_hide_formal_sources_or_expand_document_ranges(self):
        paths, result, _, projection = self.context_publication()
        for format_name, options in (("whole", {}), ("fragment", {key: projection[key] for key in ("source_views", "batch_context")})):
            output, _ = self.package(paths, result["run_id"], "scope-" + format_name, **options)
            original = json.loads(output.read_bytes())
            def hide_source(payload):
                evidence = payload["evidence"]
                hidden = evidence["sources"].pop()["source"]["path"]
                payload["scope"]["source_paths"].remove(hidden)
                evidence["coverage"]["requested_source_paths"].remove(hidden)
                evidence["coverage"]["read_source_paths"].remove(hidden)
            mutations = [hide_source,
                lambda p: p["evidence"]["publications"][0]["documents"][0]["metadata"]["sources"][0].update(locator="L99-L99"),
                lambda p: p["evidence"]["publications"][0]["documents"][0].update(path="memory/records/unselected-document.md")]
            for mutate in mutations:
                package = deepcopy(original)
                mutate(package["payload"])
                package["payload_sha256"] = hashlib.sha256(canonical(package["payload"])).hexdigest()
                output.write_bytes(canonical(package) + b"\n")
                with self.subTest(format=format_name, mutation=mutate), self.assertRaises(CaptureError):
                    verify_reading_package(output)
