"""Three-domain CLI flow with synthetic native responses, never a live model."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine_fixtures import isolated_engine
from git_fixtures import configure_test_repository
from quality_fixtures import add_quality, passed_review
from test_pipeline import git
from pdm.cli import main
from pdm.contracts import canonical
from pdm.knowledge_quality import resolve_statistic
from pdm.pipeline import _load, _read_reviewed
from pdm.transaction import PipelineGit


class CliQualityRepairFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-artificial-cli-repair-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "data"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial CLI Repair")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial data baseline")
        isolated_engine(self)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.ledger = self.private / "audit.json"
        self.specifications = (("health", "steps", (1000, 2000), "1500"),
                               ("career", "work_material_kg", (6, 8), "7"),
                               ("learning", "lab_sample_kg", (6, 8), "7"))
        self.sources = {}
        self.calls = []
        self.bindings = []

    def invoke(self, arguments, expected=0):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(arguments)
        self.assertEqual(code, expected, errors.getvalue() or output.getvalue())
        self.assertEqual(errors.getvalue(), "")
        return json.loads(output.getvalue())

    def snapshot(self):
        return (git(self.repo, "rev-parse", "HEAD"),
                git(self.repo, "status", "--porcelain", "--untracked-files=all"),
                {str(path.relative_to(self.repo)): path.read_bytes()
                 for path in (self.repo / "memory").rglob("*") if path.is_file()})

    def budget_flags(self, operation):
        return ["--budget-ledger", str(self.ledger), "--operation-id", operation]

    def native(self, role, payload, timeout, *, on_start):
        """Check real request assembly/accounting; do not simulate comprehension."""
        invocation = "model-" + f"{len(self.calls) + 1:032x}"
        on_start(invocation)
        self.calls.append((role, deepcopy(payload), timeout))
        if role == "answer":
            citations = []
            for domain, _, _, _ in self.specifications:
                source = self.sources[domain]
                citations.append({key: source[key] for key in ("source_id", "source_version")}
                                 | {"locator": "L2-L3"})
            output = {"answer": "人工健康样例的平均步数为1500[1]；工作物料与学习实验样本的平均质量均为7kg[2][3]。",
                      "citations": citations,
                      "limitations": ["仅测试人工样例的传输与程序门禁，不验证真实模型的语义理解。"]}
        elif role == "extract" and "quality_repair_context" in payload:
            self.assertEqual(payload["quality_repair_context"]["missing_bindings"], self.bindings)
            records = {obj["metadata"]["id"]: obj for obj in payload["revision_context"]["candidate"]["objects"]
                       if obj["path"].startswith("memory/records/")}
            output = {"statistic_bindings": [dict(binding, body_evidence=records[binding["record_id"]]["body"])
                                               for binding in self.bindings]}
        elif role == "extract":
            self.assertNotIn("revision_context", payload)
            objects, wikis, bindings = [], [], []
            for domain, _, _, mean in self.specifications:
                source = self.sources[domain]
                identity = {key: source[key] for key in ("source_id", "source_version")}
                ref = dict(identity, locator="L1-L3", quote=source["body"].splitlines()[0])
                rid = "artificial-" + domain
                body = f"人工{domain}样例：所选原表两行观测的mean={mean}，不作原因解释。"
                objects.append({"id": rid, "title": "人工" + domain, "type": "knowledge",
                    "source_kind": "external_source", "sources": [ref], "domains": [domain],
                    "valid_from": None, "valid_to": None, "verification_status": "unverified",
                    "lifecycle_status": "undetermined", "body": body})
                wikis.append({"id": rid + "-topic", "title": "人工主题" + domain,
                              "sources": [ref], "body": body, "record_ids": [rid]})
                stat = next(item for item in payload["quality_context"]["statistics"]
                            if item["operation"] == "mean"
                            and resolve_statistic(payload["quality_context"], item)["source_id"] == source["source_id"])
                bindings.append({"stat_id": stat["stat_id"], "record_id": rid})
            self.bindings = sorted(bindings, key=lambda item: (item["stat_id"], item["record_id"]))
            output = add_quality({"objects": objects, "wikis": wikis, "reuse_refs": []}, payload["quality_context"])
            self.assertEqual(output["quality"]["statistic_claims"], [])
        else:
            self.assertNotIn("revision_context", payload)
            self.assertNotIn("raw_extraction_output", payload["candidate"])
            self.assertNotIn("quality_repair_context", payload["candidate"])
            output = passed_review(payload["required_checks"])
            output["repair_scope"] = {"kind": "none", "missing_bindings": []}
            if sum(item[0] == "review" for item in self.calls) == 1:
                checks = {item["check_id"]: item for item in payload["required_checks"]}
                for item in output["checks"]:
                    check = checks[item["check_id"]]
                    if check["kind"] == "object_statistics_time" and check["object_paths"][0].startswith("memory/records/"):
                        item.update(result="needs_revision", reason="Only the listed statistic bindings are missing; the frozen text is the review target.")
                output.update(verdict="needs_revision", repair_scope={"kind": "statistic_bindings",
                                                                         "missing_bindings": deepcopy(self.bindings)})
        return {"output": output, "model": "artificial-cli-fixture", "invocation_id": invocation,
                "elapsed_seconds": 0.01, "usage": None}

    def test_default_cli_repairs_bindings_then_publishes_and_reads_three_domains(self):
        for domain, metric, values, _ in self.specifications:
            body = f"date,{metric}\n2026-01-01,{values[0]}\n2026-01-02,{values[1]}\n"
            input_path = self.private / (domain + ".json")
            input_path.write_text(json.dumps({"source_key": "artificial:cli:" + domain,
                "source_type": "note", "acquisition_status": "full_text", "body": body}))
            captured = self.invoke(["capture", "--repo", str(self.repo), "--input", str(input_path)])
            self.sources[domain] = captured | {"body": body}
        self.invoke(["budget", "create", "--repo", str(self.repo), "--ledger", str(self.ledger),
                     "--mode", "audit", "--initial-used", "0", "--authorization-ref", "artificial-cli-only"])
        source_flags = [item for source in self.sources.values() for item in ("--source", source["path"])]
        self.invoke(["production", "prepare", "--repo", str(self.repo), "--id", "cli-repair", *source_flags])
        run = ["production", "run", "--repo", str(self.repo), "--id", "cli-repair",
               "--local-only", "--max-steps", "1", *self.budget_flags("artificial-cli-worker")]
        with patch("pdm.model_calls._call_transport", side_effect=self.native):
            first = self.invoke(run, expected=4)
            self.assertEqual(first["status"], "needs_revision", first)
            old_path = first["last_result"]["stage_path"]
            old_review = first["last_result"]["review_path"]
            frozen = {path: (self.repo / path).read_bytes() for path in (old_path, old_review)}
            original = _load(frozen[old_path], "candidate")
            self.assertEqual(original["skill_bundle"]["skills"]["extract"]["ref"], "knowledge-extract@11")
            self.assertEqual(original["skill_bundle"]["skills"]["review"]["ref"], "knowledge-review@9")
            second = self.invoke(run)
            self.assertEqual(second["status"], "ready", second)
            candidate, _, _ = _read_reviewed(PipelineGit(self.repo), second["final_run"])
            self.assertEqual(candidate["objects"], original["objects"])
            self.assertEqual(candidate["operations"], original["operations"])
            self.assertEqual(set(candidate["raw_extraction_output"]), {"statistic_bindings"})
            unchanged_output = deepcopy(candidate["extraction_output"])
            unchanged_output["quality"]["statistic_claims"] = original["extraction_output"]["quality"]["statistic_claims"]
            self.assertEqual(unchanged_output, original["extraction_output"])
            self.assertEqual(candidate["extraction"]["output_sha256"],
                             "sha256-" + hashlib.sha256(canonical(candidate["raw_extraction_output"])).hexdigest())
            claims = candidate["extraction_output"]["quality"]["statistic_claims"]
            self.assertEqual(len(claims), 3)
            for claim in claims:
                domain = claim["record_id"].removeprefix("artificial-")
                self.assertEqual(claim["sources"][0]["source_id"], self.sources[domain]["source_id"])
                self.assertEqual(claim["sources"][0]["source_version"], self.sources[domain]["source_version"])
            self.assertEqual({claim["record_id"]: claim["value"] for claim in claims},
                             {"artificial-" + domain: mean for domain, _, _, mean in self.specifications})
            published = self.invoke(run)
            self.assertEqual(published["status"], "published", published)
            self.assertEqual([role for role, _, _ in self.calls], ["extract", "review", "extract", "review"])
            self.assertEqual({timeout for _, _, timeout in self.calls}, {900})
            self.assertEqual(frozen, {path: (self.repo / path).read_bytes() for path in frozen})
            before = self.snapshot()
            run_flags = ["--run", published["publication"]["run_id"]]
            catalog = self.invoke(["knowledge", "--repo", str(self.repo), *source_flags, *run_flags])
            self.assertTrue(catalog["classification"]["complete"])
            for domain, _, _, _ in self.specifications:
                self.assertEqual([entry["id"] for entry in catalog["domains"][domain]["records"]], ["artificial-" + domain])
            answer = self.invoke(["ask", "--repo", str(self.repo), "--current", *source_flags, *run_flags,
                                  "--question", "这三份人工样例提供了哪些观测？", *self.budget_flags("artificial-cli-answer")])
            self.assertEqual(len(answer["citations"]), 3)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual([role for role, _, _ in self.calls], ["extract", "review", "extract", "review", "answer"])


if __name__ == "__main__":
    unittest.main()
