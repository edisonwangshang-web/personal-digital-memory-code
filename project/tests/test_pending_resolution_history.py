"""A past resolution survives valid later updates, without bypassing its proof."""

import json
import unittest

import test_context as context_fixtures
import test_pending as pending_fixtures
import test_pending_groups as group_fixtures
from pdm.context import execute_continuation
from pdm.contracts import canonical, markdown, parse_markdown
from pdm.pending import REGISTRY_PATH, read_registry, resolve_pending
from pdm.pipeline import process_sources, publish, verify_publication


class PendingResolutionHistoryTests(unittest.TestCase):
    setUp = context_fixtures.ContextTests.setUp
    capture = context_fixtures.ContextTests.capture
    ref = staticmethod(context_fixtures.ContextTests.ref)
    model = context_fixtures.ContextTests.model
    process = context_fixtures.ContextTests.process
    answered = context_fixtures.ContextTests.answered
    plan = context_fixtures.ContextTests.plan
    snapshot = context_fixtures.ContextTests.snapshot
    assert_error = context_fixtures.ContextTests.assert_error
    commit = pending_fixtures.PendingTests.commit

    def published_reply(self, *, resolve):
        original, item, reply = self.answered()
        if resolve:
            job = self.plan(original, reply)["jobs"][0]
            done = execute_continuation(self.repo, job,
                process_call=lambda repo, work: self.process(work["source_paths"], work["related_runs"]))
            run = done["publication_run_id"]
        else:
            result = self.process([self.source["path"], reply["path"]])
            run = result["run_id"]
            publish(self.repo, run)
        return item, reply, run

    def later_update(self, reply, run):
        fresh = self.capture("later-update", "用户：补充一项新的来源信息。\n")
        # A valid newer version may no longer need the old reply as a source.
        # Historical resolution must inspect the original published version.
        self.use_reply = False
        result = self.process([self.source["path"], reply["path"], fresh["path"]], [run])
        published = publish(self.repo, result["run_id"])
        verify_publication(self.repo, result["run_id"])
        return published

    def test_resolved_retry_after_later_update_uses_original_published_reply_references(self):
        item, reply, run = self.published_reply(resolve=True)
        saved = read_registry(self.repo)["items"][0]
        self.later_update(reply, run)
        before, calls = self.snapshot(), self.invocations
        repeated = resolve_pending(self.repo, item["item_id"], run)
        self.assertEqual(repeated["pending_status"], "resolved")
        self.assertFalse(repeated["changed"])
        self.assertEqual(read_registry(self.repo)["items"][0]["resolution"], saved["resolution"])
        self.assertEqual((self.snapshot(), self.invocations), (before, calls))

    def test_first_resolution_cannot_use_superseded_publication(self):
        item, reply, run = self.published_reply(resolve=False)
        self.later_update(reply, run)
        before, calls = self.snapshot(), self.invocations
        self.assert_error("publication_mismatch", lambda: resolve_pending(self.repo, item["item_id"], run))
        self.assertEqual(read_registry(self.repo)["items"][0]["pending_status"], "answered")
        self.assertEqual((self.snapshot(), self.invocations), (before, calls))

    def test_historical_retry_still_rejects_changed_resolution_anchor(self):
        item, reply, run = self.published_reply(resolve=True)
        newer = self.later_update(reply, run)
        metadata, body = parse_markdown((self.repo / REGISTRY_PATH).read_bytes())
        registry = json.loads(body)
        registry["items"][0]["resolution"]["commit_sha"] = newer["commit_sha"]
        self.commit(REGISTRY_PATH, markdown(metadata, canonical(registry).decode() + "\n"))
        before, calls = self.snapshot(), self.invocations
        self.assert_error("pending_publication_mismatch",
            lambda: resolve_pending(self.repo, item["item_id"], run))
        self.assertEqual((self.snapshot(), self.invocations), (before, calls))


class PendingGroupResolutionHistoryTests(unittest.TestCase):
    setUp = group_fixtures.PendingGroupTests.setUp
    capture = group_fixtures.PendingGroupTests.capture
    ref = staticmethod(group_fixtures.PendingGroupTests.ref)
    model = group_fixtures.PendingGroupTests.model
    origin_and_reply = group_fixtures.PendingGroupTests.origin_and_reply
    publication = group_fixtures.PendingGroupTests.publication
    snapshot = context_fixtures.ContextTests.snapshot

    def test_resolved_group_survives_later_ordinary_publication_without_old_reply_reference(self):
        paths = self.origin_and_reply()
        old = self.publication(paths)
        resolve_pending(self.repo, self.item["item_id"], old["run_id"])
        fresh = self.capture("after-group", "用户：后续有一项新的明确补充。\n")
        self.apply_reply = False
        changed = process_sources(self.repo, [*paths, fresh["path"]],
                                  related_runs=[old["run_id"]], model_call=self.model)
        self.assertEqual(changed["status"], "passed", changed)
        publish(self.repo, changed["run_id"])
        verify_publication(self.repo, changed["run_id"])
        before, calls = self.snapshot(), self.invocations
        resolved = resolve_pending(self.repo, self.item["item_id"], old["run_id"])
        self.assertEqual(resolved["pending_status"], "resolved")
        self.assertFalse(resolved["changed"])
        self.assertEqual((self.snapshot(), self.invocations), (before, calls))


if __name__ == "__main__":
    unittest.main()
