"""A new Skill/plan may repair quality declarations without replaying old drafts."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_pipeline
from engine_fixtures import isolated_engine, advance_engine
from pdm.batch_groups import publish_final_group, verify_group_publication
from pdm.batching import merge_batch_sources, plan_batches, project_batch_sources
from pdm.capture import capture
from pdm.contracts import CaptureError
from pdm.model_calls import request_binding
from pdm.pipeline import (_assert_substantive_revision, _load, _read_reviewed,
                          process_sources)
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit
from quality_fixtures import add_quality, passed_review


class QualityRevisionUpgradeTests(unittest.TestCase):
    setUp = test_pipeline.PipelineTests.setUp
    commit_change = test_pipeline.PipelineTests.commit_change

    def model(self, role, payload, timeout=120):
        self.number += 1
        self.roles.append(role)
        self.contexts.append((role, deepcopy(payload)))
        if role == "extract":
            records, wikis, by_source = [], [], {}
            for source in payload["sources"]:
                lines = {line["line"]: line["text"]
                         for fragment in source["fragments"] for line in fragment["lines"]}
                identity = source["metadata"]["source_id"]
                identifier = "count-" + identity[-12:]
                ref = {"source_id": identity, "source_version": source["metadata"]["source_version"],
                       "locator": "L1-L3", "quote": "\n".join(lines[number] for number in (1, 2, 3))}
                body = "The supplied table contains 2 data rows."
                record = {"id": identifier, "type": "knowledge", "title": "Artificial row count",
                          "source_kind": "external_source", "sources": [ref], "domains": ["health"],
                          "valid_from": None, "valid_to": None, "verification_status": "supported",
                          "lifecycle_status": "undetermined", "body": body}
                records.append(record)
                wikis.append({"id": "topic-" + identity[-12:], "title": "Artificial table",
                              "body": body, "sources": [ref], "record_ids": [identifier]})
                by_source[identity] = (record, {**ref, "locator": "L2-L3",
                                               "quote": lines[2] + "\n" + lines[3]})
            output = add_quality({"objects": records, "wikis": wikis}, payload["quality_context"])
            # The old final candidate intentionally lacks declarations for
            # correct body counts; children and the later revision include them.
            if len(records) == 1 or "revision_context" in payload:
                targets = {target["target_id"]: target for target in payload["quality_context"]["targets"]}
                for stat in payload["quality_context"]["statistics"]:
                    if stat["operation"] != "row_count":
                        continue
                    record, ref = by_source[targets[stat["target_id"]]["source_id"]]
                    output["quality"]["statistic_claims"].append({
                        "stat_id": stat["stat_id"], "record_id": record["id"], "value": stat["value"],
                        "body_evidence": record["body"], "sources": [ref]})
        else:
            output = passed_review(payload["required_checks"])
            if self.verdict != "passed":
                output["verdict"] = self.verdict
                output["checks"][0]["result"] = self.verdict
                output["reasons"] = ["Correct body counts need explicit statistic declarations."]
        return {"output": output, "model": "fixture-model", "usage": None, "elapsed_seconds": 0.01,
                "invocation_id": "model-" + f"{self.number:032x}"}

    def test_new_plan_and_skill_revise_only_statistics_keep_old_children_and_publish(self):
        code = isolated_engine(self)
        paths = sorted(capture(self.repo, {
            "source_key": f"fixture:revision-table:{index}", "source_type": "note",
            "acquisition_status": "full_text",
            "body": f"date,steps\n2026-01-01,{100 + index}\n2026-01-02,{200 + index}\n",
        })["path"] for index in range(4))
        old_bundle = resolve_skill_refs("knowledge-extract@5", "knowledge-review@4")
        old_plan = plan_batches(self.repo, paths, max_input_bytes=240000, max_sources=1, purpose="history")
        children = []
        for batch in old_plan["batches"]:
            projection = project_batch_sources(self.repo, old_plan, batch["batch_id"], source_paths=paths)
            child = process_sources(self.repo, [source["path"] for source in projection["source_views"]],
                batch_context=projection["batch_context"], source_views=projection["source_views"],
                draft_only=True, review_strategy="auto", skill_bundle=old_bundle, model_call=self.model)
            self.assertEqual(child["status"], "passed", child)
            children.append(child)
        merge_runs = sorted(child["run_id"] for child in children)
        old_projection = merge_batch_sources(self.repo, old_plan,
            [batch["batch_id"] for batch in old_plan["batches"]], source_paths=paths, max_bytes=240000)
        self.verdict = "needs_revision"
        rejected = process_sources(self.repo, paths, merge_runs=merge_runs,
            batch_context=old_projection["batch_context"], source_views=old_projection["source_views"],
            review_strategy="decomposed-v1", skill_bundle=old_bundle, model_call=self.model)
        self.assertEqual(rejected["status"], "needs_revision", rejected)
        old_candidate = _load((self.repo / rejected["stage_path"]).read_bytes(), "candidate")
        self.assertEqual(old_candidate["extraction_output"]["quality"]["statistic_claims"], [])
        old_requests = [(role, payload, request_binding(role, payload)) for role, payload in self.contexts]
        preserved_paths = [path for result in [*children, rejected]
                           for path in (result["stage_path"], result["review_path"])]
        preserved = {path: (self.repo / path).read_bytes() for path in preserved_paths}
        # Only a public engineering commit changes the loaded program identity.
        advance_engine(code)
        new_plan = plan_batches(self.repo, paths, max_input_bytes=240000, max_sources=1, purpose="history")
        self.assertNotEqual(new_plan["snapshot"]["versions"], old_plan["snapshot"]["versions"])
        self.assertNotEqual(new_plan["plan_id"], old_plan["plan_id"])
        new_projection = merge_batch_sources(self.repo, new_plan,
            [batch["batch_id"] for batch in new_plan["batches"]], source_paths=paths, max_bytes=240000)
        self.assertEqual(new_projection["source_views"], old_projection["source_views"])
        self.assertEqual(new_projection["batch_context"]["source_ranges"],
                         old_projection["batch_context"]["source_ranges"])
        self.assertNotEqual(new_projection["batch_context"]["parent_batch_ids"],
                            old_projection["batch_context"]["parent_batch_ids"])
        new_bundle = resolve_skill_refs("knowledge-extract@6", "knowledge-review@5")
        before = self.number
        self.verdict = "passed"
        revised = process_sources(self.repo, paths, revision_from_run=rejected["run_id"],
            merge_runs=merge_runs, batch_context=new_projection["batch_context"],
            source_views=new_projection["source_views"], review_strategy="auto",
            skill_bundle=new_bundle, model_call=self.model)
        self.assertEqual(revised["status"], "passed", revised)
        self.assertEqual(self.roles[before:], ["extract", "review"])
        git = PipelineGit(self.repo)
        candidate, _, _ = _read_reviewed(git, revised["run_id"])
        self.assertEqual(candidate["merge_inputs"], old_candidate["merge_inputs"])
        self.assertEqual(candidate["revision_context"]["candidate"], old_candidate)
        self.assertEqual(candidate["skill_bundle"], new_bundle)
        self.assertEqual(candidate["extraction_output"]["objects"], old_candidate["extraction_output"]["objects"])
        self.assertEqual(candidate["extraction_output"]["wikis"], old_candidate["extraction_output"]["wikis"])
        self.assertEqual(len(candidate["extraction_output"]["quality"]["statistic_claims"]), 4)
        unchanged = deepcopy(candidate)
        unchanged["extraction_output"]["quality"] = deepcopy(old_candidate["extraction_output"]["quality"])
        with self.assertRaises(CaptureError) as caught:
            _assert_substantive_revision(unchanged)
        self.assertEqual(caught.exception.code, "revision_unchanged")
        published = publish_final_group(self.repo, new_plan, candidate["objects"], [revised["run_id"]],
                                        source_paths=paths)
        with patch("pdm.skill_support.SKILLS_ROOT", self.repo / "no-installed-skills"):
            verified = verify_group_publication(self.repo, published["run_id"], source_paths=paths, historical=True)
            for role, payload, binding in old_requests:
                self.assertEqual(request_binding(role, payload), binding)
        authority = {entry["path"] for entry in verified["authority_snapshots"]}
        self.assertTrue(set(preserved_paths) <= authority)
        self.assertEqual(len(verified["files"]), 8)
        self.assertEqual(preserved, {path: (self.repo / path).read_bytes() for path in preserved_paths})
        with self.assertRaises(CaptureError) as caught:
            _read_reviewed(git, rejected["run_id"], historical=True)
        self.assertEqual(caught.exception.code, "review_not_passed")


if __name__ == "__main__":
    unittest.main()
