"""Statistical reuse obligations come from real temporary Git authority only."""

from copy import deepcopy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from git_fixtures import configure_test_repository
from quality_fixtures import add_quality, passed_review
from test_pipeline import git
from pdm.batching import plan_batches, project_batch_sources
from pdm.batch_groups import publish_final_group
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.knowledge_quality import prepare_quality, resolve_statistic, validate_quality_output
from pdm.model_calls import request_binding
from pdm.pipeline import (_load, _related_context, _objects, load_source, process_sources,
                          publish, verify_publication)
from pdm.reuse_quality import (collect_reuse_statistical_obligations, model_reuse_requirements,
                               validate_reuse_quality)
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class ReuseQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pdm-reuse-quality-")
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-b", "dev")
        configure_test_repository(self.repo)
        git(self.repo, "config", "user.name", "Artificial Statistical Fixture")
        git(self.repo, "config", "user.email", "stat-fixture@example.invalid")
        git(self.repo, "commit", "--allow-empty", "-m", "artificial baseline")
        self.saved = capture(self.repo, {"source_key": "artificial:table", "source_type": "note",
            "acquisition_status": "full_text", "body": "date,load_kg\n2025-01-01,10\n2025-01-02,20\n"})
        self.bundle = resolve_skill_refs("knowledge-extract@8", "knowledge-review@6")
        self.number, self.verdict = 0, "passed"
        self.roles, self.omit_claims = [], False
        self.record_id, self.reuse, self.replace = "artificial-measurement", False, False
        self.outputs = []
        self.old = process_sources(self.repo, [self.saved["path"]], model_call=self.model, skill_bundle=self.bundle)
        self.assertEqual(self.old["status"], "passed", self.old)
        self.publication = publish(self.repo, self.old["run_id"])
        self.refresh(self.old["run_id"], self.saved["path"])

    def model(self, role, payload):
        self.number += 1
        self.roles.append(role)
        if role == "review":
            output = passed_review(payload["required_checks"], payload.get("skill_bundle"))
            if self.verdict != "passed":
                output["verdict"] = self.verdict
                for check in output["checks"]:
                    check["result"] = self.verdict
        else:
            source = payload["sources"][0]
            lines = (source["body"].splitlines() if "body" in source else
                     [line["text"] for fragment in source["fragments"] for line in fragment["lines"]])
            identity = {key: source["metadata"][key] for key in ("source_id", "source_version")}
            ref = {**identity, "locator": "L1-L3", "quote": lines[0]}
            stats = [stat for stat in payload["quality_context"]["statistics"]
                     if stat["operation"] in {"mean", "row_count"}]
            body = "人工样本：" + "；".join(stat["operation"] + "=" + stat["value"] for stat in stats) + "。仅描述所选样本。"
            if self.replace:
                body = "人工文档仅说明所选表格位置，不引用任何派生统计。"
            record = {"id": self.record_id, "type": "knowledge", "title": "人工样本统计",
                      "source_kind": "user_statement", "sources": [ref], "domains": ["career"],
                      "valid_from": None, "valid_to": None, "verification_status": "unverified",
                      "lifecycle_status": "undetermined", "body": body}
            if getattr(self, "retitle", False):
                record["title"] = "人工改标题，正文统计不变"
            output = add_quality({"objects": [record], "wiki": {"id": self.record_id + "-topic",
                "title": "人工样本主题", "body": body, "sources": [ref]}}, payload["quality_context"])
            if not self.replace:
                output["quality"]["statistic_claims"] = [
                    {"stat_id": stat["stat_id"], "record_id": self.record_id, "value": stat["value"],
                     "body_evidence": stat["operation"] + "=" + stat["value"],
                     "sources": [{**identity, "locator": "L2-L2", "quote": lines[1]}]} for stat in stats]
            if payload.get("output_contract") == "batch-v1":
                output["wikis"] = [{**output.pop("wiki"), "record_ids": [self.record_id]}]
            if payload.get("skill_bundle", {}).get("skills", {}).get("extract", {}).get("ref") in {
                    "knowledge-extract@9", "knowledge-extract@10", "knowledge-extract@11", "knowledge-extract@12", "knowledge-extract@13"}:
                output["reuse_refs"] = []
            if self.reuse:
                documents = [doc for item in payload["related"] for doc in item["documents"]]
                batch = payload.get("output_contract") == "batch-v1"
                output = {"objects": [], "wikis" if batch else "wiki": [] if batch else None,
                          "quality": deepcopy(output["quality"]),
                          "reuse_refs": [{"path": doc["path"], "content_version": doc["content_version"]}
                                         for doc in documents]}
            if self.omit_claims:
                output["quality"]["statistic_claims"] = []
            self.outputs.append(deepcopy(output))
        return {"output": output, "model": "artificial-fixture", "elapsed_seconds": 0.01, "usage": None,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def refresh(self, run, source_path):
        self.git = PipelineGit(self.repo)
        self.sources = [load_source(self.repo, source_path)]
        self.context = prepare_quality(self.sources)
        self.related = _related_context(self.git, [run], self.sources)
        self.reused = [{key: deepcopy(doc[key]) for key in ("path", "metadata", "body")}
                       for item in self.related for doc in item["documents"]]
        self.output = {"quality": deepcopy(self.outputs[-1]["quality"])}

    def collect(self, **changes):
        args = {"git": self.git, "related": self.related, "sources": self.sources,
                "context": self.context, "reused_objects": self.reused}
        args.update(changes)
        return collect_reuse_statistical_obligations(**args)

    def validate(self, output=None):
        return validate_reuse_quality(self.git, self.related, self.sources, self.context,
                                      self.reused, self.output if output is None else output)

    def assert_error(self, code, call):
        with self.assertRaises(CaptureError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_exact_reuse_cannot_drop_old_claims_and_never_repairs_output(self):
        obligations = self.collect()
        self.assertEqual(len(obligations), 2)
        self.assertGreater(len(self.context["statistics"]), len(obligations))
        for claims in ([], self.output["quality"]["statistic_claims"][:1]):
            output = {"quality": {"statistic_claims": deepcopy(claims)}}
            before = deepcopy(output)
            with self.assertRaises(CaptureError) as caught:
                self.validate(output)
            self.assertEqual(caught.exception.code, "reuse_statistic_claims_missing")
            missing = sorted((item["record_id"], item["stat_id"])
                             for item in self.output["quality"]["statistic_claims"][len(claims):])
            self.assertEqual(caught.exception.details, {
                "field_path": "/quality/statistic_claims",
                "missing_claims": [{"record_id": record_id, "stat_id": stat_id}
                                   for record_id, stat_id in missing]})
            self.assertEqual(output, before)
        before = deepcopy(self.output)
        self.assertEqual(self.validate(), obligations)
        self.assertEqual(self.output, before)

    def test_compact_model_requirements_list_only_the_two_used_statistics(self):
        obligations = self.collect()
        requirements = model_reuse_requirements([*obligations, *obligations])
        self.assertEqual(len(requirements), 1)
        self.assertEqual(set(requirements[0]), {"record_id", "content_version", "stat_ids"})
        self.assertEqual(requirements[0]["record_id"], self.record_id)
        self.assertEqual(requirements[0]["stat_ids"], sorted(item["stat_id"] for item in obligations))

    def test_existing_quality_validator_still_checks_values_after_completeness(self):
        wrong = deepcopy(self.output)
        wrong["quality"]["statistic_claims"][0]["value"] = "999"
        self.assertEqual(len(self.validate(wrong)), 2)
        materialized = {"objects": [{**obj["metadata"], "body": obj["body"]} for obj in self.reused
                                    if obj["metadata"]["type"] != "wiki"],
                        "quality": {**deepcopy(self.outputs[0]["quality"]),
                                    "statistic_claims": wrong["quality"]["statistic_claims"]}}
        self.assert_error("quality_statistic_mismatch", lambda: validate_quality_output(materialized, self.context))

    def test_current_definition_must_match_more_than_stat_id_and_value(self):
        context = deepcopy(self.context)
        context["statistic_definitions"]["operations"]["mean"] = "A different artificial computation rule."
        obligations = self.collect(context=context)
        self.assertEqual(len(obligations), 1)
        remaining = next(stat for stat in context["statistics"] if stat["stat_id"] == obligations[0]["stat_id"])
        self.assertEqual(remaining["operation"], "row_count")

    def test_scope_is_checked_before_any_publication_verifier_opens_sources(self):
        with patch("pdm.pipeline.verify_publication", side_effect=AssertionError("must not open source")) as verifier:
            self.assert_error("reuse_quality_source_scope", lambda: self.collect(sources=[]))
        verifier.assert_not_called()

    def test_forged_related_bytes_or_commit_cannot_supply_obligations(self):
        for field in ("body", "commit"):
            related = deepcopy(self.related)
            if field == "body":
                related[0]["documents"][0]["body"] += "Forged text."
            else:
                related[0]["commit_sha"] = "0" * 40
            with self.subTest(field=field):
                self.assert_error("invalid_reuse_quality_authority", lambda: self.collect(related=related))

    def test_changed_reused_record_is_rejected_instead_of_waiving_its_old_claims(self):
        reused = deepcopy(self.reused)
        reused[0]["metadata"]["content_version"] = "sha256-" + "0" * 64
        self.assert_error("invalid_reuse_quality_authority", lambda: self.collect(reused_objects=reused))

    def test_newly_published_record_version_does_not_inherit_obsolete_claims(self):
        self.replace = True
        changed = process_sources(self.repo, [self.saved["path"]], related_runs=[self.old["run_id"]],
                                  model_call=self.model, skill_bundle=self.bundle)
        self.assertEqual(changed["status"], "passed", changed)
        publish(self.repo, changed["run_id"])
        self.refresh(changed["run_id"], self.saved["path"])
        self.assertEqual(self.collect(), [])
        self.assertEqual(self.validate({"quality": {"statistic_claims": []}}), [])

    def test_multi_generation_reuse_uses_verified_dag_and_deduplicates_claims(self):
        self.reuse = True
        current = process_sources(self.repo, [self.saved["path"]], related_runs=[self.old["run_id"]],
            model_call=self.model, skill_bundle=resolve_skill_refs("knowledge-extract@9", "knowledge-review@7"))
        self.assertEqual(current["status"], "passed", current)
        publish(self.repo, current["run_id"])
        self.refresh(current["run_id"], self.saved["path"])
        self.assertEqual(len(self.validate()), 2)

    def test_group_reuse_cannot_hide_nonincremental_ancestor_claims(self):
        self.group_reuse_bridge(self.model, resolve_skill_refs("knowledge-extract@9", "knowledge-review@7"))

    def group_reuse_bridge(self, model, bundle):
        plan = plan_batches(self.repo, [self.saved["path"]], max_input_bytes=20000,
                            payload_overhead_bytes=128, purpose="history")
        projected = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"],
                                          source_paths=[self.saved["path"]])
        self.reuse, self.omit_claims = True, True
        run = process_sources(self.repo, [self.saved["path"]], related_runs=[self.old["run_id"]],
            model_call=model, skill_bundle=bundle,
            batch_context=projected["batch_context"], source_views=projected["source_views"])
        self.assertEqual(run["status"], "passed", run)
        candidate = _load((self.repo / run["stage_path"]).read_bytes(), "candidate")
        group = publish_final_group(self.repo, plan, candidate["objects"], [run["run_id"]],
                                    source_paths=[self.saved["path"]])
        self.refresh(group["run_id"], self.saved["path"])
        self.assertEqual(len(self.collect()), 2)
        self.assert_error("reuse_statistic_claims_missing", self.validate)

    def test_legacy_full_reuse_bridge_cannot_hide_older_statistical_obligations(self):
        def legacy(role, payload):
            self.number += 1
            if role == "extract":
                output = deepcopy(self.outputs[0])
                output.pop("quality")
                output["wikis"] = [{**output.pop("wiki"), "record_ids": [self.record_id]}]
            else:
                output = {"verdict": "passed", "reasons": ["Artificial legacy independent Review."]}
            return {"output": output, "model": "artificial-legacy", "elapsed_seconds": 0.01, "usage": None,
                    "invocation_id": "model-" + f"{self.number:032x}"}
        # The legacy bridge has no quality claims, while its unchanged body is
        # still covered by the earlier quality publication's genuine proof.
        self.outputs.append({"quality": {"statistic_claims": []}})
        self.group_reuse_bridge(legacy, None)

    def test_new_contract_omission_fails_before_review_or_formal_writes(self):
        formal = {path: (self.repo / path).read_bytes() for path in self.publication["files"]}
        missing = sorted((item["record_id"], item["stat_id"])
                         for item in self.output["quality"]["statistic_claims"])
        self.assertEqual(len(missing), 2)
        self.omit_claims = True
        for extract_ref, review_ref in (("knowledge-extract@10", "knowledge-review@8"),
                                        ("knowledge-extract@11", "knowledge-review@9")):
            for self.reuse in (True, False):
                with self.subTest(extract=extract_ref, explicit_refs=self.reuse):
                    self.roles.clear()
                    calls = []

                    def observed_model(role, payload):
                        response = self.model(role, payload)
                        calls.append((role, deepcopy(payload), deepcopy(response)))
                        return response

                    result = process_sources(self.repo, [self.saved["path"]], related_runs=[self.old["run_id"]],
                        model_call=observed_model, skill_bundle=resolve_skill_refs(extract_ref, review_ref))
                    self.assertEqual((result["status"], result["error"]), ("failed", "reuse_statistic_claims_missing"))
                    self.assertEqual(self.roles, ["extract"])
                    self.assertEqual(self.outputs[-1]["quality"]["statistic_claims"], [])
                    failure = _load((self.repo / result["failure_path"]).read_bytes(), "failure")
                    diagnostic = failure["extraction_diagnostic"]
                    self.assertEqual(diagnostic["status"], "unvalidated_not_publishable")
                    self.assertEqual(diagnostic["response"], calls[0][2])
                    self.assertEqual(diagnostic["response"]["output"], self.outputs[-1])
                    self.assertEqual(diagnostic["request_binding"], request_binding(*calls[0][:2]))
                    self.assertEqual(diagnostic["validation_target"], "response.output")
                    self.assertEqual(diagnostic["validation_error"]["code"], "reuse_statistic_claims_missing")
                    self.assertEqual(diagnostic["validation_error"]["details"], {
                        "field_path": "/quality/statistic_claims",
                        "missing_claims": [{"record_id": record_id, "stat_id": stat_id}
                                           for record_id, stat_id in missing]})
                    self.assertFalse((self.repo / ("memory/staging/" + result["run_id"] + ".md")).exists())
                    self.assertEqual({path: (self.repo / path).read_bytes() for path in formal}, formal)

    def test_metadata_only_change_cannot_hide_unchanged_body_statistical_obligations(self):
        self.reuse = False
        self.retitle = True
        self.omit_claims = True
        self.roles.clear()
        result = process_sources(self.repo, [self.saved["path"]], related_runs=[self.old["run_id"]],
            model_call=self.model, skill_bundle=resolve_skill_refs("knowledge-extract@10", "knowledge-review@8"))
        self.assertEqual((result["status"], result["error"]), ("failed", "reuse_statistic_claims_missing"))
        self.assertEqual(self.roles, ["extract"])
        self.assertEqual(self.outputs[-1]["quality"]["statistic_claims"], [])

    def test_new_contract_complete_claims_survive_review_publish_and_historical_read(self):
        self.reuse = True
        self.roles.clear()
        result = process_sources(self.repo, [self.saved["path"]], related_runs=[self.old["run_id"]],
            model_call=self.model, skill_bundle=resolve_skill_refs("knowledge-extract@10", "knowledge-review@8"))
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(self.roles, ["extract", "review"])
        candidate = _load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        self.assertEqual(candidate["extraction_output"], self.outputs[-1])
        self.assertEqual(len(candidate["reuse_quality_requirements"][0]["stat_ids"]), 2)
        publication = publish(self.repo, result["run_id"])
        self.assertEqual(verify_publication(self.repo, result["run_id"], historical=True)["files"], publication["files"])
        self.assertEqual(candidate["extraction_output"], self.outputs[-1])

    def test_batch_group_authority_supplies_the_same_precise_obligations(self):
        saved = capture(self.repo, {"source_key": "artificial:group", "source_type": "note",
            "acquisition_status": "full_text", "body": "date,load_kg\n2025-01-01,30\n2025-01-02,50\n"})
        self.record_id = "group-measurement"
        plan = plan_batches(self.repo, [saved["path"]], max_input_bytes=20000, payload_overhead_bytes=128, purpose="history")
        projected = project_batch_sources(self.repo, plan, plan["batches"][0]["batch_id"], source_paths=[saved["path"]])
        run = process_sources(self.repo, [saved["path"]], related_runs=[], model_call=self.model, skill_bundle=self.bundle,
                              batch_context=projected["batch_context"], source_views=projected["source_views"])
        self.assertEqual(run["status"], "passed", run)
        candidate = _load((self.repo / run["stage_path"]).read_bytes(), "candidate")
        group = publish_final_group(self.repo, plan, candidate["objects"], [run["run_id"]], source_paths=[saved["path"]])
        self.refresh(group["run_id"], saved["path"])
        self.assertEqual(len(self.validate()), 2)

    def test_rejected_review_is_not_a_published_authority(self):
        self.verdict = "needs_revision"
        rejected = process_sources(self.repo, [self.saved["path"]], model_call=self.model, skill_bundle=self.bundle,
                                   related_runs=[self.old["run_id"]])
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        related = deepcopy(self.related)
        related[0]["run_id"] = rejected["run_id"]
        related[0]["manifest_path"] = "memory/runs/processes/" + rejected["run_id"] + "/manifest.md"
        with self.assertRaises(CaptureError):
            self.collect(related=related)

    def test_read_only_reconstruction_needs_no_model_or_external_cache(self):
        before = self.git.head(), self.git.run("status", "--porcelain"), self.number
        formal = {path: (self.repo / path).read_bytes() for path in self.publication["files"]}
        first = self.collect()
        with patch("pdm.model_calls.call_model", side_effect=AssertionError("no model")):
            self.assertEqual(self.collect(), first)
        self.assertEqual((self.git.head(), self.git.run("status", "--porcelain"), self.number), before)
        self.assertEqual({path: (self.repo / path).read_bytes() for path in formal}, formal)
        self.assertEqual(verify_publication(self.repo, self.old["run_id"], historical=True)["files"], self.publication["files"])


if __name__ == "__main__":
    unittest.main()
