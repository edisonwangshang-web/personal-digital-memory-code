"""Read-only domain directories over artificial verified snapshots and Git data."""

import contextlib
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import test_current_evidence as current_fixtures
from pdm.capture import capture
from pdm.cli import main
from pdm.contracts import CaptureError
from pdm.controls import record_control
from pdm.domain_catalog import build_domain_catalog, render_domain_catalog
from pdm.pipeline import process_sources, publish


def version(value):
    return "sha256-" + hashlib.sha256(value.encode()).hexdigest()


def document(folder, identifier, *, domains=None, records=None):
    metadata = {"id": identifier, "title": identifier + " TITLE", "content_version": version(identifier),
                "sources": [{"quote": "FORBIDDEN_QUOTE_SENTINEL"}]}
    if domains is not None:
        metadata["domains"] = domains
    if records is not None:
        metadata["records"] = records
    return {"path": f"memory/{folder}/{identifier}.md", "sha256": version(identifier + ":file"),
            "metadata": metadata, "body": "FORBIDDEN_BODY_SENTINEL"}


def record_ref(doc):
    return {key: doc["metadata"][key] for key in ("id", "content_version")}


def snapshot(documents):
    return {"head": "a" * 40, "scope": "explicit_current_view", "knowledge_status": "published",
            "controls": {"digest": version("controls"), "events": [{"control_id": "control-fixture", "reason": "FORBIDDEN_CONTROL_REASON"}]},
            "coverage": {"current_complete": True, "requested_source_paths": ["source-a"], "read_source_paths": ["source-a"],
                         "missing_update_paths": [], "unread_runs": []},
            "publications": [{"run_id": "run-a", "documents": documents}], "limitations": ["限定范围。"]}


class DomainCatalogProjectionTests(unittest.TestCase):
    def build(self, data, **kwargs):
        with patch("pdm.domain_catalog.build_current_evidence", return_value=data) as read:
            result = build_domain_catalog(Path("/artificial"), ["source-a"], ["run-a"], **kwargs)
        read.assert_called_once_with(Path("/artificial"), ["source-a"], ["run-a"], max_bytes=kwargs.get("max_bytes", 192000))
        return result

    def assert_code(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_three_domains_cross_domain_shared_links_and_exact_topic_union(self):
        health = document("records", "health-record", domains=["health"])
        career = document("records", "career-record", domains=["career"])
        learning = document("records", "learning-record", domains=["learning"])
        shared = document("records", "shared-record", domains=["learning", "health"])
        topic = document("topics", "combined-topic", records=[record_ref(health), record_ref(career), record_ref(learning), record_ref(shared)])
        data = snapshot([health, career, learning, shared, topic])
        before = deepcopy(data)
        result = self.build(data)
        self.assertEqual(list(result["domains"]), ["health", "career", "learning"])
        for domain in result["domains"].values():
            self.assertEqual(domain["topics"][0]["path"], topic["path"])
        self.assertEqual(result["domains"]["health"]["records"][-1]["path"], shared["path"])
        self.assertEqual(result["domains"]["learning"]["records"][-1]["path"], shared["path"])
        self.assertEqual(result["classification"]["counts"]["records"], 4)
        self.assertTrue(result["catalog_complete_within_selected_scope"])
        self.assertEqual(data, before)
        for forbidden in ("FORBIDDEN_BODY_SENTINEL", "FORBIDDEN_QUOTE_SENTINEL", "FORBIDDEN_CONTROL_REASON"):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_legacy_explicit_aliases_and_fine_tags_do_not_invalidate_known_domain(self):
        records = [document("records", "legacy-health", domains=["健康", "睡眠", "数据质量"]),
                   document("records", "legacy-career", domains=["事业"]),
                   document("records", "legacy-learning", domains=["学习"])]
        topic = document("topics", "legacy-topic", records=[record_ref(doc) for doc in records])
        result = self.build(snapshot([*records, topic]))
        self.assertTrue(result["classification"]["complete"])
        self.assertEqual(result["classification"]["unmapped_labels"], [{"path": records[0]["path"], "labels": ["数据质量", "睡眠"]}])
        self.assertEqual(result["domains"]["health"]["topics"][0]["path"], topic["path"])

    def test_unknown_or_empty_domains_stay_unclassified_without_title_or_body_guessing(self):
        records = [document("records", "health-looking-title", domains=["睡眠", "运动"]),
                   document("records", "empty-domains", domains=[])]
        result = self.build(snapshot(records))
        self.assertEqual(result["domains"]["health"]["records"], [])
        self.assertEqual(len(result["unclassified"]["records"]), 2)
        self.assertFalse(result["classification"]["complete"])

    def test_topic_partial_members_wrong_version_and_missing_member_are_explicit_gaps(self):
        known = document("records", "known", domains=["health"])
        unknown = document("records", "unknown", domains=[])
        wrong = {"id": "known", "content_version": version("other-version")}
        missing = {"id": "outside-record", "content_version": version("outside-version")}
        topic = document("topics", "partial-topic", records=[record_ref(known), record_ref(unknown), wrong, missing])
        result = self.build(snapshot([known, unknown, topic]))
        self.assertEqual(result["domains"]["health"]["topics"][0]["path"], topic["path"])
        reasons = {gap["reason"] for gap in result["classification"]["gaps"]}
        self.assertTrue({"record_version_mismatch", "missing_record", "incomplete_record_classification"} <= reasons)
        self.assertFalse(result["catalog_complete_within_selected_scope"])

    def test_topic_never_uses_its_own_domains_or_unversioned_record_name_to_guess(self):
        topic = document("topics", "health-topic", domains=["health"], records=[{"id": "missing"}])
        result = self.build(snapshot([topic]))
        self.assertEqual(result["domains"]["health"]["topics"], [])
        self.assertEqual(result["unclassified"]["topics"][0]["path"], topic["path"])
        self.assertEqual(result["classification"]["gaps"][0]["reason"], "invalid_topic_record_reference")

    def test_duplicate_current_object_across_runs_is_one_entry_with_provenance(self):
        record = document("records", "reuse-record", domains=["career"])
        data = snapshot([record])
        data["publications"].append({"run_id": "run-b", "documents": [deepcopy(record)]})
        result = self.build(data)
        entries = result["domains"]["career"]["records"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["publication_runs"], ["run-a", "run-b"])
        self.assertEqual(result["classification"]["counts"]["records"], 1)

    def test_same_path_conflicting_version_hash_metadata_or_body_is_rejected(self):
        record = document("records", "conflict", domains=["health"])
        for change in ("version", "hash", "body", "title"):
            with self.subTest(change=change):
                conflicting = deepcopy(record)
                if change == "version": conflicting["metadata"]["content_version"] = version("conflict-new")
                elif change == "hash": conflicting["sha256"] = version("conflict-file-new")
                elif change == "title": conflicting["metadata"]["title"] = "different title"
                else: conflicting["body"] = "different body"
                data = snapshot([record]); data["publications"].append({"run_id": "run-b", "documents": [conflicting]})
                self.assert_code("catalog_document_conflict", lambda: self.build(data))

    def test_filter_changes_display_only_and_keeps_all_classification_and_evidence_gaps(self):
        health = document("records", "scoped-health", domains=["health"])
        unknown = document("records", "scoped-unknown", domains=[])
        data = snapshot([health, unknown])
        data["coverage"].update(current_complete=False, missing_update_paths=["unselected-version"])
        complete = self.build(data)
        filtered = self.build(data, domain="learning")
        self.assertEqual(list(filtered["domains"]), ["learning"])
        self.assertEqual(filtered["coverage"], complete["coverage"])
        self.assertEqual(filtered["classification"], complete["classification"])
        self.assertEqual(filtered["unclassified"], complete["unclassified"])
        self.assertFalse(filtered["catalog_complete_within_selected_scope"])

    def test_invalid_filter_and_budget_reject_before_read_and_no_implicit_defaults(self):
        with patch("pdm.domain_catalog.build_current_evidence") as read:
            for domain in ("健康", "all", [], True):
                self.assert_code("invalid_domain_filter", lambda: build_domain_catalog(Path("/unused"), ["source-a"], domain=domain))
            for budget in (0, -1, True, 192001):
                self.assert_code("invalid_evidence_budget", lambda: build_domain_catalog(Path("/unused"), ["source-a"], max_bytes=budget))
            read.assert_not_called()

    def test_directory_and_markdown_have_independent_complete_output_byte_bounds(self):
        result = self.build(snapshot([document("records", "bounded", domains=["health"])]))
        self.assert_code("evidence_limit", lambda: self.build(snapshot([]), max_bytes=200))
        self.assert_code("evidence_limit", lambda: render_domain_catalog(result, max_bytes=200))
        text = render_domain_catalog(result)
        self.assertIn("[bounded TITLE](memory/records/bounded.md)", text)
        self.assertIn("覆盖范围与缺口", text)
        self.assertNotIn("FORBIDDEN_BODY_SENTINEL", text)


class DomainCatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = current_fixtures.CurrentEvidenceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.repo, self.paths = self.fixture.repo, self.fixture.paths

    def data_snapshot(self):
        return {"head": current_fixtures.git(self.repo, "rev-parse", "HEAD"),
                "status": current_fixtures.git(self.repo, "status", "--porcelain", "--untracked-files=all"),
                "memory": {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / "memory").rglob("*") if p.is_file()}}

    def model(self, role, payload):
        result = self.fixture.model(role, payload)
        if role == "extract": result["output"]["objects"][0]["domains"] = ["health", "learning"]
        return result

    def publication(self):
        processed = process_sources(self.repo, [self.paths[0]], related_runs=[], model_call=self.model)
        publication = publish(self.repo, processed["run_id"])
        return processed["run_id"], publication

    def test_actual_reviewed_publication_readonly_catalog_and_cli_json_markdown(self):
        run, _ = self.publication()
        before = self.data_snapshot()
        with patch("pdm.model_calls.call_model", side_effect=AssertionError("Directory must never invoke a model")):
            result = build_domain_catalog(self.repo, [self.paths[0]], [run])
            self.assertEqual(len(result["domains"]["health"]["records"]), 1)
            self.assertEqual(len(result["domains"]["learning"]["topics"]), 1)
            self.assertEqual(result["model_calls"], 0)
            self.assertTrue(result["catalog_complete_within_selected_scope"])
            for fmt in ("json", "markdown"):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    status = main(["knowledge", "--repo", str(self.repo), "--source", self.paths[0], "--run", run, "--format", fmt])
                self.assertEqual(status, 0)
                if fmt == "json": self.assertEqual(json.loads(output.getvalue()), result)
                else: self.assertIn("健康&运动", output.getvalue())
        self.assertEqual(self.data_snapshot(), before)

    def test_real_control_correction_excludes_old_object_and_preserves_coverage(self):
        run, _ = self.publication()
        record_control(self.repo, {"request_key": "catalog:correction", "action": "corrects",
            "source_paths": [self.paths[0]], "replacement_source_paths": [self.paths[1]]})
        before = self.data_snapshot()
        result = build_domain_catalog(self.repo, self.paths[:2], [run])
        self.assertEqual(result["domains"]["health"]["records"], [])
        self.assertTrue(result["coverage"]["excluded_sources"])
        self.assertTrue(result["coverage"]["excluded_documents"])
        self.assertNotIn("OLD_REVIEWED_ARTIFICIAL_ERROR", json.dumps(result))
        self.assertEqual(self.data_snapshot(), before)

    def test_actual_reused_publications_link_one_record_and_topic_per_domain(self):
        # A legacy create-only origin has no processed-input marker, so this
        # explicitly exercises a distinct incremental publication with reuse.
        original = process_sources(self.repo, [self.paths[0]], model_call=self.model)
        run = original["run_id"]
        publish(self.repo, run)
        reused = process_sources(self.repo, [self.paths[0]], related_runs=[run], model_call=self.model)
        publish(self.repo, reused["run_id"])
        before = self.data_snapshot()
        result = build_domain_catalog(self.repo, [self.paths[0]], [run, reused["run_id"]])
        self.assertEqual(result["classification"]["counts"]["records"], 1)
        self.assertEqual(result["classification"]["counts"]["topics"], 1)
        entry = result["domains"]["health"]["records"][0]
        self.assertEqual(entry["publication_runs"], sorted([run, reused["run_id"]]))
        self.assertEqual(self.data_snapshot(), before)

    def test_real_control_delete_prevents_any_source_body_reopen(self):
        run, publication = self.publication()
        record_control(self.repo, {"request_key": "catalog:delete", "action": "delete",
            "source_paths": [self.paths[0]], "object_paths": sorted(publication["files"])})
        before = self.data_snapshot()
        with patch("pdm.evidence.load_source", side_effect=AssertionError("Deleted source body opened")):
            result = build_domain_catalog(self.repo, [self.paths[0]], [run])
        self.assertEqual(result["domains"]["health"]["records"], [])
        self.assertEqual(result["coverage"]["unread_runs"][0]["reason"], "deleted_source")
        self.assertEqual(self.data_snapshot(), before)

    def test_unselected_sources_and_new_versions_are_never_opened_but_update_gap_is_preserved(self):
        run, _ = self.publication()
        newer = capture(self.repo, {"source_key": "fixture:current:0", "source_type": "note",
            "acquisition_status": "full_text", "body": "UNSELECTED_NEW_VERSION_SECRET\n"})
        from pdm import evidence as evidence_module, pipeline
        original_evidence, original_pipeline = evidence_module.load_source, pipeline.load_source
        def bounded(original):
            def read(repo, path, *args, **kwargs):
                self.assertEqual(path, self.paths[0])
                return original(repo, path, *args, **kwargs)
            return read
        before = self.data_snapshot()
        with patch("pdm.evidence.load_source", side_effect=bounded(original_evidence)), patch("pdm.pipeline.load_source", side_effect=bounded(original_pipeline)):
            result = build_domain_catalog(self.repo, [self.paths[0]], [run], domain="health")
        self.assertIn(newer["path"], result["coverage"]["missing_update_paths"])
        self.assertFalse(result["catalog_complete_within_selected_scope"])
        self.assertNotIn("UNSELECTED_NEW_VERSION_SECRET", json.dumps(result))
        self.assertNotIn("UNSELECTED_ARTIFICIAL_SENTINEL", json.dumps(result))
        self.assertEqual(self.data_snapshot(), before)


class DomainCatalogCliTests(unittest.TestCase):
    def test_cli_forwards_all_explicit_sources_runs_filter_and_budget_without_model_ledger(self):
        output = io.StringIO()
        with patch("pdm.domain_catalog.build_domain_catalog", return_value={"scope": "selected"}) as build, contextlib.redirect_stdout(output):
            status = main(["knowledge", "--repo", "/artificial", "--source", "source-a", "--source", "source-b",
                           "--run", "run-a", "--run", "run-b", "--domain", "career", "--budget-bytes", "16000"])
        self.assertEqual(status, 0)
        build.assert_called_once_with(Path("/artificial"), ["source-a", "source-b"], ["run-a", "run-b"], domain="career", max_bytes=16000)
        self.assertEqual(json.loads(output.getvalue())["scope"], "selected")

    def test_cli_requires_explicit_scope_and_rejects_unknown_domain_without_echo(self):
        for args in (["knowledge", "--repo", "/artificial"],
                     ["knowledge", "--repo", "/artificial", "--source", "source-a", "--domain", "PRIVATE_INVALID_DOMAIN"],
                     ["knowledge", "--repo", "/artificial", "--source", "source-a", "--format", "unsupported"]):
            with self.subTest(args=args):
                error = io.StringIO()
                with patch("pdm.domain_catalog.build_domain_catalog") as build, contextlib.redirect_stderr(error):
                    status = main(args)
                self.assertEqual(status, 2)
                build.assert_not_called()
                self.assertEqual(json.loads(error.getvalue())["error"], "invalid_arguments")
                self.assertNotIn("PRIVATE_INVALID_DOMAIN", error.getvalue())


if __name__ == "__main__":
    unittest.main()
