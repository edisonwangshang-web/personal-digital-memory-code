"""Synthetic CLI flows through real request rendering and response decoding.

Only Popen/events are replaced: no native model runs. The extraction stub must
use the actual failed/current draft and the complete issued edit set; Review
inspects source-backed content, not just self-reported coverage labels.
"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from engine_fixtures import isolated_engine
from git_fixtures import configure_test_repository
from pdm.budget import BudgetLedger
from pdm.cli import main
from pdm.model_calls import _valid
from pdm.pipeline import _hash, _load
from pdm.quality_projection import restore_quality_payload
from pdm.skill_support import resolve_skill_refs
from pdm.skill_wire import decode_shared_json
from pdm.transaction import PipelineGit
from test_pipeline import git


TEXTS = {
    "health": "人工健康样例：来源时间为2035-01-01，当时当日监测尚未结束，步数为1000。\n",
    "career": "人工工作样例：2035-01-01项目预算为10万元。\n",
    "learning": "人工学习样例：2035-01-01练习时长为20分钟，计划仅为讨论草案。\n",
    "update": "人工工作补充：2035-01-02项目预算改为12万元，是后续变化，不证明此前预算错误。\n",
}
IDS = {domain: "artificial-cli-" + domain for domain in ("health", "career", "learning")}


def pointer(value, path):
    for part in path.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def replace_pointer(value, path, replacement):
    parent, field = path.rsplit("/", 1)
    node = pointer(value, parent)
    node[int(field) if isinstance(node, list) else field] = deepcopy(replacement)


class CliReferenceRepairFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-cli-reference-flow-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "data"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial CLI Reference Flow")
        git(self.repo, "config", "user.email", "artificial@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial data baseline")
        isolated_engine(self)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.ledger = self.private / "audit.json"
        self.bundle = resolve_skill_refs()
        self.sources, self.labels, self.requests = {}, {}, []
        self.mode = "references"
        self.frozen_draft = None
        self.correct_output = None
        self.repaired_output = None
        self.rejected_content = None
        self.guard_mode = None
        self.patch_attempts = 0
        self.review_rejections = 0
        self.active_schema = None
        self.active_invocation = None
        self.failed_generators = set()
        self.original_popen = subprocess.Popen
        self.original_which = shutil.which
        self.invoke(["budget", "create", "--repo", str(self.repo), "--ledger", str(self.ledger),
                     "--mode", "audit", "--initial-used", "0", "--authorization-ref", "artificial-cli-only"])
        for name in ("health", "career", "learning"):
            self.capture(name, TEXTS[name])

    def invoke(self, arguments, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main(arguments)
        self.assertEqual(expected, status, err.getvalue() or out.getvalue())
        self.assertEqual("", err.getvalue())
        return json.loads(out.getvalue())

    def capture(self, name, body):
        path = self.private / (name + ".json")
        path.write_text(json.dumps({"source_key": "artificial:cli-reference:" + name,
            "source_type": "note", "acquisition_status": "full_text", "body": body}, ensure_ascii=False))
        saved = self.invoke(["capture", "--repo", str(self.repo), "--input", str(path)])
        self.sources[name] = {**saved, "body": body}
        self.labels[saved["source_id"]] = name
        return saved

    def flags(self, names):
        return [item for name in names for item in ("--source", self.sources[name]["path"])]

    def run_flags(self, queue):
        return ["production", "run", "--repo", str(self.repo), "--id", queue, "--local-only",
                "--max-steps", "1", "--budget-ledger", str(self.ledger), "--operation-id", "artificial-cli-worker"]

    def ref(self, name):
        source = self.sources[name]
        return {"source_id": source["source_id"], "source_version": source["source_version"],
                "locator": "L1-L1", "quote": source["body"].strip()}

    def record(self, domain, body, names):
        return {"id": IDS[domain], "type": "knowledge", "title": "人工" + domain,
            "source_kind": "external_source", "sources": [self.ref(name) for name in names],
            "domains": [domain], "valid_from": None, "valid_to": None,
            "verification_status": "unverified", "lifecycle_status": "undetermined", "body": body}

    def full_output(self, payload, *, omit_change=False):
        incremental = "update" in {self.labels[source["metadata"]["source_id"]] for source in payload["sources"]}
        bodies = {domain: TEXTS[domain].strip() for domain in IDS}
        if incremental and not omit_change:
            bodies["career"] += "\n" + TEXTS["update"].strip()
        related = {doc["metadata"]["id"]: doc for group in payload.get("related", [])
                   for doc in group["documents"]}
        if incremental:
            self.assertTrue({IDS["health"], IDS["career"], IDS["learning"]} <= set(related))
            objects = [self.record("career", bodies["career"], ["career", "update"])]
            refs = [{"path": related[IDS[domain]]["path"],
                     "content_version": related[IDS[domain]]["content_version"]}
                    for domain in ("health", "learning")]
        else:
            objects = [self.record(domain, bodies[domain], [domain]) for domain in IDS]
            refs = []
        quality = {"coverage_disposition": [], "statistic_claims": [], "temporal_claims": [{
            "record_id": IDS["health"], "statement": bodies["health"], "basis": "source_time",
            "source_refs": [self.ref("health")], "system_fact_id": None}]}
        for target in payload["quality_context"]["targets"]:
            name = self.labels[target["source_id"]]
            domain = "career" if name == "update" else name
            excerpt = (TEXTS["career"].strip() if name == "update" and omit_change else TEXTS[name].strip())
            quality["coverage_disposition"].append({"target_id": target["target_id"], "status": "summarized",
                "record_ids": [IDS[domain]], "body_evidence": [{"record_id": IDS[domain], "text": excerpt}], "reason": ""})
        wiki = {"id": "artificial-cli-overview", "title": "人工三领域主题",
            "body": "\n".join(bodies.values()), "record_ids": list(IDS.values()),
            "sources": [self.ref(name) for name in ("health", "career", "learning", "update")
                        if name in self.sources and (name != "update" or incremental)]}
        return {"objects": objects, "wikis": [wiki], "reuse_refs": refs, "quality": quality}

    def check_goals(self, payload):
        goal = payload["completion_goals"]
        self.assertEqual("completion-goals-v1", goal["contract"])
        self.assertEqual({target["target_id"] for target in payload["quality_context"]["targets"]},
                         set(goal["all_target_ids"]))
        if "extraction_feedback" in payload:
            draft = payload["extraction_feedback"]["draft"]
            for obj in draft.get("objects", []):
                self.assertIn("memory/records/" + obj["id"] + ".md", goal["required_current_draft_paths"])

    def extract(self, payload):
        self.check_goals(payload)
        if self.guard_mode is not None:
            name = "guard-" + self.guard_mode
            statement = "当前已经完成人工处理。" if self.guard_mode == "current" else "人工处理完成。"
            record = self.record("health", statement, [name])
            record["id"] = "artificial-time-" + self.guard_mode
            target = payload["quality_context"]["targets"][0]
            return {"objects": [record], "reuse_refs": [], "wikis": [{
                "id": record["id"] + "-topic", "title": "人工时间反例", "body": statement,
                "sources": [self.ref(name)], "record_ids": [record["id"]]}],
                "quality": {"coverage_disposition": [{"target_id": target["target_id"],
                    "status": "summarized", "record_ids": [record["id"]],
                    "body_evidence": [{"record_id": record["id"], "text": statement}], "reason": ""}],
                    "statistic_claims": [], "temporal_claims": [{"record_id": record["id"],
                        "statement": statement, "basis": "current_state" if self.guard_mode == "current" else "unknown",
                        "source_refs": [] if self.guard_mode == "current" else [self.ref(name)], "system_fact_id": None}]}}
        if "extraction_feedback" in payload:
            feedback = payload["extraction_feedback"]
            self.assertEqual(self.frozen_draft, feedback["draft"])
            issues = feedback["validation_error"]["details"]["issues"]
            context = feedback["repair_context"]
            paths = context["field_paths"]
            self.assertEqual("reference-fields-repair-v1", context["contract"])
            self.assertEqual(_hash(feedback["draft"]), context["draft_sha256"])
            self.assertEqual(_hash(issues), context["issues_sha256"])
            self.assertEqual(feedback["generation_invocations"], context["generation_invocations"])
            self.assertTrue(self.failed_generators <= set(context["generation_invocations"]))
            self.assertEqual({issue["field_path"] for issue in issues}, set(paths))
            self.assertEqual({"/objects/0/sources/0", "/wikis/0/sources/0"}, set(paths))
            self.assertFalse(feedback["validation_error"]["details"]["issues_truncated"])
            self.patch_attempts += 1
            repairs = []
            expected = deepcopy(feedback["draft"])
            for path in paths:
                bad = pointer(feedback["draft"], path)
                name = self.labels[bad["source_id"]]
                reference = self.ref(name)
                if self.mode == "exhaust":
                    reference["quote"] = "人工持续错误引文，不能匹配来源。"
                repairs.append({"field_path": path, "reference": reference})
                replace_pointer(expected, path, reference)
            self.repaired_output = expected
            if self.mode == "exhaust":
                self.frozen_draft = deepcopy(expected)
                self.failed_generators.add(self.active_invocation)
            return {"reference_repairs": repairs}
        if "revision_context" in payload:
            current = payload["revision_context"]["candidate"]
            actual = {obj["metadata"]["id"]: obj for obj in current["objects"]}
            old = {obj["metadata"]["id"]: obj for obj in self.rejected_content["objects"]}
            self.assertEqual(old, actual)
            reasons = payload["revision_context"]["review_record"]["output"]["reasons"]
            self.assertIn("人工工作变化缺少12万元后续预算正文。", reasons)
            self.assertEqual(TEXTS["health"].strip(), actual[IDS["health"]]["body"])
            self.assertEqual(TEXTS["learning"].strip(), actual[IDS["learning"]]["body"])
            output = self.full_output(payload)
            career = next(obj for obj in output["objects"] if obj["id"] == IDS["career"])
            career["body"] = actual[IDS["career"]]["body"] + "\n" + TEXTS["update"].strip()
            return output
        incremental = "update" in {self.labels[source["metadata"]["source_id"]] for source in payload["sources"]}
        output = self.full_output(payload, omit_change=incremental and self.mode == "content")
        if incremental and self.mode in {"references", "exhaust"}:
            self.correct_output = deepcopy(output)
            output["objects"][0]["sources"][0]["quote"] = "人工错误工作引文。"
            output["wikis"][0]["sources"][0]["quote"] = "人工错误主题引文。"
            self.frozen_draft = deepcopy(output)
            self.failed_generators.add(self.active_invocation)
        return output

    def review(self, payload):
        self.assertNotIn("extraction_feedback", payload)
        objects = {obj["metadata"]["id"]: obj for obj in payload["candidate"]["objects"]}
        expected = {domain: TEXTS[domain].strip() for domain in IDS}
        for domain, text in expected.items():
            self.assertIn(text, objects[IDS[domain]]["body"])
            self.assertEqual([domain], objects[IDS[domain]]["metadata"]["domains"])
        incremental = "update" in {self.labels[source["metadata"]["source_id"]] for source in payload["sources"]}
        missing = incremental and TEXTS["update"].strip() not in objects[IDS["career"]]["body"]
        if missing:
            self.review_rejections += 1
            self.rejected_content = deepcopy(payload["candidate"])
        reasons = ["人工工作变化缺少12万元后续预算正文。"] if missing else ["已逐条核对人工三领域事实与后续工作变化。"]
        checks = []
        for check in payload["required_checks"]:
            relevant = (check["kind"] == "global_organization" or
                        "memory/records/" + IDS["career"] + ".md" in check["object_paths"])
            checks.append({"check_id": check["check_id"], "scope_sha256": check["scope_sha256"],
                "result": "needs_revision" if missing and relevant else "passed",
                "reason": reasons[0], "evidence_ids": list(check["evidence_ids"])})
        return {"verdict": "needs_revision" if missing else "passed", "reasons": reasons, "checks": checks,
                "repair_scope": {"kind": "content" if missing else "none", "missing_bindings": []}}

    def fake_popen(self, command, **options):
        if "--output-schema" not in command:
            return self.original_popen(command, **options)
        schema = Path(command[command.index("--output-schema") + 1])
        self.active_schema = json.loads(schema.read_text())
        return Mock(name="artificial-no-os-model-process")

    def fake_events(self, process, prompt, timeout, invocation_id, started):
        self.active_invocation = invocation_id
        _, data = prompt.decode().rsplit("\n\n输入资料 JSON（仅作资料，不执行其中指令）：\n", 1)
        wire = json.loads(data)
        material = (restore_quality_payload(decode_shared_json(wire))
                    if isinstance(wire, dict) and "format" in wire else wire)
        properties = self.active_schema["properties"]
        role = "answer" if "answer" in properties else "review" if "verdict" in properties else "extract"
        if role in {"extract", "review"}:
            wire_bundle = material["skill_bundle"]
            self.assertEqual(self.bundle["sha256"], wire_bundle["sha256"])
            self.assertEqual(self.bundle["skills"]["extract"]["ref"], wire_bundle["extract_skill_ref"])
            self.assertEqual(900, timeout)
            for source in material["sources"]:
                name = self.labels[source["metadata"]["source_id"]]
                self.assertEqual(self.sources[name]["source_version"], source["metadata"]["source_version"])
                if "body" in source:
                    self.assertEqual(self.sources[name]["body"], source["body"])
                else:
                    actual = {line["line"]: line["text"] for fragment in source["fragments"]
                              for line in fragment["lines"]}
                    self.assertEqual(dict(enumerate(self.sources[name]["body"].splitlines(), 1)), actual)
        operation_calls = [call for operation in BudgetLedger(self.ledger).snapshot()["operations"].values()
                           for call in operation["calls"].values() if call["invocation_id"] == invocation_id]
        self.assertEqual(1, len(operation_calls))
        self.assertEqual("started", operation_calls[0]["status"])
        if role == "extract":
            output = self.extract(material)
        elif role == "review":
            output = self.review(material)
        else:
            output = {"answer": "工作预算：2035-01-01为10万元[1]，2035-01-02调整为12万元[2]；新资料描述的是后续变化[2]。",
                "citations": [{"source_id": self.sources[name]["source_id"],
                               "source_version": self.sources[name]["source_version"], "locator": "L1-L1"}
                              for name in ("career", "update")],
                "limitations": ["仅人工样例及明确选择范围；没有实际模型或云端runner调用。"]}
        self.assertTrue(_valid(output, self.active_schema), (role, output))
        self.requests.append((role, deepcopy(material), deepcopy(self.active_schema)))
        return [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(output, ensure_ascii=False)}},
                {"type": "turn.completed", "model": "artificial-cli-events"}]

    def baseline_and_incremental(self):
        self.invoke(["production", "prepare", "--repo", str(self.repo), "--id", "baseline",
                     *self.flags(["health", "career", "learning"])])
        first = self.invoke(self.run_flags("baseline"))
        self.assertEqual("ready", first["status"])
        base = self.invoke(self.run_flags("baseline"))
        self.assertEqual("published", base["status"])
        self.formal = {path: (self.repo / path).read_bytes() for path in base["publication"]["files"]}
        self.capture("update", TEXTS["update"])
        self.invoke(["production", "prepare", "--repo", str(self.repo), "--id", "incremental",
                     *self.flags(["health", "career", "learning", "update"]),
                     "--allowed-run", base["publication"]["run_id"]])

    def complete_and_read(self, result):
        self.assertEqual("ready", result["status"], result)
        candidate = _load((self.repo / f"memory/staging/{result['final_run']}.md").read_bytes(), "candidate")
        if self.repaired_output is not None:
            self.assertEqual(self.repaired_output, candidate["extraction_output"])
            self.assertEqual(self.correct_output, candidate["extraction_output"])
        published = self.invoke(self.run_flags("incremental"))
        self.assertEqual("published", published["status"])
        for domain in ("health", "learning"):
            path = "memory/records/" + IDS[domain] + ".md"
            self.assertEqual(self.formal[path], (self.repo / path).read_bytes())
        text = (self.repo / ("memory/records/" + IDS["career"] + ".md")).read_text()
        self.assertIn("预算为10万元", text)
        self.assertIn("预算改为12万元", text)
        self.assertIn("不证明此前预算错误", text)
        before = PipelineGit(self.repo).head(), self.ledger.read_bytes()
        source_flags = self.flags(["health", "career", "learning", "update"])
        run = ["--run", published["publication"]["run_id"]]
        catalog = self.invoke(["knowledge", "--repo", str(self.repo), *source_flags, *run])
        self.assertTrue(catalog["classification"]["complete"])
        answer = self.invoke(["ask", "--repo", str(self.repo), "--current", *source_flags, *run,
            "--question", "人工工作预算有什么后续变化？", "--budget-ledger", str(self.ledger),
            "--operation-id", "artificial-reading"])
        self.assertIn("12万元", answer["answer"])
        self.assertEqual(before[0], PipelineGit(self.repo).head())
        self.assertEqual(2, len(answer["citations"]))
        return published

    def native_context(self):
        from contextlib import ExitStack
        context = ExitStack()
        context.enter_context(patch("pdm.model_calls.shutil.which", side_effect=lambda executable, **options:
            "/artificial/codex" if executable == "codex" else self.original_which(executable, **options)))
        context.enter_context(patch("pdm.model_calls.subprocess.Popen", side_effect=self.fake_popen))
        context.enter_context(patch("pdm.model_calls._events", side_effect=self.fake_events))
        return context

    def test_standard_cli_repairs_two_references_without_rewriting_valid_content_then_reviews_and_reads(self):
        with self.native_context():
            self.baseline_and_incremental()
            first = self.invoke(self.run_flags("incremental"), expected=4)
            self.assertEqual("failed", first["status"])
            failure = _load((self.repo / first["last_result"]["failure_path"]).read_bytes(), "failure")
            errors = failure["extraction_diagnostic"]["validation_error"]["details"]
            self.assertEqual(2, len(errors["issues"]))
            self.assertFalse(errors["issues_truncated"])
            self.assertEqual(self.formal, {path: (self.repo / path).read_bytes() for path in self.formal})
            repaired = self.invoke(self.run_flags("incremental"))
            self.assertEqual(1, self.patch_attempts)
            published = self.complete_and_read(repaired)
            self.assertEqual("local_only", published["delivery"]["scope"])
            self.assertEqual("knowledge-extract@16", self.bundle["skills"]["extract"]["ref"])

    def test_standard_cli_content_omission_requires_current_draft_repair_and_fresh_review(self):
        self.mode = "content"
        with self.native_context():
            self.baseline_and_incremental()
            first = self.invoke(self.run_flags("incremental"), expected=4)
            self.assertEqual("needs_revision", first["status"])
            self.assertEqual(1, self.review_rejections)
            repaired = self.invoke(self.run_flags("incremental"))
            self.assertEqual(0, self.patch_attempts)
            self.complete_and_read(repaired)

    def test_standard_cli_stops_after_four_failed_reference_attempts_and_restart_does_not_reset(self):
        self.mode = "exhaust"
        with self.native_context():
            self.baseline_and_incremental()
            for number in range(1, 5):
                result = self.invoke(self.run_flags("incremental"), expected=4)
                self.assertIn(result["status"], {"failed", "attempt_limit"}, result)
            self.assertEqual("attempt_limit", result["status"])
            self.assertEqual(3, self.patch_attempts)
            before = len(self.requests), self.ledger.read_bytes()
            frozen_counters = {path: (self.repo / path).read_bytes() for path in git(self.repo, "ls-tree", "-r",
                "--name-only", "HEAD", "--", "memory/runs/processes/queue-attempts").splitlines()}
            repeated = self.invoke(self.run_flags("incremental"), expected=4)
            self.assertEqual("attempt_limit", repeated["status"])
            self.assertEqual(before, (len(self.requests), self.ledger.read_bytes()))
            self.assertEqual(frozen_counters, {path: (self.repo / path).read_bytes() for path in frozen_counters})
            self.assertEqual(self.formal, {path: (self.repo / path).read_bytes() for path in self.formal})
            counters = {path: (self.repo / path).read_bytes() for path in git(self.repo, "ls-tree", "-r",
                "--name-only", "HEAD", "--", "memory/runs/processes/queue-attempts").splitlines()}
            # Distinct artificial inputs exercise semantic time guards, not a
            # new identity for the exhausted reference-repair work above.
            for mode, expected in (("current", "quality_current_state_unverified"),
                                   ("unknown", "quality_temporal_mismatch")):
                with self.subTest(time_basis=mode):
                    self.guard_mode = mode
                    name = "guard-" + mode
                    self.capture(name, "人工独立时间反例原文，没有给出处理完成事实。\n")
                    self.invoke(["production", "prepare", "--repo", str(self.repo), "--id", name, *self.flags([name])])
                    failed = self.invoke(self.run_flags(name), expected=4)
                    self.assertEqual("failed", failed["status"])
                    self.assertEqual(expected, failed["last_result"]["error"])
                    failure = _load((self.repo / failed["last_result"]["failure_path"]).read_bytes(), "failure")
                    self.assertIn(expected, {issue["code"] for issue in
                        failure["extraction_diagnostic"]["validation_error"]["details"]["issues"]})
            self.assertEqual(counters, {path: (self.repo / path).read_bytes() for path in counters})
            self.guard_mode = None


if __name__ == "__main__":
    unittest.main()
