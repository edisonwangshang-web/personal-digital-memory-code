"""New quality publications remain usable through scoped legacy consumers."""

import copy
import json
import unittest
from unittest.mock import patch

import test_skill_quality_pipeline as ordinary
import test_skill_quality_decomposed as batch
from test_pipeline import git
from pdm.batching import plan_batches, project_batch_sources
from pdm.capture import capture
from pdm.context import plan_answered_pending
from pdm.contracts import CaptureError, parse_markdown
from pdm.evidence import _peek_publication, build_current_evidence
from pdm.pending import answer_pending, list_pending, register_pending, resolve_pending, _publication_source_refs
from pdm.pipeline import _candidate_binding, _document, _read_reviewed, process_sources, publish
from pdm.portable import prepare_reading_package, verify_reading_package
from pdm.revision_context import build_revision_context
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class QualityPublicationConsumersTests(unittest.TestCase):
    setUp = ordinary.SkillQualityPipelineTests.setUp
    model = ordinary.SkillQualityPipelineTests.model
    process = ordinary.SkillQualityPipelineTests.process
    read = ordinary.SkillQualityPipelineTests.read
    assert_error = ordinary.SkillQualityPipelineTests.assert_error

    def snapshot(self):
        adapter = PipelineGit(self.repo)
        return (adapter.head(), len(self.calls),
                {str(path.relative_to(self.repo)): path.read_bytes()
                 for path in (self.repo / "memory").rglob("*.md")})

    def assert_views(self, run):
        before = self.snapshot()
        for history in (False, True):
            view = build_current_evidence(self.repo, [self.saved["path"]], [run], history=history)
            self.assertEqual(len(view["publications"]), 1)
            self.assertEqual(len(view["publications"][0]["documents"]), 2)
            self.assertEqual(view["coverage"]["unread_runs"], [])
        self.assertEqual(self.snapshot(), before)

    def test_ordinary_quality_current_history_and_portable_preserve_actual_documents(self):
        processed = self.process()
        self.assertEqual(processed["status"], "passed", processed)
        publish(self.repo, processed["run_id"])
        self.assert_views(processed["run_id"])
        output = self.repo.parent / (self.repo.name + "-quality-package.json")
        self.addCleanup(lambda: output.unlink(missing_ok=True))
        before = self.snapshot()
        prepared = prepare_reading_package(self.repo, [self.saved["path"]],
            ["What is the artificial plan?"], [processed["run_id"]], output=output)
        self.assertEqual(verify_reading_package(output, expected_sha256=prepared["sha256"])["status"],
                         "integrity_verified")
        package = json.loads(output.read_bytes())
        self.assertEqual(len(package["payload"]["evidence"]["publications"][0]["documents"]), 2)
        self.assertEqual(self.snapshot(), before)

    def test_quality_revision_current_and_historical_consumers_accept_bound_feedback(self):
        def reject(role, output):
            if role == "review":
                output["verdict"] = "needs_revision"
                output["checks"][0]["result"] = "needs_revision"
        self.mutate = reject
        rejected = self.process()
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        before = self.snapshot()
        with patch("pdm.pipeline._read_reviewed",
                   side_effect=CaptureError("review_not_passed", "Rejected dependency")):
            self.assert_error("review_not_passed", lambda: build_revision_context(
                PipelineGit(self.repo), rejected["run_id"], source_paths=[self.saved["path"]]))
        self.assertEqual(self.snapshot(), before)
        self.mutate = lambda role, output: output["objects"][0].update(
            body=output["objects"][0]["body"] + "保留用户明确的资料整理目标。") if role == "extract" else None
        revised = process_sources(self.repo, [self.saved["path"]], model_call=self.model,
                                  revision_from_run=rejected["run_id"])
        self.assertEqual(revised["status"], "passed", revised)
        publish(self.repo, revised["run_id"])
        self.assert_views(revised["run_id"])

    def replace_publication_declarations(self, processed, published, mutate):
        candidate = self.read(processed["stage_path"], "candidate")
        manifest = self.read(published["manifest_path"], "manifest")
        mutate(candidate, manifest)
        candidate["binding"] = _candidate_binding(candidate)
        manifest["binding"] = candidate["binding"]
        for path, kind, value in ((processed["stage_path"], "candidate", candidate),
                                  (published["manifest_path"], "manifest", manifest)):
            (self.repo / path).write_bytes(_document(kind, value, "0.10"))
            git(self.repo, "add", path)
        git(self.repo, "commit", "-m", "fixture: tamper declared publication scope")

    def test_quality_label_cannot_replace_quality_contract_or_expand_ordinary_source_limit(self):
        processed = self.process()
        published = publish(self.repo, processed["run_id"])
        adapter = PipelineGit(self.repo)
        candidate = self.read(processed["stage_path"], "candidate")
        manifest = self.read(published["manifest_path"], "manifest")
        saved = copy.deepcopy(candidate), copy.deepcopy(manifest)
        for mode in ("missing_quality", "fake_batch", "invalid_strategy", "ordinary_over_limit"):
            with self.subTest(mode=mode):
                def mutate(changed, declared):
                    changed.clear()
                    changed.update(copy.deepcopy(saved[0]))
                    declared.clear()
                    declared.update(copy.deepcopy(saved[1]))
                    if mode == "missing_quality":
                        for field in ("quality_contract", "skill_bundle", "quality_context",
                                      "extraction_request_binding"):
                            changed.pop(field)
                    elif mode == "fake_batch":
                        changed["batch_context"] = {}
                    elif mode == "invalid_strategy":
                        changed["review_strategy"] = []
                    else:
                        additional = []
                        for number in range(8):
                            identifier = "src-" + f"{number:064x}"
                            version = "sha256-" + f"{number:064x}"
                            additional.append({"source_id": identifier, "source_version": version,
                                "path": f"memory/sources/inbox/{identifier}/{version}.md"})
                        changed["additional_sources"] = additional
                        declared["additional_sources"] = additional
                self.replace_publication_declarations(processed, published, mutate)
                before = self.snapshot()
                with patch("pdm.pipeline.load_source") as read, patch("pdm.pipeline.verify_publication") as verify:
                    with self.assertRaises(CaptureError):
                        _peek_publication(adapter, processed["run_id"], adapter.head())
                    with self.assertRaises(CaptureError):
                        _publication_source_refs(adapter, processed["run_id"])
                    read.assert_not_called()
                    verify.assert_not_called()
                self.assertEqual(self.snapshot(), before)

    def test_quality_confirmation_registers_then_answer_is_processed_and_resolved(self):
        def confirm(role, output):
            if role == "review":
                output["verdict"] = "needs_confirmation"
                output["checks"][0]["result"] = "needs_confirmation"
        self.mutate = confirm
        waiting = self.process(related_runs=[])
        self.assertEqual(waiting["status"], "needs_confirmation", waiting)
        self.assertEqual(waiting["registration_status"], "registered")
        item = list_pending(self.repo)[0]
        self.assertEqual(item["pending_status"], "open")
        self.assertEqual(len(item["object_refs"]), 2)
        reply = capture(self.repo, {
            "source_key": "quality:reply", "source_type": "conversation",
            "acquisition_status": "full_text", "body": "用户：我计划整理运动资料。\n"})
        answer_pending(self.repo, item["item_id"], reply["path"])
        before = self.snapshot()
        with patch("pdm.pipeline._read_reviewed",
                   side_effect=CaptureError("review_not_passed", "Rejected dependency")):
            self.assert_error("review_not_passed", lambda: plan_answered_pending(self.repo,
                allowed_source_paths=[self.saved["path"], reply["path"]], allowed_run_ids=[waiting["run_id"]]))
        self.assertEqual(self.snapshot(), before)
        planned = plan_answered_pending(self.repo,
            allowed_source_paths=[self.saved["path"], reply["path"]], allowed_run_ids=[waiting["run_id"]])
        self.assertEqual(len(planned["jobs"]), 1)
        job = planned["jobs"][0]
        context = {key: job[key] for key in ("job_id", "parent_item_id", "question", "parent_run_ids",
                                           "source_refs", "object_refs", "reply_sources")}
        paths = [self.saved["path"], reply["path"]]
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        projection = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=paths)
        self.mutate = None
        def continuation_model(role, payload):
            response = self.model(role, payload)
            if role == "extract":
                output = response["output"]
                refs = [{"source_id": source["metadata"]["source_id"],
                         "source_version": source["metadata"]["source_version"],
                         "locator": "L1-L1", "quote": source["fragments"][0]["lines"][0]["text"]}
                        for source in payload["sources"]]
                output["objects"][0]["sources"] = refs
                wiki = output.pop("wiki")
                wiki.update(sources=refs, record_ids=["sport-plan"])
                output["wikis"] = [wiki]
            return response
        completed = process_sources(self.repo, paths, model_call=continuation_model,
            related_runs=[], skill_bundle=self.bundle, continuation_context=context,
            batch_context=projection["batch_context"], source_views=projection["source_views"])
        self.assertEqual(completed["status"], "passed", completed)
        candidate = self.read(completed["stage_path"], "candidate")
        self.assertEqual(candidate["continuation_context"], context)
        self.assertEqual(candidate["skill_bundle"], self.bundle)
        publish(self.repo, completed["run_id"])
        resolved = resolve_pending(self.repo, item["item_id"], completed["run_id"])
        self.assertEqual(resolved["pending_status"], "resolved")
        before = self.snapshot()
        view = build_current_evidence(self.repo, [self.saved["path"], reply["path"]], [completed["run_id"]])
        pending = view["pending"]["items"][0]
        self.assertEqual(pending["pending_status"], "resolved")
        self.assertEqual(pending["resolution_verification"]["status"], "verified_history")
        self.assertEqual(self.snapshot(), before)

    def test_pending_never_treats_a_rejected_dependency_as_a_completed_root_review(self):
        processed = self.process(related_runs=[])
        candidate = self.read(processed["stage_path"], "candidate")
        item = {"question": "人工问题", "run_ids": [processed["run_id"]],
                "source_refs": [{key: self.saved[key] for key in ("path", "source_id", "source_version")}],
                "object_refs": [{"path": obj["path"], "id": obj["metadata"]["id"],
                                 "content_version": obj["metadata"]["content_version"]}
                                for obj in candidate["objects"]]}
        before = self.snapshot()
        # Simulate a dependency rejecting before the root Review is read.
        # Even with root non-passing results allowed, that failure must escape.
        with patch("pdm.pipeline._read_fixed_candidate",
                   side_effect=CaptureError("review_not_passed", "Rejected dependency")):
            self.assert_error("review_not_passed", lambda: _read_reviewed(
                PipelineGit(self.repo), processed["run_id"], allow_nonpassed=True))
            self.assert_error("review_not_passed", lambda: register_pending(self.repo, item))
        self.assertEqual(list_pending(self.repo), [])
        self.assertEqual(self.snapshot(), before)

    def test_pending_rejects_quality_candidate_with_missing_actual_review_checks(self):
        processed = self.process(related_runs=[])
        candidate = self.read(processed["stage_path"], "candidate")
        review = self.read(processed["review_path"], "review")
        del review["output"]["checks"]
        (self.repo / processed["review_path"]).write_bytes(_document("review", review, "0.10"))
        git(self.repo, "add", processed["review_path"])
        git(self.repo, "commit", "-m", "fixture: remove actual quality checks")
        item = {"question": "人工问题", "run_ids": [processed["run_id"]],
                "source_refs": [{key: self.saved[key] for key in ("path", "source_id", "source_version")}],
                "object_refs": [{"path": obj["path"], "id": obj["metadata"]["id"],
                                 "content_version": obj["metadata"]["content_version"]}
                                for obj in candidate["objects"]]}
        before = self.snapshot()
        with self.assertRaises(CaptureError):
            register_pending(self.repo, item)
        self.assertEqual(list_pending(self.repo), [])
        self.assertEqual(self.snapshot(), before)


class QualityBatchConsumersTests(unittest.TestCase):
    setUp = batch.SkillQualityDecomposedTests.setUp
    sources = batch.SkillQualityDecomposedTests.sources
    prepare = batch.SkillQualityDecomposedTests.prepare
    model = batch.SkillQualityDecomposedTests.model

    def test_real_quality_batch_expands_source_limit_only_after_binding_and_scope_checks(self):
        paths, _, projection = self.prepare(9)
        processed = process_sources(self.repo, paths, related_runs=[], model_call=self.model,
            batch_context=projection["batch_context"], source_views=projection["source_views"],
            skill_bundle=resolve_skill_refs("knowledge-extract@8", "knowledge-review@6"))
        self.assertEqual(processed["status"], "passed", processed)
        published = publish(self.repo, processed["run_id"])
        self.assertEqual(parse_markdown((self.repo / published["manifest_path"]).read_bytes())[0]["schema_version"],
                         "0.10")
        before = PipelineGit(self.repo).head(), self.number
        with patch("pdm.evidence.verify_publication") as verify, patch("pdm.pipeline.load_source") as read:
            with self.assertRaises(CaptureError) as raised:
                build_current_evidence(self.repo, paths[:-1], [processed["run_id"]])
            self.assertEqual(raised.exception.code, "evidence_source_mismatch")
            verify.assert_not_called()
            read.assert_not_called()
        view = build_current_evidence(self.repo, paths, [processed["run_id"]])
        self.assertEqual(len(view["publications"]), 1)
        self.assertEqual(len(view["publications"][0]["documents"]), 18)
        self.assertEqual(view["coverage"]["unread_runs"], [])
        self.assertEqual((PipelineGit(self.repo).head(), self.number), before)
