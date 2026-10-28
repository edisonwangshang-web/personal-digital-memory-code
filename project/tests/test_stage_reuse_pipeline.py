"""Adopt a fixed unreviewed stage across an engine upgrade using real Git/audit."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_production
import test_skill_quality_production
from engine_fixtures import advance_engine, isolated_engine
from pdm.batch_groups import verify_group_publication
from pdm.budget import BudgetedModelCall
from pdm.contracts import CaptureError, parse_markdown
from pdm.model_calls import MAX_INPUT_BYTES, ModelCallError, request_binding
from pdm.pipeline import (_hash, _load, _read_reviewed, process_sources, resume_decomposed_processing)
from pdm.production import _save, prepare_production, run_production
from pdm.transaction import PipelineGit


class StageReusePipelineTests(unittest.TestCase):
    state = test_production.ProductionTests.state
    attempts = test_production.ProductionTests.attempts
    sources = test_skill_quality_production.SkillQualityProductionTests.sources
    assert_no_formal = test_production.ProductionTests.assert_no_formal

    def setUp(self):
        test_production.ProductionTests.setUp(self)
        self.code = isolated_engine(self)
        self.paths = self.sources(1)
        self.fail_extracts = 0
        self.review_timeout = False

    def transport(self, role, payload, timeout=120):
        result = test_skill_quality_production.SkillQualityProductionTests.transport(self, role, payload, timeout)
        if role == "extract" and self.fail_extracts:
            self.fail_extracts -= 1
            result["output"]["objects"] = []
        if role == "review" and self.review_timeout:
            raise ModelCallError("timeout", result["invocation_id"], 1.0)
        return result

    def prepare(self, name):
        result = prepare_production(self.repo, name, self.paths, use_default_skills=True,
                                    review_strategy="decomposed-v1")
        self.assertEqual(result["status"], "ready", result)
        return result

    def run_queue(self, name, *, steps=1):
        return run_production(self.repo, name, model_call=self.adapter, max_steps=steps)

    def one_work(self):
        values = self.attempts()
        self.assertEqual(len(values), 1, values)
        return values[0]

    def block_original(self, *, prior_failures=0):
        self.prepare("old")
        self.fail_extracts = prior_failures
        for _ in range(prior_failures):
            self.assertEqual(self.run_queue("old")["status"], "failed")
        # Exercise the actual old immutable plan and its pre-launch capacity
        # exit; no model response, audit role or counter is fabricated.
        with patch("pdm.decomposed_review._source_contract", return_value=False), \
                patch("pdm.decomposed_review._input_bytes", return_value=MAX_INPUT_BYTES + 1):
            result = self.run_queue("old")
        self.assertEqual(result["status"], "blocked_capacity", result)
        row = deepcopy(self.one_work()["attempts"][-1])
        self.assertEqual(row["status"], "blocked_capacity")
        self.assertEqual(self.roles, ["extract"] * (prior_failures + 1))
        operation = self.ledger.operation(row["operation_id"])
        self.assertEqual(set(operation["calls"]), {"extract"})
        self.assertEqual(operation["calls"]["extract"]["status"], "completed")
        stage_path = "memory/staging/" + row["run_id"] + ".md"
        plan_path = "memory/runs/processes/" + row["run_id"] + "/decomposed-plan.md"
        self.old = {"row": row, "candidate": _load((self.repo / stage_path).read_bytes(), "candidate"),
                    "protected": {path: (self.repo / path).read_bytes() for path in
                        (stage_path, plan_path, "memory/runs/processes/queues/old/state.md")},
                    "audit": deepcopy(operation),
                    "payload": deepcopy(next(payload for role, payload in reversed(self.payloads) if role == "extract"))}
        self.assert_no_formal()
        return self.old

    def upgrade_target(self):
        advance_engine(self.code)
        return self.prepare("new")

    def assert_old_preserved(self):
        self.assertEqual(self.one_work()["attempts"][self.old["row"]["attempt"] - 1], self.old["row"])
        self.assertEqual({path: (self.repo / path).read_bytes() for path in self.old["protected"]},
                         self.old["protected"])
        self.assertEqual(self.ledger.operation(self.old["row"]["operation_id"]), self.old["audit"])

    def adopted_rejection(self, *, rejections=1):
        self.block_original(prior_failures=1)
        self.upgrade_target()
        remaining = [rejections]

        def transport(role, payload, timeout=120):
            response = self.transport(role, payload, timeout=timeout)
            if role == "extract" and "revision_context" in payload:
                for record in response["output"]["objects"]:
                    record["body"] += " This observation is limited to the supplied artificial source."
                for topic in response["output"]["wikis"]:
                    topic["body"] += " Only the selected artificial source is summarized here."
            if role == "review" and remaining[0]:
                remaining[0] -= 1
                response["output"].update(verdict="needs_revision",
                    reasons=["Clarify the exact supplied-source boundary in the current record."],
                    repair_scope={"kind": "content", "missing_bindings": []})
                response["output"]["checks"][0].update(result="needs_revision",
                    reason="The current record needs an explicit supplied-source boundary.")
            return response

        self.adapter = BudgetedModelCall(self.ledger, "artificial-adopted-content-repair", transport)
        result = self.run_queue("new")
        self.assertEqual(result["status"], "needs_revision", result)
        candidate, review, _ = _read_reviewed(PipelineGit(self.repo), result["active_run_id"],
                                             historical=True, allow_nonpassed=True)
        self.assertIn("stage_reuse_ref", candidate)
        self.assertEqual(review["output"]["verdict"], "needs_revision")
        self.assertEqual([row["attempt"] for row in self.one_work()["attempts"]], [1, 2, 3])
        self.assertEqual(self.roles.count("extract"), 2)
        return result, candidate, review

    def test_engine_upgrade_adopts_next_slot_without_extract_and_rebuilds_publication_history(self):
        self.block_original()
        prepared = self.upgrade_target()
        result = self.run_queue("new", steps=8)
        self.assertEqual(result["status"], "published", result)
        rows = self.one_work()["attempts"]
        self.assertEqual([row["attempt"] for row in rows], [1, 2])
        self.assertEqual(rows[0], self.old["row"])
        self.assertEqual(self.roles.count("extract"), 1)
        self.assertGreater(self.roles.count("review"), 0)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), result["final_run"], historical=True)
        self.assertIn("stage_reuse_ref", candidate)
        self.assertEqual(candidate["extraction"], self.old["candidate"]["extraction"])
        self.assertEqual(candidate["extraction_request_binding"], request_binding("extract", self.old["payload"]))
        ref = candidate["stage_reuse_ref"]
        self.assertTrue((self.repo / ref["path"]).is_file())
        metadata, _ = parse_markdown((self.repo / ref["path"]).read_bytes())
        self.assertEqual(metadata, {"kind": "stage_extraction_reuse", "schema_version": "0.19",
                                    "run_id": candidate["run_id"]})
        witness = _load((self.repo / ref["path"]).read_bytes(), "stage_extraction_reuse", "0.19")["witness"]
        self.assertEqual(witness["original_request_binding"], candidate["extraction_request_binding"])
        self.assertNotIn("original_payload", witness)
        self.assertNotIn("response", witness)
        self.assertNotIn("candidate", witness)
        self.assertEqual(candidate["batch_context"]["plan_id"], prepared["plan_id"])
        self.assertNotEqual(candidate["versions"], self.old["candidate"]["versions"])
        self.assert_old_preserved()
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number
        proof = verify_group_publication(self.repo, result["publication"]["run_id"], source_paths=self.paths)
        self.assertEqual(len(proof["files"]), 2)
        with tempfile.TemporaryDirectory(prefix="pdm-stage-reuse-recovery-") as temporary:
            recovered = Path(temporary) / "recovered"
            PipelineGit(self.repo).run("clone", "--no-hardlinks", str(self.repo), str(recovered))
            restored = verify_group_publication(recovered, result["publication"]["run_id"],
                                                source_paths=self.paths, historical=True)
            self.assertEqual(restored["files"], proof["files"])
            self.assertEqual(restored["commit_sha"], proof["commit_sha"])
        self.assertEqual(self.run_queue("new", steps=8)["status"], "published")
        self.assertEqual((PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number), before)

    def test_live_source_extract_audit_must_remain_completed_before_adoption(self):
        self.block_original()
        self.upgrade_target()
        original_operation = self.ledger.operation

        def changed(operation_id):
            value = original_operation(operation_id)
            if operation_id == self.old["row"]["operation_id"]:
                value["calls"]["extract"]["status"] = "unknown"
            return value

        before = self.number, self.ledger.path.read_bytes(), deepcopy(self.one_work())
        with patch.object(self.ledger, "operation", side_effect=changed):
            try:
                result = self.run_queue("new", steps=8)
            except CaptureError:
                pass
            else:
                self.assertIn(result["status"], {"blocked_context", "blocked_capacity", "unknown"}, result)
        self.assertEqual((self.number, self.ledger.path.read_bytes(), self.one_work()), before)
        self.assert_no_formal()

    def test_any_original_review_start_prevents_new_stage_adoption(self):
        self.block_original()
        self.review_timeout = True
        original = self.old["row"]
        resumed = resume_decomposed_processing(self.repo, original["run_id"], allowed_source_paths=self.paths,
            model_call=self.adapter.for_operation(original["operation_id"]))
        self.assertEqual(resumed["status"], "unknown", resumed)
        self.assertEqual(self.roles, ["extract", "review"])
        self.review_timeout = False
        self.upgrade_target()
        before = self.number, self.ledger.path.read_bytes(), deepcopy(self.one_work())
        try:
            result = self.run_queue("new", steps=8)
        except CaptureError:
            pass
        else:
            self.assertIn(result["status"], {"blocked_context", "blocked_capacity", "unknown"}, result)
        self.assertEqual((self.number, self.ledger.path.read_bytes(), self.one_work()), before)
        self.assert_no_formal()

    def test_four_used_slots_cannot_gain_a_review_only_fifth_attempt(self):
        self.block_original(prior_failures=3)
        self.upgrade_target()
        before = self.number, self.ledger.path.read_bytes(), deepcopy(self.one_work())
        result = self.run_queue("new", steps=8)
        self.assertEqual(result["status"], "attempt_limit", result)
        self.assertEqual((self.number, self.ledger.path.read_bytes(), self.one_work()), before)
        self.assert_old_preserved()
        self.assert_no_formal()

    def test_changed_adoption_sidecar_cannot_publish_after_review(self):
        self.block_original()
        self.upgrade_target()
        reviewed = self.run_queue("new")
        self.assertEqual(reviewed["status"], "ready", reviewed)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), reviewed["final_run"], historical=True)
        ref = candidate["stage_reuse_ref"]
        path = ref["path"]
        original = (self.repo / path).read_bytes()
        (self.repo / path).write_bytes(original + b"\n")
        git = PipelineGit(self.repo)
        git.run("add", "--", path)
        git.run("commit", "-m", "artificial adoption sidecar tampering")
        before = self.number, self.ledger.path.read_bytes(), deepcopy(self.one_work())
        with self.assertRaises(CaptureError):
            self.run_queue("new", steps=8)
        self.assertEqual((self.number, self.ledger.path.read_bytes(), self.one_work()), before)
        self.assert_no_formal()

    def test_unstarted_adoption_reservation_resumes_the_same_second_slot(self):
        self.block_original()
        self.upgrade_target()
        interrupted = []

        def stop_before_stage(repo, paths, **options):
            if options.get("_preflight_only"):
                return process_sources(repo, paths, **options)
            interrupted.append(deepcopy(options["stage_reuse"]))
            raise KeyboardInterrupt("artificial adopted-stage reservation before any Review")

        with patch("pdm.pipeline.process_sources", side_effect=stop_before_stage):
            with self.assertRaises(KeyboardInterrupt):
                self.run_queue("new")
        self.assertEqual(len(interrupted), 1)
        rows = deepcopy(self.one_work()["attempts"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["status"], "reserved")
        self.assertIn("stage_reuse", rows[1])
        operation = self.ledger.operation(rows[1]["operation_id"])
        self.assertTrue(operation is None or not operation["calls"])
        result = self.run_queue("new", steps=8)
        self.assertEqual(result["status"], "published", result)
        restored = self.one_work()["attempts"]
        self.assertEqual(len(restored), 2)
        for key in ("run_id", "operation_id", "attempt", "stage_reuse", "stage_reuse_sha256"):
            self.assertEqual(restored[1][key], rows[1][key])
        self.assertEqual(self.roles.count("extract"), 1)
        self.assert_old_preserved()

    def test_adopted_prelaunch_review_failure_resumes_same_slot_without_extract(self):
        self.block_original()
        advance_engine(self.code)
        prepared = prepare_production(self.repo, "new", self.paths, use_default_skills=True,
                                      review_strategy="auto")
        self.assertEqual(prepared["status"], "ready", prepared)
        adapter = BudgetedModelCall(self.ledger, "artificial-adopted-prelaunch-worker")
        prelaunch = []

        def transport(role, payload, timeout=120, on_start=None):
            if role == "review" and not prelaunch:
                prelaunch.append(deepcopy(payload))
                raise ModelCallError("codex_not_found", "model-" + "f" * 32)
            on_start("model-" + f"{self.number + 1:032x}")
            return self.transport(role, payload, timeout=timeout)

        with patch("pdm.model_calls._call_transport", side_effect=transport):
            first = run_production(self.repo, "new", model_call=adapter, max_steps=1)
            self.assertEqual(first["status"], "failed", first)
            rows = deepcopy(self.one_work()["attempts"])
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["status"], "failed")
            self.assertEqual(rows[1]["error"], "codex_not_found")
            self.assertEqual(self.roles, ["extract"])
            self.assertNotIn("review", self.ledger.operation(rows[1]["operation_id"])["calls"])
            stage_path = "memory/staging/" + rows[1]["run_id"] + ".md"
            original = (self.repo / stage_path).read_bytes()
            result = run_production(self.repo, "new", model_call=adapter, max_steps=8)
        self.assertEqual(result["status"], "published", result)
        current = self.one_work()["attempts"]
        self.assertEqual(len(current), 2)
        self.assertEqual((current[1]["run_id"], current[1]["operation_id"], current[1]["attempt"]),
                         (rows[1]["run_id"], rows[1]["operation_id"], 2))
        self.assertEqual((self.repo / stage_path).read_bytes(), original)
        self.assertEqual(self.roles.count("extract"), 1)
        self.assertEqual(len(prelaunch), 1)
        self.assert_old_preserved()

    def test_adopted_auto_review_that_really_started_cannot_switch_to_child_replay(self):
        self.block_original()
        advance_engine(self.code)
        prepared = prepare_production(self.repo, "new", self.paths, use_default_skills=True,
                                      review_strategy="auto")
        self.assertEqual(prepared["status"], "ready", prepared)
        adapter = BudgetedModelCall(self.ledger, "artificial-adopted-started-worker")
        self.review_timeout = True

        def transport(role, payload, timeout=120, on_start=None):
            on_start("model-" + f"{self.number + 1:032x}")
            return self.transport(role, payload, timeout=timeout)

        with patch("pdm.model_calls._call_transport", side_effect=transport):
            first = run_production(self.repo, "new", model_call=adapter, max_steps=1)
            self.assertEqual(first["status"], "unknown", first)
            rows = deepcopy(self.one_work()["attempts"])
            self.assertEqual(len(rows), 2)
            call = deepcopy(self.ledger.operation(rows[1]["operation_id"])["calls"]["review"])
            self.assertEqual((call["status"], call["error_code"]), ("unknown", "timeout"))
            before = self.number, self.ledger.path.read_bytes()
            resumed = run_production(self.repo, "new", model_call=adapter, max_steps=8)
        self.assertEqual(resumed["status"], "unknown", resumed)
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before)
        current = self.one_work()["attempts"]
        self.assertEqual(len(current), 2)
        self.assertEqual(current[0], rows[0])
        for key in ("run_id", "operation_id", "attempt", "stage_reuse", "stage_reuse_sha256", "status"):
            self.assertEqual(current[1][key], rows[1][key])
        self.assertEqual(self.roles, ["extract", "review"])
        self.assertEqual(self.ledger.operation(rows[1]["operation_id"])["calls"]["review"], call)
        self.assert_no_formal()

    def test_adopted_review_rejection_repairs_in_fourth_slot_and_preserves_original_proof(self):
        rejected, adopted, old_review = self.adopted_rejection()
        prefix = deepcopy(self.one_work()["attempts"])
        source_bytes = {path: (self.repo / path).read_bytes() for path in self.paths}
        audit_prefix = {row["operation_id"]: deepcopy(self.ledger.operation(row["operation_id"])) for row in prefix}
        protected = {path: (self.repo / path).read_bytes() for path in
                     (rejected["last_result"]["stage_path"], rejected["last_result"]["review_path"])}
        repaired = self.run_queue("new")
        self.assertEqual(repaired["status"], "ready", repaired)
        rows = self.one_work()["attempts"]
        self.assertEqual(rows[:3], prefix)
        self.assertEqual([row["attempt"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(rows[3]["revision_from_run"], adopted["run_id"])
        self.assertNotIn("stage_reuse", rows[3])
        self.assertEqual(self.roles.count("extract"), 3)
        request = [payload for role, payload in self.payloads if role == "extract"][-1]
        self.assertEqual(request["revision_context"]["source_run_id"], adopted["run_id"])
        unresolved = deepcopy(old_review["output"])
        unresolved["checks"] = [check for check in unresolved["checks"]
                                if check["result"] in {"needs_revision", "needs_confirmation"}]
        self.assertEqual(request["revision_context"]["review_record"]["output"], unresolved)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), repaired["final_run"])
        self.assertNotIn("stage_reuse_ref", candidate)
        self.assertEqual(candidate["revision_context"]["candidate"]["binding"], adopted["binding"])
        published = self.run_queue("new", steps=8)
        self.assertEqual(published["status"], "published", published)
        proof = verify_group_publication(self.repo, published["publication"]["run_id"], source_paths=self.paths)
        self.assertEqual(len(proof["files"]), 2)
        old, review, _ = _read_reviewed(PipelineGit(self.repo), adopted["run_id"], historical=True,
                                       allow_nonpassed=True)
        self.assertEqual((old, review), (adopted, old_review))
        self.assertEqual({path: (self.repo / path).read_bytes() for path in protected}, protected)
        self.assertEqual({path: (self.repo / path).read_bytes() for path in source_bytes}, source_bytes)
        self.assertEqual({key: self.ledger.operation(key) for key in audit_prefix}, audit_prefix)
        self.assert_old_preserved()

    def test_rejected_adoption_witness_cannot_change_source_hash_or_target_before_repair(self):
        self.adopted_rejection()
        original = deepcopy(self.one_work())
        path = original["attempts"][2]["stage_reuse"]["snapshots"]["counter"]["path"]
        for field in ("source", "hash", "target"):
            with self.subTest(field=field):
                changed = deepcopy(original)
                row = changed["attempts"][2]
                if field == "source":
                    row["stage_reuse"]["source_run_id"] = changed["attempts"][0]["run_id"]
                    row["stage_reuse_sha256"] = _hash(row["stage_reuse"])
                elif field == "target":
                    row["stage_reuse"]["target"]["attempt"] = 4
                    row["stage_reuse_sha256"] = _hash(row["stage_reuse"])
                else:
                    row["stage_reuse_sha256"] = "sha256-" + "0" * 64
                git = PipelineGit(self.repo)
                with git.lock():
                    _save(git, path, changed, "production_attempts", git.check())
                before = self.number, self.ledger.path.read_bytes(), deepcopy(self.one_work())
                try:
                    result = self.run_queue("new")
                except CaptureError as error:
                    self.assertEqual(error.code, "stage_reuse_attempt_mismatch")
                else:
                    self.assertEqual(result["status"], "blocked_context", result)
                    self.assertEqual(result["last_result"]["error"], "stage_reuse_attempt_mismatch")
                self.assertEqual((self.number, self.ledger.path.read_bytes(), self.one_work()), before)
                self.assert_no_formal()

    def test_adopted_rejection_and_fourth_slot_rejection_cannot_open_fifth_slot(self):
        _, adopted, _ = self.adopted_rejection(rejections=2)
        prefix = deepcopy(self.one_work()["attempts"])
        final = self.run_queue("new")
        self.assertEqual(final["status"], "attempt_limit", final)
        rows = self.one_work()["attempts"]
        self.assertEqual(rows[:3], prefix)
        self.assertEqual([row["attempt"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(rows[3]["status"], "needs_revision")
        self.assertEqual(rows[3]["revision_from_run"], adopted["run_id"])
        self.assertNotIn("stage_reuse", rows[3])
        self.assertEqual(self.roles.count("extract"), 3)
        before = self.number, self.ledger.path.read_bytes(), deepcopy(self.one_work())
        self.assertEqual(self.run_queue("new", steps=8)["status"], "attempt_limit")
        self.assertEqual((self.number, self.ledger.path.read_bytes(), self.one_work()), before)
        self.assert_old_preserved()
        self.assert_no_formal()

    def test_new_engine_target_repairs_rejected_adoption_in_the_same_work_fourth_slot(self):
        rejected, adopted, old_review = self.adopted_rejection()
        original = deepcopy(self.one_work())
        prefix = original["attempts"]
        witness = prefix[2]["stage_reuse"]
        work_id = witness["target"]["work_id"]
        counter_path = witness["snapshots"]["counter"]["path"]
        old_queue = self.repo / "memory/runs/processes/queues/new/state.md"
        old_queue_bytes = old_queue.read_bytes()
        upgrade = self.code / "project/src/pdm/fixture_repair_upgrade.py"
        upgrade.write_text("# Artificial repair-engine upgrade; source and rules are unchanged.\n")
        engine = PipelineGit(self.code)
        engine.run("add", "--", str(upgrade.relative_to(self.code)))
        engine.run("commit", "-m", "artificial third engineering version")
        prepared = prepare_production(self.repo, "repair-target", self.paths, use_default_skills=True,
                                      review_strategy="decomposed-v1")
        self.assertEqual(prepared["status"], "ready", prepared)
        self.assertNotEqual(prepared["plan_id"], adopted["batch_context"]["plan_id"])
        repaired = self.run_queue("repair-target")
        self.assertEqual(repaired["status"], "ready", repaired)
        current = self.one_work()
        self.assertEqual(current["identity"], original["identity"])
        self.assertEqual(current["attempts"][:3], prefix)
        self.assertEqual(len(current["attempts"]), 4)
        last = current["attempts"][3]
        self.assertEqual(last["operation_id"], "batch:" + work_id + ":4")
        self.assertEqual(last["revision_from_run"], adopted["run_id"])
        self.assertNotIn("stage_reuse", last)
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                                   "memory/runs/processes/queue-attempts"), counter_path)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), repaired["final_run"])
        self.assertEqual(candidate["batch_context"]["plan_id"], prepared["plan_id"])
        self.assertNotEqual(candidate["versions"], adopted["versions"])
        self.assertEqual(candidate["revision_context"]["source_run_id"], adopted["run_id"])
        self.assertEqual(candidate["revision_context"]["review_record"]["output"], old_review["output"])
        self.assertEqual(self.roles.count("extract"), 3)
        published = self.run_queue("repair-target", steps=8)
        self.assertEqual(published["status"], "published", published)
        proof = verify_group_publication(self.repo, published["publication"]["run_id"], source_paths=self.paths)
        self.assertEqual(len(proof["files"]), 2)
        old, review, _ = _read_reviewed(PipelineGit(self.repo), adopted["run_id"], historical=True,
                                       allow_nonpassed=True)
        self.assertEqual((old, review), (adopted, old_review))
        self.assertEqual(old_queue.read_bytes(), old_queue_bytes)
        self.assert_old_preserved()


if __name__ == "__main__":
    unittest.main()
