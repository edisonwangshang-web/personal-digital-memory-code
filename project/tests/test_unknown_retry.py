"""Bounded extract-timeout retries use artificial data and real local audit/Git."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from engine_fixtures import advance_engine, isolated_engine
import test_production
import test_skill_quality_production
from pdm.batch_groups import _marker_path, publish_final_group
from pdm.budget import BudgetBlocked, BudgetLedger, BudgetedModelCall
from pdm.capture import capture
from pdm.compatibility import SUPPORTED_SCHEMA_VERSIONS
from pdm.contracts import CaptureError, canonical, markdown, parse_markdown
from pdm.controls import record_control
from pdm.model_calls import ModelCallError
from pdm.pipeline import _committed, _hash as pipeline_hash, _load, _read_reviewed, process_sources, publish
import pdm.production as production
from pdm.production import prepare_production, run_production
from pdm.rebuild import prepare_rebuild
from pdm.transaction import PipelineGit


class ExtractTimeoutRetryTests(unittest.TestCase):
    state = test_production.ProductionTests.state
    attempts = test_production.ProductionTests.attempts
    sources = test_skill_quality_production.SkillQualityProductionTests.sources

    def setUp(self):
        test_production.ProductionTests.setUp(self)
        self.code = isolated_engine(self)
        self.paths = self.sources(1)
        self.queue = "automatic-timeouts"
        self.failure_mode = "timeout"
        self.responses = []
        self.feedback_chain = False

    def transport(self, role, payload, timeout=120):
        result = test_skill_quality_production.SkillQualityProductionTests.transport(self, role, payload, timeout)
        self.responses.append((role, deepcopy(result)))
        if self.feedback_chain:
            if role == "extract":
                if self.roles.count("extract") in {1, 3}:
                    raise ModelCallError("timeout", result["invocation_id"], 1.0)
                if "revision_context" in payload:
                    previous = payload["revision_context"]["candidate"]["objects"]
                    records = {obj["metadata"]["id"]: obj for obj in previous
                               if obj["metadata"]["type"] != "wiki"}
                    for record in result["output"]["objects"]:
                        record["body"] = records[record["id"]]["body"] + " This observation is limited to its supplied artificial source."
                    for topic in result["output"]["wikis"]:
                        topic["body"] += " This topic covers only the supplied artificial source."
            elif self.roles.count("review") == 1:
                result["output"].update(verdict="needs_revision",
                    reasons=["Clarify the exact supplied-source scope before publication."],
                    repair_scope={"kind": "content", "missing_bindings": []})
                result["output"]["checks"][0].update(result="needs_revision",
                    reason="The exact supplied-source scope needs clarification.")
            return result
        mode = self.failure_mode
        if role == "extract" and mode in {"timeout", "cli_turn_failed", "cli_permission_denied", "cli_auth_required"}:
            # BudgetedModelCall performs its real start/finish accounting. Only
            # the external provider is replaced; no audit state is hand-edited.
            raise ModelCallError(mode, result["invocation_id"], 1.0)
        if role == "extract" and mode == "known_failure":
            result["output"]["objects"] = []
        if role == "review" and mode == "review_timeout":
            raise ModelCallError("timeout", result["invocation_id"], 1.0)
        return result

    def prepare(self, name=None, *, paths=None, rebuild=None, legacy=False):
        name = name or self.queue
        selected = paths or self.paths
        options = {"use_default_skills": True}
        if rebuild is not None:
            options["rebuild_context"] = rebuild
        if not legacy:
            result = prepare_production(self.repo, name, selected, **options)
        else:
            original = production._save

            def save(git, path, value, kind, head):
                # An explicit compatibility fixture: its first stored request
                # has the layout emitted before the timeout policy existed.
                if kind == "production_queue" and value["queue_id"] == name and git._existing(path) is None:
                    value = deepcopy(value)
                    value["request"].pop("timeout_retry_policy", None)
                return original(git, path, value, kind, head)

            with patch("pdm.production._save", side_effect=save):
                result = prepare_production(self.repo, name, selected, **options)
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(self.state(name)["request"].get("timeout_retry_policy"),
                         None if legacy else "bounded-timeout-v1")
        return result

    def run_slice(self, name=None, *, adapter=None, steps=1):
        return run_production(self.repo, name or self.queue,
                              model_call=adapter or self.adapter, max_steps=steps)

    def accounting(self):
        return self.number, self.ledger.path.read_bytes(), deepcopy(self.attempts())

    def one_work(self):
        rows = self.attempts()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def assert_no_formal(self):
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                                   "memory/records", "memory/topics"), "")

    def assert_retired(self, action):
        with self.assertRaises(CaptureError) as raised:
            action()
        self.assertEqual(raised.exception.code, "unknown_retry_retired_run")

    def test_four_timeouts_stop_the_task_and_preserve_all_four_unknown_calls(self):
        self.prepare()
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "attempt_limit", result)
        rows = self.one_work()["attempts"]
        self.assertEqual([row["attempt"] for row in rows], [1, 2, 3, 4])
        self.assertEqual([row["status"] for row in rows], ["unknown"] * 4)
        self.assertEqual(self.roles, ["extract"] * 4)
        self.assertEqual(self.ledger.snapshot()["started_calls"], 4)
        for row in rows:
            call = self.ledger.operation(row["operation_id"])["calls"]["extract"]
            self.assertEqual((call["status"], call["error_code"]), ("unknown", "timeout"))
        before = self.accounting()
        self.assertEqual(self.run_slice(steps=8)["status"], "attempt_limit")
        self.assertEqual(self.accounting(), before)
        self.assert_no_formal()

    def test_successful_successor_links_the_receipt_and_publishes_only_once(self):
        self.prepare()
        first = self.run_slice()
        self.assertEqual((first["status"], first["last_result"]["retryable"], first["last_result"]["call_outcome"]),
                         ("failed", True, "unknown"))
        old = deepcopy(self.one_work()["attempts"][0])
        original_audit = self.ledger.snapshot()["operations"][old["operation_id"]]
        original_files = {name: (self.repo / f"memory/runs/processes/{old['run_id']}/{name}.md").read_bytes()
                          for name in ("started", "engine", "failure")}
        git = PipelineGit(self.repo)
        tracked = git.text("ls-tree", "-r", "--name-only", "HEAD").splitlines()
        protected = git.head(), {path: (self.repo / path).read_bytes() for path in tracked}, self.accounting()
        old_formats = tuple(value for value in SUPPORTED_SCHEMA_VERSIONS if value != "0.16")
        with patch("pdm.compatibility.SUPPORTED_SCHEMA_VERSIONS", old_formats):
            with self.assertRaises(CaptureError) as incompatible:
                capture(self.repo, {"source_key": "artificial:old-writer-after-timeout", "source_type": "note",
                    "acquisition_status": "full_text", "body": "Artificial incompatible writer sample.\n"})
        self.assertEqual(incompatible.exception.code, "incompatible_memory_schema")
        self.assertEqual((git.head(), {path: (self.repo / path).read_bytes() for path in tracked}, self.accounting()),
                         protected)
        self.failure_mode = None
        ready = self.run_slice()
        self.assertEqual(ready["status"], "ready", ready)
        rows = self.one_work()["attempts"]
        self.assertEqual(rows[0], old)
        self.assertEqual(rows[1]["attempt"], 2)
        ref = rows[1]["timeout_retry_ref"]
        self.assertEqual(set(ref), {"path", "commit_sha", "sha256"})
        metadata, body = parse_markdown((self.repo / ref["path"]).read_bytes())
        self.assertEqual(metadata["schema_version"], "0.16")
        self.assertIn(old["run_id"], body)
        self.assertIn(old["operation_id"], body)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), ready["final_run"])
        started = _load(_committed(PipelineGit(self.repo),
            f"memory/runs/processes/{ready['final_run']}/started.md"), "processing_attempt")
        self.assertEqual(candidate["timeout_retry_ref"], ref)
        self.assertEqual(started["timeout_retry_ref"], ref)
        for _, payload in self.payloads:
            self.assertNotIn('"timeout_retry_ref"', json.dumps(payload))
        self.assertEqual(self.ledger.snapshot()["operations"][old["operation_id"]], original_audit)
        self.assertEqual(original_files, {name: (self.repo / f"memory/runs/processes/{old['run_id']}/{name}.md").read_bytes()
                                         for name in original_files})
        before_calls = self.number, self.ledger.path.read_bytes()
        published = self.run_slice()
        self.assertEqual(published["status"], "published", published)
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before_calls)
        before = PipelineGit(self.repo).head(), self.accounting()
        self.assertEqual(self.run_slice()["status"], "published")
        self.assertEqual((PipelineGit(self.repo).head(), self.accounting()), before)
        self.assertEqual(self.roles, ["extract", "extract", "review"])
        # A removed retirement receipt is evidence loss, never a new chance to
        # recreate its decision or adopt its already-published successor.
        git.text("rm", "--", ref["path"])
        git.text("commit", "-m", "artificial deletion of a required timeout receipt")
        before = git.head(), self.accounting()
        with self.assertRaises(CaptureError):
            self.run_slice(steps=8)
        self.assertEqual((git.head(), self.accounting()), before)
        self.prepare("deleted-receipt-alias")
        before = git.head(), self.accounting()
        with self.assertRaises(CaptureError):
            self.run_slice("deleted-receipt-alias", steps=8)
        self.assertEqual(self.accounting(), before[1])
        # The first worker may bind this alias to the audit pool. It may not
        # change the source counter, receipt, knowledge or original evidence.
        self.assertEqual(git.text("diff", "--name-only", before[0], "HEAD"),
                         production._path("deleted-receipt-alias"))
        self.assertFalse((self.repo / ref["path"]).exists())

    def test_timeout_during_revision_preserves_the_current_draft_and_unresolved_review(self):
        self.feedback_chain = True
        self.prepare()
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        rows = self.one_work()["attempts"]
        self.assertEqual([row["attempt"] for row in rows], [1, 2, 3, 4])
        self.assertEqual([row["status"] for row in rows], ["unknown", "needs_revision", "unknown", "passed"])
        self.assertEqual(self.roles, ["extract", "extract", "review", "extract", "extract", "review"])
        for row in (rows[0], rows[2]):
            call = self.ledger.operation(row["operation_id"])["calls"]["extract"]
            self.assertEqual((call["status"], call["error_code"]), ("unknown", "timeout"))
        extracts = [payload for role, payload in self.payloads if role == "extract"]
        for payload in extracts[2:]:
            feedback = payload["revision_context"]
            self.assertEqual(feedback["source_run_id"], rows[1]["run_id"])
            self.assertEqual(feedback["review_record"]["output"]["verdict"], "needs_revision")
            self.assertEqual(feedback["review_record"]["output"]["reasons"],
                ["Clarify the exact supplied-source scope before publication."])
            self.assertTrue(feedback["candidate"]["objects"])
        for _, payload in self.payloads:
            self.assertNotIn('"timeout_retry_ref"', json.dumps(payload))
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), rows[3]["run_id"])
        self.assertEqual(candidate["timeout_retry_ref"], rows[3]["timeout_retry_ref"])
        self.assertEqual(candidate["revision_context"]["source_run_id"], rows[1]["run_id"])
        before = PipelineGit(self.repo).head(), self.accounting()
        self.assertEqual(self.run_slice(steps=8)["status"], "published")
        self.assertEqual((PipelineGit(self.repo).head(), self.accounting()), before)

    def test_restart_queue_and_worker_changes_do_not_reset_timeout_attempts(self):
        self.prepare()
        original = None
        for number in range(1, 5):
            if number == 2:
                self.prepare("renamed")
                before = self.accounting()
                with self.assertRaises(CaptureError):
                    self.run_slice("renamed")
                self.assertEqual(self.accounting(), before)
                self.adapter = BudgetedModelCall(self.ledger, "other-worker", self.transport)
            result = self.run_slice()
            rows = self.one_work()["attempts"]
            self.assertEqual(len(rows), number)
            self.assertEqual(rows[-1]["attempt"], number)
            if original is None:
                original = deepcopy(rows[0])
            self.assertEqual(rows[0], original)
            self.assertEqual(result["status"], "failed" if number < 4 else "attempt_limit", result)
        before = self.accounting()
        self.prepare("third-label")
        self.assertEqual(self.run_slice("third-label", steps=8)["status"], "attempt_limit")
        self.assertEqual(self.accounting(), before)

    def test_source_deletion_after_timeout_blocks_the_receipt_without_a_new_call(self):
        self.prepare()
        self.run_slice()
        record_control(self.repo, {"request_key": "artificial:delete-before-timeout-retry", "action": "delete",
                                   "source_paths": self.paths})
        self.failure_mode = None
        before = self.accounting()
        with self.assertRaises(CaptureError):
            self.run_slice(steps=8)
        self.assertEqual(self.accounting(), before)
        self.assert_no_formal()

    def test_non_timeout_unknowns_and_permission_failures_are_not_replayed(self):
        for index, code in enumerate(("cli_turn_failed", "cli_permission_denied", "cli_auth_required")):
            with self.subTest(error=code):
                path = capture(self.repo, {"source_key": "artificial:unsafe-unknown:" + code,
                    "source_type": "note", "acquisition_status": "full_text",
                    "body": "Artificial observation for " + code + ".\n"})["path"]
                name = "blocked-" + str(index)
                self.prepare(name, paths=[path])
                self.failure_mode = code
                first = self.run_slice(name, steps=8)
                self.assertEqual(first["status"], "unknown", first)
                before = self.accounting()
                self.failure_mode = None
                again = self.run_slice(name, steps=8)
                self.assertEqual(again["status"], "unknown", again)
                self.assertEqual(self.accounting(), before)

    def test_review_timeout_is_not_treated_as_an_unstaged_extract_retry(self):
        self.prepare()
        self.failure_mode = "review_timeout"
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(self.roles, ["extract", "review"])
        row = self.one_work()["attempts"][0]
        self.assertEqual(row["status"], "unknown")
        calls = self.ledger.operation(row["operation_id"])["calls"]
        self.assertEqual(calls["extract"]["status"], "completed")
        self.assertEqual((calls["review"]["status"], calls["review"]["error_code"]), ("unknown", "timeout"))
        failure_path = self.repo / f"memory/runs/processes/{row['run_id']}/failure.md"
        failure = failure_path.read_bytes()
        before = self.accounting()
        self.failure_mode = None
        self.assertEqual(self.run_slice(steps=8)["status"], "unknown")
        self.assertEqual(self.accounting()[:2], before[:2])
        recovered = self.one_work()["attempts"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual({key: recovered[0][key] for key in ("run_id", "operation_id", "attempt", "status")},
                         {key: row[key] for key in ("run_id", "operation_id", "attempt", "status")})
        self.assertEqual(failure_path.read_bytes(), failure)
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                                   "memory/runs/processes/timeout-retries"), "")
        self.assert_no_formal()

    def test_guarded_execution_stops_unknown_without_the_manual_timeout_policy(self):
        self.prepare()
        checkpoints = []

        def guard(repo, phase, value):
            self.assertEqual(repo, self.repo)
            checkpoints.append((phase, deepcopy(value)))

        result = run_production(self.repo, self.queue, model_call=self.adapter,
                                max_steps=8, execution_guard=guard)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(self.roles, ["extract"])
        row = self.one_work()["attempts"][0]
        self.assertEqual(row["status"], "unknown")
        call = self.ledger.operation(row["operation_id"])["calls"]["extract"]
        self.assertEqual((call["status"], call["error_code"]), ("unknown", "timeout"))
        before = self.number, self.ledger.path.read_bytes()
        self.assertEqual(run_production(self.repo, self.queue, model_call=self.adapter,
            max_steps=8, execution_guard=guard)["status"], "unknown")
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before)
        self.assertEqual(len(self.one_work()["attempts"]), 1)
        self.assertIn("before_call", [phase for phase, _ in checkpoints])
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                                   "memory/runs/processes/timeout-retries"), "")
        self.assert_no_formal()

    def test_true_started_call_and_prelaunch_budget_failure_do_not_autoretry(self):
        self.prepare()
        self.failure_mode = None
        # Simulate a process interruption after the durable launch, before its
        # finish can be written. This is genuinely started, not a timeout row.
        with patch.object(BudgetLedger, "finish", side_effect=KeyboardInterrupt("artificial interrupted finish")):
            with self.assertRaises(KeyboardInterrupt):
                self.run_slice(steps=8)
        call = next(iter(self.ledger.snapshot()["operations"].values()))["calls"]["extract"]
        self.assertEqual(call["status"], "started")
        before = self.accounting()
        self.assertEqual(self.run_slice(steps=8)["status"], "unknown")
        self.assertEqual(self.accounting(), before)
        path = capture(self.repo, {"source_key": "artificial:budget-before-launch", "source_type": "note",
            "acquisition_status": "full_text", "body": "Artificial budget gate sample.\n"})["path"]
        self.prepare("budget-blocked", paths=[path])
        before = self.number, self.ledger.path.read_bytes()
        with patch.object(BudgetLedger, "reserve", side_effect=BudgetBlocked("budget_exhausted")):
            with self.assertRaises(BudgetBlocked):
                self.run_slice("budget-blocked", steps=8)
        self.assertEqual((self.number, self.ledger.path.read_bytes()), before)

    def test_unstarted_successor_reservation_resumes_the_same_run_and_operation(self):
        self.prepare()
        self.run_slice()
        self.failure_mode = None
        original = PipelineGit.write_files
        interrupted = []
        invocation = []

        def process(repo, paths, **options):
            if not options.get("_preflight_only"):
                invocation.append((list(paths), dict(options)))
            return process_sources(repo, paths, **options)

        def write(git, files, expected_head, message):
            result = original(git, files, expected_head, message)
            for path, raw in files.items():
                if path.endswith("/started.md"):
                    value = _load(raw, "processing_attempt")
                    if value.get("timeout_retry_ref"):
                        interrupted.append((value["run_id"], deepcopy(value["timeout_retry_ref"])))
                        raise KeyboardInterrupt("artificial successor reservation interruption")
            return result

        with patch.object(PipelineGit, "write_files", write), patch("pdm.pipeline.process_sources", side_effect=process):
            with self.assertRaises(KeyboardInterrupt):
                self.run_slice()
        self.assertEqual(len(interrupted), 1)
        rows = self.one_work()["attempts"]
        self.assertEqual(len(rows), 2)
        successor = deepcopy(rows[1])
        self.assertEqual(successor["run_id"], interrupted[0][0])
        operation = self.ledger.operation(successor["operation_id"])
        self.assertTrue(operation is None or not operation["calls"])
        self.assertEqual(self.roles, ["extract"])
        self.assertEqual(len(invocation), 1)
        paths, options = invocation[0]
        altered_batch = deepcopy(options["batch_context"])
        altered_batch["parent_batch_ids"] = ["batch-" + "0" * 64]
        for field, replacement in (("timeout_retry_ref", None), ("skill_bundle", None),
                                   ("review_strategy", "legacy-v1"), ("rebuild_context", {"invalid": True}),
                                   ("batch_context", altered_batch)):
            with self.subTest(direct_library_override=field):
                changed = {**options, field: replacement, "expected_head": PipelineGit(self.repo).head()}
                before = PipelineGit(self.repo).head(), self.accounting()
                with self.assertRaises(CaptureError) as raised:
                    process_sources(self.repo, paths, **changed)
                self.assertNotEqual(raised.exception.code, "head_conflict")
                self.assertEqual((PipelineGit(self.repo).head(), self.accounting()), before)
        result = self.run_slice(steps=8)
        self.assertEqual(result["status"], "published", result)
        rows = self.one_work()["attempts"]
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]["run_id"], rows[1]["operation_id"]),
                         (successor["run_id"], successor["operation_id"]))
        self.assertEqual(self.roles, ["extract", "extract", "review"])

    def test_old_target_cannot_start_a_reserved_successor_after_an_engine_upgrade(self):
        self.prepare()
        self.run_slice()
        self.failure_mode = None
        invocation = []

        def stop_before_started(repo, paths, **options):
            if options.get("_preflight_only"):
                return process_sources(repo, paths, **options)
            invocation.append((list(paths), dict(options)))
            raise KeyboardInterrupt("artificial reservation before model launch")

        with patch("pdm.pipeline.process_sources", side_effect=stop_before_started):
            with self.assertRaises(KeyboardInterrupt):
                self.run_slice()
        self.assertEqual(len(invocation), 1)
        rows = self.one_work()["attempts"]
        self.assertEqual([row["status"] for row in rows], ["unknown", "reserved"])
        advance_engine(self.code)
        paths, options = invocation[0]
        options["expected_head"] = PipelineGit(self.repo).head()
        before = PipelineGit(self.repo).head(), self.accounting()
        with self.assertRaises(CaptureError) as rejected:
            process_sources(self.repo, paths, **options)
        self.assertNotEqual(rejected.exception.code, "head_conflict")
        self.assertEqual((PipelineGit(self.repo).head(), self.accounting()), before)
        self.assertEqual(self.roles, ["extract"])
        self.assert_no_formal()

    def test_three_known_failures_plus_last_timeout_cannot_create_a_fifth_attempt(self):
        self.prepare()
        self.failure_mode = "known_failure"
        for _ in range(3):
            self.assertEqual(self.run_slice()["status"], "failed")
        self.failure_mode = "timeout"
        fourth = self.run_slice()
        self.assertEqual(fourth["status"], "attempt_limit", fourth)
        rows = self.one_work()["attempts"]
        self.assertEqual([row["status"] for row in rows], ["failed"] * 3 + ["unknown"])
        self.assertEqual(self.roles, ["extract"] * 4)
        before = self.accounting()
        self.assertEqual(self.run_slice(steps=8)["status"], "attempt_limit")
        self.assertEqual(self.accounting(), before)

    def test_old_unmarked_queue_stays_safe_and_current_target_preserves_same_work(self):
        self.prepare("legacy", legacy=True)
        first = self.run_slice("legacy")
        self.assertEqual(first["status"], "unknown", first)
        old_row = deepcopy(self.one_work()["attempts"][0])
        old_audit = deepcopy(self.ledger.snapshot()["operations"])
        before = self.accounting()
        # Re-preparing an old queue never silently enables the newer policy.
        reused = prepare_production(self.repo, "legacy", self.paths, use_default_skills=True)
        self.assertTrue(reused["reused"])
        self.assertNotIn("timeout_retry_policy", self.state("legacy")["request"])
        self.assertEqual(self.run_slice("legacy")["status"], "unknown")
        self.assertEqual(self.accounting(), before)
        advance_engine(self.code)
        self.prepare("current-target")
        self.failure_mode = None
        result = self.run_slice("current-target", steps=8)
        self.assertEqual(result["status"], "published", result)
        rows = self.one_work()["attempts"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], old_row)
        self.assertEqual(rows[1]["attempt"], 2)
        self.assertEqual(self.ledger.snapshot()["operations"][old_row["operation_id"]], old_audit[old_row["operation_id"]])
        self.assertEqual(self.roles, ["extract", "extract", "review"])

    def test_cross_engine_target_keeps_the_same_rebuild_receipt_and_attempt_history(self):
        self.failure_mode = None
        self.prepare("original-published")
        published = self.run_slice("original-published", steps=8)
        self.assertEqual(published["status"], "published", published)
        context = prepare_rebuild(self.repo, "artificial timeout retry rebuild", self.paths,
            sorted(published["publication"]["files"]), expected_head=PipelineGit(self.repo).head())["rebuild_context"]
        self.prepare("legacy-fresh", rebuild=context, legacy=True)
        self.failure_mode = "timeout"
        self.assertEqual(self.run_slice("legacy-fresh")["status"], "unknown")
        unknown_work = next(row for row in self.attempts() if row["identity"].get("rebuild_context") == context)
        old = deepcopy(unknown_work["attempts"][0])
        advance_engine(self.code)
        self.prepare("current-fresh", rebuild=context)
        self.failure_mode = None
        result = self.run_slice("current-fresh", steps=8)
        self.assertEqual(result["status"], "published", result)
        matching = [row for row in self.attempts() if row["identity"].get("rebuild_context") == context]
        self.assertEqual(len(matching), 1)
        self.assertEqual(len(matching[0]["attempts"]), 2)
        self.assertEqual(matching[0]["attempts"][0], old)
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), result["final_run"])
        self.assertEqual(candidate["rebuild_context"], context)
        self.assertIn("timeout_retry_ref", candidate)

    def test_pool_and_target_plan_mismatch_stop_before_any_additional_call(self):
        self.prepare()
        self.run_slice()
        other = BudgetLedger.create(self.ledger.path.parent / "other.json", None, 0,
                                    "artificial different pool", mode="audit")
        before = self.accounting(), other.path.read_bytes()
        with self.assertRaises(CaptureError):
            self.run_slice(adapter=BudgetedModelCall(other, "worker", self.transport))
        self.assertEqual((self.accounting(), other.path.read_bytes()), before)
        git = PipelineGit(self.repo)
        state = self.state(self.queue)
        state["plan"]["plan_id"] = "plan-" + "0" * 64
        with git.lock():
            production._save(git, production._path(self.queue), state, "production_queue", git.check())
        before = self.accounting()
        with self.assertRaises(CaptureError):
            self.run_slice(steps=8)
        self.assertEqual(self.accounting(), before)

    def test_late_original_output_cannot_publish_or_be_adopted(self):
        self.prepare()
        self.run_slice()
        original = deepcopy(self.one_work()["attempts"][0])
        original_audit = deepcopy(self.ledger.snapshot()["operations"][original["operation_id"]])
        # This artificial late provider body is not a recovered candidate or a
        # claim that the original unknown call completed in the real service.
        late = self.ledger.path.parent / "artificial-late-extraction.json"
        late.write_text(json.dumps(self.responses[0][1]))
        self.failure_mode = None
        ready = self.run_slice()
        self.assertEqual(ready["status"], "ready", ready)
        git = PipelineGit(self.repo)
        candidate, _, _ = _read_reviewed(git, ready["final_run"])
        old = original["run_id"]
        # Gate fixtures deliberately lack a complete old candidate. Dedicated
        # retirement rejection must happen before candidate parsing; a mere
        # missing/invalid-stage error would not prove this boundary.
        stage = f"memory/staging/{old}.md"
        review = f"memory/runs/processes/{old}/review.md"
        with git.lock():
            git.write_files({stage: markdown({"kind": "candidate", "run_id": old, "schema_version": "0.15"},
                                canonical({"run_id": old, "source": candidate["source"],
                                           "artificial_late_gate_fixture": True}).decode() + "\n"),
                             review: markdown({"kind": "review", "run_id": old, "schema_version": "0.15"},
                                canonical({"run_id": old, "artificial_late_gate_fixture": True}).decode() + "\n")},
                            git.check(), "artificial late-result publication gate fixture")
        before = self.accounting()
        self.assert_retired(lambda: publish(self.repo, old))
        plan = self.state(self.queue)["plan"]
        self.assert_retired(lambda: publish_final_group(self.repo, plan, candidate["objects"], [old],
                                                        source_paths=self.paths))
        # A matching plan marker must not make the queue adopt a group whose
        # purported proof points back to the retired original run.
        group = "run-" + "f" * 32
        manifest_path = f"memory/runs/processes/{group}/manifest.md"
        source = deepcopy(candidate["source"])
        marker_path = _marker_path(plan)
        with git.lock():
            git.write_files({manifest_path: markdown({"kind": "batch_group_manifest", "run_id": group, "schema_version": "0.6"},
                    canonical({"run_id": group, "plan": plan, "source": source,
                               "proofs": [{"run_id": old}]}).decode() + "\n"),
                marker_path: markdown({"kind": "batch_group_inputs", "run_id": group, "schema_version": "0.6"},
                    canonical({"run_id": group, "plan_id": plan["plan_id"],
                               "plan_sha256": pipeline_hash(plan), "manifest_path": manifest_path}).decode() + "\n")},
                git.check(), "artificial late group adoption gate fixture")
        self.assert_retired(lambda: self.run_slice(steps=8))
        self.assertEqual(self.accounting(), before)
        self.assertEqual(self.ledger.snapshot()["operations"][original["operation_id"]], original_audit)
        self.assert_no_formal()


if __name__ == "__main__":
    unittest.main()
