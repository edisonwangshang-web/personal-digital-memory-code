"""Complete-source Review scopes and immutable historical contracts; artificial inputs."""

from copy import deepcopy
import hashlib
import json
import unittest

import test_decomposed_review as legacy
import test_knowledge_quality as fixtures
from quality_fixtures import add_quality, passed_review
from pdm.contracts import CaptureError, parse_markdown, quality_schema_version
from pdm.decomposed_review import (
    REQUEST_CONTRACT, SOURCE_REQUEST_CONTRACT, _certificate, _document, _hash,
    _layout, _plan, _request, _source_layout, _read,
)
from pdm.knowledge_quality import prepare_quality
from pdm.model_calls import render_model_request
from pdm.pipeline import _candidate_binding, _document as candidate_document
from pdm.skill_support import resolve_skill_refs


def material(pair=("knowledge-extract@12", "knowledge-review@10"), *, tables=True):
    bodies = (["date,health_metric\n2025-01-01,2\n2025-01-02,4\n",
               "date,work_metric\n2025-01-01,3\n2025-01-02,5\n"] if tables else [])
    bodies += ["Artificial source dictionary defines the supplied observations.\nRetain this unquoted exception.\n",
               "Artificial learning note records a plan, not a completed action.\nRetain its qualification.\n"]
    sources = [fixtures.source(body, "source-material-" + str(index)) for index, body in enumerate(bodies)]
    views = [fixtures.view(source, [(f"fragment-{index}-{line}", line, line)
                                   for line in range(1, len(source["body"].splitlines()) + 1)])
             for index, source in enumerate(sources)]
    context = prepare_quality(sources, source_views=views)
    records = [{"id": "artificial-" + str(index), "body": "Artificial bounded source summary " + str(index),
                "sources": [fixtures.ref(source)]} for index, source in enumerate(sources)]
    output = add_quality({"objects": records}, context)
    candidate = fixtures.candidate(output)
    for obj in candidate["objects"]:
        obj["metadata"]["content_version"] = "sha256-" + "0" * 64
    bundle = resolve_skill_refs(*pair)
    candidate.update(binding="sha256-" + "1" * 64, quality_contract="skill-quality-v1",
                     skill_bundle=bundle, quality_context=context)
    payload = {"sources": views, "quality_contract": "skill-quality-v1",
               "skill_bundle": bundle, "quality_context": context,
               "batch_context": {"source_ranges": [
                   {"fragment_id": fragment["fragment_id"], "path": view["path"],
                    "source_id": view["metadata"]["source_id"],
                    "source_version": view["metadata"]["source_version"]}
                   for view in views for fragment in view["fragments"]]}}
    return candidate, payload


def request_plan(candidate, payload, contract=SOURCE_REQUEST_CONTRACT):
    units, graph = (_source_layout(candidate, payload) if contract == SOURCE_REQUEST_CONTRACT
                    else _layout(candidate, payload))
    inventory = [{"path": obj["path"], "id": obj["metadata"]["id"],
                  "content_version": obj["metadata"]["content_version"]}
                 for obj in candidate["objects"]]
    return {"plan_id": "decomposed-" + "2" * 64, "inventory": inventory,
            "units": units, "relation_graph": graph, "request_contract": contract,
            "input_scope_inventory": [], "input_scope_inventory_sha256": _hash([])}


def certificates(plan, candidate, payload):
    result = []
    for unit in plan["units"][:-1]:
        request = _request(plan, unit, candidate, payload)
        output = passed_review(request["required_checks"], candidate["skill_bundle"])
        checked = {"quality_contract": "skill-quality-v1", "output": output,
                   "call": {"output_sha256": _hash(output)}, "request_sha256": _hash(request)}
        result.append(_certificate(unit, checked, {"sha256": "sha256-" + "3" * 64},
                                   request_contract=plan["request_contract"]))
    return result


class SourceMaterialTests(unittest.TestCase):
    def test_existing_v1_11_9_and_12_10_render_bytes_match_the_committed_renderer(self):
        # Captured from the isolated committed 2e4bc300 renderer, before v2.
        expected = {
            ("knowledge-extract@11", "knowledge-review@9"): "62e008afcb4040f49e267812fa8e65c2a30279810ccabb5eeb51f9ad35b5241d",
            ("knowledge-extract@12", "knowledge-review@10"): "5a4d2f9e546843c24cc5b7d6bd875e436fc386768a405df7c9e73a38de6e214f",
        }
        for pair, digest in expected.items():
            with self.subTest(pair=pair):
                candidate, payload = material(pair)
                plan = request_plan(candidate, payload, REQUEST_CONTRACT)
                self.assertEqual([unit["kind"] for unit in plan["units"]],
                                 ["object"] * 4 + ["coverage", "global"])
                rendered = [render_model_request("review", _request(plan, unit, candidate, payload))
                            for unit in plan["units"]]
                self.assertEqual(hashlib.sha256(b"\x00".join(rendered)).hexdigest(), digest)

    def test_each_table_is_complete_and_keeps_every_dictionary_and_exception(self):
        candidate, payload = material()
        units, _ = _source_layout(candidate, payload)
        coverage = [unit for unit in units if unit["kind"] == "coverage"]
        self.assertEqual(len(coverage), 2)
        groups = {view["path"]: {fragment["fragment_id"] for fragment in view["fragments"]}
                  for view in payload["sources"]}
        table_paths = {source["path"] for source in candidate["quality_context"]["sources"] if source["tables"]}
        background = set().union(*(ids for path, ids in groups.items() if path not in table_paths))
        expected = {frozenset(groups[path] | background) for path in table_paths}
        self.assertEqual({frozenset(unit["fragment_ids"]) for unit in coverage}, expected)
        self.assertEqual(set().union(*(set(unit["fragment_ids"]) for unit in coverage)),
                         {item["fragment_id"] for item in payload["batch_context"]["source_ranges"]})
        # Persisted extraction history is not another channel for source text
        # outside the current complete-source obligation.
        payload["prior_candidates"] = [{"run_id": "artificial-prior", "body": "UNLISTED_OLD_DRAFT",
            "batch_context": {"source_ranges": payload["batch_context"]["source_ranges"]}}]
        plan = request_plan(candidate, payload)
        for unit in coverage:
            request = _request(plan, unit, candidate, payload)
            self.assertEqual(request["candidate"]["objects"], candidate["objects"])
            self.assertEqual(request["prior_candidates"], [])
            self.assertNotIn("UNLISTED_OLD_DRAFT", json.dumps(request))
            for view in request["sources"]:
                self.assertEqual(view, next(original for original in payload["sources"]
                                            if original["path"] == view["path"]))
            self.assertTrue(request["quality_context"]["statistics"])

    def test_work_and_learning_prose_uses_complete_sources(self):
        candidate, payload = material(tables=False)
        coverage = [unit for unit in _source_layout(candidate, payload)[0] if unit["kind"] == "coverage"]
        self.assertEqual({frozenset(unit["fragment_ids"]) for unit in coverage},
                         {frozenset(fragment["fragment_id"] for fragment in view["fragments"])
                          for view in payload["sources"]})

    def test_global_retains_bodies_coverage_and_bound_conclusions_without_duplicate_statistics(self):
        candidate, payload = material()
        # Artificial declaration detail models already-verified statistics;
        # source/object units keep the corresponding ordinary declarations.
        candidate["extraction_output"]["quality"]["statistic_claims"] = [{
            "record_id": "artificial-0", "stat_id": candidate["quality_context"]["statistics"][0]["stat_id"],
            "sources": [fixtures.ref(fixtures.source("date,health_metric\n2025-01-01,2\n2025-01-02,4\n", "source-material-0"))],
            "value": "2", "body_evidence": "Artificial bounded source summary 0"}]
        plan = request_plan(candidate, payload)
        preceding = certificates(plan, candidate, payload)
        request = _request(plan, plan["units"][-1], candidate, payload, preceding)
        self.assertEqual(request["candidate"]["objects"], candidate["objects"])
        self.assertEqual(request["relation_graph"], plan["relation_graph"])
        self.assertEqual(request["candidate"]["quality"]["coverage_disposition"],
                         candidate["extraction_output"]["quality"]["coverage_disposition"])
        self.assertEqual(request["candidate"]["quality"]["statistic_claims"], [])
        summary = request["candidate"]["quality_declaration_scope"]["statistic_claims_certificate_summary"]
        self.assertEqual(summary["claim_count"], 1)
        self.assertEqual(summary["complete_claims_sha256"],
                         _hash(candidate["extraction_output"]["quality"]["statistic_claims"]))
        self.assertEqual(len(request["review_certificates"]), len(plan["units"]) - 1)
        self.assertTrue(all("checks" not in cert["output"] for cert in preceding))
        self.assertEqual(candidate["extraction_output"]["quality"]["statistic_claims"][0]["value"], "2")
        for altered in (preceding[:-1], list(reversed(preceding))):
            with self.assertRaisesRegex(CaptureError, "分解审核证明"):
                _request(plan, plan["units"][-1], candidate, payload, altered)
        nonpassed = deepcopy(preceding)
        nonpassed[0]["output"]["verdict"] = "needs_confirmation"
        with self.assertRaises(CaptureError):
            _request(plan, plan["units"][-1], candidate, payload, nonpassed)


class SourceContractLifecycleTests(unittest.TestCase):
    setUp = legacy.DecomposedReviewTests.setUp
    sources = legacy.DecomposedReviewTests.sources
    prepare = legacy.DecomposedReviewTests.prepare
    run_review = legacy.DecomposedReviewTests.run_review
    verify = legacy.DecomposedReviewTests.verify

    def model(self, role, payload):
        response = legacy.DecomposedReviewTests.model(self, role, payload)
        if role == "review" and "required_checks" in payload:
            response["output"] = passed_review(payload["required_checks"], payload["skill_bundle"])
        return response

    def freeze(self, pair=("knowledge-extract@12", "knowledge-review@10")):
        legacy.DecomposedReviewTests.freeze(self)
        quality = {"quality_contract": "skill-quality-v1", "skill_bundle": resolve_skill_refs(*pair),
                   "quality_context": prepare_quality(self.inputs, source_views=self.payload["sources"])}
        self.payload.update(deepcopy(quality))
        self.candidate.update(deepcopy(quality))
        self.candidate["extraction_output"] = add_quality(self.candidate["extraction_output"], quality["quality_context"])
        self.candidate["binding"] = _candidate_binding(self.candidate)
        with self.git.lock():
            self.stage = self.git.write_changes(
                {self.stage_path: candidate_document("candidate", self.candidate, quality_schema_version(quality["skill_bundle"]))},
                {self.stage_path: (self.repo / self.stage_path).read_bytes()}, self.git.check(), "artificial quality candidate")

    def test_new_plan_proof_and_nodes_use_19_and_reopen_without_model_calls(self):
        self.freeze()
        result = self.run_review()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["proof"]["schema_version"], "0.19")
        plan, data = _read(self.git, result["proof"]["plan_path"], "decomposed_review_plan")
        self.assertEqual(plan["request_contract"], SOURCE_REQUEST_CONTRACT)
        self.assertEqual(parse_markdown(data)[0]["schema_version"], "0.19")
        self.assertEqual(parse_markdown((self.repo / self.stage_path).read_bytes())[0]["schema_version"], "0.18")
        for node in result["proof"]["nodes"]:
            value, raw = _read(self.git, node["path"], "decomposed_review_result")
            self.assertEqual(parse_markdown(raw)[0]["schema_version"], "0.19")
            _, reservation = _read(self.git, value["reservation"]["path"], "decomposed_review_reservation")
            self.assertEqual(parse_markdown(reservation)[0]["schema_version"], "0.19")
        self.assertTrue(self.verify(result))
        for change in ("schema", "coverage"):
            altered = deepcopy(result)
            if change == "schema":
                altered["proof"]["schema_version"] = "0.18"
            else:
                altered["proof"]["nodes"].pop(-2)
            with self.subTest(change=change), self.assertRaises(CaptureError):
                self.verify(altered)
        before = self.git.head(), self.ledger.path.read_bytes(), self.number
        self.assertEqual(self.run_review()["attempted_calls"], 0)
        self.assertEqual((self.git.head(), self.ledger.path.read_bytes(), self.number), before)

    def test_saved_v1_12_10_plan_preserves_layout_requests_and_schema_18(self):
        self.freeze()
        plan = _plan(self.git, self.candidate, self.stage, self.payload, request_contract=REQUEST_CONTRACT)
        path = "memory/runs/processes/" + self.candidate["run_id"] + "/decomposed-plan.md"
        raw = _document("decomposed_review_plan", plan)
        static = [render_model_request("review", _request(plan, unit, self.candidate, self.payload))
                  for unit in plan["units"][:-1]]
        with self.git.lock():
            self.git.write_files({path: raw}, self.git.check(), "artificial persisted v1 plan")
        result = self.run_review()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["proof"]["schema_version"], "0.18")
        saved, after = _read(self.git, path, "decomposed_review_plan")
        self.assertEqual(after, raw)
        self.assertEqual([render_model_request("review", _request(saved, unit, self.candidate, self.payload))
                          for unit in saved["units"][:-1]], static)
        self.assertTrue(self.verify(result))

    def test_11_9_cannot_opt_into_v2_and_keeps_the_old_plan_contract(self):
        self.freeze(("knowledge-extract@11", "knowledge-review@9"))
        before = self.git.head(), self.ledger.path.read_bytes(), self.number
        with self.assertRaises(CaptureError):
            _plan(self.git, self.candidate, self.stage, self.payload, request_contract=SOURCE_REQUEST_CONTRACT)
        self.assertEqual((self.git.head(), self.ledger.path.read_bytes(), self.number), before)
        result = self.run_review()
        self.assertEqual(result["proof"]["schema_version"], "0.15")
        plan, _ = _read(self.git, result["proof"]["plan_path"], "decomposed_review_plan")
        self.assertEqual(plan["request_contract"], REQUEST_CONTRACT)
        self.assertTrue(self.verify(result))


if __name__ == "__main__":
    unittest.main()
