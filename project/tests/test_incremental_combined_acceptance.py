"""Artificial eight-source acceptance with content-aware extract/Review stubs.

The transports below never call a model. They intentionally describe each
provided field in a separate paragraph, and Review compares those paragraphs
and the update statements with the supplied evidence. Source identity alone
does not manufacture a summarized disposition or a passed verdict.
"""

from copy import deepcopy
import csv
import io
import json
import unittest

from engine_fixtures import isolated_engine
import test_production
from pdm.capture import capture
from pdm.contracts import parse_markdown
from pdm.evidence import build_current_evidence
from pdm.knowledge_quality import resolve_statistic
from pdm.pipeline import _load, _read_reviewed
from pdm.production import prepare_production, run_production
from pdm.skill_support import resolve_skill_refs
from pdm.transaction import PipelineGit


OLD = {
    "daily": ('\ufeff"date",时区,当日未完结,步数,平均压力\n'
              '2032-04-01,Asia/Shanghai,否,1000,10\n'
              '2032-04-02,Asia/Shanghai,否,2000,20\n'
              '2032-04-03,Asia/Shanghai,是,700,12\n'),
    "sleep": ('date,睡眠得分,睡眠分钟\n2032-04-01,70,400\n'
              '2032-04-02,75,420\n2032-04-03,72,410\n'),
    "scope": ('Artificial package A\nCalendar labels 2032-04-01 through 2032-04-03. '
              'The final daily date is partial; sleep date basis is unspecified. '
              'Date coverage does not prove complete wear.\n'),
    "dictionary": ('Artificial A dictionary\n步数 means steps, 平均压力 is the supplied stress score; '
                   '睡眠得分 is a score, 睡眠分钟 is minutes; -- means no supplied measurement.\n'),
}
NEW = {
    "daily": ('\ufeff"date",时区,当日未完结,步数,平均压力,最高心率\n'
              '2032-04-02,Asia/Shanghai,否,2000,20,144\n'
              '2032-04-03,Asia/Shanghai,否,3000,25,150\n'
              '2032-04-04,Asia/Shanghai,否,4000,30,155\n'
              '2032-04-05,Asia/Shanghai,是,500,8,100\n'),
    "sleep": ('wake_date,睡眠得分,睡眠分钟\n2032-04-02,75,420\n'
              '2032-04-03,80,430\n2032-04-04,--,--\n2032-04-05,85,450\n'),
    "scope": ('Artificial package B\nDaily calendar dates 2032-04-02 through 2032-04-05; '
              '2032-04-05 is partial. Sleep dates are wake dates. '
              'This note labels the sleep gap 2032-04-03, whereas the sleep CSV marks '
              '2032-04-04 missing; retain both source statements without resolving the conflict.\n'),
    "dictionary": ('Artificial B dictionary\n最高心率 is maximum heart rate in bpm. '
                   'The same supplied stress and sleep units apply; -- is missing, not zero. '
                   'Different sleep date bases must not be treated as the same night.\n'),
}
RECORDS = {kind: "artificial-" + kind for kind in OLD}


def source_text(source):
    if "body" in source:
        return source["body"]
    lines = {line["line"]: line["text"] for fragment in source["fragments"]
             for line in fragment["lines"]}
    return "\n".join(lines[number] for number in sorted(lines)) + "\n"


class IncrementalCombinedAcceptanceTests(unittest.TestCase):
    def setUp(self):
        test_production.ProductionTests.setUp(self)
        isolated_engine(self)
        self.labels = {}
        self.extract_count = {"initial": 0, "incremental": 0}
        self.mode = "complete"
        self.review_findings = []
        self.bundle = resolve_skill_refs()
        self.assertEqual("knowledge-extract@15", self.bundle["skills"]["extract"]["ref"])
        self.assertEqual("knowledge-review@13", self.bundle["skills"]["review"]["ref"])
        self.ledger.reserve("artificial-old-unknown", ["answer"])
        self.ledger.start("artificial-old-unknown", "answer", "0" * 64)
        self.ledger.finish("artificial-old-unknown", "answer", "unknown", error_code="timeout")
        self.old_unknown = self.ledger.operation("artificial-old-unknown")
        self.old_paths = self.capture_package("old", OLD)

    def capture_package(self, period, materials):
        paths = []
        for kind, text in materials.items():
            result = capture(self.repo, {"source_key": f"artificial:combined:{period}:{kind}",
                "source_type": "note", "acquisition_status": "full_text", "body": text})
            paths.append(result["path"])
            metadata, _ = parse_markdown((self.repo / result["path"]).read_bytes())
            self.labels[metadata["source_id"]] = (period, kind)
        return paths

    def source_ref(self, source):
        lines = source_text(source).splitlines()
        return {"source_id": source["metadata"]["source_id"],
                "source_version": source["metadata"]["source_version"],
                "locator": f"L1-L{len(lines)}", "quote": lines[0]}

    def field_paragraph(self, context, target):
        period, _ = self.labels[target["source_id"]]
        column, counts = target["column"], target["counts"]
        text = (f"{period} {column}: rows={counts['rows']}; present={counts['present']}; "
                f"blank={counts['blank']}; declared_missing={counts['declared_missing']}.")
        table = next(table for source in context["sources"] for table in source["tables"]
                     if table["table_id"] == target["table_id"])
        text += f" Source date interval={table['date_interval']}; unit={target.get('unit') or 'unspecified'}."
        claims = []
        for descriptor in context["statistics"]:
            if descriptor["target_id"] != target["target_id"]:
                continue
            operation = descriptor["operation"]
            if operation not in {"row_count", "present_count", "mean"}:
                continue
            statistic = resolve_statistic(context, descriptor)
            if operation == "mean":
                text += (f" Program mean={statistic['value']}; valid_n={statistic['valid_n']} "
                         "(same field and source period; partial-day rows excluded from descriptive samples).")
            claims.append(descriptor["stat_id"])
        return text, claims

    def update_statements(self, sources):
        lookup = {self.labels[source["metadata"]["source_id"]]: source for source in sources}
        if ("new", "daily") not in lookup:
            return {}
        old_rows = list(csv.DictReader(io.StringIO(source_text(lookup["old", "daily"]).lstrip("\ufeff"))))
        new_rows = list(csv.DictReader(io.StringIO(source_text(lookup["new", "daily"]).lstrip("\ufeff"))))
        prior = {row["date"]: row for row in old_rows}
        later = {row["date"]: row for row in new_rows}
        overlap = sorted(set(prior) & set(later))
        changed = next(day for day in overlap if prior[day]["步数"] != later[day]["步数"])
        historical = min(later)
        return {
            "new_dates": "New calendar dates: " + ", ".join(sorted(set(later) - set(prior)))
                         + "; final supplied day is partial.",
            "change": (f"{changed} steps: earlier partial snapshot {prior[changed]['步数']} -> later snapshot "
                       f"{later[changed]['步数']}; a later observation, not proof the old snapshot was erroneous."),
            "historical": (f"{historical} max_hr: {later[historical]['最高心率']} bpm; a newly supplied field "
                           "within these selected exports, not proof of absence elsewhere."),
        }

    def extraction(self, payload):
        incremental = any(self.labels[source["metadata"]["source_id"]][0] == "new"
                          for source in payload["sources"])
        phase = "incremental" if incremental else "initial"
        self.extract_count[phase] += 1
        first_bad = incremental and self.mode == "repair" and self.extract_count[phase] == 1
        sources = payload["sources"]
        context = payload["quality_context"]
        refs = {source["metadata"]["source_id"]: self.source_ref(source) for source in sources}
        paragraphs = {kind: [] for kind in RECORDS}
        claims, coverage = [], []
        for target in context["targets"]:
            period, kind = self.labels[target["source_id"]]
            if target["kind"] == "metric":
                text, stat_ids = self.field_paragraph(context, target)
            else:
                text = period + " source statement: " + source_text(next(source for source in sources
                    if source["metadata"]["source_id"] == target["source_id"])).strip()
                stat_ids = []
            deferred = ((first_bad and period == "new" and target.get("column") == "最高心率")
                        or (incremental and self.mode == "deferred" and period == "new"))
            record_id = RECORDS[kind]
            if deferred:
                coverage.append({"target_id": target["target_id"], "status": "deferred",
                    "record_ids": [], "body_evidence": [],
                    "reason": "Artificial processable source material was left unfinished in this draft."})
                continue
            paragraphs[kind].append(text)
            coverage.append({"target_id": target["target_id"], "status": "summarized",
                "record_ids": [record_id], "body_evidence": [{"record_id": record_id, "text": text}],
                "reason": ""})
            claims.extend({"stat_id": sid, "record_id": record_id, "body_evidence": text} for sid in stat_ids)
        if first_bad:
            # Deliberately make a structurally legal but semantically false
            # coverage mapping: another field from the same source is not a
            # summary of maximum heart rate. Content-aware Review must reject
            # it even though no target is mechanically declared deferred.
            wrong_excerpt = next(text for text in paragraphs["daily"] if text.startswith("new 步数:"))
            heart = next(target["target_id"] for target in context["targets"]
                         if self.labels[target["source_id"]] == ("new", "daily")
                         and target.get("column") == "最高心率")
            next(row for row in coverage if row["target_id"] == heart).update(
                status="summarized", record_ids=[RECORDS["daily"]],
                body_evidence=[{"record_id": RECORDS["daily"], "text": wrong_excerpt}], reason="")
        updates = self.update_statements(sources)
        if updates:
            paragraphs["daily"].extend(value for key, value in updates.items()
                                         if not (first_bad and key == "change"))
        objects = [{"id": record_id, "type": "knowledge", "title": "Artificial " + kind,
            "source_kind": "external_source", "domains": ["health"],
            "sources": [refs[source["metadata"]["source_id"]] for source in sources
                        if (self.labels[source["metadata"]["source_id"]][1] == kind
                            or kind in {"daily", "sleep"}
                            and self.labels[source["metadata"]["source_id"]][1] == "dictionary")],
            "valid_from": None, "valid_to": None, "verification_status": "unverified",
            "lifecycle_status": "undetermined", "body": "\n".join(paragraphs[kind])}
            for kind, record_id in RECORDS.items()]
        if incremental and self.mode == "omit":
            objects = [obj for obj in objects if obj["id"] != RECORDS["scope"]]
            # Keep quality internally valid for a different record citing the
            # same source; the independent old-record guard must still reject.
            dictionary = next(obj for obj in objects if obj["id"] == RECORDS["dictionary"])
            dictionary["body"] += "\n" + "\n".join(paragraphs["scope"])
            dictionary["sources"].extend(refs[source["metadata"]["source_id"]] for source in sources
                                          if self.labels[source["metadata"]["source_id"]][1] == "scope")
            for row in coverage:
                if row["record_ids"] == [RECORDS["scope"]]:
                    row["record_ids"] = [RECORDS["dictionary"]]
                    row["body_evidence"][0]["record_id"] = RECORDS["dictionary"]
        wiki = {"id": "artificial-monitoring", "title": "Artificial monitoring overview",
            "body": ("Artificial selected-export overview. Daily observations and sleep observations retain "
                     "their separate periods and date bases. Conflicting gap labels remain source-specific.\n"
                     + "\n".join(value for key, value in updates.items()
                                  if not (first_bad and key == "change"))),
            "sources": list(refs.values()), "record_ids": [obj["id"] for obj in objects]}
        return {"objects": objects, "wikis": [wiki], "reuse_refs": [],
                "quality": {"coverage_disposition": coverage, "statistic_claims": claims, "temporal_claims": []}}

    def review(self, payload):
        objects = {obj["metadata"]["id"]: obj for obj in payload["candidate"]["objects"]}
        quality = payload["quality_context"]
        target_index = {target["target_id"]: target for target in quality["targets"]}
        dispositions = {row["target_id"]: row for row in
                        payload["candidate"]["extraction_output"]["quality"]["coverage_disposition"]}
        missing = {}
        for target_id, target in target_index.items():
            period, kind = self.labels[target["source_id"]]
            row = dispositions[target_id]
            record = objects.get(RECORDS[kind])
            if target["kind"] == "metric":
                # Review does not reuse extraction's paragraph renderer. It
                # separately checks field identity, observed coverage and the
                # precise candidate excerpt against the fixed input inventory.
                counts = target["counts"]
                required = [f"{period} {target['column']}:",
                            f"rows={counts['rows']};", f"present={counts['present']};"]
                faithful = record is not None and any(
                    entry["record_id"] == RECORDS[kind] and entry["text"] in record["body"]
                    and all(token in entry["text"] for token in required)
                    for entry in row["body_evidence"])
            else:
                source = next(source for source in payload["sources"]
                              if source["metadata"]["source_id"] == target["source_id"])
                expected = period + " source statement: " + source_text(source).strip()
                faithful = record is not None and expected in record["body"] and any(
                    entry["text"] == expected for entry in row["body_evidence"])
            if row["status"] != "summarized" or not faithful:
                missing[target_id] = "Missing actual paragraph for " + target.get("column", kind)
        updates = self.update_statements(payload["sources"])
        daily = objects[RECORDS["daily"]]["body"]
        absent_updates = [key for key, text in updates.items() if text not in daily]
        issues = [*missing.values(), *("Missing update statement: " + key for key in absent_updates)]
        dictionaries = {source["metadata"]["source_id"] for source in payload["sources"]
                        if self.labels[source["metadata"]["source_id"]][1] == "dictionary"}
        for kind in ("daily", "sleep"):
            cited = {ref["source_id"] for ref in objects[RECORDS[kind]]["metadata"]["sources"]}
            if not dictionaries <= cited:
                issues.append("Missing dictionary premise in " + kind)
        self.review_findings.append(issues)
        checks = []
        for check in payload["required_checks"]:
            target_problem = any(target in missing for target in check["target_ids"])
            content_problem = bool(issues) and check["kind"] in {"global_organization", "object_fidelity"}
            if check["kind"] == "object_classification":
                self.assertTrue(all(objects[path.split("/")[-1][:-3]]["metadata"]["domains"] == ["health"]
                    for path in check["object_paths"] if path.startswith("memory/records/")))
            failed = target_problem or content_problem
            checks.append({"check_id": check["check_id"], "scope_sha256": check["scope_sha256"],
                "result": "needs_revision" if failed else "passed",
                "reason": "; ".join(issues) if failed else
                    "Artificial Review compared exact field paragraphs, update statements, source scope and classification.",
                "evidence_ids": list(check["evidence_ids"])})
        return {"verdict": "needs_revision" if issues else "passed",
            "reasons": issues or ["The selected artificial source fields and all update statements are present."],
            "checks": checks, "repair_scope": {"kind": "content" if issues else "none", "missing_bindings": []}}

    def transport(self, role, payload, timeout=120):
        self.number += 1
        self.roles.append(role)
        self.payloads.append((role, deepcopy(payload)))
        output = self.extraction(payload) if role == "extract" else self.review(payload)
        return {"output": output, "model": "artificial-no-external-model", "usage": None,
                "elapsed_seconds": 0.01, "invocation_id": "model-" + f"{self.number:032x}"}

    def baseline(self):
        prepare_production(self.repo, "initial", self.old_paths, use_default_skills=True)
        result = run_production(self.repo, "initial", model_call=self.adapter)
        self.assertEqual("published", result["status"], result)
        self.old_run = result["publication"]["run_id"]
        self.old_formal = {path: (self.repo / path).read_bytes() for path in result["publication"]["files"]}
        self.assertEqual(5, len(self.old_formal))
        self.old_record_metadata = {path: parse_markdown(data)[0]
                                    for path, data in self.old_formal.items() if path.startswith("memory/records/")}
        self.all_paths = self.old_paths + self.capture_package("new", NEW)
        prepared = prepare_production(self.repo, "incremental", self.all_paths,
            allowed_run_ids=[self.old_run], use_default_skills=True)
        self.assertEqual(1, prepared["coverage"]["batch_count"])
        self.assertEqual("ready", prepared["status"])

    def assert_formal_content(self, result):
        self.assertEqual("published", result["status"], result)
        daily = (self.repo / "memory/records/artificial-daily.md").read_text()
        sleep = (self.repo / "memory/records/artificial-sleep.md").read_text()
        scope = (self.repo / "memory/records/artificial-scope.md").read_text()
        dictionary = (self.repo / "memory/records/artificial-dictionary.md").read_text()
        self.assertIn("New calendar dates: 2032-04-04, 2032-04-05", daily)
        self.assertIn("2032-04-03 steps: earlier partial snapshot 700 -> later snapshot 3000", daily)
        self.assertIn("not proof the old snapshot was erroneous", daily)
        self.assertIn("2032-04-02 max_hr: 144 bpm", daily)
        self.assertIn("not proof of absence elsewhere", daily)
        self.assertIn("old 步数: rows=3; present=3", daily)
        self.assertIn("new 步数: rows=4; present=4", daily)
        self.assertIn("old 睡眠得分: rows=3; present=3", sleep)
        self.assertIn("new 睡眠得分: rows=4; present=3", sleep)
        self.assertIn("sleep date basis is unspecified", scope)
        self.assertIn("Sleep dates are wake dates", scope)
        self.assertIn("2032-04-04 missing", scope)
        self.assertIn("-- is missing, not zero", dictionary)
        for path, old in self.old_record_metadata.items():
            new, _ = parse_markdown((self.repo / path).read_bytes())
            self.assertEqual((old["id"], old["recorded_at"]), (new["id"], new["recorded_at"]))
            self.assertNotEqual(old["content_version"], new["content_version"])
        candidate, _, _ = _read_reviewed(PipelineGit(self.repo), result["final_run"])
        self.assertTrue(all(row["status"] == "summarized" for row in
                            candidate["extraction_output"]["quality"]["coverage_disposition"]))
        self.assertGreaterEqual(len(candidate["extraction_output"]["quality"]["statistic_claims"]), 20)
        self.assertTrue(all(operation["action"] == "update" for operation in candidate["operations"].values()))
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes()
        evidence = build_current_evidence(self.repo, self.all_paths, [result["publication"]["run_id"]])
        self.assertTrue(evidence["coverage"]["current_complete"])
        self.assertIn("max_hr: 144 bpm", json.dumps(evidence, ensure_ascii=False))
        self.assertEqual(before, (PipelineGit(self.repo).head(), self.ledger.path.read_bytes()))
        self.assertEqual(self.old_unknown, self.ledger.operation("artificial-old-unknown"))

    def test_eight_sources_publish_three_kinds_of_update_and_read_exact_formal_content(self):
        self.baseline()
        result = run_production(self.repo, "incremental", model_call=self.adapter)
        self.assert_formal_content(result)
        self.assertEqual({"initial": 1, "incremental": 1}, self.extract_count)
        self.assertEqual([[], []], self.review_findings)
        before = PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number
        self.assertEqual("published", run_production(self.repo, "incremental", model_call=self.adapter)["status"])
        self.assertEqual(before, (PipelineGit(self.repo).head(), self.ledger.path.read_bytes(), self.number))

    def test_content_review_repairs_two_omissions_together_without_losing_old_or_new_fields(self):
        self.baseline()
        self.mode = "repair"
        first = run_production(self.repo, "incremental", model_call=self.adapter, max_steps=1)
        self.assertEqual("needs_revision", first["status"], first)
        old, _, _ = _read_reviewed(PipelineGit(self.repo), first["active_run_id"], allow_nonpassed=True)
        self.assertTrue(all(row["status"] == "summarized" for row in
                            old["extraction_output"]["quality"]["coverage_disposition"]))
        self.assertTrue(any("最高心率" in finding for finding in self.review_findings[-1]))
        self.assertIn("Missing update statement: change", self.review_findings[-1])
        self.assertEqual(self.old_formal, {path: (self.repo / path).read_bytes() for path in self.old_formal})
        result = run_production(self.repo, "incremental", model_call=self.adapter)
        self.assert_formal_content(result)
        extracts = [payload for role, payload in self.payloads if role == "extract"]
        feedback = extracts[-1]["revision_context"]["review_record"]["output"]
        self.assertIn("Missing update statement: change", feedback["reasons"])
        self.assertNotIn("quality_repair_context", extracts[-1])
        self.assertEqual([], self.review_findings[-1])
        attempts = test_production.ProductionTests.attempts(self)
        incremental = next(row for row in attempts if len(row["attempts"]) == 2)
        self.assertEqual(["needs_revision", "passed"], [row["status"] for row in incremental["attempts"]])
        self.assertEqual([1, 2], [row["attempt"] for row in incremental["attempts"]])

    def test_all_new_content_deferred_is_rejected_with_specific_content_findings(self):
        self.baseline()
        self.mode = "deferred"
        result = run_production(self.repo, "incremental", model_call=self.adapter, max_steps=1)
        self.assertEqual("needs_revision", result["status"], result)
        self.assertGreater(len(self.review_findings[-1]), 4)
        self.assertEqual(self.old_formal, {path: (self.repo / path).read_bytes() for path in self.old_formal})
        self.assertEqual(self.old_unknown, self.ledger.operation("artificial-old-unknown"))

    def test_omitted_old_record_never_publishes_and_preserves_the_failed_response(self):
        self.baseline()
        self.mode = "omit"
        result = run_production(self.repo, "incremental", model_call=self.adapter, max_steps=1)
        self.assertEqual("failed", result["status"], result)
        self.assertEqual("related_records_omitted", result["last_result"]["error"])
        failure = _load((self.repo / result["last_result"]["failure_path"]).read_bytes(), "failure")
        diagnostic = failure["extraction_diagnostic"]
        self.assertEqual(["memory/records/artificial-scope.md"],
                         diagnostic["validation_error"]["details"]["missing_record_paths"])
        self.assertIn("output", diagnostic["response"])
        self.assertEqual(self.old_formal, {path: (self.repo / path).read_bytes() for path in self.old_formal})
        self.assertEqual(self.old_unknown, self.ledger.operation("artificial-old-unknown"))


if __name__ == "__main__":
    unittest.main()
