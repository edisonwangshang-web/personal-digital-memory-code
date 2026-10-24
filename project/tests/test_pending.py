"""统一待确认的隔离 Git 回归；不访问真实记忆或云端模型。"""

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_fixtures import configure_test_repository
from pdm.capture import capture
from pdm.contracts import CaptureError, M1_SCHEMA_VERSION, canonical, markdown, parse_markdown
from pdm.controls import record_control
from pdm.pending import (REGISTRY_PATH, answer_pending, list_pending, pending_update,
                         read_registry, register_pending, resolve_pending)
from pdm.pipeline import process, process_sources, publish
from pdm.transaction import PipelineGit


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout.decode().strip()


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-b", "main")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Isolated Test")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "isolated baseline")
        self.source = self.capture("question", "用户：下个月开始，日期需要确认。\n")
        self.item = {"question": "具体从哪一天开始？", "source_refs": [self.ref(self.source)],
                     "run_ids": ["run-" + "1" * 32]}
        self.invocations = 0

    def capture(self, key, body="用户：定为下个月第一天。\n", *, link=False):
        return capture(self.repo, {"source_key": "fixture:" + key, "source_type": "note",
            "platform": "isolated", "original_url": "https://example.invalid/" + key,
            "acquisition_status": "link_only" if link else "full_text", "original_time": None,
            "body": "" if link else body})

    @staticmethod
    def ref(source):
        return {key: source[key] for key in ("source_id", "source_version", "path")}

    def assert_error(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        if code is not None:
            self.assertEqual(caught.exception.code, code)

    def commit(self, path, data):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        git(self.repo, "add", path)
        git(self.repo, "commit", "-m", "isolated persisted change")

    def model(self, role, payload):
        self.invocations += 1
        if role == "extract":
            source = payload["sources"][0]
            ref = {"source_id": source["metadata"]["source_id"], "source_version": source["metadata"]["source_version"],
                   "locator": "L1-L1", "quote": source["body"].splitlines()[0]}
            output = {"objects": [{"id": "clarified-date", "type": "knowledge", "title": "日期补充",
                "source_kind": "user_statement", "sources": [ref], "domains": [], "valid_from": None, "valid_to": None,
                "verification_status": "unverified", "lifecycle_status": "undetermined", "body": "保存实际答复，未推断执行。"}],
                "wiki": {"id": "clarified-topic", "title": "隔离主题", "body": "答复有来源。", "sources": [ref]}}
        else:
            output = {"verdict": "passed", "reasons": ["保留原始表达和未执行状态。"]}
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.invocations:032x}"}

    def fake_manifest(self, run, sources):
        value = {"run_id": run, "source": self.ref(sources[0])}
        if len(sources) > 1:
            value["additional_sources"] = [self.ref(source) for source in sources[1:]]
        path = f"memory/runs/processes/{run}/manifest.md"
        self.commit(path, markdown({"schema_version": M1_SCHEMA_VERSION, "kind": "manifest", "run_id": run},
                                   canonical(value).decode() + "\n"))
        return path

    def answered(self):
        registered = register_pending(self.repo, self.item)
        reply = self.capture("reply")
        answered = answer_pending(self.repo, registered["item_id"], reply["path"])
        return registered, reply, answered

    def test_missing_registry_is_empty_and_read_only(self):
        before = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(list_pending(self.repo), [])
        self.assertEqual(read_registry(self.repo), {"items": []})
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertFalse((self.repo / REGISTRY_PATH).exists())

    def test_registration_is_stable_across_retries_and_origination_runs(self):
        first = register_pending(self.repo, self.item)
        second = register_pending(self.repo, copy.deepcopy(self.item), first["head"])
        self.assertEqual(first["item_id"], second["item_id"])
        self.assertEqual(second["head"], first["head"])
        self.assertFalse(second["changed"])
        repeated = {**self.item, "run_ids": ["run-" + "2" * 32]}
        third = register_pending(self.repo, repeated)
        self.assertEqual(third["item_id"], first["item_id"])
        self.assertTrue(third["changed"])
        items = list_pending(self.repo, "open")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["run_ids"], ["run-" + "1" * 32, "run-" + "2" * 32])
        self.assertEqual(items[0]["reply_sources"], [])
        self.assertIsNone(items[0]["resolution"])
        self.assertEqual(list_pending(self.repo, "resolved"), [])

    def test_explicit_key_conflict_preserves_the_original(self):
        register_pending(self.repo, {**self.item, "key": "clarify:start"})
        head = git(self.repo, "rev-parse", "HEAD")
        before = (self.repo / REGISTRY_PATH).read_bytes()
        self.assert_error("pending_key_conflict", lambda: register_pending(
            self.repo, {**self.item, "key": "clarify:start", "question": "是否已完成？"}))
        self.assertEqual((self.repo / REGISTRY_PATH).read_bytes(), before)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_source_identity_mismatch_is_rejected_before_reading(self):
        invalid = copy.deepcopy(self.item)
        invalid["source_refs"][0]["source_version"] = "sha256-" + "0" * 64
        with patch("pdm.pipeline.load_source") as read:
            self.assert_error("pending_source_mismatch", lambda: register_pending(self.repo, invalid))
            read.assert_not_called()

    def test_pure_helper_builds_one_cas_without_creating_a_commit(self):
        head = git(self.repo, "rev-parse", "HEAD")
        files, expected, saved = pending_update(self.repo, self.item, head=head)
        self.assertEqual(set(files), {REGISTRY_PATH})
        self.assertEqual(expected, {REGISTRY_PATH: None})
        self.assertEqual(saved["pending_status"], "open")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertFalse((self.repo / REGISTRY_PATH).exists())
        adapter = PipelineGit(self.repo)
        with adapter.lock():
            committed = adapter.write_changes(files, expected, head, "isolated integrated review and pending")
        self.assertEqual(git(self.repo, "rev-parse", committed + "^"), head)
        self.assertEqual(list_pending(self.repo)[0], saved)

    def test_confirmation_review_precedes_recoverable_registry_registration(self):
        def needs_confirmation(role, payload):
            result = self.model(role, payload)
            if role == "review":
                result["output"] = {"verdict": "needs_confirmation", "reasons": [self.item["question"]]}
            return result
        first = process_sources(self.repo, [self.source["path"]], model_call=needs_confirmation, related_runs=[])
        self.assertEqual(first["status"], "needs_confirmation")
        item = list_pending(self.repo)[0]
        self.assertEqual(item["run_ids"], [first["run_id"]])
        self.assertEqual(item["pending_status"], "open")
        self.assertEqual(len(item["object_refs"]), 2)
        self.assertFalse((self.repo / "memory/records/clarified-date.md").exists())
        review_head = git(self.repo, "log", "-1", "--format=%H", "--", first["review_path"])
        stage_head = git(self.repo, "log", "-1", "--format=%H", "--", first["stage_path"])
        self.assertEqual(git(self.repo, "rev-parse", review_head + "^"), stage_head)
        registry_head = git(self.repo, "log", "-1", "--format=%H", "--", REGISTRY_PATH)
        self.assertEqual(git(self.repo, "rev-parse", registry_head + "^"), review_head)
        self.assertEqual(first["registration_status"], "registered")
        second = process_sources(self.repo, [self.source["path"]], model_call=needs_confirmation, related_runs=[])
        repeated = list_pending(self.repo)
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["item_id"], item["item_id"])
        self.assertEqual(repeated[0]["run_ids"], sorted([first["run_id"], second["run_id"]]))

    def test_unbound_candidate_reference_is_rejected(self):
        invalid = {**self.item, "object_refs": [{"id": "invented", "content_version": "sha256-" + "0" * 64,
                                                  "path": "memory/records/invented.md"}]}
        self.assert_error("pending_object_mismatch", lambda: register_pending(self.repo, invalid))

    def test_original_scope_can_include_two_explicit_versions(self):
        updated = self.capture("question", "用户：更正了提问的内容。\n")
        result = register_pending(self.repo, {**self.item, "source_refs": [self.ref(self.source), self.ref(updated)]})
        self.assertEqual(result["pending_status"], "open")
        self.assertEqual(len(list_pending(self.repo)[0]["source_refs"]), 2)

    def test_captured_reply_is_answered_and_retry_does_not_resolve(self):
        registered, reply, answered = self.answered()
        self.assertEqual(answered["pending_status"], "answered")
        item = list_pending(self.repo, "answered")[0]
        self.assertIsNone(item["resolution"])
        self.assertEqual(item["reply_sources"][0]["path"], reply["path"])
        self.assertEqual(item["reply_sources"][0]["commit_sha"], reply["commit_sha"])
        self.assertEqual(item["reply_sources"][0]["associated_head"], reply["head"])
        repeated = answer_pending(self.repo, registered["item_id"], reply["path"])
        self.assertFalse(repeated["changed"])
        self.assertEqual(repeated["head"], answered["head"])

    def test_link_only_and_uncaptured_replies_are_rejected(self):
        registered = register_pending(self.repo, self.item)
        link = self.capture("link", link=True)
        self.assert_error("pending_reply_missing_text", lambda: answer_pending(self.repo, registered["item_id"], link["path"]))
        self.assert_error("path_rejected", lambda: answer_pending(self.repo, registered["item_id"], "memory/profile.md"))
        absent = "memory/sources/inbox/src-" + "f" * 64 + "/sha256-" + "f" * 64 + ".md"
        self.assert_error(None, lambda: answer_pending(self.repo, registered["item_id"], absent))
        self.assertEqual(list_pending(self.repo)[0]["pending_status"], "open")

    def test_conflicting_reply_version_is_not_silently_replaced(self):
        registered, _, _ = self.answered()
        revised = self.capture("reply", "用户：答复有了另一版本。\n")
        self.assert_error("pending_reply_conflict", lambda: answer_pending(self.repo, registered["item_id"], revised["path"]))

    def test_original_question_source_is_not_new_reply_evidence(self):
        registered = register_pending(self.repo, self.item)
        with patch("pdm.pipeline.load_source") as read:
            self.assert_error("pending_reply_not_new", lambda: answer_pending(self.repo, registered["item_id"], self.source["path"]))
            read.assert_not_called()

    def test_deleted_question_and_reply_are_hidden_and_actions_stop_before_body_reads(self):
        for target in ("question", "reply"):
            with self.subTest(target=target):
                # Each control affects only this subcase's independent source IDs.
                source = self.capture("delete-question-" + target, "用户：这一事项需要澄清。\n")
                item = {"question": "删除后不再展示的问题", "source_refs": [self.ref(source)]}
                registered = register_pending(self.repo, item)
                reply = self.capture("delete-reply-" + target)
                answer_pending(self.repo, registered["item_id"], reply["path"])
                controlled = source if target == "question" else reply
                record_control(self.repo, {"request_key": "fixture:delete-" + target, "action": "delete",
                                           "source_paths": [controlled["path"]]})
                self.assertNotIn(registered["item_id"], [saved["item_id"] for saved in list_pending(self.repo)])
                with patch("pdm.pipeline.load_source") as read, patch("pdm.pipeline.verify_publication") as verify:
                    self.assert_error("pending_scope_deleted", lambda: answer_pending(self.repo, registered["item_id"], reply["path"]))
                    self.assert_error("pending_scope_deleted", lambda: resolve_pending(self.repo, registered["item_id"], "run-" + "3" * 32))
                    read.assert_not_called()
                    verify.assert_not_called()

    def test_corrected_question_can_be_resolved_from_current_reply(self):
        registered, reply, _ = self.answered()
        record_control(self.repo, {"request_key": "fixture:correct-original", "action": "corrects",
            "source_paths": [self.source["path"]], "replacement_source_paths": [reply["path"]],
            "basis_source_path": reply["path"]})
        self.assertEqual(len(list_pending(self.repo)), 1)
        processed = process(self.repo, reply["path"], model_call=self.model)
        publish(self.repo, processed["run_id"], processed["head"])
        result = resolve_pending(self.repo, registered["item_id"], processed["run_id"])
        self.assertEqual(result["pending_status"], "resolved")

    def test_corrected_reply_is_rejected_before_publication_or_source_body_reads(self):
        registered, reply, _ = self.answered()
        corrected = self.capture("reply-correction", "用户：更正先前的答复。\n")
        record_control(self.repo, {"request_key": "fixture:correct-reply", "action": "corrects",
            "source_paths": [reply["path"]], "replacement_source_paths": [corrected["path"]],
            "basis_source_path": corrected["path"]})
        with patch("pdm.pipeline.verify_publication") as verify, patch("pdm.pipeline.load_source") as read:
            self.assert_error("pending_reply_excluded", lambda: resolve_pending(self.repo, registered["item_id"], "run-" + "3" * 32))
            verify.assert_not_called()
            read.assert_not_called()

    def test_explicit_replacement_reply_continues_the_same_item_without_old_body_read(self):
        from pdm.pipeline import load_source
        registered, reply, answered = self.answered()
        corrected = self.capture("reply-correction", "用户：更正先前的答复。\n")
        record_control(self.repo, {"request_key": "fixture:correct-reply", "action": "corrects",
            "source_paths": [reply["path"]], "replacement_source_paths": [corrected["path"]],
            "basis_source_path": corrected["path"]})
        with patch("pdm.pipeline.load_source", wraps=load_source) as read:
            result = answer_pending(self.repo, registered["item_id"], corrected["path"])
            self.assertEqual([call.args[1] for call in read.call_args_list], [corrected["path"]])
        self.assertEqual(result["item_id"], registered["item_id"])
        item = list_pending(self.repo)[0]
        self.assertEqual(item["pending_status"], "answered")
        self.assertEqual([ref["path"] for ref in item["reply_sources"]], [corrected["path"]])
        # Prior associations stay available in the actual Git history.
        _, previous = parse_markdown(PipelineGit(self.repo).read(answered["head"], REGISTRY_PATH))
        self.assertEqual(json.loads(previous)["items"][0]["reply_sources"][0]["path"], reply["path"])
        processed = process(self.repo, corrected["path"], model_call=self.model)
        publish(self.repo, processed["run_id"], processed["head"])
        self.assertEqual(resolve_pending(self.repo, registered["item_id"], processed["run_id"])["pending_status"], "resolved")

    def test_unrelated_reply_cannot_replace_a_controlled_reply(self):
        registered, reply, _ = self.answered()
        corrected = self.capture("reply-correction", "用户：更正先前的答复。\n")
        unrelated = self.capture("unrelated-reply", "用户：与更正无关的回答。\n")
        record_control(self.repo, {"request_key": "fixture:correct-reply", "action": "corrects",
            "source_paths": [reply["path"]], "replacement_source_paths": [corrected["path"]],
            "basis_source_path": corrected["path"]})
        before = (self.repo / REGISTRY_PATH).read_bytes()
        with patch("pdm.pipeline.load_source") as read:
            self.assert_error("pending_reply_excluded", lambda: answer_pending(self.repo, registered["item_id"], unrelated["path"]))
            read.assert_not_called()
        self.assertEqual((self.repo / REGISTRY_PATH).read_bytes(), before)

    def test_previously_associated_replacement_gets_a_fresh_anchor(self):
        registered, reply, _ = self.answered()
        corrected = self.capture("reply-correction", "用户：更正先前的答复。\n")
        answer_pending(self.repo, registered["item_id"], corrected["path"])
        controlled = record_control(self.repo, {"request_key": "fixture:correct-reply", "action": "corrects",
            "source_paths": [reply["path"]], "replacement_source_paths": [corrected["path"]],
            "basis_source_path": corrected["path"]})
        result = answer_pending(self.repo, registered["item_id"], corrected["path"])
        self.assertTrue(result["changed"])
        item = list_pending(self.repo)[0]
        self.assertEqual(len(item["reply_sources"]), 1)
        self.assertEqual(item["reply_sources"][0]["associated_head"], controlled["head"])

    def test_unanswered_item_cannot_be_resolved(self):
        registered = register_pending(self.repo, self.item)
        with patch("pdm.pipeline.verify_publication") as verify:
            self.assert_error("pending_status_conflict", lambda: resolve_pending(self.repo, registered["item_id"], "run-" + "3" * 32))
            verify.assert_not_called()

    def test_resolution_scope_rejects_unassociated_sources_before_verification(self):
        registered, reply, _ = self.answered()
        unrelated = self.capture("unrelated", "不得读取这份无关来源正文。\n")
        run = "run-" + "3" * 32
        self.fake_manifest(run, [reply, unrelated])
        with patch("pdm.pipeline.verify_publication") as verify, patch("pdm.pipeline.load_source") as read:
            self.assert_error("pending_publication_mismatch", lambda: resolve_pending(self.repo, registered["item_id"], run))
            verify.assert_not_called()
            read.assert_not_called()

    def test_publication_without_the_reply_is_rejected_before_verification(self):
        registered, _, _ = self.answered()
        run = "run-" + "3" * 32
        self.fake_manifest(run, [self.source])
        with patch("pdm.pipeline.verify_publication") as verify:
            self.assert_error("pending_publication_mismatch", lambda: resolve_pending(self.repo, registered["item_id"], run))
            verify.assert_not_called()
        self.assertEqual(list_pending(self.repo)[0]["pending_status"], "answered")

    def test_run_context_containing_reply_without_any_formal_reference_cannot_resolve(self):
        registered, reply, _ = self.answered()
        def unused_reply(role, payload):
            if role == "extract":
                original = next(source for source in payload["sources"] if source["path"] == self.source["path"])
                return self.model(role, {**payload, "sources": [original]})
            return self.model(role, payload)
        processed = process_sources(self.repo, [self.source["path"], reply["path"]], model_call=unused_reply, related_runs=[])
        self.assertEqual(processed["status"], "passed")
        publish(self.repo, processed["run_id"], processed["head"])
        before = (self.repo / REGISTRY_PATH).read_bytes()
        head = git(self.repo, "rev-parse", "HEAD")
        self.assert_error("pending_reply_not_applied", lambda: resolve_pending(self.repo, registered["item_id"], processed["run_id"]))
        self.assertEqual((self.repo / REGISTRY_PATH).read_bytes(), before)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(list_pending(self.repo)[0]["pending_status"], "answered")

    def test_wiki_only_reply_reference_does_not_complete_formal_record_processing(self):
        registered, reply, _ = self.answered()
        def wiki_only(role, payload):
            if role == "extract":
                original = next(source for source in payload["sources"] if source["path"] == self.source["path"])
                answer = next(source for source in payload["sources"] if source["path"] == reply["path"])
                result = self.model(role, {**payload, "sources": [original]})
                result["output"]["wiki"]["sources"] = [{"source_id": answer["metadata"]["source_id"],
                    "source_version": answer["metadata"]["source_version"], "locator": "L1-L1",
                    "quote": answer["body"].splitlines()[0]}]
                return result
            return self.model(role, payload)
        processed = process_sources(self.repo, [self.source["path"], reply["path"]], model_call=wiki_only)
        self.assertEqual(processed["status"], "passed")
        publish(self.repo, processed["run_id"], processed["head"])
        self.assert_error("pending_reply_not_applied", lambda: resolve_pending(self.repo, registered["item_id"], processed["run_id"]))
        self.assertEqual(list_pending(self.repo)[0]["pending_status"], "answered")

    def test_every_active_reply_needs_an_actual_formal_record_reference(self):
        registered, reply, _ = self.answered()
        second = self.capture("second-reply", "用户：还有另一条必要的答复。\n")
        answer_pending(self.repo, registered["item_id"], second["path"])
        def first_reply_only(role, payload):
            if role == "extract":
                first = next(source for source in payload["sources"] if source["path"] == reply["path"])
                return self.model(role, {**payload, "sources": [first]})
            return self.model(role, payload)
        processed = process_sources(self.repo, [reply["path"], second["path"]], model_call=first_reply_only)
        publish(self.repo, processed["run_id"], processed["head"])
        self.assert_error("pending_reply_not_applied", lambda: resolve_pending(self.repo, registered["item_id"], processed["run_id"]))
        self.assertEqual(list_pending(self.repo)[0]["pending_status"], "answered")

    def test_all_active_replies_referenced_by_a_formal_record_can_resolve(self):
        registered, reply, _ = self.answered()
        second = self.capture("second-reply", "用户：还有另一条必要的答复。\n")
        answer_pending(self.repo, registered["item_id"], second["path"])
        def all_replies(role, payload):
            result = self.model(role, payload)
            if role == "extract":
                refs = [{"source_id": source["metadata"]["source_id"],
                         "source_version": source["metadata"]["source_version"], "locator": "L1-L1",
                         "quote": source["body"].splitlines()[0]} for source in payload["sources"]]
                result["output"]["objects"][0]["sources"] = refs
            return result
        processed = process_sources(self.repo, [reply["path"], second["path"]], model_call=all_replies)
        publish(self.repo, processed["run_id"], processed["head"])
        result = resolve_pending(self.repo, registered["item_id"], processed["run_id"])
        self.assertEqual(result["pending_status"], "resolved")

    def test_successful_reviewed_reply_publication_resolves_and_retries(self):
        registered, reply, _ = self.answered()
        processed = process(self.repo, reply["path"], model_call=self.model)
        self.assertEqual(processed["status"], "passed")
        published = publish(self.repo, processed["run_id"], processed["head"])
        result = resolve_pending(self.repo, registered["item_id"], processed["run_id"])
        self.assertEqual(result["pending_status"], "resolved")
        item = list_pending(self.repo, "resolved")[0]
        self.assertEqual(item["resolution"], {key: published[key] for key in ("run_id", "commit_sha", "manifest_path")})
        repeated = resolve_pending(self.repo, registered["item_id"], processed["run_id"])
        self.assertFalse(repeated["changed"])
        self.assertEqual(repeated["head"], result["head"])

    def test_older_publication_cannot_complete_a_later_answer(self):
        reply = self.capture("reply")
        processed = process(self.repo, reply["path"], model_call=self.model)
        publish(self.repo, processed["run_id"], processed["head"])
        registered = register_pending(self.repo, self.item)
        answer_pending(self.repo, registered["item_id"], reply["path"])
        self.assert_error("pending_publication_mismatch", lambda: resolve_pending(self.repo, registered["item_id"], processed["run_id"]))
        self.assertEqual(list_pending(self.repo)[0]["pending_status"], "answered")

    def test_hand_edited_reply_anchor_cannot_make_an_old_publication_new(self):
        reply = self.capture("reply")
        processed = process(self.repo, reply["path"], model_call=self.model)
        publish(self.repo, processed["run_id"], processed["head"])
        registered = register_pending(self.repo, self.item)
        answer_pending(self.repo, registered["item_id"], reply["path"])
        metadata, body = parse_markdown((self.repo / REGISTRY_PATH).read_bytes())
        value = json.loads(body)
        value["items"][0]["reply_sources"][0]["associated_head"] = reply["head"]
        self.commit(REGISTRY_PATH, markdown(metadata, canonical(value).decode() + "\n"))
        self.assert_error("pending_publication_mismatch", lambda: resolve_pending(self.repo, registered["item_id"], processed["run_id"]))
        self.assertEqual(list_pending(self.repo)[0]["pending_status"], "answered")

    def test_manifest_alone_without_review_and_real_publication_cannot_resolve(self):
        registered, reply, _ = self.answered()
        run = "run-" + "3" * 32
        self.fake_manifest(run, [reply])
        self.assert_error(None, lambda: resolve_pending(self.repo, registered["item_id"], run))
        self.assertEqual(list_pending(self.repo)[0]["pending_status"], "answered")

    def test_head_and_pending_cas_conflicts_preserve_the_latest_data(self):
        original = register_pending(self.repo, self.item)
        files, expected, _ = pending_update(self.repo, {**self.item, "run_ids": ["run-" + "2" * 32]}, head=original["head"])
        register_pending(self.repo, {**self.item, "run_ids": ["run-" + "3" * 32]})
        current = (self.repo / REGISTRY_PATH).read_bytes()
        self.assert_error("head_conflict", lambda: register_pending(self.repo, self.item, original["head"]))
        adapter = PipelineGit(self.repo)
        with adapter.lock():
            self.assert_error("head_conflict", lambda: adapter.write_changes(files, expected, original["head"], "stale change"))
        self.assertEqual((self.repo / REGISTRY_PATH).read_bytes(), current)

    def test_dirty_registry_and_other_worktree_changes_are_rejected(self):
        registered = register_pending(self.repo, self.item)
        path = self.repo / REGISTRY_PATH
        path.write_bytes(path.read_bytes() + b" ")
        self.assert_error("dirty_worktree", lambda: list_pending(self.repo))
        self.assert_error("saved_bytes_changed", lambda: read_registry(self.repo))
        self.assert_error("dirty_worktree", lambda: register_pending(self.repo, self.item, registered["head"]))

    def test_duplicate_keys_unknown_fields_and_duplicate_items_are_rejected(self):
        register_pending(self.repo, self.item)
        original = (self.repo / REGISTRY_PATH).read_bytes()
        metadata, body = parse_markdown(original)
        registry = json.loads(body)
        malformed = [
            markdown(metadata, '{"items":[],"items":[]}\n'),
            markdown(metadata, canonical({**registry, "unknown": True}).decode()),
            markdown(metadata, canonical({"items": [registry["items"][0], registry["items"][0]]}).decode()),
            markdown({**metadata, "kind": "other"}, body),
            b'---\n{"schema_version":"0.4","kind":"pending_registry","kind":"pending_registry"}\n---\n' + body.encode(),
        ]
        for data in malformed:
            with self.subTest(data=data[:100]):
                self.commit(REGISTRY_PATH, data)
                self.assert_error(None, lambda: list_pending(self.repo))

    def test_unrecognized_schema_cannot_be_used_as_a_registry(self):
        self.commit(REGISTRY_PATH, markdown({"schema_version": "future", "kind": "pending_registry"}, '{"items":[]}\n'))
        self.assert_error("unknown_memory_schema", lambda: list_pending(self.repo))
        self.assert_error("invalid_pending_data", lambda: read_registry(self.repo))

    def test_registry_symlinks_and_deleted_registry_are_rejected(self):
        registered = register_pending(self.repo, self.item)
        target = self.repo / REGISTRY_PATH
        target.unlink()
        git(self.repo, "add", REGISTRY_PATH)
        git(self.repo, "commit", "-m", "isolated remove registry")
        self.assert_error("pending_registry_deleted", lambda: list_pending(self.repo))
        outside = self.repo / "outside.md"
        outside.write_text("private external text", encoding="utf-8")
        target.symlink_to(outside)
        self.assert_error("path_rejected", lambda: read_registry(self.repo))
        self.assertNotEqual(git(self.repo, "rev-parse", "HEAD"), registered["head"])

    def test_input_unknown_fields_and_unrecognized_status_are_rejected(self):
        self.assert_error("invalid_pending_data", lambda: register_pending(self.repo, {**self.item, "pending_status": "resolved"}))
        self.assert_error("invalid_pending_status", lambda: list_pending(self.repo, "done"))


if __name__ == "__main__":
    unittest.main()
