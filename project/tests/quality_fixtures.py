"""Synthetic model helpers only; these are never used by production transports."""

import copy


def add_quality(output, context):
    """Bind each synthetic target to existing records that cite its source."""
    output = copy.deepcopy(output)
    coverage = []
    for target in context["targets"]:
        records = [record for record in output["objects"]
                   if any(ref["source_id"] == target["source_id"]
                          and ref["source_version"] == target["source_version"]
                          for ref in record["sources"])]
        if not records:
            raise AssertionError("Synthetic fixture needs a record citing every quality target")
        coverage.append({
            "target_id": target["target_id"], "status": "summarized",
            "record_ids": [record["id"] for record in records],
            "body_evidence": [{"record_id": record["id"], "text": record["body"]}
                              for record in records],
            "reason": "",
        })
    output["quality"] = {"coverage_disposition": coverage, "statistic_claims": [], "temporal_claims": []}
    return output


def passed_review(required_checks, skill_bundle=None):
    result = {
        "verdict": "passed",
        "reasons": ["Synthetic independent Review inspected the fixed evidence and final object scope."],
        "checks": [{"check_id": check["check_id"], "scope_sha256": check["scope_sha256"],
                    "result": "passed",
                    "reason": "Synthetic check compared the fixed source evidence with the exact candidate.",
                    "evidence_ids": list(check["evidence_ids"])}
                   for check in required_checks],
    }
    if (skill_bundle is not None
            and (skill_bundle["skills"]["extract"]["ref"], skill_bundle["skills"]["review"]["ref"])
                in {("knowledge-extract@11", "knowledge-review@9"),
                    ("knowledge-extract@12", "knowledge-review@10"),
                    ("knowledge-extract@13", "knowledge-review@11"),
                    ("knowledge-extract@14", "knowledge-review@12"),
                    ("knowledge-extract@15", "knowledge-review@13"),
                    ("knowledge-extract@16", "knowledge-review@14")}):
        result["repair_scope"] = {"kind": "none", "missing_bindings": []}
    return result
