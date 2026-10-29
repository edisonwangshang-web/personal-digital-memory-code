"""Artificial ordinary statistic claims expand without model-generated roots."""

from copy import deepcopy
import unittest

from pdm.contracts import CaptureError
from pdm.knowledge_quality import validate_quality_output
from pdm.skill_support import resolve_skill_refs
from pdm.statistic_materialization import CONTRACT, materialize_statistic_claims
from test_dev37_policy_integration import repair_fixture


class StatisticMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = repair_fixture(resolve_skill_refs("knowledge-extract@15", "knowledge-review@13"))
        self.raw = deepcopy(self.fixture.output)
        self.raw["quality"]["statistic_claims"] = [deepcopy(self.fixture.missing)]

    def materialize(self, raw=None, sources=None, **options):
        return materialize_statistic_claims(self.raw if raw is None else raw, self.fixture.quality,
            self.fixture.sources if sources is None else sources, **options)

    def test_expansion_keeps_raw_text_and_maps_value_to_actual_original_line(self):
        before = deepcopy((self.raw, self.fixture.sources))
        output, marker = self.materialize()
        self.assertEqual(CONTRACT, marker["contract"])
        self.assertEqual("0.22", marker["schema_version"])
        self.assertEqual(self.raw["objects"], output["objects"])
        self.assertEqual(self.raw["quality"]["coverage_disposition"], output["quality"]["coverage_disposition"])
        claim = output["quality"]["statistic_claims"][0]
        self.assertEqual({**self.fixture.missing, "value": "2", "sources": [{
            "source_id": self.fixture.sources[0]["metadata"]["source_id"],
            "source_version": self.fixture.sources[0]["metadata"]["source_version"],
            "locator": "L2-L2", "quote": '"2030-01-01","10"'}]}, claim)
        validate_quality_output(output, self.fixture.quality)
        self.assertNotEqual(marker["raw_output_sha256"], marker["materialized_output_sha256"])
        self.assertEqual(before, (self.raw, self.fixture.sources))

    def test_short_contract_rejects_old_full_claim_or_unrecognized_fields(self):
        for extra in ({"value": "2"}, {"sources": []}, {"unexpected": "value"}):
            raw = deepcopy(self.raw)
            raw["quality"]["statistic_claims"][0].update(extra)
            with self.subTest(extra=extra), self.assertRaises(CaptureError) as error:
                self.materialize(raw)
            self.assertEqual("invalid_short_statistic_claim", error.exception.code)

    def test_unknown_statistic_duplicate_and_unprovided_body_are_rejected(self):
        for change, code in (("stat", "statistic_materialization_statistic_missing"),
                             ("duplicate", "duplicate_short_statistic_claim"),
                             ("body", "quality_body_mapping_mismatch")):
            raw = deepcopy(self.raw)
            claim = raw["quality"]["statistic_claims"][0]
            if change == "stat":
                claim["stat_id"] = "stat-not-provided"
            elif change == "duplicate":
                raw["quality"]["statistic_claims"].append(deepcopy(claim))
            else:
                claim["body_evidence"] = "并不在固定正文里的2行"
            with self.subTest(change=change), self.assertRaises(CaptureError) as error:
                self.materialize(raw)
            self.assertEqual(code, error.exception.code)

    def test_entire_statistic_sample_must_be_retained_in_the_record_citations(self):
        raw = deepcopy(self.raw)
        raw["objects"][0]["sources"][0].update(locator="L2-L2", quote='"2030-01-01","10"')
        with self.assertRaises(CaptureError) as error:
            self.materialize(raw)
        self.assertEqual("quality_source_reference_mismatch", error.exception.code)

    def test_equal_numbers_do_not_authorize_another_record_or_source(self):
        raw = deepcopy(self.raw)
        raw["quality"]["statistic_claims"][0]["record_id"] = "table-1"
        with self.assertRaises(CaptureError) as error:
            self.materialize(raw)
        self.assertEqual("quality_source_reference_mismatch", error.exception.code)

    def test_changed_source_bytes_or_bom_cannot_reuse_fixed_quality(self):
        for change in ("value", "bom"):
            sources = deepcopy(self.fixture.sources)
            sources[0]["body"] = (sources[0]["body"].replace('"10"', '"11"') if change == "value"
                                   else sources[0]["body"].removeprefix("\ufeff"))
            with self.subTest(change=change), self.assertRaises(CaptureError) as error:
                self.materialize(sources=sources)
            self.assertEqual("statistic_materialization_context_changed", error.exception.code)

    def test_explicitly_reused_body_can_supply_its_own_exact_claim(self):
        raw = deepcopy(self.raw)
        raw["objects"].pop(0)
        raw["reuse_refs"] = [{"path": self.fixture.frozen[0]["path"],
                               "content_version": self.fixture.frozen[0]["metadata"]["content_version"]}]
        output, marker = self.materialize(raw, reused_objects=[self.fixture.frozen[0]])
        self.assertEqual(raw["objects"], output["objects"])
        self.assertEqual("table-0", output["quality"]["statistic_claims"][0]["record_id"])
        self.assertEqual(CONTRACT, marker["contract"])
        altered = deepcopy(self.fixture.frozen[0])
        altered["body"] = "该条人工正文已改，不再含原摘录。"
        with self.assertRaises(CaptureError):
            self.materialize(raw, reused_objects=[altered])

    def test_an_empty_claim_list_does_not_invent_associations_from_body_numbers(self):
        raw = deepcopy(self.raw)
        raw["quality"]["statistic_claims"] = []
        output, marker = self.materialize(raw)
        self.assertEqual(raw, output)
        self.assertEqual(marker["raw_output_sha256"], marker["materialized_output_sha256"])


class StatisticMaterializationPipelineTests(unittest.TestCase):
    def setUp(self):
        import test_skill_quality_pipeline
        from engine_fixtures import isolated_engine
        from pdm.capture import capture
        test_skill_quality_pipeline.SkillQualityPipelineTests.setUp(self)
        isolated_engine(self)
        self.bundle = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.path = capture(self.repo, {"source_key": "artificial:ordinary-statistic-claims",
            "source_type": "note", "acquisition_status": "full_text",
            "body": '\ufeff"date","steps"\n"2028-01-01","6"\n"2028-01-02","8"\n'})["path"]
        self.calls, self.full_shape, self.reuse_prior = [], False, False

    def model(self, role, payload):
        from quality_fixtures import add_quality, passed_review
        self.calls.append((role, deepcopy(payload)))
        if role == "extract":
            if self.reuse_prior:
                documents = [doc for run in payload["related"] for doc in run["documents"]]
                output = {"objects": [{**doc["metadata"], "body": doc["body"]} for doc in documents
                                      if doc["metadata"]["type"] != "wiki"],
                          "wiki": None, "reuse_refs": [{"path": doc["path"],
                              "content_version": doc["metadata"]["content_version"]} for doc in documents]}
                output = add_quality(output, payload["quality_context"])
                output["objects"] = []
                return {"output": output, "model": "artificial-test", "usage": None,
                        "elapsed_seconds": 0.01, "invocation_id": "model-" + f"{len(self.calls):032x}"}
            item = payload["sources"][0]
            ref = {"source_id": item["metadata"]["source_id"],
                   "source_version": item["metadata"]["source_version"],
                   "locator": "L1-L3", "quote": item["body"].splitlines()[0]}
            output = {"objects": [{"id": "artificial-learning-observations", "title": "人工学习观测",
                "type": "knowledge", "source_kind": "external_source", "domains": ["learning"],
                "valid_from": None, "valid_to": None, "verification_status": "unverified",
                "lifecycle_status": "undetermined", "sources": [ref], "body": "人工学习观测表扫描2行。"}],
                "wiki": {"id": "artificial-learning-topic", "title": "人工学习主题",
                    "body": "仅整理明确提供的人工学习观测表。", "sources": [ref]}, "reuse_refs": []}
            output = add_quality(output, payload["quality_context"])
            stat = next(row for row in payload["quality_context"]["statistics"] if row["operation"] == "row_count")
            claim = {"stat_id": stat["stat_id"], "record_id": "artificial-learning-observations",
                     "body_evidence": "扫描2行"}
            if self.full_shape:
                claim.update(value="2", sources=[{**ref, "locator": "L2-L2", "quote": item["body"].splitlines()[1]}])
            output["quality"]["statistic_claims"] = [claim]
        else:
            output = passed_review(payload["required_checks"], payload["skill_bundle"])
        return {"output": output, "model": "artificial-test", "usage": None,
                "elapsed_seconds": 0.01, "invocation_id": "model-" + f"{len(self.calls):032x}"}

    def process(self):
        from pdm.pipeline import process_sources
        return process_sources(self.repo, [self.path], related_runs=[],
                               model_call=self.model, skill_bundle=self.bundle)

    def test_raw_short_response_is_distinct_and_full_candidate_publishes_and_reads(self):
        from pdm.evidence import build_current_evidence
        from pdm.pipeline import _hash, _load, _read_reviewed, publish, verify_publication
        from pdm.transaction import PipelineGit
        result = self.process()
        self.assertEqual("passed", result["status"], result)
        stage = _load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        self.assertIn("statistic_claims_materialization", stage)
        raw = stage["raw_extraction_output"]
        full = stage["extraction_output"]
        self.assertEqual({"stat_id", "record_id", "body_evidence"},
                         set(raw["quality"]["statistic_claims"][0]))
        self.assertEqual({"stat_id", "record_id", "body_evidence", "value", "sources"},
                         set(full["quality"]["statistic_claims"][0]))
        self.assertEqual(_hash(raw), stage["extraction"]["output_sha256"])
        self.assertNotEqual(_hash(full), stage["extraction"]["output_sha256"])
        self.assertNotIn("statistic_claims_materialization", self.calls[1][1]["candidate"])
        current, _, _ = _read_reviewed(PipelineGit(self.repo), result["run_id"])
        self.assertEqual(stage, current)
        published = publish(self.repo, result["run_id"])
        self.assertEqual("published_local", published["status"])
        for historical in (False, True):
            self.assertEqual(published["commit_sha"],
                             verify_publication(self.repo, result["run_id"], historical=historical)["commit_sha"])
        evidence = build_current_evidence(self.repo, [self.path], [result["run_id"]])
        self.assertEqual("published", evidence["knowledge_status"])
        self.assertTrue(evidence["publications"][0]["documents"])
        self.assertEqual(["extract", "review"], [role for role, _ in self.calls])

    def test_materialization_marker_tampering_cannot_be_hidden_by_rebinding_candidate(self):
        from pdm.pipeline import _candidate_binding, _document, _load, _read_fixed_candidate
        from pdm.transaction import PipelineGit
        from test_pipeline import git
        result = self.process()
        self.assertEqual("passed", result["status"], result)
        path = self.repo / result["stage_path"]
        stage = _load(path.read_bytes(), "candidate")
        stage["statistic_claims_materialization"]["raw_output_sha256"] = "sha256-" + "0" * 64
        stage["binding"] = _candidate_binding(stage)
        path.write_bytes(_document("candidate", stage, "0.22"))
        git(self.repo, "add", result["stage_path"])
        git(self.repo, "commit", "-m", "artificial tampered materialization marker")
        with self.assertRaises(CaptureError) as error:
            _read_fixed_candidate(PipelineGit(self.repo), result["run_id"], historical=True)
        self.assertEqual("statistic_materialization_changed", error.exception.code)

    def test_previous_pair_keeps_native_full_claim_shape_without_new_marker(self):
        from pdm.pipeline import _load, _read_reviewed
        from pdm.transaction import PipelineGit
        self.bundle = resolve_skill_refs("knowledge-extract@14", "knowledge-review@12")
        self.full_shape = True
        result = self.process()
        self.assertEqual("passed", result["status"], result)
        stage = _load((self.repo / result["stage_path"]).read_bytes(), "candidate")
        self.assertNotIn("statistic_claims_materialization", stage)
        self.assertNotIn("raw_extraction_output", stage)
        self.assertIn("value", stage["extraction_output"]["quality"]["statistic_claims"][0])
        current, _, _ = _read_reviewed(PipelineGit(self.repo), result["run_id"])
        self.assertEqual(stage, current)

    def test_new_ordinary_materialization_precedes_exact_old_statistic_inheritance(self):
        from pdm.pipeline import _load, _read_reviewed, process_sources, publish, verify_publication
        from pdm.transaction import PipelineGit
        self.bundle = resolve_skill_refs("knowledge-extract@14", "knowledge-review@12")
        self.full_shape = True
        first = self.process()
        self.assertEqual("passed", first["status"], first)
        publish(self.repo, first["run_id"])
        self.bundle = resolve_skill_refs("knowledge-extract@15", "knowledge-review@13")
        self.reuse_prior = True
        second = process_sources(self.repo, [self.path], related_runs=[first["run_id"]],
                                 model_call=self.model, skill_bundle=self.bundle)
        self.assertEqual("passed", second["status"], second)
        stage = _load((self.repo / second["stage_path"]).read_bytes(), "candidate")
        self.assertEqual([], stage["raw_extraction_output"]["quality"]["statistic_claims"])
        self.assertEqual(CONTRACT, stage["statistic_claims_materialization"]["contract"])
        self.assertEqual(1, len(stage["reuse_statistic_inheritance"]["claims"]))
        self.assertEqual(1, len(stage["extraction_output"]["quality"]["statistic_claims"]))
        self.assertEqual("2", stage["extraction_output"]["quality"]["statistic_claims"][0]["value"])
        current, _, _ = _read_reviewed(PipelineGit(self.repo), second["run_id"])
        self.assertEqual(stage, current)
        published = publish(self.repo, second["run_id"])
        self.assertEqual(published["commit_sha"], verify_publication(self.repo, second["run_id"])["commit_sha"])
        self.assertEqual(published["commit_sha"],
                         verify_publication(self.repo, second["run_id"], historical=True)["commit_sha"])
        self.assertEqual(["extract", "review", "extract", "review"], [role for role, _ in self.calls])


if __name__ == "__main__":
    unittest.main()
