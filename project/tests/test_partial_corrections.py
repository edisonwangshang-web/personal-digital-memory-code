"""Object-version corrections over artificial, isolated Git repositories."""

from datetime import datetime
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError, parse_markdown
from pdm.controls import (object_policy, plan_control, read_controls, record_control,
                          source_locator_policy, source_policy)
from pdm.evidence import ask_current, ask_sources, build_current_evidence, evidence
from pdm.pipeline import load_source, process_sources, publish, verify_publication


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class PartialCorrectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial baseline")
        self.old = capture(self.repo, {"source_key": "fixture:partial:old", "source_type": "note",
                           "acquisition_status": "full_text", "body": "Artificial A was two.\nArtificial B remains seven.\n"})
        self.new = capture(self.repo, {"source_key": "fixture:partial:correction", "source_type": "note",
                           "acquisition_status": "full_text", "body": "Correction: artificial A is one; B is unchanged.\n"})
        self.paths = [self.old["path"], self.new["path"]]
        self.calls = []
        old = process_sources(self.repo, [self.paths[0]], model_call=self.model)
        self.assertEqual(old["status"], "passed")
        self.old_run = old["run_id"]
        self.publication = publish(self.repo, self.old_run)
        self.record_a = "memory/records/state-artificial-a.md"
        self.record_b = "memory/records/state-artificial-b.md"
        self.topic = "memory/topics/topic-artificial.md"
        self.versions = {path: parse_markdown((self.repo / path).read_bytes())[0]["content_version"]
                         for path in self.publication["files"]}

    def model(self, role, payload):
        self.calls.append(role)
        if role == "extract":
            sources = {source["path"]: source for source in payload["sources"]}
            updated = self.paths[1] in sources
            def ref(path, line):
                source = sources[path]
                return {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                        "locator": f"L{line}-L{line}", "quote": source["body"].splitlines()[line - 1]}
            refs = [ref(self.paths[1] if updated else self.paths[0], 1), ref(self.paths[0], 2)]
            objects = [{"id": f"state-artificial-{key}", "type": "state", "title": f"Artificial {key}",
                        "source_kind": "user_statement", "sources": [refs[index]], "domains": ["health"],
                        "valid_from": None, "valid_to": None, "verification_status": "unverified",
                        "lifecycle_status": "current", "body": body}
                       for index, (key, body) in enumerate((("a", "Artificial A is one." if updated else "Artificial A was two."),
                                                           ("b", "Artificial B remains seven.")))]
            output = {"objects": objects, "wiki": {"id": "topic-artificial", "title": "Artificial topic",
                       "sources": refs, "body": "Corrected artificial topic." if updated else "Old artificial topic."}}
        elif role == "review":
            output = {"verdict": "passed", "reasons": ["Artificial isolated fixture."]}
        else:
            output = {"answer": "Artificial B remains seven.", "citations": [{
                      "source_id": self.old["source_id"], "source_version": self.old["source_version"],
                      "locator": "L2-L2"}], "limitations": ["Explicit artificial scope only."]}
        return {"output": output, "invocation_id": "model-" + f"{len(self.calls):032x}",
                "model": "artificial", "elapsed_seconds": 0.01, "usage": None}

    def payload(self, **changes):
        value = {"request_key": "fixture:partial-control", "action": "corrects", "scope": "objects",
                 "source_paths": [self.paths[0]], "object_paths": [self.record_a, self.topic],
                 "object_versions": {path: self.versions[path] for path in [self.record_a, self.topic]},
                 "replacement_source_paths": [self.paths[1]], "basis_source_path": self.paths[1]}
        value.update(changes)
        return value

    def assert_code(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def snapshot(self):
        return (git(self.repo, "rev-parse", "HEAD"),
                {str(path.relative_to(self.repo)): path.read_bytes() for path in (self.repo / "memory").rglob("*.md")})

    def test_partial_scope_keeps_other_record_current_and_raw_explicitly_restricted(self):
        before_calls = len(self.calls)
        control = record_control(self.repo, self.payload())
        self.assertEqual(len(self.calls), before_calls)
        metadata, _ = parse_markdown((self.repo / control["path"]).read_bytes())
        self.assertEqual((metadata["schema_version"], metadata["kind"]), ("0.6", "object_control"))
        snapshot = read_controls(self.repo)
        decision = source_policy(snapshot, self.paths[0])
        self.assertFalse(decision["excluded"])
        self.assertEqual([item["locator"] for item in decision["object_restrictions"]], ["L1-L1"])
        self.assertTrue(source_locator_policy(snapshot, self.paths[0], "L1-L2")["excluded"])
        self.assertFalse(source_locator_policy(snapshot, self.paths[0], "L2-L2")["excluded"])
        self.assertIn("Artificial B", load_source(self.repo, self.paths[0])["body"])
        self.assertTrue(object_policy(snapshot, self.record_a, content_version=self.versions[self.record_a])["excluded"])
        self.assertFalse(object_policy(snapshot, self.record_b, content_version=self.versions[self.record_b])["excluded"])
        before = self.snapshot()
        selected = build_current_evidence(self.repo, self.paths, [self.old_run])
        self.assertEqual([doc["path"] for doc in selected["publications"][0]["documents"]], [self.record_b])
        self.assertEqual(selected["sources"][0]["knowledge_status"], "published")
        self.assertTrue(selected["sources"][0]["source_policy"]["object_restrictions"])
        self.assertTrue(any("旧行只作纠正历史上下文" in line for line in selected["limitations"]))
        self.assertEqual(self.snapshot(), before)

    def test_current_answer_rejects_old_corrected_line_but_accepts_other_fact(self):
        record_control(self.repo, self.payload())
        before = self.snapshot()
        output = ask_current(self.repo, self.paths, "Artificial B?", [self.old_run], model_call=self.model)
        self.assertEqual(output["citations"][0]["locator"], "L2-L2")
        def wrong(role, payload):
            result = self.model(role, payload)
            result["output"]["citations"][0]["locator"] = "L1-L1"
            return result
        self.assert_code("invalid_citation", lambda: ask_current(self.repo, self.paths, "Artificial A?", model_call=wrong))
        self.assertEqual(self.snapshot(), before)

    def test_history_retains_bound_old_facts_with_current_controls_and_no_current_claim(self):
        record_control(self.repo, self.payload())
        selected = build_current_evidence(self.repo, self.paths, [self.old_run], history=True)
        self.assertEqual(len(selected["publications"][0]["documents"]), 3)
        self.assertFalse(selected["coverage"]["current_complete"])
        self.assertFalse(source_locator_policy(read_controls(self.repo), self.paths[0], "L1-L1", purpose="history")["excluded"])

    def test_old_entry_points_cannot_bypass_partial_controls_or_call_model(self):
        record_control(self.repo, self.payload())
        with patch("pdm.evidence.load_source", side_effect=AssertionError("Legacy body must not be opened")):
            self.assert_code("current_evidence_required", lambda: evidence(self.repo, self.paths[0]))
            self.assert_code("current_evidence_required", lambda: ask_sources(self.repo, self.paths, "Artificial?", model_call=self.model))
        self.assertEqual(self.calls, ["extract", "review"])

    def test_partial_old_source_does_not_authorize_opening_unselected_replacement(self):
        record_control(self.repo, self.payload())
        from pdm import evidence as evidence_module
        original = evidence_module.load_source
        def selected_only(repo, path, *args, **kwargs):
            if path != self.paths[0]:
                self.fail("Partial control expanded the explicit source boundary")
            return original(repo, path, *args, **kwargs)
        before = self.snapshot()
        with patch("pdm.evidence.load_source", side_effect=selected_only):
            selected = build_current_evidence(self.repo, [self.paths[0]], [self.old_run])
        self.assertEqual(selected["coverage"]["missing_update_paths"], [self.paths[1]])
        self.assertFalse(selected["coverage"]["current_complete"])
        self.assertEqual([doc["path"] for doc in selected["publications"][0]["documents"]], [self.record_b])
        self.assertEqual(self.snapshot(), before)

    def test_dependency_plan_requires_explicit_topic_but_does_not_suppress_its_unrelated_lines(self):
        payload = self.payload(object_paths=[self.record_a], object_versions={self.record_a: self.versions[self.record_a]})
        before = self.snapshot()
        plan = plan_control(self.repo, payload)
        self.assertEqual(plan["missing_object_paths"], [self.topic])
        self.assertEqual(plan["source_suppression"], "object_locators")
        self.assertEqual(plan["source_locators"][self.paths[0]][0]["locator"], "L1-L1")
        self.assert_code("control_dependencies", lambda: record_control(self.repo, payload))
        self.assertEqual(self.snapshot(), before)

    def test_explicit_versions_and_real_basis_are_required_before_writes(self):
        before = self.snapshot()
        for changes in ({"object_versions": {}}, {"object_paths": []}, {"basis_source_path": None},
                        {"basis_source_path": self.paths[0]}, {"action": "delete"}):
            self.assert_code("invalid_control_scope", lambda changes=changes: record_control(self.repo, self.payload(**changes)))
        stale = dict(self.payload()["object_versions"])
        stale[self.record_a] = "sha256-" + "f" * 64
        self.assert_code("control_version_mismatch", lambda: record_control(self.repo, self.payload(object_versions=stale)))
        missing = self.paths[1].replace(self.new["source_version"], "sha256-" + "f" * 64)
        with self.assertRaises(CaptureError):
            record_control(self.repo, self.payload(replacement_source_paths=[missing], basis_source_path=missing))
        self.assertEqual(self.snapshot(), before)

    def test_link_only_basis_is_not_a_real_correction(self):
        link = capture(self.repo, {"source_key": "fixture:partial:link", "source_type": "note",
                       "acquisition_status": "link_only", "original_url": "https://example.invalid/fixture", "body": ""})
        before = self.snapshot()
        self.assert_code("control_basis_missing", lambda: record_control(self.repo, self.payload(
                         replacement_source_paths=[link["path"]], basis_source_path=link["path"])))
        self.assertEqual(self.snapshot(), before)

    def test_multiline_crlf_reference_is_bound_without_normalizing_original_bytes(self):
        source = capture(self.repo, {"source_key": "fixture:partial:crlf", "source_type": "note",
                         "acquisition_status": "full_text", "body": "Artificial C was two.\r\nEvidence note.\r\n"})
        def model(role, payload):
            self.calls.append(role)
            if role == "extract":
                given = payload["sources"][0]
                refs = [{"source_id": given["metadata"]["source_id"], "source_version": given["metadata"]["source_version"],
                         "locator": "L1-L2", "quote": given["body"]}]
                output = {"objects": [{"id": "state-artificial-crlf", "type": "state", "title": "Artificial CRLF",
                           "source_kind": "user_statement", "sources": refs, "domains": ["health"],
                           "valid_from": None, "valid_to": None, "verification_status": "unverified",
                           "lifecycle_status": "current", "body": "Artificial CRLF fact."}],
                           "wiki": {"id": "topic-artificial-crlf", "title": "Artificial CRLF topic",
                                    "sources": refs, "body": "Artificial CRLF summary."}}
            else:
                output = {"verdict": "passed", "reasons": ["Artificial isolated fixture."]}
            return {"output": output, "invocation_id": "model-" + f"{len(self.calls):032x}",
                    "model": "artificial", "elapsed_seconds": 0.01, "usage": None}
        processed = process_sources(self.repo, [source["path"]], model_call=model)
        self.assertEqual(processed["status"], "passed", processed)
        published = publish(self.repo, processed["run_id"])
        versions = {path: parse_markdown((self.repo / path).read_bytes())[0]["content_version"]
                    for path in published["files"]}
        raw = (self.repo / source["path"]).read_bytes()
        result = record_control(self.repo, self.payload(request_key="fixture:crlf-control", source_paths=[source["path"]],
                                object_paths=sorted(versions), object_versions=versions))
        self.assertEqual(result["plan"]["source_locators"][source["path"]][0]["locator"], "L1-L2")
        self.assertEqual((self.repo / source["path"]).read_bytes(), raw)

    def test_corrected_same_id_new_version_reads_and_unaffected_record_is_exact_reuse(self):
        old_b = (self.repo / self.record_b).read_bytes()
        record_control(self.repo, self.payload())
        updated = process_sources(self.repo, self.paths, related_runs=[self.old_run], model_call=self.model)
        self.assertEqual(updated["status"], "passed", updated)
        publication = publish(self.repo, updated["run_id"])
        verify_publication(self.repo, updated["run_id"], source_paths=self.paths)
        self.assertEqual((self.repo / self.record_b).read_bytes(), old_b)
        new_version = parse_markdown((self.repo / self.record_a).read_bytes())[0]["content_version"]
        self.assertNotEqual(new_version, self.versions[self.record_a])
        self.assertFalse(object_policy(read_controls(self.repo), self.record_a, content_version=new_version)["excluded"])
        selected = build_current_evidence(self.repo, self.paths, [updated["run_id"]])
        self.assertEqual(len(selected["publications"][0]["documents"]), 3)
        self.assertEqual(selected["publications"][0]["object_origins"], publication["object_origins"])
        self.assertEqual(self.calls, ["extract", "review", "extract", "review"])

    def test_fresh_identity_cannot_restore_restricted_raw_fact_without_correction_basis(self):
        record_control(self.repo, self.payload())
        protected = {path: (self.repo / path).read_bytes() for path in self.publication["files"]}
        def resurrect(role, payload):
            result = self.model(role, payload)
            if role == "extract":
                result["output"]["objects"] = [result["output"]["objects"][0]]
                result["output"]["objects"][0]["id"] = "state-artificial-resurrected"
                result["output"]["wiki"]["id"] = "topic-artificial-resurrected"
            return result
        result = process_sources(self.repo, [self.paths[0]], model_call=resurrect)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "correction_evidence_missing")
        self.assertEqual(self.calls, ["extract", "review", "extract"])
        self.assertEqual({path: (self.repo / path).read_bytes() for path in protected}, protected)
        self.assertFalse((self.repo / "memory/records/state-artificial-resurrected.md").exists())

    def test_new_scope_idempotency_and_legacy_source_scope_remain_separate(self):
        first = record_control(self.repo, self.payload())
        before = self.snapshot()
        self.assertTrue(record_control(self.repo, self.payload())["reused"])
        self.assertEqual(self.snapshot(), before)
        legacy = self.payload()
        legacy.pop("scope")
        legacy.pop("object_versions")
        self.assert_code("control_key_conflict", lambda: record_control(self.repo, legacy))
        legacy.update(request_key="fixture:source-wide", object_paths=[])
        result = record_control(self.repo, legacy)
        metadata, _ = parse_markdown((self.repo / result["path"]).read_bytes())
        self.assertEqual(metadata["schema_version"], "0.4")
        self.assertTrue(source_policy(read_controls(self.repo), self.paths[0])["excluded"])
        self.assertNotEqual(first["control_id"], result["control_id"])

    def test_supersession_time_keeps_future_or_unknown_partial_fact_from_becoming_current_complete(self):
        record_control(self.repo, self.payload(action="supersedes", effective_from="9999-12"))
        at = datetime(2026, 10, 19, tzinfo=ZoneInfo("Asia/Shanghai"))
        snapshot = read_controls(self.repo)
        self.assertFalse(source_locator_policy(snapshot, self.paths[0], "L1-L1", as_of=at)["excluded"])
        self.assertFalse(object_policy(snapshot, self.record_a, content_version=self.versions[self.record_a], as_of=at)["excluded"])
        selected = build_current_evidence(self.repo, self.paths, [self.old_run])
        self.assertEqual(len(selected["publications"][0]["documents"]), 3)
        unknown = self.payload(request_key="fixture:unknown-time", action="supersedes", effective_from=None)
        record_control(self.repo, unknown)
        selected = build_current_evidence(self.repo, self.paths, [self.old_run])
        self.assertFalse(selected["coverage"]["current_complete"])

    def test_source_delete_after_partial_still_requires_all_objects_and_blocks_all_versions(self):
        record_control(self.repo, self.payload())
        payload = {"request_key": "fixture:delete-after-partial", "action": "delete", "source_paths": [self.paths[0]]}
        plan = plan_control(self.repo, payload)
        self.assertEqual(plan["required_object_paths"], sorted([self.record_a, self.record_b, self.topic]))
        self.assert_code("control_dependencies", lambda: record_control(self.repo, payload))
        record_control(self.repo, {**payload, "object_paths": plan["required_object_paths"]})
        self.assertTrue(source_policy(read_controls(self.repo), self.paths[0], purpose="history")["excluded"])
        self.assert_code("source_excluded", lambda: load_source(self.repo, self.paths[0], purpose="history"))
        self.assertTrue(all(not (self.repo / path).exists() for path in plan["required_object_paths"]))


if __name__ == "__main__":
    unittest.main()
