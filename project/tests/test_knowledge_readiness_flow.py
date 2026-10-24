"""Full knowledge flow in temporary Git; fake models verify contracts, not meaning."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from git_fixtures import configure_test_repository
from test_pipeline import git
from pdm.capture import capture
from pdm.contracts import CaptureError, parse_markdown
from pdm.domain_catalog import build_domain_catalog
from pdm.evidence import ask_current, build_current_evidence
from pdm.model_calls import request_binding
from pdm.pipeline import _load, process_sources, publish, verify_publication
from pdm.skill_support import resolve_skill_refs


class KnowledgeReadinessFlowTests(unittest.TestCase):
    """Exercise capture, two independent calls, publication and selected reads."""

    SPECS = (
        {"id": "exercise-plan", "domain": "health", "title": "整理运动资料",
         "quote": "用户：我计划整理运动资料。", "body": "人工对话（2026-10-01）记载，用户计划整理运动资料。"},
        {"id": "work-plan", "domain": "career", "title": "整理工作方案",
         "quote": "用户：我计划整理工作方案。", "body": "人工对话（2026-10-01）记载，用户计划整理工作方案。"},
        {"id": "python-plan", "domain": "learning", "title": "学习Python",
         "quote": "用户：我计划学习Python。", "body": "人工对话（2026-10-01）记载，用户计划学习Python。"},
    )
    TOPIC = "memory/topics/personal-plans.md"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Knowledge Flow Fixture")
        git(self.repo, "config", "user.email", "knowledge-flow@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "isolated knowledge flow baseline")
        self.specs = {}
        self.paths = []
        for spec in self.SPECS:
            self.add_source(spec)
        self.bundle = resolve_skill_refs()
        self.calls = []

    def add_source(self, spec):
        saved = capture(self.repo, {
            "source_key": "knowledge-flow:" + spec["id"], "source_type": "conversation",
            "platform": "fixture", "original_url": None, "acquisition_status": "full_text",
            "original_time": "2026-10-01", "body": spec["quote"] + "\n",
        })
        self.specs[saved["path"]] = copy.deepcopy(spec)
        self.paths.append(saved["path"])
        return saved

    def model(self, role, payload):
        self.calls.append((role, copy.deepcopy(payload)))
        if role == "extract":
            records, refs, record_by_source = [], [], {}
            for source in sorted(payload["sources"], key=lambda item: self.specs[item["path"]]["id"]):
                spec = self.specs[source["path"]]
                ref = {"source_id": source["metadata"]["source_id"],
                       "source_version": source["metadata"]["source_version"],
                       "locator": "L1-L1", "quote": spec["quote"]}
                refs.append(ref)
                record_by_source[ref["source_id"]] = spec
                records.append({
                    "id": spec["id"], "type": "goal", "title": spec["title"],
                    "source_kind": "user_statement", "sources": [ref], "domains": [spec["domain"]],
                    "valid_from": None, "valid_to": None, "verification_status": "supported",
                    "lifecycle_status": "undetermined", "body": spec["body"],
                })
            output = {
                "objects": records,
                "wiki": {"id": "personal-plans", "title": "用户计划",
                         "body": "\n".join(record["body"] for record in records), "sources": refs},
                "quality": {
                    "coverage_disposition": [{
                        "target_id": target["target_id"], "status": "summarized",
                        "record_ids": [record_by_source[target["source_id"]]["id"]],
                        "body_evidence": [{"record_id": record_by_source[target["source_id"]]["id"],
                                           "text": record_by_source[target["source_id"]]["body"]}],
                        "reason": "",
                    } for target in payload["quality_context"]["targets"]],
                    "statistic_claims": [], "temporal_claims": [],
                },
            }
        elif role == "review":
            output = {
                "verdict": "passed", "reasons": ["人工测试返回；用于核对工程传输和门禁。"],
                "checks": [{
                    "check_id": check["check_id"], "scope_sha256": check["scope_sha256"],
                    "result": "passed", "reason": "逐项返回绑定原文和候选的人工审核结果。",
                    "evidence_ids": check["evidence_ids"],
                } for check in payload["required_checks"]],
            }
        else:
            entries = payload["evidence"]["sources"]
            entry = next(entry for entry in entries if entry["lines"])
            output = {
                "answer": "用户计划见本次提供的原文。[1]",
                "citations": [{"source_id": entry["source"]["source_id"],
                               "source_version": entry["source"]["source_version"],
                               "locator": "L1-L1"}],
                "limitations": ["人工传输测试，不验证模型对内容的理解。"],
            }
        return {"output": output, "model": "knowledge-flow-fake", "usage": None,
                "elapsed_seconds": 0.01, "invocation_id": "model-" + f"{len(self.calls):032x}"}

    def process_and_publish(self, paths=None, *, related_runs=None):
        selected = self.paths if paths is None else paths
        result = process_sources(self.repo, selected, related_runs=related_runs,
                                 skill_bundle=self.bundle, model_call=self.model)
        self.assertEqual(result["status"], "passed", result)
        publication = publish(self.repo, result["run_id"])
        self.assertEqual(publication["status"], "published_local")
        self.assertEqual(verify_publication(self.repo, result["run_id"])["commit_sha"],
                         publication["commit_sha"])
        return result, publication

    def snapshot(self):
        return {
            "head": git(self.repo, "rev-parse", "HEAD"),
            "status": git(self.repo, "status", "--porcelain", "--untracked-files=all"),
            "memory": {str(path.relative_to(self.repo)): path.read_bytes()
                       for path in (self.repo / "memory").rglob("*") if path.is_file()},
        }

    @staticmethod
    def documents(evidence):
        return {document["path"]: document for publication in evidence["publications"]
                for document in publication["documents"]}

    def test_three_domains_flow_uses_pinned_skills_and_only_selected_record_for_answers(self):
        self.assertEqual(self.bundle["skills"]["extract"]["ref"], "knowledge-extract@8")
        self.assertEqual(self.bundle["skills"]["review"]["ref"], "knowledge-review@6")
        result, publication = self.process_and_publish()
        self.assertEqual([role for role, _ in self.calls], ["extract", "review"])
        candidate = _load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        review = _load((self.repo / result["review_path"]).read_bytes(), "review")
        self.assertEqual(candidate["skill_bundle"], self.bundle)
        self.assertEqual(candidate["extraction_request_binding"], request_binding(*self.calls[0]))
        self.assertEqual(review["request_binding"], request_binding(*self.calls[1]))
        self.assertNotEqual(candidate["extraction"]["invocation_id"], review["call"]["invocation_id"])
        instructions = {check["kind"]: check["instruction"]
                        for check in self.calls[1][1]["required_checks"]}
        self.assertIn("Read this document without navigation or the processing conversation",
                      instructions["object_fidelity"])
        self.assertIn("Distinguish same-name fields", instructions["object_statistics_time"])
        self.assertIn("Each final record and Wiki must remain readable on its own",
                      instructions["global_organization"])
        catalog = build_domain_catalog(self.repo, self.paths, [result["run_id"]])
        self.assertEqual(catalog["classification"]["counts"]["records"], 3)
        before = self.snapshot()
        for source_path, spec in self.specs.items():
            with self.subTest(domain=spec["domain"]):
                record_path = "memory/records/" + spec["id"] + ".md"
                metadata, _ = parse_markdown((self.repo / record_path).read_bytes())
                self.assertEqual(metadata["domains"], [spec["domain"]])
                answer = ask_current(self.repo, self.paths, "这条用户计划是什么？", [result["run_id"]],
                                     object_paths=[record_path], compact_sources=True, model_call=self.model)
                evidence = self.calls[-1][1]["evidence"]
                self.assertEqual(set(self.documents(evidence)), {record_path})
                self.assertEqual(evidence["coverage"]["requested_object_paths"], [record_path])
                self.assertEqual(evidence["coverage"]["read_object_paths"], [record_path])
                self.assertEqual(evidence["coverage"]["dependency_object_paths"], [])
                self.assertEqual(evidence["coverage"]["read_source_paths"], [source_path])
                self.assertEqual(set(evidence["coverage"]["not_needed_source_paths"]),
                                 set(self.paths) - {source_path})
                self.assertTrue(evidence["coverage"]["object_selection_complete"])
                self.assertFalse(evidence["coverage"]["current_complete"])
                self.assertEqual(evidence["sources"][0]["source"]["body"], "")
                self.assertEqual(evidence["sources"][0]["source"]["content_scope"], "fragment")
                self.assertEqual(evidence["sources"][0]["lines"], [{"line": 1, "text": spec["quote"]}])
                self.assertEqual(answer["knowledge_status"], "published")
                self.assertEqual(answer["evidence_scope"], "explicit_current_view")
                self.assertEqual(answer["evidence_head"], before["head"])
                self.assertEqual(answer["evidence_publications"][0]["commit_sha"], publication["commit_sha"])
                for other in self.SPECS:
                    if other["id"] != spec["id"]:
                        self.assertNotIn(other["body"], json.dumps(evidence, ensure_ascii=False))
        self.assertEqual([role for role, _ in self.calls], ["extract", "review", "answer", "answer", "answer"])
        self.assertEqual(self.snapshot(), before)

    def test_selected_topic_reads_exact_record_dependencies_without_another_model_call(self):
        result, _ = self.process_and_publish()
        before = self.snapshot()
        evidence = build_current_evidence(self.repo, self.paths, [result["run_id"]],
                                          object_paths=[self.TOPIC], compact_sources=True)
        records = {"memory/records/" + spec["id"] + ".md" for spec in self.SPECS}
        documents = self.documents(evidence)
        self.assertEqual(set(documents), records | {self.TOPIC})
        self.assertEqual(evidence["coverage"]["requested_object_paths"], [self.TOPIC])
        self.assertEqual(set(evidence["coverage"]["dependency_object_paths"]), records)
        self.assertEqual(set(evidence["coverage"]["read_object_paths"]), set(documents))
        for dependency in documents[self.TOPIC]["metadata"]["records"]:
            record = documents["memory/records/" + dependency["id"] + ".md"]
            self.assertEqual(record["metadata"]["content_version"], dependency["content_version"])
        self.assertTrue(evidence["coverage"]["current_complete"])
        self.assertEqual([role for role, _ in self.calls], ["extract", "review"])
        self.assertEqual(self.snapshot(), before)

    def test_incremental_related_scope_reuses_records_and_distinguishes_history(self):
        old_paths = list(self.paths)
        old, old_publication = self.process_and_publish(related_runs=[])
        added = {"id": "writing-plan", "domain": "learning", "title": "练习写作",
                 "quote": "用户：我计划练习写作。", "body": "人工对话（2026-10-01）记载，用户计划练习写作。"}
        self.add_source(added)
        selected_paths = list(self.paths)
        unselected = self.add_source({"id": "unselected-private", "domain": "career",
                                      "title": "未选人工资料", "quote": "UNSELECTED_ARTIFICIAL_PRIVATE_SOURCE",
                                      "body": "UNSELECTED_ARTIFICIAL_PRIVATE_KNOWLEDGE"})
        updated, _ = self.process_and_publish(selected_paths, related_runs=[old["run_id"]])
        self.assertEqual([role for role, _ in self.calls], ["extract", "review", "extract", "review"])
        extraction = self.calls[2][1]
        self.assertEqual([item["run_id"] for item in extraction["related"]], [old["run_id"]])
        supplied = extraction["related"][0]["documents"]
        self.assertEqual({item["path"] for item in supplied}, set(old_publication["files"]))
        self.assertNotIn(unselected["path"], [item["path"] for item in extraction["sources"]])
        self.assertNotIn("UNSELECTED_ARTIFICIAL_PRIVATE_SOURCE", json.dumps(extraction, ensure_ascii=False))
        candidate = _load((self.repo / updated["stage_path"]).read_bytes(), "candidate")
        for spec in self.SPECS:
            self.assertEqual(candidate["operations"]["memory/records/" + spec["id"] + ".md"]["action"], "reuse")
        self.assertEqual(candidate["operations"][self.TOPIC]["action"], "update")
        before = self.snapshot()
        current = build_current_evidence(self.repo, selected_paths, [old["run_id"], updated["run_id"]],
                                         object_paths=[self.TOPIC], compact_sources=True)
        self.assertEqual([item["run_id"] for item in current["publications"]], [updated["run_id"]])
        self.assertIn({"run_id": old["run_id"], "reason": "historical_publication"},
                      current["coverage"]["unread_runs"])
        self.assertIn(added["body"], self.documents(current)[self.TOPIC]["body"])
        history = build_current_evidence(self.repo, old_paths, [old["run_id"]], history=True,
                                         object_paths=[self.TOPIC], compact_sources=True)
        self.assertEqual(history["scope"], "explicit_history_view")
        self.assertFalse(history["coverage"]["current_complete"])
        self.assertNotIn(added["body"], json.dumps(history, ensure_ascii=False))
        self.assertEqual({item["id"] for item in self.documents(history)[self.TOPIC]["metadata"]["records"]},
                         {spec["id"] for spec in self.SPECS})
        self.assertEqual(self.snapshot(), before)

    def test_related_run_outside_authorized_source_selection_stops_before_model_or_write(self):
        old, _ = self.process_and_publish(related_runs=[])
        saved = self.add_source({"id": "other-writing-plan", "domain": "learning", "title": "整理写作笔记",
                                 "quote": "用户：我计划整理写作笔记。",
                                 "body": "人工对话（2026-10-01）记载，用户计划整理写作笔记。"})
        before = self.snapshot()
        count = len(self.calls)
        with self.assertRaises(CaptureError) as caught:
            process_sources(self.repo, [saved["path"]], related_runs=[old["run_id"]],
                            skill_bundle=self.bundle, model_call=self.model)
        self.assertEqual(caught.exception.code, "evidence_source_mismatch")
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
