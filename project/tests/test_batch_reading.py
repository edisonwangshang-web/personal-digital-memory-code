"""Fragment reading over synthetic captures; no external models or data."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_batching as batch_fixtures
import test_batch_groups as group_fixtures
import test_partial_corrections as correction_fixtures
from pdm.batching import merge_batch_sources, plan_batches, project_batch_sources
from pdm.capture import capture
from pdm.contracts import CaptureError, canonical
from pdm.controls import record_control
from pdm.domain_catalog import build_domain_catalog
from pdm.evidence import ask_current, build_current_evidence, build_evidence_sources
from pdm.pending import register_pending
from pdm.pipeline import load_source, process_sources, publish
from pdm.portable import prepare_reading_package, verify_reading_package
from pdm.transaction import PipelineGit


class BatchReadingTests(unittest.TestCase):
    setUp = batch_fixtures.BatchingTests.setUp
    source = batch_fixtures.BatchingTests.source
    readonly = batch_fixtures.BatchingTests.readonly
    assert_error = batch_fixtures.BatchingTests.assert_error

    def projection(self):
        body = "day,value\nunit,kg\n" + "".join(
            f"{number},PRIVATE_ROW_{number}_" + "x" * 350 + "\n" for number in range(6))
        path = self.source("reading-table.csv", body)
        full = plan_batches(self.repo, [path], max_input_bytes=100_000)
        first = project_batch_sources(self.repo, full, full["batches"][0]["batch_id"], source_paths=[path],
                                      fragment_ids=[fragment["fragment_id"] for fragment in full["fragments"][:2]])
        plan = plan_batches(self.repo, [path], max_input_bytes=first["input_bytes"] + 10)
        self.assertEqual(6, len(plan["batches"]))
        view = project_batch_sources(self.repo, plan, plan["batches"][2]["batch_id"], source_paths=[path])
        return path, plan, view

    def options(self, projection):
        return {key: projection[key] for key in ("source_views", "batch_context")}

    def answer_model(self, locator="L5-L5"):
        def answer(role, payload):
            self.assertEqual("answer", role)
            source = payload["evidence"]["sources"][0]["source"]
            self.assertEqual("", source["body"])
            return {"output": {"answer": "人工范围内的记录。[1]", "citations": [{
                "source_id": source["source_id"], "source_version": source["source_version"], "locator": locator}],
                "limitations": ["仅核验人工资料范围。"]}, "model": "fixture", "usage": None,
                "elapsed_seconds": 0.01, "invocation_id": "model-" + "a" * 32}
        return answer

    def formal_model(self, role, payload):
        self.number += 1
        source = payload["sources"][0]
        ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
               "locator": "L5-L5", "quote": source["body"].splitlines()[4]}
        if role == "extract":
            output = {"objects": [{"id": "reading-health", "type": "knowledge", "title": "人工健康事实",
                "source_kind": "user_statement", "sources": [ref], "domains": ["health"],
                "valid_from": None, "valid_to": None, "verification_status": "unverified",
                "lifecycle_status": "undetermined", "body": "PRIVATE_FORMAL_ROW_2：人工健康记录。"}],
                "wiki": {"id": "reading-topic", "title": "人工主题", "sources": [ref],
                         "body": "PRIVATE_FORMAL_ROW_2：人工综合。"}}
        else:
            output = {"verdict": "passed", "reasons": ["人工独立审核。"]}
        return {"output": output, "model": "fixture", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def test_current_accepts_nine_explicit_sources_but_legacy_stays_eight(self):
        paths = [self.source("expanded" + str(index), "人工来源。\n") for index in range(9)]
        before = self.readonly()
        current = build_current_evidence(self.repo, paths)
        self.assertEqual(9, len(current["sources"]))
        self.assertEqual([], current["coverage"]["unread_source_paths"])
        with self.assertRaises(CaptureError):
            build_evidence_sources(self.repo, paths)
        output = self.repo.parent / (self.repo.name + "-expanded.json")
        self.addCleanup(lambda: output.unlink(missing_ok=True))
        prepared = prepare_reading_package(self.repo, paths, ["仅检查范围。"], output=output)
        package = json.loads(output.read_bytes())
        self.assertEqual("0.6", package["payload"]["schema_version"])
        self.assertIsNone(package["payload"]["scope"]["batch_context"])
        self.assertEqual("integrity_verified", verify_reading_package(output, expected_sha256=prepared["sha256"])["status"])
        self.assertEqual(before, self.readonly())

    def test_fragment_payload_contains_only_global_lines_and_explicit_missing_ranges(self):
        path, _, projection = self.projection()
        before = self.readonly()
        current = build_current_evidence(self.repo, [path], **self.options(projection))
        entry = current["sources"][0]
        self.assertEqual("", entry["source"]["body"])
        self.assertEqual("fragment", entry["source"]["content_scope"])
        self.assertEqual([1, 2, 5], [line["line"] for line in entry["lines"]])
        self.assertEqual(8, entry["source_view"]["source_line_count"])
        encoded = json.dumps(current)
        self.assertIn("PRIVATE_ROW_2_", encoded)
        for number in (0, 1, 3, 4, 5):
            self.assertNotIn(f"PRIVATE_ROW_{number}_", encoded)
        self.assertEqual(["L3-L4", "L6-L8"], current["coverage"]["fragment_missing_ranges"][path])
        self.assertFalse(current["coverage"]["current_complete"])
        self.assertEqual(before, self.readonly())

    def test_answer_accepts_global_line_five_but_rejects_unprovided_or_hole_spanning_locator(self):
        path, _, projection = self.projection()
        before = self.readonly()
        result = ask_current(self.repo, [path], "人工片段说明什么？", model_call=self.answer_model(), **self.options(projection))
        self.assertEqual("L5-L5", result["citations"][0]["locator"])
        for locator in ("L3-L3", "L3-L5", "L1-L5"):
            self.assert_error("invalid_citation", lambda locator=locator: ask_current(
                self.repo, [path], "人工？", model_call=self.answer_model(locator), **self.options(projection)))
        self.assertEqual(before, self.readonly())

    def test_views_cannot_expand_scope_or_change_real_fragment_bytes(self):
        path, _, projection = self.projection()
        options = self.options(projection)
        with patch("pdm.evidence.load_source", side_effect=AssertionError("unselected source opened")):
            self.assert_error("batch_scope_mismatch", lambda: build_current_evidence(
                self.repo, [self.saved["path"]], **options))
        changed = deepcopy(options)
        changed["source_views"][0]["fragments"][0]["lines"][0]["text"] = "tampered"
        self.assert_error("fragment_bytes_mismatch", lambda: build_current_evidence(self.repo, [path], **changed))
        self.assert_error("invalid_batch_context", lambda: build_current_evidence(
            self.repo, [path], source_views=projection["source_views"]))

    def test_formal_and_domain_entries_require_all_referenced_ranges_in_actual_view(self):
        path, plan, projection = self.projection()
        processed = process_sources(self.repo, [path], related_runs=[], model_call=self.formal_model)
        publication = publish(self.repo, processed["run_id"])
        selected = build_current_evidence(self.repo, [path], [processed["run_id"]], **self.options(projection))
        self.assertEqual(2, len(selected["publications"][0]["documents"]))
        catalog = build_domain_catalog(self.repo, [path], [processed["run_id"]], **self.options(projection))
        self.assertEqual(1, len(catalog["domains"]["health"]["records"]))
        other = project_batch_sources(self.repo, plan, plan["batches"][3]["batch_id"], source_paths=[path])
        excluded = build_current_evidence(self.repo, [path], [processed["run_id"]], **self.options(other))
        self.assertEqual([], excluded["publications"][0]["documents"])
        self.assertEqual(set(publication["files"]), set(excluded["coverage"]["excluded_documents"]))
        self.assertNotIn("PRIVATE_FORMAL_ROW_2", json.dumps(excluded))
        empty_catalog = build_domain_catalog(self.repo, [path], [processed["run_id"]], **self.options(other))
        self.assertEqual([], empty_catalog["domains"]["health"]["records"])

    def test_partial_source_does_not_leak_pending_question_from_unread_context(self):
        path, _, projection = self.projection()
        source = load_source(self.repo, path)
        register_pending(self.repo, {"question": "PRIVATE_UNREAD_CONFIRMATION", "source_refs": [
            {key: source[key] for key in ("source_id", "source_version", "path")}]})
        current = build_current_evidence(self.repo, [path], **self.options(projection))
        self.assertEqual([], current["pending"]["items"])
        self.assertTrue(current["pending"]["coverage"]["gaps"])
        self.assertNotIn("PRIVATE_UNREAD_CONFIRMATION", json.dumps(current))

    def test_fragment_package_binds_ranges_and_rejects_truncated_lines_even_with_rehashed_payload(self):
        path, _, projection = self.projection()
        output = self.repo.parent / (self.repo.name + "-fragment.json")
        self.addCleanup(lambda: output.unlink(missing_ok=True))
        prepared = prepare_reading_package(self.repo, [path], ["人工片段？"], output=output, **self.options(projection))
        original = output.read_bytes()
        package = json.loads(original)
        self.assertEqual("0.6", package["payload"]["schema_version"])
        self.assertEqual(projection["batch_context"], package["payload"]["scope"]["batch_context"])
        self.assertNotIn("PRIVATE_ROW_0_", original.decode())
        self.assertEqual("integrity_verified", verify_reading_package(output, expected_sha256=prepared["sha256"])["status"])
        package["payload"]["evidence"]["sources"][0]["lines"].pop()
        package["payload_sha256"] = hashlib.sha256(canonical(package["payload"])).hexdigest()
        output.write_bytes(canonical(package) + b"\n")
        with self.assertRaises(CaptureError):
            verify_reading_package(output)
        self.assert_error("reading_package_mismatch", lambda: verify_reading_package(output, expected_sha256=prepared["sha256"]))
        package["payload"]["schema_version"] = "0.4"
        package["payload"]["scope"].pop("batch_context")
        package["payload_sha256"] = hashlib.sha256(canonical(package["payload"])).hexdigest()
        output.write_bytes(canonical(package) + b"\n")
        with self.assertRaises(CaptureError):
            verify_reading_package(output)
        package["payload"]["evidence"].pop("batch_context")
        for entry in package["payload"]["evidence"]["sources"]:
            entry.pop("source_view")
            entry["source"].pop("content_scope")
        package["payload_sha256"] = hashlib.sha256(canonical(package["payload"])).hexdigest()
        output.write_bytes(canonical(package) + b"\n")
        with self.assertRaises(CaptureError):
            verify_reading_package(output)

    def test_selected_paths_missing_from_projection_remain_explicit_unread_scope(self):
        path, _, projection = self.projection()
        selected = build_current_evidence(self.repo, [self.saved["path"], path], **self.options(projection))
        self.assertIn(self.saved["path"], selected["coverage"]["unread_source_paths"])
        self.assertNotIn("我想整理隔离样例", json.dumps(selected, ensure_ascii=False))
        self.assertFalse(selected["coverage"]["current_complete"])


class BatchControlledReadingTests(unittest.TestCase):
    setUp = correction_fixtures.PartialCorrectionTests.setUp
    model = correction_fixtures.PartialCorrectionTests.model
    payload = correction_fixtures.PartialCorrectionTests.payload

    def test_unselected_object_control_paths_and_identity_do_not_reach_current_or_answer_payload(self):
        control = record_control(self.repo, self.payload(request_key="PRIVATE_UNSELECTED_CONTROL_SENTINEL"))
        unrelated = capture(self.repo, {"source_key": "fixture:unrelated-work", "source_type": "note",
                            "acquisition_status": "full_text", "body": "Selected work fact remains unchanged.\n"})
        before = PipelineGit(self.repo).head()
        selected = build_current_evidence(self.repo, [unrelated["path"]])
        payloads = []
        def answer(role, payload):
            payloads.append(payload)
            return {"output": {"answer": "Only the selected work fact is available.", "citations": [{
                "source_id": unrelated["source_id"], "source_version": unrelated["source_version"], "locator": "L1-L1"}],
                "limitations": ["Explicit synthetic scope."]}, "model": "fixture", "usage": None,
                "elapsed_seconds": 0.01, "invocation_id": "model-" + "c" * 32}
        ask_current(self.repo, [unrelated["path"]], "Selected work?", model_call=answer)
        for value in (selected, payloads[0]):
            encoded = json.dumps(value)
            for forbidden in (self.old["path"], self.new["path"], control["path"], control["control_id"],
                              "PRIVATE_UNSELECTED_CONTROL_SENTINEL", self.record_a, self.topic):
                self.assertNotIn(forbidden, encoded)
        for entry in selected["sources"]:
            self.assertNotIn("object_controls", entry["source"])
        partial = build_current_evidence(self.repo, self.paths, [self.old_run])
        self.assertEqual([self.record_b], [document["path"] for document in partial["publications"][0]["documents"]])
        self.assertTrue(all("object_controls" not in entry["source"] for entry in partial["sources"]))
        self.assertEqual(before, PipelineGit(self.repo).head())

    def test_controlled_old_fragment_is_rejected_before_source_body_read(self):
        plan = plan_batches(self.repo, [self.old["path"]], max_input_bytes=100_000)
        projection = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=[self.old["path"]])
        record_control(self.repo, self.payload())
        before = PipelineGit(self.repo).head()
        with patch("pdm.evidence.load_source", side_effect=AssertionError("controlled text opened")):
            with self.assertRaises(CaptureError) as raised:
                build_current_evidence(self.repo, self.paths, source_views=projection["source_views"],
                                       batch_context=projection["batch_context"])
        self.assertEqual("controlled_source_view", raised.exception.code)
        self.assertEqual(before, PipelineGit(self.repo).head())


class BatchExpandedPublicationReadingTests(unittest.TestCase):
    setUp = group_fixtures.BatchGroupTests.setUp
    model = group_fixtures.BatchGroupTests.model
    reviewed = group_fixtures.BatchGroupTests.reviewed

    def test_group_with_eleven_records_and_eleven_topics_is_readable_under_current_budget(self):
        from pdm.batch_groups import publish_final_group
        paths = [capture(self.repo, {"source_key": "fixture:reading-group:" + str(number), "source_type": "note",
                    "acquisition_status": "full_text", "body": f"人工事实{number}。\n"})["path"] for number in range(11)]
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        projection = merge_batch_sources(self.repo, plan, [item["batch_id"] for item in plan["batches"]],
                                         source_paths=paths, max_bytes=192_000)
        result, candidate = self.reviewed(plan, paths, projection)
        published = publish_final_group(self.repo, plan, candidate["objects"], [result["run_id"]], source_paths=paths)
        self.assertEqual(22, len(published["files"]))
        current = build_current_evidence(self.repo, paths, [published["run_id"]])
        self.assertEqual(22, len(current["publications"][0]["documents"]))
        catalog = build_domain_catalog(self.repo, paths, [published["run_id"]])
        self.assertEqual(11, len(catalog["domains"]["learning"]["records"]))
        self.assertEqual(11, len(catalog["domains"]["learning"]["topics"]))
        self.assertTrue(catalog["catalog_complete_within_selected_scope"])


if __name__ == "__main__":
    unittest.main()
