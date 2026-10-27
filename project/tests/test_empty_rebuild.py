"""Explicit new rounds after cleanup use artificial Git data and fake models."""

from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_production
from engine_fixtures import advance_engine, isolated_engine
from quality_fixtures import add_quality, passed_review
from test_pipeline import git
from pdm.batch_groups import verify_group_publication
from pdm.capture import capture
from pdm.cli import main
from pdm.contracts import CaptureError, SUPPORTED_SCHEMA_VERSIONS, parse_markdown
from pdm.controls import record_control
from pdm.model_calls import ModelCallError
from pdm.pipeline import _computation_snapshots, _read_reviewed
from pdm.production import prepare_production, run_production
from pdm.rebuild import allow_rebuild_targets, prepare_rebuild, validate_rebuild_context
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class EmptyRebuildTests(unittest.TestCase):
    state = test_production.ProductionTests.state
    attempts = test_production.ProductionTests.attempts

    def setUp(self):
        test_production.ProductionTests.setUp(self)
        git(self.repo, "branch", "-m", "dev")
        self.code = isolated_engine(self)
        self.paths = [capture(self.repo, {
            "source_key": "artificial:empty-rebuild", "source_type": "note",
            "acquisition_status": "full_text",
            "body": "Artificial learning observation for explicit rebuilding.\n",
        })["path"]]
        self.bundle = resolve_skill_refs("knowledge-extract@11", "knowledge-review@9")
        self.mode = "valid"
        self.failed_extractions = 0
        self.prepare_queue("original")
        initial = run_production(self.repo, "original", model_call=self.adapter)
        self.assertEqual(initial["status"], "published", initial)
        self.targets = sorted(initial["publication"]["files"])
        self.formal_bytes = {name: (self.repo / name).read_bytes() for name in self.targets}
        self.parent = prepare_rebuild(self.repo, "artificial-first-cleanup", self.paths,
                                      self.targets)["rebuild_context"]

    def transport(self, role, payload, timeout=120):
        result = test_production.ProductionTests.transport(self, role, payload, timeout)
        if role == "extract":
            result["output"] = add_quality(result["output"], payload["quality_context"])
            result["output"]["reuse_refs"] = []
            if self.mode in {"invalid", "unknown_then_invalid"}:
                self.failed_extractions += 1
                if self.mode == "unknown_then_invalid" and self.failed_extractions == 1:
                    raise ModelCallError("timeout", result["invocation_id"], 1.0)
                result["output"] = {}
        else:
            result["output"] = passed_review(payload["required_checks"],
                                              payload.get("skill_bundle"))
        return result

    def prepare_queue(self, name, context=None):
        return prepare_production(self.repo, name, self.paths, skill_bundle=self.bundle,
                                  review_strategy="auto", rebuild_context=context)

    def new_round(self, intent="artificial-user-new-round", parent=None):
        return prepare_rebuild(self.repo, intent, self.paths, [],
                               previous_rebuild_context=self.parent if parent is None else parent)

    def commit(self, changes):
        for name, raw in changes.items():
            path = self.repo / name
            if raw is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            git(self.repo, "add", "--", name)
        git(self.repo, "commit", "-m", "artificial verification change")

    def snapshot(self):
        return (git(self.repo, "rev-parse", "HEAD"), self.ledger.path.read_bytes(), self.number,
                {name: (self.repo / name).read_bytes() for name in
                 git(self.repo, "ls-files").splitlines()})

    def assert_blocked_without_write(self, action, code=None):
        before = self.snapshot()
        with self.assertRaises(CaptureError) as caught:
            action()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.snapshot(), before)

    def counter_for(self, context):
        return next(value for value in self.attempts()
                    if value["identity"].get("rebuild_context") == context)

    def test_new_empty_round_only_adds_a_receipt_and_keeps_parent_permissions(self):
        before = self.snapshot()
        result = self.new_round()
        context = result["rebuild_context"]
        changes = git(self.repo, "diff-tree", "--no-commit-id", "--name-status", "-r",
                      result["head"]).splitlines()
        self.assertEqual(changes, ["A\t" + context["path"]])
        self.assertEqual(git(self.repo, "show", "-s", "--format=%P", result["head"]), before[0])
        header, _ = parse_markdown((self.repo / context["path"]).read_bytes())
        self.assertEqual(header["schema_version"], "0.17")
        value = validate_rebuild_context(PipelineGit(self.repo), context, self.paths)
        self.assertEqual(value["targets"], [])
        self.assertEqual(value["previous_rebuild_context"], self.parent)
        objects = [{"path": name, "metadata": parse_markdown(raw)[0], "body": "Artificial replacement."}
                   for name, raw in self.formal_bytes.items()]
        self.assertEqual(allow_rebuild_targets(PipelineGit(self.repo), objects, context), set(self.targets))
        self.assertEqual(self.ledger.path.read_bytes(), before[1])
        self.assertEqual(self.number, before[2])
        for name, raw in before[3].items():
            self.assertEqual((self.repo / name).read_bytes(), raw)
        self.assertTrue(all(not (self.repo / name).exists() for name in self.targets))

    def test_exhausted_old_unknown_and_audit_remain_through_new_full_pipeline(self):
        self.prepare_queue("old-round", self.parent)
        self.mode = "unknown_then_invalid"
        stopped = run_production(self.repo, "old-round", model_call=self.adapter)
        self.assertEqual(stopped["status"], "attempt_limit", stopped)
        old_counter = deepcopy(self.counter_for(self.parent))
        self.assertEqual([row["status"] for row in old_counter["attempts"]],
                         ["unknown", "failed", "failed", "failed"])
        old_operations = deepcopy(self.ledger.snapshot()["operations"])
        sources = {name: (self.repo / name).read_bytes() for name in self.paths}
        context = self.new_round()["rebuild_context"]
        self.mode = "valid"
        start = self.number
        self.prepare_queue("new-round", context)
        result = run_production(self.repo, "new-round", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.number, start + 2)
        self.assertEqual(self.roles[-2:], ["extract", "review"])
        self.assertEqual(self.counter_for(self.parent), old_counter)
        self.assertEqual(len(self.counter_for(context)["attempts"]), 1)
        for operation, original in old_operations.items():
            self.assertEqual(self.ledger.snapshot()["operations"][operation], original)
        self.assertEqual(sources, {name: (self.repo / name).read_bytes() for name in self.paths})
        latest_extract = [payload for role, payload in self.payloads if role == "extract"][-1]
        self.assertEqual(latest_extract["related"], [])
        self.assertEqual(latest_extract["prior_candidates"], [])
        self.assertEqual(latest_extract["reuse_quality_requirements"], [])
        self.assertNotIn("revision_context", latest_extract)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), result["final_run"])
        snapshots = _computation_snapshots(PipelineGit(self.repo), candidate)
        self.assertIn(context, snapshots)
        self.assertIn(self.parent, snapshots)
        for historical in (False, True):
            verified = verify_group_publication(self.repo, result["publication"]["run_id"],
                                                source_paths=self.paths, historical=historical)
            self.assertEqual(sorted(verified["files"]), self.targets)
            self.assertIn(context, verified["authority_snapshots"])
            self.assertIn(self.parent, verified["authority_snapshots"])

    def test_same_intent_after_publication_is_idempotent_and_never_cleans_again(self):
        context = self.new_round()["rebuild_context"]
        self.prepare_queue("new-round", context)
        published = run_production(self.repo, "new-round", model_call=self.adapter)
        self.assertEqual(published["status"], "published", published)
        before = self.snapshot()
        replay = self.new_round()
        self.assertEqual(replay["status"], "already_prepared")
        self.assertEqual(replay["rebuild_context"], context)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(all((self.repo / name).is_file() for name in self.targets))

    def test_queue_or_engine_change_cannot_refresh_the_new_round_four_attempt_limit(self):
        context = self.new_round()["rebuild_context"]
        self.prepare_queue("new-round", context)
        self.mode = "invalid"
        stopped = run_production(self.repo, "new-round", model_call=self.adapter)
        self.assertEqual(stopped["status"], "attempt_limit", stopped)
        self.assertEqual(len(self.counter_for(context)["attempts"]), 4)
        attempts = deepcopy(self.attempts())
        audit = self.ledger.path.read_bytes()
        count = self.number
        advance_engine(self.code)
        self.prepare_queue("renamed-round", context)
        again = run_production(self.repo, "renamed-round", model_call=self.adapter)
        self.assertEqual(again["status"], "attempt_limit", again)
        self.assertEqual((self.attempts(), self.ledger.path.read_bytes(), self.number),
                         (attempts, audit, count))

    def test_parent_must_be_v1_and_cannot_be_mixed_with_deletion_targets(self):
        context = self.new_round()["rebuild_context"]
        self.assert_blocked_without_write(lambda: self.new_round("artificial-recursive", context),
                                          "rebuild_previous_must_cleanup")
        self.assert_blocked_without_write(lambda: prepare_rebuild(self.repo,
            "artificial-mixed", self.paths, self.targets, previous_rebuild_context=self.parent),
            "invalid_rebuild_scope")
        self.assert_blocked_without_write(lambda: prepare_rebuild(self.repo,
            "artificial-empty-without-parent", self.paths, []), "invalid_rebuild_scope")
        self.assert_blocked_without_write(lambda: self.new_round("artificial-bad-reference",
            {**self.parent, "sha256": "sha256-" + "f" * 64}))

    def test_sources_and_controls_cannot_expand_the_parent_cleanup_authority(self):
        other = capture(self.repo, {"source_key": "artificial:other-rebuild-source",
            "source_type": "note", "acquisition_status": "full_text",
            "body": "Artificial unrelated learning observation.\n"})
        self.assert_blocked_without_write(lambda: prepare_rebuild(self.repo,
            "artificial-wrong-source", [other["path"]], [], previous_rebuild_context=self.parent),
            "rebuild_source_scope_changed")
        record_control(self.repo, {"request_key": "artificial-delete-rebuild-source", "action": "delete",
            "source_paths": self.paths, "object_paths": [], "replacement_source_paths": [],
            "basis_source_path": None, "effective_from": None})
        self.assert_blocked_without_write(lambda: self.new_round("artificial-after-control"))

    def test_write_then_delete_since_original_cleanup_still_blocks_new_authority(self):
        target = self.targets[0]
        self.commit({target: self.formal_bytes[target]})
        self.commit({target: None})
        self.assert_blocked_without_write(lambda: self.new_round(), "rebuild_target_changed")

    def test_parent_tamper_even_restored_to_original_bytes_is_not_accepted(self):
        raw = (self.repo / self.parent["path"]).read_bytes()
        self.commit({self.parent["path"]: raw + b"\n"})
        self.commit({self.parent["path"]: raw})
        self.assert_blocked_without_write(lambda: self.new_round(), "rebuild_context_changed")

    def test_guard17_rejects_old_writer_before_any_data_or_audit_change(self):
        context = self.new_round()["rebuild_context"]
        old_formats = tuple(item for item in SUPPORTED_SCHEMA_VERSIONS if item != "0.17")
        with patch("pdm.compatibility.SUPPORTED_SCHEMA_VERSIONS", old_formats):
            self.assert_blocked_without_write(lambda: capture(self.repo, {
                "source_key": "artificial:old-writer-empty-round", "source_type": "note",
                "acquisition_status": "full_text", "body": "Artificial incompatible writer.\n",
            }), "incompatible_memory_schema")
        self.assertEqual(validate_rebuild_context(PipelineGit(self.repo), context, self.paths)["targets"], [])

    def test_cli_previous_context_is_explicit_mutually_exclusive_and_non_model(self):
        with tempfile.TemporaryDirectory(prefix="pdm-empty-rebuild-cli-") as name:
            reference = Path(name) / "parent.json"
            reference.write_text(json.dumps(self.parent))
            arguments = ["rebuild-prepare", "--repo", str(self.repo), "--intent-id", "artificial-cli-new-round",
                         "--source", self.paths[0], "--previous-rebuild-context", str(reference),
                         "--expected-head", "artificial-expected-head"]
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("pdm.rebuild.prepare_rebuild", return_value={"status": "prepared",
                        "rebuild_context": self.parent}) as prepare, patch("pdm.cli._model_budget") as budget, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(arguments)
            self.assertEqual(code, 0, stderr.getvalue())
            prepare.assert_called_once_with(self.repo, "artificial-cli-new-round", self.paths, [],
                expected_head="artificial-expected-head", previous_rebuild_context=self.parent)
            budget.assert_not_called()
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("pdm.rebuild.prepare_rebuild") as prepare, patch("pdm.cli._model_budget") as budget, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main([*arguments, "--target", self.targets[0]])
            self.assertEqual(code, 2)
            prepare.assert_not_called()
            budget.assert_not_called()


if __name__ == "__main__":
    unittest.main()
