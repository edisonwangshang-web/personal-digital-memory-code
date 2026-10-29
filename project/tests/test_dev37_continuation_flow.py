"""A real temporary Git history repairs one old-policy synthetic rejection."""

from copy import deepcopy
import json
import unittest

import test_production
from engine_fixtures import isolated_engine, advance_engine
from quality_fixtures import add_quality, passed_review
from pdm.batch_groups import verify_group_publication
from pdm.capture import capture
from pdm.evidence import build_current_evidence
from pdm.pipeline import _read_reviewed, verify_publication
from pdm.production import prepare_production, run_production
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


class Dev37ContinuationFlowTests(unittest.TestCase):
    def setUp(self):
        test_production.ProductionTests.setUp(self)
        self.code = isolated_engine(self)
        self.path = capture(self.repo, {"source_key": "artificial:learning-policy-repair",
            "source_type": "note", "acquisition_status": "full_text",
            "body": '\ufeff"date",practice_min\n2028-01-01,6\n2028-01-02,8\n'})["path"]
        self.paths = [self.path]
        self.rejected = False

    def transport(self, role, payload, timeout=120):
        response = test_production.ProductionTests.transport(self, role, payload, timeout)
        if role == "extract":
            revised = "revision_context" in payload
            for record in response["output"]["objects"]:
                record["body"] = ("Artificial learning observations are limited to the supplied table."
                                  + (" This revision explicitly retains the source's bounded scope." if revised else ""))
            for topic in response["output"]["wikis"]:
                topic["body"] = "This artificial topic relates only the supplied learning observations."
            response["output"] = add_quality(response["output"], payload["quality_context"])
            response["output"]["reuse_refs"] = []
        else:
            output = passed_review(payload["required_checks"], payload["skill_bundle"])
            if not self.rejected:
                self.rejected = True
                output.update(verdict="needs_revision", reasons=["Artificial source scope wording needs repair."],
                              repair_scope={"kind": "content", "missing_bindings": []})
                output["checks"][0].update(result="needs_revision",
                    reason="Artificial independent content Review requires explicit source scope.")
            response["output"] = output
        return response

    def test_old_rejection_uses_next_same_work_attempt_with_new_parser_then_publishes_once(self):
        old_bundle = resolve_skill_refs("knowledge-extract@13", "knowledge-review@11")
        new_bundle = resolve_skill_refs("knowledge-extract@14", "knowledge-review@12")
        first_plan = prepare_production(self.repo, "old-policy", self.paths, skill_bundle=old_bundle,
                                        review_strategy="decomposed-v1")
        self.assertEqual("ready", first_plan["status"])
        first = run_production(self.repo, "old-policy", model_call=self.adapter, max_steps=1)
        self.assertEqual("needs_revision", first["status"], first)
        old_state = deepcopy(test_production.ProductionTests.state(self, "old-policy"))
        git = PipelineGit(self.repo)
        old, _, _ = _read_reviewed(git, first["active_run_id"], allow_nonpassed=True)
        self.assertEqual("source-quality-v1", old["quality_context"]["policy_version"])
        self.assertIsNone(old["quality_context"]["sources"][0]["tables"][0]["date_interval"])
        self.assertEqual({target["column"] for target in old["quality_context"]["targets"]},
                         {'"date"', "practice_min"})
        frozen_paths = [self.path, first["last_result"]["stage_path"], first["last_result"]["review_path"]]
        frozen = {path: (self.repo / path).read_bytes() for path in frozen_paths}
        counter_before = deepcopy(test_production.ProductionTests.attempts(self))
        self.assertEqual([1], [row["attempt"] for row in counter_before[0]["attempts"]])
        audit_before = json.loads(self.ledger.path.read_text())

        advance_engine(self.code)
        second_plan = prepare_production(self.repo, "repaired-policy", self.paths, skill_bundle=new_bundle,
                                         review_strategy="decomposed-v1")
        self.assertEqual("ready", second_plan["status"])
        self.assertNotEqual(first_plan["plan_id"], second_plan["plan_id"])
        second = run_production(self.repo, "repaired-policy", model_call=self.adapter, max_steps=1)
        self.assertEqual("ready", second["status"], second)
        counters = test_production.ProductionTests.attempts(self)
        self.assertEqual(len(counters), 1)
        self.assertEqual(counters[0]["attempts"][0], counter_before[0]["attempts"][0])
        self.assertEqual([1, 2], [row["attempt"] for row in counters[0]["attempts"]])
        new, _, _ = _read_reviewed(git, second["final_run"])
        self.assertEqual("source-quality-v2", new["quality_context"]["policy_version"])
        table = new["quality_context"]["sources"][0]["tables"][0]
        self.assertEqual(table["date_interval"], ["2028-01-01", "2028-01-02"])
        self.assertEqual(table["distinct_dates"], 2)
        self.assertEqual([target["column"] for target in new["quality_context"]["targets"]], ["practice_min"])
        self.assertEqual(new["revision_context"]["candidate"], old)
        self.assertNotIn("quality_repair_context", new)
        payload = [payload for role, payload in self.payloads if role == "extract"][-1]
        self.assertEqual(payload["revision_context"]["source_run_id"], first["active_run_id"])
        self.assertEqual(payload["revision_context"]["review_record"]["output"]["verdict"], "needs_revision")
        self.assertEqual(payload["quality_context"]["policy_version"], "source-quality-v2")
        self.assertEqual(payload["revision_context"]["candidate"]["quality_provenance"]["quality_context"]["policy_version"],
                         "source-quality-v1")
        self.assertEqual(old_state, test_production.ProductionTests.state(self, "old-policy"))
        self.assertEqual(frozen, {path: (self.repo / path).read_bytes() for path in frozen})
        for operation, original in audit_before["operations"].items():
            self.assertEqual(json.loads(self.ledger.path.read_text())["operations"][operation], original)

        published = run_production(self.repo, "repaired-policy", model_call=self.adapter)
        self.assertEqual("published", published["status"], published)
        group = published["publication"]["run_id"]
        self.assertEqual(len(verify_group_publication(self.repo, group, source_paths=self.paths)["files"]), 2)
        for historical in (False, True):
            self.assertEqual("published_local", verify_publication(self.repo, group,
                             source_paths=self.paths, historical=historical)["status"])
        self.assertTrue(build_current_evidence(self.repo, self.paths, [group])["coverage"]["current_complete"])
        before = git.head(), self.ledger.path.read_bytes(), self.number
        repeated = run_production(self.repo, "repaired-policy", model_call=self.adapter)
        self.assertEqual("published", repeated["status"])
        self.assertEqual(before, (git.head(), self.ledger.path.read_bytes(), self.number))
        self.assertEqual(frozen, {path: (self.repo / path).read_bytes() for path in frozen})


if __name__ == "__main__":
    unittest.main()
