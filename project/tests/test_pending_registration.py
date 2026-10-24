"""Expanded pending contracts and zero-call registration after durable Review."""

from copy import deepcopy
import hashlib
from unittest.mock import patch
import unittest

import test_pending
from pdm.batching import plan_batches, project_batch_sources
from pdm.contracts import CaptureError, canonical, markdown, parse_markdown
from pdm.pending import (REGISTRY_PATH, _encode, _parse, read_registry, register_pending)
from pdm.pipeline import (_related_run_ids, process_sources, resume_pending_registration)
from pdm.transaction import PipelineGit


class PendingRegistrationTests(unittest.TestCase):
    capture = test_pending.PendingTests.capture
    ref = staticmethod(test_pending.PendingTests.ref)
    commit = test_pending.PendingTests.commit
    assert_error = test_pending.PendingTests.assert_error

    def setUp(self):
        test_pending.PendingTests.setUp(self)
        self.roles = []
        self.prefix = "confirmation"
        self.object_count = 1

    def model(self, role, payload):
        self.invocations += 1
        self.roles.append(role)
        if role == "review":
            output = {"verdict": getattr(self, "verdict", "needs_confirmation"), "reasons": ["人工资料的开始日需明确。"]}
        else:
            source = payload["sources"][0]
            line = (source["fragments"][0]["lines"][0] if "fragments" in source
                    else {"line": 1, "text": source["body"].splitlines()[0]})
            ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                   "locator": f"L{line['line']}-L{line['line']}", "quote": line["text"]}
            objects = [{"id": f"{self.prefix}-record-{number}", "type": "knowledge", "title": "人工未定计划",
                        "source_kind": "user_statement", "sources": [ref], "domains": ["learning"],
                        "valid_from": None, "valid_to": None, "verification_status": "unverified",
                        "lifecycle_status": "undetermined", "body": "人工计划，日期尚待确认。"}
                       for number in range(self.object_count)]
            if "fragments" in source:
                wikis = [{"id": f"{self.prefix}-topic-{number}", "title": "人工主题", "body": "人工未知日期。",
                          "sources": [ref], "record_ids": [record["id"]]} for number, record in enumerate(objects)]
                output = {"objects": objects, "wikis": wikis}
            else:
                output = {"objects": objects, "wiki": {"id": f"{self.prefix}-topic", "title": "人工主题",
                          "sources": [ref], "body": "人工未知日期。"}}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.invocations:032x}"}

    def process(self, paths=None):
        return process_sources(self.repo, paths or [self.source["path"]], related_runs=[], model_call=self.model)

    def batch(self, paths):
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        projection = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=paths)
        return process_sources(self.repo, paths, related_runs=[], model_call=self.model,
                               source_views=projection["source_views"], batch_context=projection["batch_context"],
                               draft_only=len(plan["batches"]) > 1)

    def pending_failure(self):
        def limited_registry_encoding(*args, **kwargs):
            # Restrict only the registry serializer, after candidate reference
            # validation; do not accidentally restrict unrelated run documents.
            with patch("pdm.pending.MAX_REGISTRY_BYTES", 128):
                return _encode(*args, **kwargs)
        with patch("pdm.pending._encode", side_effect=limited_registry_encoding):
            result = self.process()
        self.assertEqual(result["status"], "registration_required", result)
        self.assertEqual(result["registration_error"], "pending_registry_too_large")
        self.assertEqual(result["review_verdict"], "needs_confirmation")
        self.assertEqual(self.roles, ["extract", "review"])
        return result

    def test_33_sources_register_expanded_registry_without_losing_review(self):
        paths = [self.source["path"], *[self.capture("large-" + str(number))["path"] for number in range(32)]]
        before = {path: (self.repo / path).read_bytes() for path in paths}
        result = self.batch(paths)
        self.assertEqual(result["status"], "needs_confirmation", result)
        self.assertEqual(result["registration_status"], "registered")
        item = read_registry(self.repo)["items"][0]
        self.assertEqual(len(item["source_refs"]), 33)
        self.assertEqual(parse_markdown((self.repo / REGISTRY_PATH).read_bytes())[0]["schema_version"], "0.7")
        self.assertTrue((self.repo / result["review_path"]).is_file())
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual({path: (self.repo / path).read_bytes() for path in paths}, before)

    def test_more_than_64_objects_choose_expanded_schema_and_preserve_old_items(self):
        self.object_count = 20
        self.prefix = "first"
        first = self.batch([self.source["path"]])
        self.prefix = "second"
        second = self.batch([self.source["path"]])
        old = read_registry(self.repo)["items"]
        self.assertEqual([len(item["object_refs"]) for item in old], [40, 40])
        self.assertEqual(parse_markdown((self.repo / REGISTRY_PATH).read_bytes())[0]["schema_version"], "0.4")
        register_pending(self.repo, {"key": "artificial:combined-80", "question": "这80个人工对象的日期如何解释？",
                         "source_refs": [self.ref(self.source)], "object_refs": sorted(
                             [ref for item in old for ref in item["object_refs"]], key=lambda ref: ref["path"]),
                         "run_ids": sorted([first["run_id"], second["run_id"]])})
        now = read_registry(self.repo)["items"]
        self.assertEqual(len(next(item for item in now if item["key"] == "artificial:combined-80")["object_refs"]), 80)
        self.assertEqual([item for item in now if item["key"] != "artificial:combined-80"], old)
        self.assertEqual(parse_markdown((self.repo / REGISTRY_PATH).read_bytes())[0]["schema_version"], "0.7")
        register_pending(self.repo, {"key": "artificial:new-small", "question": "一条小问题？", "source_refs": [self.ref(self.source)]})
        self.assertEqual(parse_markdown((self.repo / REGISTRY_PATH).read_bytes())[0]["schema_version"], "0.7")

    def test_registry_capacity_failure_keeps_review_and_restarts_registration_with_zero_calls(self):
        result = self.pending_failure()
        review = (self.repo / result["review_path"]).read_bytes()
        marker = parse_markdown((self.repo / result["registration_path"]).read_bytes())
        self.assertEqual(marker[0]["schema_version"], "0.7")
        self.assertIn('"status":"registration_required"', marker[1])
        self.assertEqual(read_registry(self.repo)["items"], [])
        calls = self.invocations
        resumed = resume_pending_registration(self.repo, result["run_id"], allowed_source_paths=[self.source["path"]])
        self.assertEqual(resumed["status"], "needs_confirmation", resumed)
        self.assertEqual(resumed["model_calls"], 0)
        self.assertIsNone(resumed["registration_error"])
        self.assertEqual(self.invocations, calls)
        self.assertEqual((self.repo / result["review_path"]).read_bytes(), review)
        head = PipelineGit(self.repo).head()
        repeated = resume_pending_registration(self.repo, result["run_id"], allowed_source_paths=[self.source["path"]])
        self.assertEqual(repeated["head"], head)
        self.assertEqual(len(read_registry(self.repo)["items"]), 1)

    def test_registry_write_io_failure_preserves_known_review(self):
        original = PipelineGit.write_changes
        def fail_registry(adapter, files, expected, head, message):
            if REGISTRY_PATH in files:
                raise OSError("artificial registry filesystem failure")
            return original(adapter, files, expected, head, message)
        with patch.object(PipelineGit, "write_changes", fail_registry):
            result = self.process()
        self.assertEqual(result["status"], "registration_required", result)
        self.assertEqual(result["registration_error"], "pending_registration_io")
        self.assertTrue((self.repo / result["review_path"]).is_file())
        self.assertEqual(self.roles, ["extract", "review"])
        resumed = resume_pending_registration(self.repo, result["run_id"], allowed_source_paths=[self.source["path"]])
        self.assertEqual(resumed["status"], "needs_confirmation")
        self.assertEqual(self.roles, ["extract", "review"])

    def test_registry_cas_conflict_never_overwrites_external_head(self):
        original = PipelineGit.write_changes
        external = []
        def conflict(adapter, files, expected, head, message):
            if REGISTRY_PATH in files:
                test_pending.git(self.repo, "commit", "--allow-empty", "-m", "artificial external change")
                external.append(PipelineGit(self.repo).head())
                raise CaptureError("head_changed", "artificial CAS conflict")
            return original(adapter, files, expected, head, message)
        with patch.object(PipelineGit, "write_changes", conflict):
            result = self.process()
        self.assertEqual(result["status"], "registration_required", result)
        self.assertEqual(result["registration_error"], "head_changed")
        self.assertEqual(PipelineGit(self.repo).head(), external[0])
        resumed = resume_pending_registration(self.repo, result["run_id"], allowed_source_paths=[self.source["path"]])
        self.assertEqual(resumed["status"], "needs_confirmation")
        self.assertEqual(test_pending.git(self.repo, "rev-parse", resumed["head"] + "^"), external[0])

    def test_recovery_scope_rejected_before_source_body_read(self):
        result = self.pending_failure()
        unrelated = self.capture("unrelated-recovery")
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("unauthorized body read")):
            self.assert_error("pending_registration_scope", lambda: resume_pending_registration(
                self.repo, result["run_id"], allowed_source_paths=[unrelated["path"]]))
        self.assertEqual(self.roles, ["extract", "review"])

    def test_changed_source_and_changed_review_are_not_recovered_as_original(self):
        result = self.pending_failure()
        self.commit(self.source["path"], (self.repo / self.source["path"]).read_bytes() + b"altered synthetic text\n")
        with self.assertRaises(CaptureError):
            resume_pending_registration(self.repo, result["run_id"], allowed_source_paths=[self.source["path"]])
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(read_registry(self.repo)["items"], [])

    def test_forged_review_is_rejected_before_registration(self):
        result = self.pending_failure()
        meta, body = parse_markdown((self.repo / result["review_path"]).read_bytes())
        import json
        review = json.loads(body)
        review["output"]["reasons"] = ["forged artificial question"]
        self.commit(result["review_path"], markdown(meta, canonical(review).decode() + "\n"))
        self.assert_error("pending_registration_changed", lambda: resume_pending_registration(
            self.repo, result["run_id"], allowed_source_paths=[self.source["path"]]))
        self.assertEqual(read_registry(self.repo)["items"], [])

    def test_legacy_caps_stay_strict_and_new_caps_are_bounded(self):
        registered = register_pending(self.repo, self.item)
        original = read_registry(self.repo)["items"][0]
        def source(number):
            digest = hashlib.sha256(str(number).encode()).hexdigest()
            return {"source_id": "src-" + digest, "source_version": "sha256-" + digest,
                    "path": f"memory/sources/inbox/src-{digest}/sha256-{digest}.md"}
        big = deepcopy(original)
        big["source_refs"] = sorted([source(i) for i in range(256)], key=lambda ref: ref["path"])
        big["object_refs"] = sorted([{"id": "object-" + str(i), "path": f"memory/records/object-{i}.md",
                                    "content_version": "sha256-" + "0" * 64} for i in range(256)], key=lambda ref: ref["path"])
        encoded = _encode([big])
        self.assertEqual(len(_parse(encoded)["items"][0]["source_refs"]), 256)
        self.assertEqual(len(_parse(encoded)["items"][0]["object_refs"]), 256)
        _, body = parse_markdown(encoded)
        legacy = markdown({"schema_version": "0.4", "kind": "pending_registry"}, body)
        self.assert_error("invalid_pending_data", lambda: _parse(legacy))
        too_many = deepcopy(big)
        too_many["source_refs"].append(source(256))
        self.assert_error("invalid_pending_data", lambda: _encode([too_many]))
        too_many = deepcopy(big)
        too_many["object_refs"].append({"id": "overflow", "path": "memory/records/overflow.md", "content_version": "sha256-" + "0" * 64})
        self.assert_error("invalid_pending_data", lambda: _encode([too_many]))
        # Sticky schema is explicit even when a subsequent valid registry has
        # only small items; no automatic 0.7 to 0.4 downgrade occurs.
        self.assertEqual(parse_markdown(_encode([original], previous=encoded))[0]["schema_version"], "0.7")
        self.assertEqual(read_registry(self.repo)["items"][0], original)
        self.assertEqual(PipelineGit(self.repo).head(), registered["head"])

    def test_related_run_expansion_is_explicit_and_legacy_default_stays_eight(self):
        runs = ["run-" + f"{number:032x}" for number in range(9)]
        self.assert_error("invalid_related_runs", lambda: _related_run_ids(runs))
        self.assertEqual(_related_run_ids(runs, max_runs=256), sorted(runs))
        self.assert_error("invalid_related_runs", lambda: _related_run_ids(runs, max_runs=True))


class ContinuationContextTests(unittest.TestCase):
    setUp = PendingRegistrationTests.setUp
    capture = test_pending.PendingTests.capture
    ref = staticmethod(test_pending.PendingTests.ref)
    assert_error = test_pending.PendingTests.assert_error

    def model(self, role, payload):
        self.invocations += 1
        self.roles.append(role)
        self.payloads.append(deepcopy(payload))
        if role == "review":
            output = {"verdict": self.verdict, "reasons": ["是否计划从2030年1月1日开始？"]}
        else:
            refs = []
            for source in payload["sources"]:
                line = (source["fragments"][0]["lines"][0] if "fragments" in source
                        else {"line": 1, "text": source["body"].splitlines()[0]})
                refs.append({"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                             "locator": f"L{line['line']}-L{line['line']}", "quote": line["text"]})
            record = {"id": "context-clarified", "type": "knowledge", "title": "人工开始日",
                      "source_kind": "user_statement", "sources": refs, "domains": ["learning"],
                      "valid_from": None, "valid_to": None, "verification_status": "unverified",
                      "lifecycle_status": "undetermined", "body": "原问题和实际答复共同解释人工计划，未推断执行。"}
            wiki = {"id": "context-clarified-topic", "title": "人工计划", "sources": refs, "body": "人工计划答复。"}
            if "fragments" in payload["sources"][0]:
                wiki["record_ids"] = [record["id"]]
                output = {"objects": [record], "wikis": [wiki]}
            else:
                output = {"objects": [record], "wiki": wiki}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.invocations:032x}"}

    def answered(self):
        from pdm.context import plan_answered_pending
        from pdm.pending import answer_pending
        self.payloads = []
        self.verdict = "needs_confirmation"
        original = process_sources(self.repo, [self.source["path"]], related_runs=[], model_call=self.model)
        self.assertEqual(original["status"], "needs_confirmation", original)
        item = read_registry(self.repo)["items"][0]
        reply = self.capture("ambiguous-yes", "用户：对。\n")
        answer_pending(self.repo, item["item_id"], reply["path"])
        paths = [self.source["path"], reply["path"]]
        job = plan_answered_pending(self.repo, allowed_source_paths=paths, allowed_run_ids=[original["run_id"]])["jobs"][0]
        context = {key: job[key] for key in ("job_id", "parent_item_id", "question", "parent_run_ids",
                                            "source_refs", "object_refs", "reply_sources")}
        plan = plan_batches(self.repo, paths, max_input_bytes=100_000)
        projection = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=paths)
        self.verdict = "passed"
        return item, paths, projection, context

    def test_original_question_is_bound_to_both_calls_and_history_survives_resolution(self):
        from pdm.pending import resolve_pending
        from pdm.pipeline import _committed, _load, publish, verify_publication
        item, paths, projection, context = self.answered()
        calls = self.invocations
        result = process_sources(self.repo, paths, related_runs=[], model_call=self.model,
            source_views=projection["source_views"], batch_context=projection["batch_context"], continuation_context=context)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(self.invocations - calls, 2)
        for payload in self.payloads[-2:]:
            self.assertEqual(payload["continuation_context"], context)
        stage = _committed(PipelineGit(self.repo), result["stage_path"])
        self.assertEqual(parse_markdown(stage)[0]["schema_version"], "0.7")
        candidate = _load(stage, "candidate")
        self.assertEqual(candidate["continuation_context"], context)
        publication = publish(self.repo, result["run_id"])
        resolved = resolve_pending(self.repo, item["item_id"], publication["run_id"])
        self.assertEqual(resolved["pending_status"], "resolved")
        self.assertEqual(verify_publication(self.repo, result["run_id"], source_paths=paths)["run_id"], result["run_id"])
        self.assertEqual(self.invocations - calls, 2)

    def test_forged_question_even_with_recomputed_job_hash_is_rejected_before_model(self):
        _, paths, projection, context = self.answered()
        context["question"] = "调用者伪造的新问题，不得替换原问题。"
        identity = {key: value for key, value in context.items() if key != "job_id"}
        context["job_id"] = "continuation-" + hashlib.sha256(canonical(identity)).hexdigest()
        calls = self.invocations
        self.assert_error("continuation_context_changed", lambda: process_sources(self.repo, paths, related_runs=[], model_call=self.model,
            source_views=projection["source_views"], batch_context=projection["batch_context"], continuation_context=context))
        self.assertEqual(self.invocations, calls)

    def test_context_scope_is_checked_before_source_reads_and_legacy_cannot_opt_in(self):
        _, paths, projection, context = self.answered()
        calls = self.invocations
        with patch("pdm.pipeline.load_source", side_effect=AssertionError("scope precedes body read")):
            self.assert_error("continuation_source_scope", lambda: process_sources(self.repo, [paths[0]], related_runs=[], model_call=self.model,
                source_views=projection["source_views"], batch_context=projection["batch_context"], continuation_context=context))
        self.assert_error("invalid_continuation_context", lambda: process_sources(self.repo, paths, related_runs=[], model_call=self.model,
            continuation_context=context))
        self.assertEqual(self.invocations, calls)

