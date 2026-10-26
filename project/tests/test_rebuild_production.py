"""Explicit cleanup generations use real temporary Git, never real models."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_production
from engine_fixtures import advance_engine, isolated_engine
from test_pipeline import git
from quality_fixtures import add_quality, passed_review
from pdm.batching import project_batch_sources
from pdm.budget import BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.model_calls import ModelCallError
from pdm.pipeline import _read_reviewed
from pdm.production import (_revision_feedback, _save, _verify_queue_publication, prepare_production,
                            run_production)
from pdm.rebuild import prepare_rebuild
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class RebuildProductionTests(unittest.TestCase):
    state = test_production.ProductionTests.state
    attempts = test_production.ProductionTests.attempts

    def setUp(self):
        test_production.ProductionTests.setUp(self)
        self.code = isolated_engine(self)
        self.paths = self.sources(1)
        self.bundle = resolve_skill_refs("knowledge-extract@10", "knowledge-review@8")
        self.prepare_queue("original")
        self.original = run_production(self.repo, "original", model_call=self.adapter)
        self.assertEqual(self.original["status"], "published", self.original)
        self.targets = sorted(self.original["publication"]["files"])

    def sources(self, count):
        return [capture(self.repo, {"source_key": f"artificial:rebuild:{index}",
                "source_type": "note", "acquisition_status": "full_text",
                "body": f"Artificial learning observation {index}.\n"})["path"]
                for index in range(count)]

    def transport(self, role, payload, timeout=120):
        result = test_production.ProductionTests.transport(self, role, payload, timeout)
        if role == "extract":
            if "revision_context" in payload:
                for record in result["output"]["objects"]:
                    record["body"] += " This observation is limited to its supplied artificial source."
                for topic in result["output"]["wikis"]:
                    topic["body"] += " This topic describes only its supplied artificial source."
            result["output"] = {} if getattr(self, "invalid_extract", False) else add_quality(
                result["output"], payload["quality_context"])
            if not getattr(self, "invalid_extract", False):
                result["output"]["reuse_refs"] = []
        else:
            result["output"] = passed_review(payload["required_checks"])
            if getattr(self, "reject_reviews", 0):
                self.reject_reviews -= 1
                result["output"]["verdict"] = "needs_revision"
                result["output"]["reasons"] = ["Clarify the supplied artificial source scope."]
                result["output"]["checks"][0].update(result="needs_revision", reason="The supplied artificial source scope needs clarification.")
        return result

    def prepare_queue(self, name, context=None, **options):
        return prepare_production(self.repo, name, self.paths, allowed_run_ids=[],
                    skill_bundle=self.bundle, review_strategy="auto", rebuild_context=context,
                    **options)

    def cleanup(self, intent="artificial explicit rebuild"):
        result = prepare_rebuild(self.repo, intent, self.paths, self.targets,
                                 expected_head=PipelineGit(self.repo).head())
        self.assertEqual(result["status"], "prepared", result)
        return result["rebuild_context"]

    def assert_error(self, code, action):
        with self.assertRaises(CaptureError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_generation_publishes_same_ids_from_fresh_input_and_replays_without_calls(self):
        original_counter = deepcopy(self.attempts())
        old_audit = self.ledger.snapshot()["operations"]
        source_bytes = {path: (self.repo / path).read_bytes() for path in self.paths}
        context = self.cleanup()
        self.assertEqual(self.prepare_queue("rebuild", context)["status"], "ready")
        self.assertEqual(self.state("rebuild")["request"]["rebuild_context"], context)
        self.assertEqual(self.state("rebuild")["plan"]["snapshot"]["rebuild_context"], context)
        result = run_production(self.repo, "rebuild", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        extract = [p for role, p in self.payloads if role == "extract"][-1]
        self.assertEqual(extract["rebuild_context"], context)
        self.assertEqual(extract["related"], [])
        self.assertEqual(extract["prior_candidates"], [])
        self.assertEqual(extract["reuse_quality_requirements"], [])
        self.assertNotIn("revision_context", extract)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), result["final_run"])
        self.assertEqual(candidate["rebuild_context"], context)
        self.assertEqual(candidate["extraction_output"]["reuse_refs"], [])
        self.assertEqual(sorted(result["publication"]["files"]), self.targets)
        self.assertEqual(source_bytes, {path: (self.repo / path).read_bytes() for path in self.paths})
        self.assertTrue(all(self.ledger.snapshot()["operations"][key] == value for key, value in old_audit.items()))
        self.assertTrue(all(value in self.attempts() for value in original_counter))
        rebuilt = [value for value in self.attempts() if value not in original_counter]
        self.assertEqual(len(rebuilt), 1)
        self.assertEqual(rebuilt[0]["identity"]["rebuild_context"], context)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number
        again = run_production(self.repo, "rebuild", model_call=self.adapter)
        self.assertEqual(again["status"], "published", again)
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number), before)

    def test_old_unknown_is_preserved_and_only_explicit_generation_has_new_budget(self):
        advance_engine(self.code)
        self.prepare_queue("uncertain")

        def interrupted(*args, **kwargs):
            raise ModelCallError("cli_turn_failed", "model-" + "e" * 32)

        unknown = run_production(self.repo, "uncertain",
                    model_call=BudgetedModelCall(self.ledger, "worker", interrupted), max_steps=1)
        self.assertEqual(unknown["status"], "unknown", unknown)
        old_counter = deepcopy(self.attempts())
        old_audit = self.ledger.snapshot()["operations"]
        self.assertTrue(any(c["status"] == "unknown" for op in old_audit.values() for c in op["calls"].values()))
        self.prepare_queue("renamed_without_receipt")
        count = self.number
        blocked = run_production(self.repo, "renamed_without_receipt", model_call=self.adapter)
        self.assertEqual(blocked["status"], "unknown", blocked)
        self.assertEqual(self.number, count)
        context = self.cleanup()
        self.prepare_queue("explicit", context)
        result = run_production(self.repo, "explicit", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.number, count + 2)
        self.assertTrue(all(value in self.attempts() for value in old_counter))
        self.assertTrue(all(self.ledger.snapshot()["operations"][key] == value for key, value in old_audit.items()))

    def test_same_receipt_queue_and_engine_changes_cannot_reset_four_attempt_limit(self):
        context = self.cleanup()
        self.prepare_queue("four", context)
        self.invalid_extract = True
        first = run_production(self.repo, "four", model_call=self.adapter, max_steps=8)
        self.assertEqual(first["status"], "attempt_limit", first)
        counter = next(value for value in self.attempts() if value["identity"].get("rebuild_context") == context)
        self.assertEqual(len(counter["attempts"]), 4)
        before = self.number, self.ledger.path.read_bytes(), deepcopy(self.attempts())
        advance_engine(self.code)
        self.prepare_queue("new_queue_new_engine", context)
        again = run_production(self.repo, "new_queue_new_engine", model_call=self.adapter, max_steps=8)
        self.assertEqual(again["status"], "attempt_limit", again)
        self.assertEqual((self.number, self.ledger.path.read_bytes(), self.attempts()), before)
        self.assertEqual(len([value for value in self.attempts() if value["identity"].get("rebuild_context") == context]), 1)

    def test_invalid_receipt_and_wrong_source_scope_do_not_create_queue_or_spend(self):
        context = self.cleanup()
        other = self.sources(2)[1]
        for label, ref, paths in (("hash", {**context, "sha256":"sha256-" + "0" * 64}, self.paths),
                                  ("scope", context, [*self.paths, other])):
            with self.subTest(label=label):
                before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
                with self.assertRaises(CaptureError):
                    prepare_production(self.repo, label, paths, skill_bundle=self.bundle, rebuild_context=ref)
                self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes()), before)
                self.assertIsNone(self.state(label))

    def test_related_and_continuation_context_are_not_allowed_for_rebuild(self):
        context = self.cleanup()
        cases = ({"allowed_run_ids":[self.original["publication"]["run_id"]]},
                 {"required_context_sources":self.paths},
                 {"required_context_sources":self.paths,
                  "continuation_context":{"reply_sources":[{"path":self.paths[0]}]}})
        for index, options in enumerate(cases):
            before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
            self.assert_error("invalid_queue_rebuild_context", lambda: prepare_production(
                self.repo, f"forbidden{index}", self.paths, skill_bundle=self.bundle,
                rebuild_context=context, **options))
            self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes()), before)

    def test_multi_batch_rebuild_is_rejected_before_queue_or_attempt(self):
        self.paths = self.sources(9)
        context = self.cleanup()
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), deepcopy(self.attempts())
        self.assert_error("queue_rebuild_requires_single_batch", lambda: self.prepare_queue("multi", context))
        self.assertIsNone(self.state("multi"))
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.attempts()), before)

    def test_blocked_preflight_claims_no_generation_attempt_or_audit_role(self):
        context = self.cleanup()
        self.prepare_queue("capacity", context)
        before = deepcopy(self.attempts()), self.ledger.path.read_bytes(), self.number
        with patch("pdm.pipeline.preflight_processing", return_value={"status":"blocked_capacity",
                   "request_bytes":256001, "model_calls":0, "attempted_calls":0}) as checked:
            result = run_production(self.repo, "capacity", model_call=self.adapter, max_steps=1)
        self.assertEqual(result["status"], "blocked_capacity", result)
        self.assertEqual(checked.call_args.kwargs["rebuild_context"], context)
        self.assertEqual(checked.call_args.kwargs["related_runs"], [])
        self.assertNotIn("revision_from_run", checked.call_args.kwargs)
        self.assertEqual((self.attempts(), self.ledger.path.read_bytes(), self.number), before)

    def test_rebuild_queue_requires_the_same_explicit_context_on_prepare_replay(self):
        context = self.cleanup()
        self.prepare_queue("same", context)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
        self.assertTrue(self.prepare_queue("same", context)["reused"])
        self.assert_error("queue_identity_conflict", lambda: self.prepare_queue("same"))
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes()), before)

    def test_changed_receipt_on_existing_queue_is_rejected_before_claiming_attempt(self):
        context = self.cleanup()
        self.prepare_queue("saved", context)
        path = self.repo / context["path"]
        path.write_bytes(path.read_bytes() + b"\n")
        git(self.repo, "add", context["path"])
        git(self.repo, "commit", "-m", "artificial receipt tampering")
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), deepcopy(self.attempts())
        self.assert_error("rebuild_context_changed", lambda: run_production(self.repo, "saved", model_call=self.adapter))
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.attempts()), before)

    def test_publication_adoption_rejects_a_different_valid_generation(self):
        first = self.cleanup("first explicit generation")
        self.prepare_queue("first", first)
        published = run_production(self.repo, "first", model_call=self.adapter)
        self.assertEqual(published["status"], "published", published)
        second = self.cleanup("second explicit generation")
        wrong_state = deepcopy(self.state("first"))
        wrong_state["request"]["rebuild_context"] = second
        self.assert_error("queue_publication_rebuild_mismatch", lambda: _verify_queue_publication(
            PipelineGit(self.repo), wrong_state, published["publication"]))

    def test_same_generation_review_feedback_repairs_then_publishes(self):
        context = self.cleanup()
        self.prepare_queue("feedback", context)
        self.reject_reviews = 1
        first = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(first["status"], "needs_revision", first)
        fixed = {path: (self.repo / path).read_bytes() for path in
                 (first["last_result"]["stage_path"], first["last_result"]["review_path"])}
        second = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(second["status"], "ready", second)
        request = [p for role, p in self.payloads if role == "extract"][-1]
        self.assertEqual(request["rebuild_context"], context)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), second["final_run"])
        self.assertEqual(candidate["revision_context"]["candidate"]["rebuild_context"], context)
        self.assertEqual(request["revision_context"]["source_run_id"], first["active_run_id"])
        self.assertEqual(request["related"], [])
        self.assertEqual(request["prior_candidates"], [])
        counter = next(value for value in self.attempts() if value["identity"].get("rebuild_context") == context)
        self.assertEqual(len(counter["attempts"]), 2)
        self.assertEqual(counter["attempts"][1]["revision_from_run"], first["active_run_id"])
        published = run_production(self.repo, "feedback", model_call=self.adapter, max_steps=1)
        self.assertEqual(published["status"], "published", published)
        self.assertEqual(fixed, {path: (self.repo / path).read_bytes() for path in fixed})
        self.assertEqual(self.roles, ["extract", "review"] * 3)

    def test_rebuild_cannot_take_feedback_from_a_nonrebuild_rejection(self):
        advance_engine(self.code)
        prepare_production(self.repo, "old_rejection", self.paths,
            allowed_run_ids=[self.original["publication"]["run_id"]],
            skill_bundle=self.bundle, review_strategy="auto")
        self.reject_reviews = 1
        rejected = run_production(self.repo, "old_rejection", model_call=self.adapter, max_steps=1)
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        context = self.cleanup()
        self.prepare_queue("new_generation", context)
        state = self.state("new_generation")
        projection = project_batch_sources(self.repo, state["plan"], state["plan"]["batches"][0]["batch_id"], source_paths=self.paths)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number
        self.assert_error("queue_revision_context_mismatch", lambda: _revision_feedback(
            PipelineGit(self.repo), [{"status":"needs_revision", "run_id":rejected["active_run_id"]}],
            state, projection, self.paths, draft=False, merge_runs=()))
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number), before)

    def test_request_and_plan_generation_mismatch_is_rejected_before_attempt(self):
        context = self.cleanup()
        self.prepare_queue("early_guard", context)
        original = self.state("early_guard")
        for missing in ("request", "plan"):
            with self.subTest(missing=missing):
                altered = deepcopy(original)
                container = altered["request"] if missing == "request" else altered["plan"]["snapshot"]
                container.pop("rebuild_context")
                repository = PipelineGit(self.repo)
                with repository.lock():
                    _save(repository, "memory/runs/processes/queues/early_guard/state.md", altered,
                          "production_queue", repository.check())
                before = repository.head(), self.ledger.path.read_bytes(), deepcopy(self.attempts()), self.number
                self.assert_error("queue_rebuild_plan_mismatch", lambda: run_production(
                    self.repo, "early_guard", model_call=self.adapter))
                self.assertEqual((repository.head(), self.ledger.path.read_bytes(), self.attempts(), self.number), before)


if __name__ == "__main__":
    unittest.main()
