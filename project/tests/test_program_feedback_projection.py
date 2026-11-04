"""Program upgrades keep historical errors while projecting current diagnostics.

All sources, transports, engine upgrades and audit grants here are artificial.
The full ordinary pipeline still generates and independently reviews a new draft.
"""

from copy import deepcopy
from contextlib import nullcontext
import unittest
from unittest.mock import patch

from engine_fixtures import advance_engine
from pdm.contracts import CaptureError
from pdm.pipeline import _committed, _load, _run_path
from pdm.transaction import PipelineGit
import test_program_refresh as fixtures


class ProgramFeedbackProjectionTests(unittest.TestCase):
    setUp = fixtures.ProgramRefreshTests.setUp
    sources = fixtures.ProgramRefreshTests.sources
    state = fixtures.ProgramRefreshTests.state
    attempts = fixtures.ProgramRefreshTests.attempts
    prepare = fixtures.ProgramRefreshTests.prepare
    run_slice = fixtures.ProgramRefreshTests.run_slice
    counter_files = fixtures.ProgramRefreshTests.counter_files
    checkpoint = fixtures.ProgramRefreshTests.checkpoint
    second_engine_upgrade = fixtures.ProgramRefreshTests.second_engine_upgrade
    write_queue_state = fixtures.ProgramRefreshTests.write_queue_state

    def transport(self, role, payload, timeout=120):
        response = fixtures.ProgramRefreshTests.transport(self, role, payload, timeout)
        if role == "extract" and self.failure_mode == "envelope":
            response["output"]["unexpected_envelope_field"] = True
        if role == "extract" and self.failure_mode == "condition":
            response["output"]["objects"][0]["body"] += (
                " 选择条件是：多项任务或未来协作扩展，使用组合工具。")
            if getattr(self, "actual_state", False):
                response["output"]["objects"][0]["body"] += " 当前已经完成全部事项。"
        if role == "extract":
            mapping = getattr(self, "id_override", {})
            if getattr(self, "id_suffix", None):
                mapping = {item["id"]: item["id"] + self.id_suffix
                           for item in response["output"]["objects"] + response["output"]["wikis"]}
            def renamed(value):
                if isinstance(value, dict):
                    return {key: renamed(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [renamed(item) for item in value]
                return mapping.get(value, value) if isinstance(value, str) else value
            response["output"] = renamed(response["output"])
        return response

    def legacy_failure(self, *, actual_state=False):
        self.prepare()
        self.failure_mode, self.actual_state = "condition", actual_state
        # Reproduce a prior program that did not recognize an explicit
        # method-selection condition. No accepted source or draft is edited.
        from pdm.extraction_validation import collect_issues
        def old_issue_walk(output, *args, **kwargs):
            result = collect_issues(output, *args, **kwargs)
            future = output["objects"][0]["body"].index("未来")
            # The old checker stopped at its incorrect condition diagnostic;
            # the independent current-state defect was not yet reported.
            result["issues"] = [issue for issue in result["issues"]
                                if issue.get("body_location", {}).get("start") == future]
            self.assertTrue(result["issues"])
            return result
        with patch("pdm.knowledge_quality._v3_conditional_comma_spans", return_value=[]), (
                patch("pdm.extraction_validation.collect_issues", side_effect=old_issue_walk)
                if actual_state else nullcontext()):
            result = self.run_slice()
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["last_result"]["error"], "quality_temporal_missing", result)
        self.failure_mode = None
        row = deepcopy(self.attempts()[0]["attempts"][-1])
        path = _run_path(row["run_id"], "failure")
        return row, path, _committed(PipelineGit(self.repo), path)

    def refresh(self):
        advance_engine(self.engine)
        return self.prepare(refresh=True)

    def extract_payloads(self):
        return [payload for role, payload in self.payloads if role == "extract"]

    def test_resolved_machine_errors_are_null_without_adopting_the_failed_draft(self):
        old, path, raw = self.legacy_failure()
        before_counter, before_audit = self.counter_files(), self.ledger.path.read_bytes()
        self.refresh()
        snapshot = self.state(self.queue)["program_refresh"]["feedback_projection"]
        self.assertEqual(snapshot["source_run_id"], old["run_id"])
        self.assertIsNone(snapshot["validation_error"])
        self.assertEqual(self.counter_files(), before_counter)
        self.assertEqual(self.ledger.path.read_bytes(), before_audit)
        self.assertEqual(self.roles, ["extract"])
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
                         "memory/staging", "memory/records", "memory/topics"), "")
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "extract", "review"])
        feedback = self.extract_payloads()[-1]["extraction_feedback"]
        self.assertIsNone(feedback["validation_error"])
        original = _load(raw, "failure")["extraction_diagnostic"]
        self.assertEqual(feedback["draft"]["quality"], original["response"]["output"]["quality"])
        self.assertEqual(_committed(PipelineGit(self.repo), path), raw)
        self.assertEqual(self.attempts()[0]["attempts"][0], old)

    def test_current_issues_are_recollected_instead_of_filtering_historical_codes(self):
        old, path, raw = self.legacy_failure(actual_state=True)
        self.refresh()
        error = self.state(self.queue)["program_refresh"]["feedback_projection"]["validation_error"]
        self.assertIsInstance(error, dict)
        issues = error["details"]["issues"]
        self.assertTrue(issues)
        body = _load(raw, "failure")["extraction_diagnostic"]["response"]["output"]["objects"][0]["body"]
        future_start, state_start = body.index("未来"), body.index("当前")
        old_issues = _load(raw, "failure")["extraction_diagnostic"]["validation_error"]["details"]["issues"]
        self.assertFalse(any(issue.get("body_location", {}).get("start") == state_start for issue in old_issues))
        spans = [item["body_location"] for item in issues if "body_location" in item]
        self.assertTrue(any(location["start"] == state_start for location in spans), issues)
        self.assertFalse(any(location["start"] == future_start for location in spans), issues)
        self.assertNotEqual(error, _load(raw, "failure")["extraction_diagnostic"]["validation_error"])
        result = self.run_slice()
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(self.extract_payloads()[-1]["extraction_feedback"]["validation_error"], error)
        self.assertEqual(self.attempts()[0]["attempts"][0], old)

    def test_a_second_upgrade_rebuilds_the_first_projected_request_from_its_fixed_view(self):
        old, _, _ = self.legacy_failure()
        self.refresh()
        # The first new request receives no stale machine errors, but its new
        # generated draft has a genuine defect. Its full input gets audited.
        self.failure_mode = "temporal"
        self.assertEqual(self.run_slice()["status"], "failed")
        self.assertIsNone(self.extract_payloads()[-1]["extraction_feedback"]["validation_error"])
        prior_rows = deepcopy(self.attempts()[0]["attempts"])
        prior_operations = deepcopy(self.ledger.snapshot()["operations"])
        self.second_engine_upgrade()
        self.prepare(refresh=True)
        next_error = self.state(self.queue)["program_refresh"]["feedback_projection"]["validation_error"]
        self.assertIsNotNone(next_error)
        self.failure_mode = None
        # The current checker now sees a genuine error, not the first
        # projection's null. Rebuilding that first request from current errors
        # would fail its original scope/payload/rendered-request fingerprints.
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "extract", "extract", "review"])
        self.assertEqual(self.attempts()[0]["attempts"][:2], prior_rows)
        self.assertEqual(self.attempts()[0]["attempts"][0], old)
        for key, operation in prior_operations.items():
            self.assertEqual(self.ledger.snapshot()["operations"][key], operation)

    def test_same_program_retry_uses_the_latest_failure_not_the_old_refresh_projection(self):
        self.legacy_failure()
        self.refresh()
        self.failure_mode = "temporal"
        self.assertEqual(self.run_slice()["status"], "failed")
        self.assertIsNone(self.extract_payloads()[-1]["extraction_feedback"]["validation_error"])
        second = deepcopy(self.attempts()[0]["attempts"][-1])
        self.assertEqual(self.run_slice()["status"], "failed")
        third_payload = self.extract_payloads()[-1]
        self.assertEqual(third_payload["extraction_feedback"]["source_run_id"], second["run_id"])
        self.assertIsNotNone(third_payload["extraction_feedback"]["validation_error"])
        rows = deepcopy(self.attempts()[0]["attempts"])
        self.assertNotIn("program_refresh", rows[-1]["extraction_feedback_ref"])
        self.second_engine_upgrade()
        self.prepare(refresh=True)
        self.failure_mode = None
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.attempts()[0]["attempts"][:3], rows)
        self.assertEqual([row["attempt"] for row in self.attempts()[0]["attempts"]], [1, 2, 3, 4])
        self.assertEqual(self.roles, ["extract"] * 4 + ["review"])

    def test_normal_envelope_errors_are_not_cleared_by_the_bounded_diagnostic_walker(self):
        self.prepare()
        self.failure_mode = "envelope"
        result = self.run_slice()
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["last_result"]["error"], "invalid_model_output")
        self.refresh()
        error = self.state(self.queue)["program_refresh"]["feedback_projection"]["validation_error"]
        self.assertIsNotNone(error)
        self.assertIn("invalid_model_output", [issue["code"] for issue in error["details"]["issues"]])
        self.failure_mode = None
        self.assertEqual(self.run_slice()["status"], "ready")

    def test_legacy_v1_refresh_keeps_the_original_error_projection(self):
        _, _, raw = self.legacy_failure()
        from pdm.production import _save
        def old_writer(git, path, data, kind, head):
            # A genuine old-format transition is born directly from its old
            # queue, unlike a later edit that strips a new witness's snapshot.
            if kind == "production_queue" and data.get("program_refresh", {}).get("contract") == "same-skill-program-refresh-v2":
                data = deepcopy(data)
                data["program_refresh"]["contract"] = "same-skill-program-refresh-v1"
                del data["program_refresh"]["feedback_projection"]
            return _save(git, path, data, kind, head)
        with patch("pdm.production._save", side_effect=old_writer):
            self.refresh()
        result = self.run_slice()
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(self.extract_payloads()[-1]["extraction_feedback"]["validation_error"],
                         _load(raw, "failure")["extraction_diagnostic"]["validation_error"])

    def test_existing_explicit_rebuild_scope_is_kept_in_the_current_projection(self):
        from pdm.production import prepare_production
        from pdm.rebuild import prepare_rebuild
        from pdm.skill_support import resolve_skill_refs
        self.prepare()
        original = self.run_slice(steps=8)
        self.assertEqual(original["status"], "published", original)
        context = prepare_rebuild(self.repo, "artificial explicit projection rebuild", self.paths,
                                  sorted(original["publication"]["files"]),
                                  expected_head=PipelineGit(self.repo).head())["rebuild_context"]
        self.queue = "artificial-rebuild-projection"
        options = {"skill_bundle": resolve_skill_refs("knowledge-extract@16", "knowledge-review@14"),
                   "rebuild_context": context}
        prepare_production(self.repo, self.queue, self.paths, **options)
        self.failure_mode = "condition"
        with patch("pdm.knowledge_quality._v3_conditional_comma_spans", return_value=[]):
            self.assertEqual(self.run_slice()["status"], "failed")
        advance_engine(self.engine)
        prepare_production(self.repo, self.queue, self.paths, refresh_program=True,
                           model_call=self.adapter, **options)
        self.assertIsNone(self.state(self.queue)["program_refresh"]["feedback_projection"]["validation_error"])
        self.failure_mode = None
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.extract_payloads()[-1]["rebuild_context"], context)

    def test_intervening_same_program_publication_does_not_rewrite_fixed_feedback(self):
        from pdm.capture import capture
        from pdm.production import prepare_production, run_production
        from pdm.skill_support import resolve_skill_refs
        self.legacy_failure()
        self.refresh()
        main_state = deepcopy(self.state(self.queue))
        self.assertIsNone(main_state["program_refresh"]["feedback_projection"]["validation_error"])
        old_output = self.extract_payloads()[0]
        old_record = "record-" + old_output["sources"][0]["metadata"]["source_id"][-10:]
        old_topic = "topic-" + old_output["sources"][0]["metadata"]["source_id"][-10:]
        other = capture(self.repo, {"source_key": "artificial:intervening-feedback-target",
                        "source_type": "note", "acquisition_status": "full_text",
                        "body": "Artificial independent work observation.\n"})
        self.id_override = {"record-" + other["source_id"][-10:]: old_record,
                            "topic-" + other["source_id"][-10:]: old_topic}
        prepare_production(self.repo, "artificial-other-target", [other["path"]],
                           skill_bundle=resolve_skill_refs("knowledge-extract@16", "knowledge-review@14"))
        result = run_production(self.repo, "artificial-other-target", model_call=self.adapter)
        self.assertEqual(result["status"], "published", result)
        self.id_override, self.id_suffix = {}, "-separate"
        # A different authorized publication used the old draft's names after
        # the refresh. A new output with separate names remains legitimate;
        # it must not be blocked by reinterpreting the fixed feedback snapshot.
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        self.assertIsNone(self.extract_payloads()[-1]["extraction_feedback"]["validation_error"])
        self.assertEqual(self.state(self.queue)["program_refresh"], main_state["program_refresh"])

    def test_missing_or_tampered_projection_is_rejected_without_new_attempt_or_call(self):
        self.legacy_failure()
        self.refresh()
        original = deepcopy(self.state(self.queue))
        for change in ("missing", "downgrade", "source_run_id", "draft_sha256", "context_sha256", "validation_error"):
            with self.subTest(change=change):
                state = deepcopy(original)
                projection = state["program_refresh"]["feedback_projection"]
                if change == "missing":
                    del state["program_refresh"]["feedback_projection"]
                elif change == "downgrade":
                    state["program_refresh"]["contract"] = "same-skill-program-refresh-v1"
                    del state["program_refresh"]["feedback_projection"]
                elif change == "source_run_id":
                    projection[change] = "run-" + "0" * 32
                elif change in {"draft_sha256", "context_sha256"}:
                    projection[change] = "sha256-" + "0" * 64
                else:
                    issue = {"code": "quality_temporal_missing", "message": "Artificial changed error"}
                    projection[change] = {**issue, "details": {"issues": [issue], "issues_truncated": False}}
                self.write_queue_state(state)
                before = self.checkpoint()
                with self.assertRaises(CaptureError):
                    self.run_slice()
                self.assertEqual(self.checkpoint(), before)
        self.write_queue_state(original)

    def test_unstarted_refresh_cannot_be_downgraded_after_its_birth(self):
        self.prepare()
        self.refresh()
        original = deepcopy(self.state(self.queue))
        self.assertIsNone(original["program_refresh"]["feedback_projection"])
        # An unchanged witness survives a later legitimate progress save.
        # That latest queue commit must not be mistaken for its birth.
        original["revision"] += 1
        self.write_queue_state(original)
        from pdm.production import _check_program_refresh_counter
        before = self.checkpoint()
        _check_program_refresh_counter(PipelineGit(self.repo), original)
        self.assertEqual(self.checkpoint(), before)
        state = deepcopy(original)
        state["program_refresh"]["contract"] = "same-skill-program-refresh-v1"
        del state["program_refresh"]["feedback_projection"]
        self.write_queue_state(state)
        before, roles = self.checkpoint(), list(self.roles)
        with self.assertRaises(CaptureError) as caught:
            self.run_slice()
        self.assertEqual(caught.exception.code, "program_refresh_proof_changed")
        self.assertEqual(self.checkpoint(), before)
        self.assertEqual(self.roles, roles)


if __name__ == "__main__":
    unittest.main()
