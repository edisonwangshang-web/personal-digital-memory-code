"""Real Git interruptions before final staging, using artificial sources only."""

from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import test_assembly_production
from pdm.budget import BudgetLedger, BudgetedModelCall
from pdm.contracts import CaptureError, canonical, markdown, parse_markdown
from pdm.pipeline import _committed, _load, _read_reviewed, verify_publication
from pdm.production import run_production
from pdm.transaction import PipelineGit


class AssemblyReservationResumeTests(unittest.TestCase):
    setUp = test_assembly_production.AssemblyProductionTests.setUp
    state = test_assembly_production.AssemblyProductionTests.state
    attempts = test_assembly_production.AssemblyProductionTests.attempts
    transport = test_assembly_production.AssemblyProductionTests.transport
    prepare = test_assembly_production.AssemblyProductionTests.prepare

    def interrupt_before_final_stage(self):
        paths = self.prepare()
        ready = run_production(self.repo, "assembly", model_call=self.adapter, max_steps=2)
        self.assertEqual(ready["status"], "ready", ready)
        self.assertEqual(self.roles, ["extract", "review"] * 2)
        before = deepcopy(self.payloads), self.ledger.path.read_bytes()
        original = PipelineGit.write_files
        interrupted = []

        def write(git, files, expected_head, message):
            for path, raw in files.items():
                if not path.startswith("memory/staging/"):
                    continue
                metadata, _ = parse_markdown(raw)
                if metadata.get("schema_version") != "0.14":
                    continue
                candidate = _load(raw, "candidate")
                if "assembly" not in candidate:
                    continue
                run = candidate["run_id"]
                started = f"memory/runs/processes/{run}/started.md"
                engine = f"memory/runs/processes/{run}/engine.md"
                self.assertEqual(_load(_committed(git, started), "processing_attempt")["reserved_calls"], 0)
                self.assertTrue(_committed(git, engine))
                self.assertFalse(git.path(path).exists())
                self.interrupted_candidate = deepcopy(candidate)
                interrupted.append(run)
                raise KeyboardInterrupt("Artificial interruption after reservation, before final staging")
            return original(git, files, expected_head, message)

        with patch.object(PipelineGit, "write_files", write):
            with self.assertRaises(KeyboardInterrupt):
                run_production(self.repo, "assembly", model_call=self.adapter, max_steps=1)
        self.assertEqual(len(interrupted), 1)
        self.assertEqual((self.payloads, self.ledger.path.read_bytes()), before)
        final = next(item for item in self.attempts() if item["identity"]["phase"] == "final")
        self.assertEqual(len(final["attempts"]), 1)
        attempt = final["attempts"][0]
        self.assertEqual((attempt["run_id"], attempt["status"]), (interrupted[0], "reserved"))
        operation = self.ledger.operation(attempt["operation_id"])
        self.assertTrue(operation is None or not operation["calls"])
        return paths, attempt

    @contextmanager
    def reservation_variant(self):
        """Fork one interrupted fixture; each negative starts with original bytes."""
        original = self.repo, self.ledger, self.adapter
        with tempfile.TemporaryDirectory(prefix="pdm-artificial-assembly-variant-") as directory:
            root = Path(directory)
            repo = root / "data"
            shutil.copytree(original[0], repo)
            audit = root / "audit.json"
            shutil.copy2(original[1].path, audit)
            shutil.copy2(original[1].path.with_name(original[1].path.name + ".lock"),
                         audit.with_name(audit.name + ".lock"))
            ledger = BudgetLedger(audit, repo=repo)
            try:
                self.repo, self.ledger = repo, ledger
                self.adapter = BudgetedModelCall(ledger, "worker", self.transport)
                yield
            finally:
                self.repo, self.ledger, self.adapter = original

    def assert_blocked_without_calls(self, attempt, *, adapter=None, queue="assembly"):
        before = self.number, self.ledger.path.read_bytes(), deepcopy(self.attempts())
        try:
            result = run_production(self.repo, queue, model_call=adapter or self.adapter)
        except CaptureError:
            pass
        else:
            self.assertIn(result["status"], {"unknown", "blocked_context", "failed"}, result)
        self.assertEqual((self.number, self.ledger.path.read_bytes(), self.attempts()), before)
        self.assertFalse((self.repo / f"memory/staging/{attempt['run_id']}.md").exists())
        self.assertEqual(PipelineGit(self.repo).text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                                   "memory/records", "memory/topics"), "")

    def test_queue_resumes_same_zero_call_assembly_reservation_before_staging(self):
        paths, attempt = self.interrupt_before_final_stage()
        git = PipelineGit(self.repo)
        run = attempt["run_id"]
        reservation = {name: _committed(git, f"memory/runs/processes/{run}/{name}.md")
                       for name in ("started", "engine")}
        calls_before = deepcopy(self.payloads)
        ledger_before = self.ledger.path.read_bytes()
        # Reproduce an already-persisted queue outcome from the older entrypoint.
        # Only the newly added recovery decision is disabled; no state is edited.
        with patch("pdm.assembly_final.can_resume_unstaged_assembly", return_value=False):
            legacy = run_production(self.repo, "assembly", model_call=self.adapter, max_steps=1)
        self.assertEqual(legacy["status"], "unknown", legacy)
        self.assertEqual(self.state("assembly")["status"], "unknown")
        self.assertEqual(self.payloads, calls_before)
        self.assertEqual(self.ledger.path.read_bytes(), ledger_before)

        result = run_production(self.repo, "assembly", model_call=self.adapter)

        self.assertEqual(result["status"], "published", result)
        self.assertEqual(result["final_run"], run)
        self.assertEqual(self.roles.count("extract"), 2)
        self.assertEqual(self.payloads[:len(calls_before)], calls_before)
        final_attempts = next(item for item in self.attempts() if item["identity"]["phase"] == "final")["attempts"]
        self.assertEqual(len(final_attempts), 1)
        self.assertEqual((final_attempts[0]["run_id"], final_attempts[0]["operation_id"], final_attempts[0]["attempt"]),
                         (run, attempt["operation_id"], 1))
        self.assertEqual(final_attempts[0]["status"], "passed")
        for name, raw in reservation.items():
            self.assertEqual(_committed(git, f"memory/runs/processes/{run}/{name}.md"), raw)
        candidate, review, _ = _read_reviewed(git, run)
        self.assertNotIn("extraction", candidate)
        self.assertEqual(candidate["assembly"]["mode"], "verbatim_assembly_no_model_extract")
        self.assertEqual(review["output"]["verdict"], "passed")
        self.assertEqual(len(verify_publication(self.repo, result["publication"]["run_id"], source_paths=paths)["files"]), 4)
        before = git.head(), self.number, self.ledger.path.read_bytes()
        self.assertEqual(run_production(self.repo, "assembly", model_call=self.adapter)["status"], "published")
        self.assertEqual((git.head(), self.number, self.ledger.path.read_bytes()), before)

    def test_pre_stage_reservation_does_not_replay_a_started_root_review(self):
        _, attempt = self.interrupt_before_final_stage()
        self.ledger.start(attempt["operation_id"], "review", "a" * 64, "model-" + "f" * 32)
        self.assert_blocked_without_calls(attempt)

    def test_pre_stage_reservation_does_not_replay_an_unknown_child_review(self):
        from pdm.decomposed_review import _hash, _layout, _node_identity
        from pdm.pipeline import _candidate_bindings, _candidate_payload, load_source
        _, attempt = self.interrupt_before_final_stage()
        git = PipelineGit(self.repo)
        candidate = self.interrupted_candidate
        sources = [load_source(self.repo, path) for path in self.state("assembly")["plan"]["source_paths"]]
        payload = _candidate_payload(git, candidate, sources)
        units, _ = _layout(candidate, payload)
        identity_plan = {"inventory": [{"id": obj["metadata"]["id"], "path": obj["path"]}
                                       for obj in sorted(candidate["objects"], key=lambda obj: obj["path"])],
                         "source_bindings": _candidate_bindings(candidate)}
        for kind in ("coverage", "global"):
            with self.subTest(kind=kind), self.reservation_variant():
                unit = next(unit for unit in units if unit["kind"] == kind)
                operation = "decomposed:" + _hash(_node_identity(identity_plan, unit))[7:] + ":1"
                self.ledger.start(operation, "review", "a" * 64, "model-" + "f" * 32)
                self.ledger.finish(operation, "review", "unknown", error_code="timeout")
                self.assert_blocked_without_calls(attempt)

    def test_pre_stage_reservation_does_not_replay_a_completed_root_extraction(self):
        _, attempt = self.interrupt_before_final_stage()
        self.ledger.start(attempt["operation_id"], "extract", "a" * 64, "model-" + "f" * 32)
        self.ledger.finish(attempt["operation_id"], "extract", "completed")
        self.assert_blocked_without_calls(attempt)

    def test_changed_assembly_reservation_is_rejected_without_a_call(self):
        _, attempt = self.interrupt_before_final_stage()
        for field, replacement in (("reserved_calls", 1), ("assembly_contract", "artificial-incorrect-contract"),
                                   ("source_paths", [])):
            with self.subTest(field=field), self.reservation_variant():
                git = PipelineGit(self.repo)
                path = f"memory/runs/processes/{attempt['run_id']}/started.md"
                raw = _committed(git, path)
                metadata, _ = parse_markdown(raw)
                changed = _load(raw, "processing_attempt")
                changed[field] = replacement
                with git.lock():
                    git.write_changes({path: markdown(metadata, canonical(changed).decode() + "\n")},
                                      {path: raw}, git.head(), "artificial changed reservation")
                self.assert_blocked_without_calls(attempt)

    def test_attempt_cannot_redirect_resume_to_empty_operation_or_another_run(self):
        _, attempt = self.interrupt_before_final_stage()
        for field, replacement in (("operation_id", "batch:artificial-empty:1"),
                                   ("run_id", "run-" + "0" * 32), ("attempt", 2)):
            with self.subTest(field=field), self.reservation_variant():
                git = PipelineGit(self.repo)
                counter_paths = git.text("ls-tree", "-r", "--name-only", "HEAD", "--",
                                         "memory/runs/processes/queue-attempts").splitlines()
                path = next(path for path in counter_paths if
                            _load(_committed(git, path), "production_attempts")["identity"]["phase"] == "final")
                raw = _committed(git, path)
                metadata, _ = parse_markdown(raw)
                changed = _load(raw, "production_attempts")
                changed["attempts"][0][field] = replacement
                with git.lock():
                    git.write_changes({path: markdown(metadata, canonical(changed).decode() + "\n")},
                                      {path: raw}, git.head(), "artificial changed attempt identity")
                self.assert_blocked_without_calls(attempt)

    def test_changed_plan_source_binding_cannot_resume_the_reservation(self):
        _, attempt = self.interrupt_before_final_stage()
        git = PipelineGit(self.repo)
        path = "memory/runs/processes/queues/assembly/state.md"
        raw = _committed(git, path)
        metadata, _ = parse_markdown(raw)
        changed = self.state("assembly")
        changed["plan"]["sources"][0]["source_version"] = "sha256-" + "0" * 64
        with git.lock():
            git.write_changes({path: markdown(metadata, canonical(changed).decode() + "\n")},
                              {path: raw}, git.head(), "artificial changed source-plan binding")
        self.assert_blocked_without_calls(attempt)

    def test_replacement_audit_pool_cannot_resume_pre_stage_reservation(self):
        _, attempt = self.interrupt_before_final_stage()
        temporary = tempfile.TemporaryDirectory(prefix="pdm-artificial-replacement-audit-")
        self.addCleanup(temporary.cleanup)
        other = BudgetLedger.create(Path(temporary.name) / "audit.json", None, 0,
                                    "artificial replacement", mode="audit")
        before = other.path.read_bytes()
        self.assert_blocked_without_calls(attempt, adapter=BudgetedModelCall(other, "worker", self.transport))
        self.assertEqual(other.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
