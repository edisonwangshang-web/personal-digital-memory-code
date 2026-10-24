"""Incremental publication uses only disposable sources and model fixtures."""

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

import test_multisource_pipeline
import test_pipeline
from pdm.capture import capture
from pdm.contracts import CaptureError, canonical, markdown, parse_markdown
from pdm.controls import read_controls, record_control
from pdm.pipeline import load_source, process_sources, publish, verify_publication
from pdm.transaction import PipelineGit


class PipelineUpdateTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    assert_error = test_pipeline.PipelineTests.assert_error
    commit_change = test_pipeline.PipelineTests.commit_change
    model = test_multisource_pipeline.MultisourcePipelineTests.model
    read = test_multisource_pipeline.MultisourcePipelineTests.read
    source = test_multisource_pipeline.MultisourcePipelineTests.source

    def initial(self):
        processed = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=self.model)
        self.assertEqual(processed["status"], "passed")
        return processed, publish(self.repo, processed["run_id"])

    def expanded(self, initial, *, model=None, run_id=None):
        other = self.source()
        paths = [self.saved["path"], other["path"]]
        return paths, process_sources(self.repo, paths, related_runs=[initial["run_id"]],
                                     run_id=run_id, model_call=model or self.model)

    def test_new_incremental_run_binds_mode_controls_and_atomic_processed_marker(self):
        processed, publication = self.initial()
        candidate_header, candidate = self.read(processed["stage_path"])
        self.assertEqual(candidate_header["schema_version"], "0.4")
        self.assertEqual(candidate["mode"], "incremental")
        self.assertEqual(candidate["related_runs"], [])
        self.assertEqual(self.contexts[0]["processing_mode"], "incremental")
        self.assertEqual(self.contexts[1]["controls"]["digest"], candidate["controls_digest"])
        marker = f"memory/runs/processes/processed/{candidate['processing_key']}.md"
        self.assertEqual(self.read(marker)[1]["run_id"], processed["run_id"])
        changed = test_pipeline.git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", publication["commit_sha"]).splitlines()
        self.assertEqual(set(changed), {*publication["files"], publication["manifest_path"], marker})
        self.assertEqual(self.read(publication["receipt_path"])[0]["schema_version"], "0.4")

    def test_independent_review_requires_controls_at_the_reserved_candidate_baseline(self):
        unrelated = self.source("unrelated-control", "用户：仅供隔离控制检查。\n", source_type="note")
        record_control(self.repo, {"request_key": "fixture:baseline-control", "action": "delete",
                                  "source_paths": [unrelated["path"]]})
        original_head = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        original_controls = read_controls(self.repo)

        def baseline_review(role, payload):
            response = self.model(role, payload)
            if role == "review":
                candidate, controls = payload["candidate"], payload["controls"]
                if (controls["head"] != candidate["base_head"]
                        or controls["digest"] != candidate["controls_digest"]):
                    response["output"] = {"verdict": "needs_revision",
                        "reasons": ["控制快照与候选基础版本不一致，不能核对审核依据。"]}
            return response

        processed = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=baseline_review)
        self.assertEqual(processed["status"], "passed")
        candidate = self.read(processed["stage_path"])[1]
        self.assertNotEqual(candidate["base_head"], original_head)
        self.assertEqual(self.contexts[0]["controls"], self.contexts[1]["controls"])
        self.assertEqual(self.contexts[1]["controls"], read_controls(self.repo, at_commit=candidate["base_head"]))
        self.assertEqual(self.contexts[1]["controls"]["events"], original_controls["events"])
        publication = publish(self.repo, processed["run_id"])
        self.assertEqual(verify_publication(self.repo, processed["run_id"])["commit_sha"], publication["commit_sha"])

    def test_update_preserves_stable_ids_and_creation_time_then_old_run_is_historical(self):
        first, first_publication = self.initial()
        old, _ = parse_markdown((self.repo / "memory/records/knowledge-shared.md").read_bytes())
        paths, second = self.expanded(first)
        second_publication = publish(self.repo, second["run_id"])
        current, _ = parse_markdown((self.repo / "memory/records/knowledge-shared.md").read_bytes())
        self.assertEqual(current["id"], old["id"])
        self.assertEqual(current["recorded_at"], old["recorded_at"])
        self.assertNotEqual(current["content_version"], old["content_version"])
        self.assertEqual(len(list((self.repo / "memory/records").glob("*.md"))), 1)
        self.assertEqual(self.contexts[-1]["related"][0]["run_id"], first["run_id"])
        self.assertEqual(len(self.contexts[-1]["related"][0]["documents"]), 2)
        self.assertTrue(all(operation["action"] == "update" for operation in second_publication["operations"].values()))
        self.assert_error("publication_mismatch", lambda: verify_publication(self.repo, first["run_id"]))
        historical = verify_publication(self.repo, first["run_id"], historical=True)
        self.assertEqual(historical["commit_sha"], first_publication["commit_sha"])
        self.assertTrue(historical["historical"])
        self.assertEqual(verify_publication(self.repo, second["run_id"], source_paths=paths)["commit_sha"], second_publication["commit_sha"])

    def test_repeat_input_after_update_is_zero_call_zero_write(self):
        first, _ = self.initial()
        paths, second = self.expanded(first)
        publication = publish(self.repo, second["run_id"])
        number = self.number
        repeated = process_sources(self.repo, list(reversed(paths)), related_runs=[first["run_id"]], model_call=self.model)
        self.assertEqual(repeated["status"], "already_processed")
        self.assertEqual(repeated["run_id"], second["run_id"])
        self.assertEqual(self.number, number)
        self.assertEqual(repeated["head"], publication["head"])

    def test_create_and_update_objects_are_one_commit(self):
        first, _ = self.initial()
        def additional(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                extra = copy.deepcopy(response["output"]["objects"][0])
                extra.update(id="knowledge-second", title="独立的第二记录")
                response["output"]["objects"].append(extra)
            return response
        _, second = self.expanded(first, model=additional)
        publication = publish(self.repo, second["run_id"])
        self.assertEqual(publication["operations"]["memory/records/knowledge-shared.md"]["action"], "update")
        self.assertEqual(publication["operations"]["memory/records/knowledge-second.md"]["action"], "create")
        changed = test_pipeline.git(self.repo, "diff-tree", "--no-commit-id", "--name-status", "-r", publication["commit_sha"])
        self.assertIn("A\tmemory/records/knowledge-second.md", changed)
        self.assertIn("M\tmemory/records/knowledge-shared.md", changed)
        self.assertIn("M\tmemory/topics/topic-shared.md", changed)

    def test_same_material_reuses_formal_bytes_and_origin_review(self):
        first, first_publication = self.initial()
        before = {path: (self.repo / path).read_bytes() for path in first_publication["files"]}
        def unchanged(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                old = {item["path"]: item for item in payload["related"][0]["documents"]}
                record = old["memory/records/knowledge-shared.md"]
                fields = set(response["output"]["objects"][0]) - {"body"}
                response["output"]["objects"] = [{**{key: record["metadata"][key] for key in fields}, "body": record["body"]}]
                wiki = old["memory/topics/topic-shared.md"]
                response["output"]["wiki"] = {**{key: wiki["metadata"][key] for key in ("id", "title", "sources")}, "body": wiki["body"]}
            return response
        _, second = self.expanded(first, model=unchanged)
        publication = publish(self.repo, second["run_id"])
        self.assertTrue(all(operation["action"] == "reuse" for operation in publication["operations"].values()))
        self.assertEqual(before, {path: (self.repo / path).read_bytes() for path in before})
        self.assertEqual(set(publication["object_origins"]), set(before))
        self.assertTrue(all(origin["run_id"] == first["run_id"] for origin in publication["object_origins"].values()))
        changed = test_pipeline.git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", publication["commit_sha"]).splitlines()
        self.assertFalse(set(changed) & set(before))

    def test_related_source_scope_is_rejected_before_any_unselected_source_load(self):
        paths = [self.saved["path"], self.source()["path"]]
        initial = process_sources(self.repo, paths, related_runs=[], model_call=self.model)
        publish(self.repo, initial["run_id"])
        self.roles.clear()
        loaded = []
        def selected_only(repo, path, *args, **kwargs):
            loaded.append(path)
            if path not in [self.saved["path"]]:
                self.fail("Unselected related source body was opened")
            return load_source(repo, path, *args, **kwargs)
        with patch("pdm.pipeline.load_source", side_effect=selected_only):
            self.assert_error("evidence_source_mismatch", lambda: process_sources(self.repo, [self.saved["path"]],
                related_runs=[initial["run_id"]], model_call=self.model))
        self.assertEqual(loaded, [self.saved["path"]])
        self.assertEqual(self.roles, [])

    def test_prior_record_change_after_review_invalidates_update(self):
        first, _ = self.initial()
        _, second = self.expanded(first)
        path = "memory/records/knowledge-shared.md"
        self.commit_change(path, (self.repo / path).read_bytes() + b"unreviewed fixture change")
        self.assert_error("publication_head_changed", lambda: publish(self.repo, second["run_id"]))

    def test_explicit_run_reuse_is_rejected_without_additional_model_calls(self):
        run_id = "run-" + "f" * 32
        first = process_sources(self.repo, [self.saved["path"]], related_runs=[], run_id=run_id, model_call=self.model)
        self.assertEqual(first["run_id"], run_id)
        count = self.number
        self.assert_error("run_already_exists", lambda: process_sources(self.repo, [self.saved["path"]],
            related_runs=[], run_id=run_id, model_call=self.model))
        self.assertEqual(self.number, count)

    def test_preallocated_duplicate_attempt_has_durable_zero_call_reuse_receipt(self):
        first, _ = self.initial()
        run_id = "run-" + "e" * 32
        number = self.number
        reused = process_sources(self.repo, [self.saved["path"]], related_runs=[], run_id=run_id, model_call=self.model)
        self.assertEqual(reused["status"], "already_processed")
        self.assertEqual(reused["attempt_run_id"], run_id)
        self.assertEqual(self.number, number)
        saved = self.read(reused["reuse_path"])[1]
        self.assertEqual(saved["original_run_id"], first["run_id"])
        self.assertEqual(saved["attempted_calls"], 0)

    def test_same_source_new_and_old_versions_are_explicitly_bound(self):
        first, _ = self.initial()
        updated = capture(self.repo, {"source_key": "fixture:one", "source_type": "conversation",
            "platform": "isolated", "acquisition_status": "full_text", "body": "用户：补充同一来源。\n"})
        result = process_sources(self.repo, [updated["path"], self.saved["path"]],
                                 related_runs=[first["run_id"]], model_call=self.model)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(self.contexts[-1]["sources"]), 2)
        self.assertEqual(publish(self.repo, result["run_id"])["status"], "published_local")

    def test_nonpassing_review_never_saves_success_marker(self):
        self.verdict = "needs_revision"
        result = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=self.model)
        candidate = self.read(result["stage_path"])[1]
        marker = self.repo / f"memory/runs/processes/processed/{candidate['processing_key']}.md"
        self.assertFalse(marker.exists())
        self.assert_error("review_not_passed", lambda: publish(self.repo, result["run_id"]))

    def test_incremental_review_remains_an_independent_invocation(self):
        def same_invocation(role, payload):
            response = self.model(role, payload)
            response["invocation_id"] = "model-" + "a" * 32
            return response
        result = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=same_invocation)
        self.assertEqual(result["error"], "review_not_independent")
        self.assertEqual(self.read(result["failure_path"])[0]["schema_version"], "0.4")
        self.assertFalse((self.repo / "memory/records").exists())

    def test_confirmation_review_is_durable_before_registry_registration(self):
        self.verdict = "needs_confirmation"
        result = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=self.model)
        self.assertEqual(result["status"], "needs_confirmation")
        metadata, body = parse_markdown((self.repo / "memory/pending.md").read_bytes())
        self.assertEqual(metadata["schema_version"], "0.4")
        registry = json.loads(body)
        self.assertEqual(len(registry["items"]), 1)
        self.assertEqual(registry["items"][0]["run_ids"], [result["run_id"]])
        self.assertEqual(result["registration_status"], "registered")
        self.assertIsNone(result["registration_error"])
        registration_path = result["registration_path"]
        git = PipelineGit(self.repo)
        review_commit = test_pipeline.git(self.repo, "log", "-1", "--format=%H", "--", result["review_path"])
        stage_commit = test_pipeline.git(self.repo, "log", "-1", "--format=%H", "--", result["stage_path"])
        registry_commit = test_pipeline.git(self.repo, "log", "-1", "--format=%H", "--", "memory/pending.md")
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", review_commit + "^"), stage_commit)
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", registry_commit + "^"), review_commit)
        self.assertEqual(registry_commit, result["head"])
        review_changes = test_pipeline.git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", review_commit).splitlines()
        registry_changes = test_pipeline.git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", registry_commit).splitlines()
        self.assertEqual(set(review_changes), {result["review_path"], result["pending_path"], registration_path})
        self.assertEqual(set(registry_changes), {"memory/pending.md", registration_path})
        self.assertEqual(git.run("ls-tree", review_commit, "--", "memory/pending.md"), b"")
        initial_metadata, initial_body = parse_markdown(git.read(review_commit, registration_path))
        final_metadata, final_body = parse_markdown((self.repo / registration_path).read_bytes())
        self.assertEqual(initial_metadata, {"schema_version": "0.7", "kind": "pending_registration", "run_id": result["run_id"]})
        self.assertEqual(final_metadata, initial_metadata)
        initial_marker, final_marker = json.loads(initial_body), json.loads(final_body)
        self.assertEqual(initial_marker["status"], "registration_required")
        self.assertIsNone(initial_marker["item_id"])
        self.assertEqual(final_marker["status"], "registered")
        self.assertEqual(final_marker["item_id"], registry["items"][0]["item_id"])
        immutable = lambda marker: {key: value for key, value in marker.items() if key not in {"status", "item_id"}}
        self.assertEqual(immutable(final_marker), immutable(initial_marker))
        for path in (result["review_path"], result["pending_path"]):
            self.assertEqual(git.read(review_commit, path), (self.repo / path).read_bytes())
            self.assertEqual(git.read(registry_commit, path), (self.repo / path).read_bytes())
        self.assertEqual(initial_marker["review_sha256"], "sha256-" + hashlib.sha256((self.repo / result["review_path"]).read_bytes()).hexdigest())
        self.assertEqual(initial_marker["stage_sha256"], "sha256-" + hashlib.sha256((self.repo / result["stage_path"]).read_bytes()).hexdigest())
        self.assertEqual(self.number, 2)
        self.assert_error("review_not_passed", lambda: publish(self.repo, result["run_id"]))
        self.assertEqual(git.head(), result["head"])
        self.assertEqual(self.number, 2)

    def test_correction_updates_same_object_with_new_evidence_and_preserves_old_proof(self):
        first, _ = self.initial()
        replacement = self.source("correction", "用户：更正原先内容，以此条为准。\n", source_type="note")
        record_control(self.repo, {"request_key": "fixture:correct", "action": "corrects",
            "source_paths": [self.saved["path"]], "replacement_source_paths": [replacement["path"]],
            "object_paths": ["memory/records/knowledge-shared.md", "memory/topics/topic-shared.md"]})
        def corrected(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                for value in [*response["output"]["objects"], response["output"]["wiki"]]:
                    value["sources"] = [ref for ref in value["sources"] if ref["source_id"] == replacement["source_id"]]
            return response
        processed = process_sources(self.repo, [self.saved["path"], replacement["path"]],
                                    related_runs=[first["run_id"]], model_call=corrected)
        publication = publish(self.repo, processed["run_id"])
        self.assertEqual(publication["status"], "published_local")
        self.assertEqual(verify_publication(self.repo, first["run_id"], historical=True)["run_id"], first["run_id"])
        self.assert_error("source_excluded", lambda: load_source(self.repo, self.saved["path"]))

    def test_new_control_after_review_invalidates_unchanged_candidate(self):
        first, _ = self.initial()
        paths, processed = self.expanded(first)
        record_control(self.repo, {"request_key": "fixture:delete-second", "action": "delete", "source_paths": [paths[1]]})
        self.assert_error("source_excluded", lambda: publish(self.repo, processed["run_id"]))

    def test_postpublication_source_deletion_cannot_be_resurrected_by_historical_verify(self):
        first, _ = self.initial()
        record_control(self.repo, {"request_key": "fixture:delete-original", "action": "delete", "source_paths": [self.saved["path"]],
            "object_paths": ["memory/records/knowledge-shared.md", "memory/topics/topic-shared.md"]})
        self.assert_error("source_excluded", lambda: verify_publication(self.repo, first["run_id"], historical=True))

    def test_incremental_receipt_interruption_reuses_original_commit_and_marker(self):
        result = process_sources(self.repo, [self.saved["path"]], related_runs=[], model_call=self.model)
        original = PipelineGit.write_files
        def stop_receipt(adapter, files, head, message):
            if any(path.endswith("/receipt.md") for path in files):
                raise CaptureError("fixture_interruption", "artificial receipt interruption")
            return original(adapter, files, head, message)
        with patch.object(PipelineGit, "write_files", stop_receipt):
            self.assert_error("fixture_interruption", lambda: publish(self.repo, result["run_id"]))
        published = verify_publication(self.repo, result["run_id"])
        self.assertFalse(published["receipt_saved"])
        recovered = publish(self.repo, result["run_id"])
        self.assertEqual(recovered["commit_sha"], published["commit_sha"])
        self.assertTrue(recovered["receipt_saved"])

    def test_authority_snapshots_include_both_versions_of_updated_formal_record(self):
        first, original = self.initial()
        _, second = self.expanded(first)
        current = publish(self.repo, second["run_id"])
        path = "memory/records/knowledge-shared.md"
        snapshots = [item for item in current["authority_snapshots"] if item["path"] == path]
        self.assertEqual({item["commit_sha"] for item in snapshots}, {original["commit_sha"], current["commit_sha"]})
        self.assertEqual(len({item["sha256"] for item in snapshots}), 2)
        self.assertEqual(current["authority_files"][path], current["files"][path])

    def test_started_marker_prevents_repeating_a_run_interrupted_inside_model_call(self):
        run_id = "run-" + "d" * 32
        calls = []
        def interrupted(role, payload):
            calls.append(role)
            raise KeyboardInterrupt("artificial process interruption")
        with self.assertRaises(KeyboardInterrupt):
            process_sources(self.repo, [self.saved["path"]], related_runs=[], run_id=run_id, model_call=interrupted)
        self.assertEqual(calls, ["extract"])
        started = self.read(f"memory/runs/processes/{run_id}/started.md")[1]
        self.assertEqual(started["reserved_calls"], 2)
        self.assert_error("run_already_exists", lambda: process_sources(self.repo, [self.saved["path"]],
            related_runs=[], run_id=run_id, model_call=interrupted))
        self.assertEqual(calls, ["extract"])

    def test_related_records_cannot_silently_disappear_from_updated_topic(self):
        first, publication = self.initial()
        other = self.source()
        self.source("unselected", "用户：这条来源不在处理范围。\n", source_type="note")
        paths = [self.saved["path"], other["path"]]
        before = {path: (self.repo / path).read_bytes() for path in test_pipeline.git(
            self.repo, "ls-tree", "-r", "--name-only", "HEAD", "--", "memory").splitlines()}
        baseline = test_pipeline.git(self.repo, "rev-parse", "HEAD")
        run_id = "run-" + "c" * 32
        extracted = []
        def renamed(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                response["output"]["objects"][0]["id"] = "knowledge-unrelated-new"
                extracted.append(copy.deepcopy(response))
            return response
        def selected_only(repo, path, *args, **kwargs):
            self.assertIn(path, paths, "Unselected source body was opened")
            return load_source(repo, path, *args, **kwargs)
        with patch("pdm.pipeline.load_source", side_effect=selected_only):
            result = process_sources(self.repo, paths, related_runs=[first["run_id"]],
                                     run_id=run_id, model_call=renamed)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "related_records_omitted")
        self.assertEqual(self.roles, ["extract", "review", "extract"])
        metadata, failure = self.read(result["failure_path"])
        self.assertEqual(metadata["schema_version"], "0.4")
        self.assertEqual((failure["phase"], failure["attempted_calls"]), ("extract", 1))
        self.assertEqual(failure["call"], {key: extracted[0][key] for key in
            ("invocation_id", "model", "elapsed_seconds", "usage")} | {
                "output_sha256": "sha256-" + hashlib.sha256(canonical(extracted[0]["output"])).hexdigest()})
        prefix = f"memory/runs/processes/{run_id}/"
        changed = test_pipeline.git(self.repo, "diff", "--name-only", baseline, "HEAD").splitlines()
        self.assertEqual(set(changed), {prefix + "started.md", result["failure_path"]})
        started = self.read(prefix + "started.md")[1]
        self.assertEqual(set(started["source_paths"]), set(paths))
        self.assertEqual(started["related_runs"], [first["run_id"]])
        self.assertFalse((self.repo / f"memory/runs/processes/processed/{started['processing_key']}.md").exists())
        with self.assertRaises(CaptureError):
            publish(self.repo, run_id)
        self.assertEqual(test_pipeline.git(self.repo, "rev-parse", "HEAD"), result["head"])
        self.assertEqual(before, {path: (self.repo / path).read_bytes() for path in before})
        self.assertEqual(verify_publication(self.repo, first["run_id"], historical=True)["commit_sha"], publication["commit_sha"])

    def test_head_change_after_extract_is_not_converted_to_a_model_output_failure(self):
        first, publication = self.initial()
        other = self.source()
        run_id = "run-" + "b" * 32
        before = {path: (self.repo / path).read_bytes() for path in publication["files"]}
        def concurrent_commit(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                response["output"]["objects"][0]["id"] = "knowledge-unrelated-new"
                test_pipeline.git(self.repo, "commit", "--allow-empty", "-m", "isolated concurrent commit")
            return response
        self.assert_error("head_conflict", lambda: process_sources(self.repo, [self.saved["path"], other["path"]],
            related_runs=[first["run_id"]], run_id=run_id, model_call=concurrent_commit))
        self.assertEqual(self.roles, ["extract", "review", "extract"])
        prefix = self.repo / f"memory/runs/processes/{run_id}"
        self.assertEqual({path.name for path in prefix.iterdir()}, {"started.md"})
        self.assertEqual(before, {path: (self.repo / path).read_bytes() for path in before})

    def test_unselected_topic_dependency_is_detected_using_only_its_header(self):
        first, publication = self.initial()
        record, _ = parse_markdown((self.repo / "memory/records/knowledge-shared.md").read_bytes())
        path = "memory/topics/topic-other.md"
        self.commit_change(path, markdown({"schema_version": "0.2", "id": "topic-other", "type": "wiki",
            "records": [{"id": record["id"], "content_version": record["content_version"]}]}, "UNSELECTED_TOPIC_BODY\n"))
        original_read = PipelineGit.read
        def selected_only(adapter, commit, relative):
            if relative == path:
                self.fail("Unselected dependent topic body was opened")
            return original_read(adapter, commit, relative)
        with patch.object(PipelineGit, "read", selected_only):
            _, result = self.expanded(first)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "required_related_context")
        self.assertTrue((self.repo / "memory/records/knowledge-shared.md").exists())
        self.assertEqual(self.roles, ["extract", "review", "extract"])

    def test_processed_input_cannot_claim_an_extra_uncompared_related_context(self):
        second_source = self.source()
        paths = [self.saved["path"], second_source["path"]]
        original = process_sources(self.repo, paths, related_runs=[], model_call=self.model)
        publish(self.repo, original["run_id"])
        def other_context(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                response["output"]["objects"][0]["id"] = "knowledge-other-context"
                response["output"]["wiki"]["id"] = "topic-other-context"
            return response
        extra = process_sources(self.repo, [second_source["path"]], model_call=other_context)
        publish(self.repo, extra["run_id"])
        count = self.number
        self.assert_error("processing_context_changed", lambda: process_sources(self.repo, paths,
            related_runs=[extra["run_id"]], model_call=self.model))
        self.assertEqual(self.number, count)
