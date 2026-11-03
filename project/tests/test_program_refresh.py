"""Artificial program-only queue upgrades preserve work, calls and immutable history."""

from copy import deepcopy
from contextlib import redirect_stdout
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from engine_fixtures import advance_engine, isolated_engine
import test_production
import test_skill_quality_production
from test_pipeline import git
from pdm.batch_groups import verify_group_publication
from pdm.budget import BudgetedModelCall
from pdm.capture import capture
from pdm.contracts import CaptureError, canonical, markdown, parse_markdown
from pdm.controls import record_control
from pdm.model_calls import ModelCallError
from pdm.pipeline import _read_reviewed
from pdm.production import prepare_production, run_production
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class ProgramRefreshTests(unittest.TestCase):
    state = test_production.ProductionTests.state
    attempts = test_production.ProductionTests.attempts
    sources = test_skill_quality_production.SkillQualityProductionTests.sources

    def setUp(self):
        test_production.ProductionTests.setUp(self)
        self.engine = isolated_engine(self)
        self.paths = self.sources()
        self.queue = "artificial-program-refresh"
        self.failure_mode = None

    def transport(self, role, payload, timeout=120):
        response = test_skill_quality_production.SkillQualityProductionTests.transport(self, role, payload, timeout)
        if role == "extract" and self.failure_mode == "temporal":
            record = response["output"]["objects"][0]
            statement = "当前已经完成全部事项。"
            record["body"] += " " + statement
            response["output"]["quality"]["temporal_claims"] = [{
                "record_id": record["id"], "statement": statement, "basis": "unknown",
                "system_fact_id": None, "source_refs": deepcopy(record["sources"])}]
        if role == "extract" and self.failure_mode == "unknown":
            raise ModelCallError("cli_turn_failed", response["invocation_id"], .01)
        return response

    def prepare(self, *, refresh=False, paths=None, queue=None, bundle=None, model_call=None):
        return prepare_production(self.repo, queue or self.queue, paths or self.paths,
            skill_bundle=bundle or resolve_skill_refs("knowledge-extract@16", "knowledge-review@14"),
            **({"refresh_program": True, "model_call": model_call or self.adapter} if refresh else {}))

    def run_slice(self, *, steps=1):
        return run_production(self.repo, self.queue, model_call=self.adapter, max_steps=steps)

    def counter_files(self):
        folder = self.repo / "memory/runs/processes/queue-attempts"
        return {str(path.relative_to(self.repo)): path.read_bytes() for path in folder.glob("*.md")}

    def checkpoint(self):
        return PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.counter_files(), self.number

    def reject_refresh(self, **options):
        before = self.checkpoint()
        with self.assertRaises(CaptureError) as caught:
            self.prepare(refresh=True, **options)
        self.assertTrue(caught.exception.code, caught.exception)
        self.assertEqual(self.checkpoint(), before)
        return caught.exception.code

    def failed(self, count=1):
        self.prepare()
        self.failure_mode = "temporal"
        for _ in range(count):
            result = self.run_slice()
            self.assertEqual(result["status"], "failed", result)
            self.assertEqual(result["last_result"]["error"], "quality_temporal_mismatch", result)
        self.failure_mode = None
        return deepcopy(self.attempts()[0]["attempts"])

    def test_seven_unstarted_queues_refresh_without_calls_or_attempt_counters(self):
        paths = self.sources(7)
        original = {}
        for index, path in enumerate(paths):
            name = "artificial-unstarted-" + str(index)
            self.prepare(queue=name, paths=[path])
            original[name] = deepcopy(self.state(name))
        audit = self.ledger.path.read_bytes()
        advance_engine(self.engine)
        for index, path in enumerate(paths):
            name = "artificial-unstarted-" + str(index)
            refreshed = self.prepare(refresh=True, queue=name, paths=[path])
            state = self.state(name)
            self.assertEqual(state["queue_id"], name)
            self.assertEqual(state["request"], original[name]["request"])
            self.assertEqual(state["status"], "ready", refreshed)
            self.assertNotEqual(state["plan"]["plan_id"], original[name]["plan"]["plan_id"])
            self.assertNotEqual(state["plan"]["snapshot"]["versions"]["program_commit"],
                                original[name]["plan"]["snapshot"]["versions"]["program_commit"])
        self.assertEqual(self.counter_files(), {})
        self.assertEqual(self.ledger.path.read_bytes(), audit)
        self.assertEqual(self.roles, [])

    def test_default_old_queue_remains_blocked_after_an_engine_upgrade(self):
        self.prepare()
        advance_engine(self.engine)
        before = self.checkpoint()
        with self.assertRaises(CaptureError) as caught:
            self.run_slice()
        self.assertEqual(caught.exception.code, "batch_snapshot_changed")
        self.assertEqual(self.checkpoint(), before)

    def test_refresh_cannot_create_a_new_queue_or_disguise_a_new_work(self):
        advance_engine(self.engine)
        self.reject_refresh()
        self.assertFalse((self.repo / ("memory/runs/processes/queues/" + self.queue + "/state.md")).exists())

    def test_refresh_is_idempotent_for_the_same_current_program(self):
        self.prepare()
        advance_engine(self.engine)
        self.prepare(refresh=True)
        state = deepcopy(self.state(self.queue))
        before = self.checkpoint()
        again = self.prepare(refresh=True)
        self.assertTrue(again["reused"], again)
        self.assertEqual(self.state(self.queue), state)
        self.assertEqual(self.checkpoint(), before)

    def test_standard_cli_refreshes_with_the_same_existing_audit_without_calling_a_model(self):
        from pdm.cli import main
        self.prepare()
        old = deepcopy(self.state(self.queue))
        audit = self.ledger.path.read_bytes()
        advance_engine(self.engine)
        output = io.StringIO()
        with redirect_stdout(output):
            returned = main(["production", "prepare", "--repo", str(self.repo), "--id", self.queue,
                "--source", self.paths[0], "--refresh-program", "--budget-ledger", str(self.ledger.path),
                "--operation-id", self.queue])
        self.assertEqual(returned, 0, output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["queue_id"], self.queue)
        self.assertNotEqual(self.state(self.queue)["plan"]["plan_id"], old["plan"]["plan_id"])
        self.assertEqual(self.ledger.path.read_bytes(), audit)
        self.assertEqual(self.number, 0)

    def test_one_failed_attempt_keeps_original_counter_audit_and_feedback_then_publishes(self):
        previous = self.failed()
        old_counter = self.counter_files()
        old_operations = deepcopy(self.ledger.snapshot()["operations"])
        old_queue = deepcopy(self.state(self.queue))
        failure_path = "memory/runs/processes/" + previous[0]["run_id"] + "/failure.md"
        old_failure = (self.repo / failure_path).read_bytes()
        advance_engine(self.engine)
        self.prepare(refresh=True)
        self.assertEqual(self.counter_files(), old_counter)
        self.assertEqual(self.ledger.snapshot()["operations"], old_operations)
        self.assertEqual(self.state(self.queue)["audit_pool"], old_queue["audit_pool"])
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        current = self.attempts()[0]["attempts"]
        self.assertEqual(current[0], previous[0])
        self.assertEqual([row["attempt"] for row in current], [1, 2])
        self.assertEqual(self.roles, ["extract", "extract", "review"])
        extracts = [payload for role, payload in self.payloads if role == "extract"]
        self.assertEqual(extracts[-1]["extraction_feedback"]["source_run_id"], previous[0]["run_id"])
        self.assertNotEqual(extracts[-1]["batch_context"]["plan_id"],
                            extracts[0]["batch_context"]["plan_id"])
        self.assertEqual((self.repo / failure_path).read_bytes(), old_failure)
        for key, value in old_operations.items():
            self.assertEqual(self.ledger.snapshot()["operations"][key], value)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), result["final_run"])
        self.assertEqual(candidate["versions"], self.state(self.queue)["plan"]["snapshot"]["versions"])
        proof = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=self.paths)
        self.assertEqual(proof["status"], "published_local")
        self.assertEqual(verify_group_publication(self.repo, proof["run_id"], historical=True)["commit_sha"],
                         proof["commit_sha"])
        before_replay = self.checkpoint()
        self.assertEqual(self.run_slice()["status"], "published")
        self.assertEqual(self.checkpoint(), before_replay)

    def test_changed_old_counter_row_is_rejected_before_any_new_call_or_write(self):
        self.failed(2)
        advance_engine(self.engine)
        self.prepare(refresh=True)
        path, raw = next(iter(self.counter_files().items()))
        metadata, body = parse_markdown(raw)
        counter = json.loads(body)
        last = deepcopy(counter["attempts"][-1])
        counter["attempts"][0]["error"] = "artificial_changed_old_error"
        (self.repo / path).write_bytes(markdown(metadata, canonical(counter).decode() + "\n"))
        git(self.repo, "add", path)
        git(self.repo, "commit", "-qm", "artificial corruption of an earlier counter row")
        before = self.checkpoint()
        with self.assertRaises(CaptureError) as caught:
            self.run_slice()
        self.assertEqual(caught.exception.code, "program_refresh_proof_changed")
        self.assertEqual(self.checkpoint(), before)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 2)
        self.assertEqual(self.attempts()[0]["attempts"][-1], last)

    def test_failed_queue_cli_refresh_and_replay_exit_zero_without_calls_or_counter_changes(self):
        from pdm.cli import main
        self.failed()
        before_counter = self.counter_files()
        before_audit = self.ledger.path.read_bytes()
        before_calls = self.number
        advance_engine(self.engine)
        arguments = ["production", "prepare", "--repo", str(self.repo), "--id", self.queue,
                     "--source", self.paths[0], "--refresh-program",
                     "--budget-ledger", str(self.ledger.path), "--operation-id", self.queue]
        first_head = None
        for refreshed in (True, False):
            with self.subTest(program_refreshed=refreshed):
                output = io.StringIO()
                with redirect_stdout(output):
                    returned = main(arguments)
                self.assertEqual(returned, 0, output.getvalue())
                result = json.loads(output.getvalue())
                self.assertIs(result["program_refreshed"], refreshed)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(self.state(self.queue)["status"], "failed")
                self.assertEqual(self.counter_files(), before_counter)
                self.assertEqual(self.ledger.path.read_bytes(), before_audit)
                self.assertEqual(self.number, before_calls)
                head = PipelineGit(self.repo).head()
                if first_head is not None:
                    self.assertEqual(head, first_head)
                first_head = head

    def test_two_failures_refresh_and_a_further_failure_consumes_third_original_slot(self):
        previous = self.failed(2)
        old_counter = self.counter_files()
        advance_engine(self.engine)
        self.prepare(refresh=True)
        self.assertEqual(self.counter_files(), old_counter)
        self.failure_mode = "temporal"
        result = self.run_slice()
        self.assertEqual(result["status"], "failed", result)
        current = self.attempts()[0]["attempts"]
        self.assertEqual(current[:2], previous)
        self.assertEqual([row["attempt"] for row in current], [1, 2, 3])
        self.assertEqual(len(self.counter_files()), 1)

    def second_engine_upgrade(self):
        path = self.engine / "project/src/pdm/fixture_upgrade.py"
        path.write_text(path.read_text() + "# Another artificial program-only upgrade.\n")
        git(self.engine, "add", str(path.relative_to(self.engine)))
        git(self.engine, "commit", "-m", "artificial second program-only upgrade")

    def write_queue_state(self, state):
        path = "memory/runs/processes/queues/" + self.queue + "/state.md"
        metadata, _ = parse_markdown((self.repo / path).read_bytes())
        raw = markdown(metadata, canonical(state).decode() + "\n")
        (self.repo / path).write_bytes(raw)
        git(self.repo, "add", path)
        git(self.repo, "commit", "-m", "artificial queue proof mutation")
        return {"path": path, "commit_sha": PipelineGit(self.repo).head(),
                "sha256": "sha256-" + hashlib.sha256(raw).hexdigest()}

    def test_ready_queue_can_refresh_twice_before_its_first_attempt(self):
        self.prepare()
        audit = self.ledger.path.read_bytes()
        advance_engine(self.engine)
        self.prepare(refresh=True)
        self.second_engine_upgrade()
        self.prepare(refresh=True)
        self.assertEqual(self.ledger.path.read_bytes(), audit)
        self.assertEqual(self.counter_files(), {})
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual([row["attempt"] for row in self.attempts()[0]["attempts"]], [1])

    def test_failed_request_survives_two_refreshes_without_an_intervening_attempt(self):
        old_rows = self.failed()
        old_counter, old_audit = self.counter_files(), self.ledger.path.read_bytes()
        old_operations = deepcopy(self.ledger.snapshot()["operations"])
        advance_engine(self.engine)
        self.prepare(refresh=True)
        self.second_engine_upgrade()
        self.prepare(refresh=True)
        self.assertEqual(self.counter_files(), old_counter)
        self.assertEqual(self.ledger.path.read_bytes(), old_audit)
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        rows = self.attempts()[0]["attempts"]
        self.assertEqual(rows[:1], old_rows)
        self.assertEqual([row["attempt"] for row in rows], [1, 2])
        self.assertEqual(self.roles, ["extract", "extract", "review"])
        self.assertEqual(self.payloads[-2][1]["extraction_feedback"]["source_run_id"], old_rows[0]["run_id"])
        for key, value in old_operations.items():
            self.assertEqual(self.ledger.snapshot()["operations"][key], value)
        proof = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=self.paths)
        self.assertEqual(verify_group_publication(self.repo, proof["run_id"], historical=True)["commit_sha"],
                         proof["commit_sha"])
        before = self.checkpoint()
        self.assertEqual(self.run_slice()["status"], "published")
        self.assertEqual(self.checkpoint(), before)

    def test_an_intervening_third_failure_retains_only_the_last_original_slot(self):
        old_rows = self.failed(2)
        advance_engine(self.engine)
        self.prepare(refresh=True)
        self.failure_mode = "temporal"
        self.assertEqual(self.run_slice()["status"], "failed")
        third_rows = deepcopy(self.attempts()[0]["attempts"])
        self.second_engine_upgrade()
        self.prepare(refresh=True)
        self.failure_mode = None
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        rows = self.attempts()[0]["attempts"]
        self.assertEqual(rows[:2], old_rows)
        self.assertEqual(rows[:3], third_rows)
        self.assertEqual([row["attempt"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(self.roles, ["extract"] * 4 + ["review"])

    def reject_changed_intermediate_refresh(self, field):
        self.failed()
        advance_engine(self.engine)
        self.prepare(refresh=True)
        first = deepcopy(self.state(self.queue))
        self.second_engine_upgrade()
        self.prepare(refresh=True)
        current = deepcopy(self.state(self.queue))
        if field == "previous_queue":
            first["program_refresh"][field]["sha256"] = "sha256-" + "0" * 64
        else:
            first["program_refresh"][field] = "sha256-" + "0" * 64
        current["program_refresh"]["previous_queue"] = self.write_queue_state(first)
        self.write_queue_state(current)
        before = self.checkpoint()
        with self.assertRaises(CaptureError) as caught:
            self.run_slice()
        self.assertEqual(caught.exception.code, "program_refresh_proof_changed")
        self.assertEqual(self.checkpoint(), before)
        self.assertEqual(self.roles, ["extract"])

    def test_changed_intermediate_queue_witness_stops_before_a_new_call(self):
        self.reject_changed_intermediate_refresh("previous_queue")

    def test_changed_intermediate_counter_digest_stops_before_a_new_call(self):
        self.reject_changed_intermediate_refresh("counter_sha256")

    def test_an_unknown_attempt_after_the_first_refresh_blocks_a_second_refresh(self):
        self.failed()
        advance_engine(self.engine)
        self.prepare(refresh=True)
        self.failure_mode = "unknown"
        self.assertEqual(self.run_slice()["status"], "unknown")
        self.second_engine_upgrade()
        self.reject_refresh()
        self.assertEqual(self.roles, ["extract", "extract"])
        self.assertEqual([row["attempt"] for row in self.attempts()[0]["attempts"]], [1, 2])

    def test_new_source_version_cannot_repurpose_a_queue_during_refresh(self):
        self.prepare()
        replacement = capture(self.repo, {"source_key": "quality:note:0", "source_type": "note",
            "acquisition_status": "full_text", "body": "Changed artificial learning observation.\n"})["path"]
        advance_engine(self.engine)
        self.reject_refresh(paths=[replacement])

    def test_control_change_blocks_even_when_selected_source_bytes_stay_the_same(self):
        self.prepare()
        unrelated = capture(self.repo, {"source_key": "artificial:unrelated-control", "source_type": "note",
            "acquisition_status": "full_text", "body": "Unrelated artificial note.\n"})["path"]
        record_control(self.repo, {"request_key": "artificial:program-refresh-delete", "action": "delete",
                                   "source_paths": [unrelated]})
        advance_engine(self.engine)
        self.reject_refresh()

    def test_skill_changes_do_not_use_the_program_only_refresh_contract(self):
        self.prepare()
        advance_engine(self.engine)
        self.reject_refresh(bundle=resolve_skill_refs("knowledge-extract@15", "knowledge-review@13"))

    def test_non_audit_or_different_audit_pool_cannot_refresh_completed_failures(self):
        self.failed()
        from pdm.budget import BudgetLedger
        other = BudgetLedger.create(self.ledger.path.parent / "other-pool.json", None, 0,
                                    "artificial distinct grant", mode="audit")
        advance_engine(self.engine)
        self.reject_refresh(model_call=BudgetedModelCall(other, "refresh-worker", self.transport))

    def test_a_finite_budget_is_not_a_live_audit_refresh_authority(self):
        self.prepare()
        from pdm.budget import BudgetLedger
        budget = BudgetLedger.create(self.ledger.path.parent / "finite-budget.json", 10, 0,
                                     "artificial finite grant", mode="budget")
        advance_engine(self.engine)
        self.reject_refresh(model_call=BudgetedModelCall(budget, "refresh-worker", self.transport))

    def test_live_original_call_must_still_be_completed_at_refresh_time(self):
        rows = self.failed()
        original = self.ledger.operation

        def became_unknown(operation_id):
            operation = original(operation_id)
            if operation_id == rows[0]["operation_id"]:
                operation["calls"]["extract"]["status"] = "unknown"
            return operation

        advance_engine(self.engine)
        with patch.object(self.ledger, "operation", side_effect=became_unknown):
            self.reject_refresh()

    def test_an_older_skill_contract_cannot_opt_into_current_program_refresh(self):
        old = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.prepare(bundle=old)
        advance_engine(self.engine)
        self.reject_refresh(bundle=old)

    def test_multibatch_plan_is_not_silently_rewritten(self):
        paths = self.sources(9)
        self.prepare(paths=paths)
        self.assertEqual(self.state(self.queue)["plan"]["coverage"]["batch_count"], 2)
        advance_engine(self.engine)
        self.reject_refresh(paths=paths)

    def test_unknown_completed_status_is_not_inferred_from_a_git_failure(self):
        self.prepare()
        self.failure_mode = "unknown"
        first = self.run_slice()
        self.assertEqual(first["status"], "unknown", first)
        advance_engine(self.engine)
        self.reject_refresh()

    def test_unexpected_stage_prevents_reusing_a_failed_extraction(self):
        rows = self.failed()
        path = "memory/staging/" + rows[0]["run_id"] + ".md"
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Artificial unexpected fixed candidate; must not be ignored.\n")
        git(self.repo, "add", path)
        git(self.repo, "commit", "-m", "artificial unexpected stage")
        advance_engine(self.engine)
        self.reject_refresh()

    def test_an_existing_independent_review_cannot_be_refreshed_as_unstarted(self):
        self.prepare()
        first = self.run_slice()
        self.assertEqual(first["status"], "ready", first)
        self.assertTrue((self.repo / ("memory/runs/processes/" + first["final_run"] + "/review.md")).is_file())
        advance_engine(self.engine)
        self.reject_refresh()

    def test_exhausted_four_attempts_cannot_be_refreshed_into_a_fifth(self):
        self.prepare()
        self.failure_mode = "temporal"
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "attempt_limit", result)
        self.assertEqual(len(self.attempts()[0]["attempts"]), 4)
        advance_engine(self.engine)
        self.reject_refresh()
        self.assertEqual(self.roles, ["extract"] * 4)

    def legacy_domain_failure(self, count=1):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            return self.failed(count)

    def test_registered_domain_upgrade_keeps_failed_request_and_counter_then_publishes(self):
        old_rows = self.legacy_domain_failure()
        old_queue = deepcopy(self.state(self.queue))
        old_counter, old_audit = self.counter_files(), self.ledger.path.read_bytes()
        old_operations = deepcopy(self.ledger.snapshot()["operations"])
        failure_path = "memory/runs/processes/" + old_rows[0]["run_id"] + "/failure.md"
        old_failure = (self.repo / failure_path).read_bytes()
        self.assertEqual(self.payloads[0][1]["domain_policy"]["version"], "three-domains-v1")
        advance_engine(self.engine)
        refreshed = self.prepare(refresh=True)
        state = self.state(self.queue)
        self.assertEqual(state["program_refresh"]["contract"], "same-sources-domain-policy-refresh-v1")
        self.assertEqual(state["request"], old_queue["request"])
        self.assertEqual(refreshed["attempts_used"], 1)
        self.assertEqual(self.counter_files(), old_counter)
        self.assertEqual(self.ledger.path.read_bytes(), old_audit)
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        rows = self.attempts()[0]["attempts"]
        self.assertEqual(rows[:1], old_rows)
        self.assertEqual([r["attempt"] for r in rows], [1, 2])
        self.assertEqual(len(self.counter_files()), 1)
        self.assertEqual(self.roles, ["extract", "extract", "review"])
        self.assertEqual(self.payloads[-2][1]["domain_policy"]["version"], "three-domains-v2")
        self.assertEqual(self.payloads[-1][1]["domain_policy"]["version"], "three-domains-v2")
        self.assertEqual((self.repo / failure_path).read_bytes(), old_failure)
        for key, value in old_operations.items():
            self.assertEqual(self.ledger.snapshot()["operations"][key], value)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), result["final_run"])
        self.assertEqual(candidate["versions"]["domain_policy_version"], "three-domains-v2")
        proof = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=self.paths)
        self.assertEqual(verify_group_publication(self.repo, proof["run_id"], historical=True)["commit_sha"], proof["commit_sha"])
        before = self.checkpoint()
        self.assertEqual(self.run_slice()["status"], "published")
        self.assertEqual(self.checkpoint(), before)

    def test_unstarted_domain_upgrade_preserves_zero_attempts_and_is_idempotent(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            self.prepare()
        before_calls, before_audit = self.number, self.ledger.path.read_bytes()
        advance_engine(self.engine)
        refreshed = self.prepare(refresh=True)
        self.assertEqual(refreshed["attempts_used"], 0)
        self.assertEqual(self.state(self.queue)["program_refresh"]["contract"], "same-sources-domain-policy-refresh-v1")
        self.assertEqual(self.counter_files(), {})
        self.assertEqual(self.number, before_calls)
        self.assertEqual(self.ledger.path.read_bytes(), before_audit)
        before = self.checkpoint()
        self.assertFalse(self.prepare(refresh=True)["program_refreshed"])
        self.assertEqual(self.checkpoint(), before)

    def test_domain_upgrade_requires_explicit_refresh(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            self.prepare()
        advance_engine(self.engine)
        before = self.checkpoint()
        with self.assertRaises(CaptureError) as caught:
            self.run_slice()
        self.assertEqual(caught.exception.code, "batch_snapshot_changed")
        self.assertEqual(self.checkpoint(), before)

    def test_domain_upgrade_rejects_changed_current_policy_sha_before_a_call(self):
        self.legacy_domain_failure()
        advance_engine(self.engine)
        from pdm.pipeline import execution_versions
        def changed(git):
            value = execution_versions(git)
            value["domain_policy_sha256"] = "sha256-" + "0" * 64
            return value
        with patch("pdm.pipeline.execution_versions", side_effect=changed):
            self.assertEqual(self.reject_refresh(), "program_refresh_rules_changed")

    def test_domain_upgrade_rejects_changed_previous_policy_sha(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            self.prepare()
        state = deepcopy(self.state(self.queue))
        state["plan"]["snapshot"]["versions"]["domain_policy_sha256"] = "sha256-" + "0" * 64
        self.write_queue_state(state)
        advance_engine(self.engine)
        self.assertEqual(self.reject_refresh(), "program_refresh_rules_changed")

    def test_domain_upgrade_rejects_an_unknown_policy_transition(self):
        self.legacy_domain_failure()
        advance_engine(self.engine)
        from pdm.pipeline import execution_versions
        def changed(git):
            value = execution_versions(git)
            value["domain_policy_version"] = "three-domains-v3"
            return value
        with patch("pdm.pipeline.execution_versions", side_effect=changed):
            self.assertEqual(self.reject_refresh(), "program_refresh_rules_changed")

    def test_domain_upgrade_cannot_hide_an_unrelated_prompt_change(self):
        self.legacy_domain_failure()
        advance_engine(self.engine)
        path = self.engine / "project/prompts/extract.md"
        path.write_text(path.read_text() + "\nArtificial extra rule outside the domain policy.\n")
        git(self.engine, "add", str(path.relative_to(self.engine)))
        git(self.engine, "commit", "-m", "artificial unrelated prompt change")
        self.assertEqual(self.reject_refresh(), "program_refresh_rules_changed")

    def add_unrelated_skill_asset(self):
        path = self.engine / "project/skills/artificial-unrelated-note.txt"
        path.write_text("Artificial unrelated future Skill asset.\n")
        git(self.engine, "add", str(path.relative_to(self.engine)))
        git(self.engine, "commit", "-m", "artificial unrelated future Skill registration")

    def test_published_domain_refresh_history_survives_an_unrelated_skill_addition(self):
        self.legacy_domain_failure()
        advance_engine(self.engine)
        self.prepare(refresh=True)
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        group = result["publication"]["run_id"]
        proof = verify_group_publication(self.repo, group, historical=True)
        self.add_unrelated_skill_asset()
        before = self.checkpoint()
        self.assertEqual(verify_group_publication(self.repo, group, historical=True)["commit_sha"], proof["commit_sha"])
        self.assertEqual(self.checkpoint(), before)

    def test_a_new_domain_refresh_rejects_an_unregistered_skill_tree_before_calls(self):
        self.legacy_domain_failure()
        self.add_unrelated_skill_asset()
        self.assertEqual(self.reject_refresh(), "program_refresh_rules_changed")
        self.assertEqual(self.roles, ["extract"])

    def test_domain_upgrade_rejects_a_downgrade_to_v1(self):
        self.prepare()
        advance_engine(self.engine)
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            self.assertEqual(self.reject_refresh(), "program_refresh_rules_changed")

    def test_domain_upgrade_rejects_a_changed_source(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            self.prepare()
        replacement = capture(self.repo, {"source_key": "quality:note:0", "source_type": "note",
            "acquisition_status": "full_text", "body": "Changed artificial work source.\n"})["path"]
        advance_engine(self.engine)
        self.reject_refresh(paths=[replacement])

    def test_domain_upgrade_rejects_a_control_change_with_unchanged_sources(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            self.prepare()
        advance_engine(self.engine)
        unrelated = capture(self.repo, {"source_key": "domain:unrelated", "source_type": "note",
            "acquisition_status": "full_text", "body": "Unrelated artificial source.\n"})["path"]
        record_control(self.repo, {"request_key": "domain:delete-control", "action": "delete", "source_paths": [unrelated]})
        self.reject_refresh()

    def test_domain_upgrade_rejects_unknown_attempts_and_keeps_them_unknown(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            self.prepare()
            self.failure_mode = "unknown"
            self.assertEqual(self.run_slice()["status"], "unknown")
        advance_engine(self.engine)
        self.reject_refresh()
        self.assertEqual(self.roles, ["extract"])
        self.assertEqual(self.attempts()[0]["attempts"][0]["status"], "unknown")

    def test_domain_upgrade_cannot_reopen_an_exhausted_work(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            self.prepare()
            self.failure_mode = "temporal"
            self.assertEqual(self.run_slice(steps=8)["status"], "attempt_limit")
        self.failure_mode = None
        advance_engine(self.engine)
        self.reject_refresh()
        self.assertEqual(len(self.attempts()[0]["attempts"]), 4)
        self.assertEqual(self.roles, ["extract"] * 4)

    def test_domain_upgrade_rejects_a_started_independent_review(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            self.prepare()
            self.assertEqual(self.run_slice()["status"], "ready")
        advance_engine(self.engine)
        self.reject_refresh()
        self.assertEqual(self.roles, ["extract", "review"])

    def test_domain_change_cannot_claim_the_unchanged_program_only_contract(self):
        self.legacy_domain_failure()
        advance_engine(self.engine)
        self.prepare(refresh=True)
        state = deepcopy(self.state(self.queue))
        state["program_refresh"]["contract"] = "same-skill-program-refresh-v1"
        self.write_queue_state(state)
        before = self.checkpoint()
        with self.assertRaises(CaptureError) as caught:
            self.run_slice()
        self.assertEqual(caught.exception.code, "program_refresh_proof_changed")
        self.assertEqual(self.checkpoint(), before)
        self.assertEqual(self.roles, ["extract"])

    def test_program_then_domain_then_program_refresh_keeps_the_old_request(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            old_rows = self.failed()
            advance_engine(self.engine)
            self.prepare(refresh=True)
        self.second_engine_upgrade()
        self.prepare(refresh=True)
        self.second_engine_upgrade()
        self.prepare(refresh=True)
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.attempts()[0]["attempts"][:1], old_rows)
        self.assertEqual(self.roles, ["extract", "extract", "review"])
        proof = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=self.paths)
        self.assertEqual(verify_group_publication(self.repo, proof["run_id"], historical=True)["commit_sha"], proof["commit_sha"])

    def test_domain_refresh_after_an_intervening_failure_keeps_earlier_program_witness(self):
        with patch("pdm.domains.DOMAIN_POLICY_VERSION", "three-domains-v1"):
            old_rows = self.failed()
            advance_engine(self.engine)
            self.prepare(refresh=True)
            self.failure_mode = "temporal"
            self.assertEqual(self.run_slice()["status"], "failed")
            self.failure_mode = None
        previous = deepcopy(self.attempts()[0]["attempts"])
        self.second_engine_upgrade()
        self.prepare(refresh=True)
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        self.assertEqual(self.attempts()[0]["attempts"][:2], previous)
        self.assertEqual(self.attempts()[0]["attempts"][:1], old_rows)
        self.assertEqual(self.roles, ["extract", "extract", "extract", "review"])


if __name__ == "__main__":
    unittest.main()
