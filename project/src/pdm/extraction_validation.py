"""Bounded, read-only diagnostics for the stable processing contract.

These probes explain an already rejected extraction. They neither repair its
evidence nor replace the normal candidate, control, or publication validation.
Only the caller's selected source snapshots are inspected.
"""

from copy import deepcopy

from .contracts import CaptureError, canonical

MAX_ISSUES = 32


def collect_issues(output, sources, *, quality_context=None, source_views=None,
                   related=(), versions=None, check_current=False, first_error=None,
                   limit=MAX_ISSUES):
    from .pipeline import ID_RE, _hash, _refs, _reuse_objects, _string, _time, _validate_domains

    if type(limit) is not int or not 1 <= limit <= MAX_ISSUES:
        raise ValueError("invalid diagnostic limit")
    issues, seen = [], set()
    truncated = False

    def add(error, *, path=None, record_id=None, ref_index=None, ref=None):
        nonlocal truncated
        details = deepcopy(error.details) if isinstance(error.details, dict) else {}
        details.pop("issues", None)
        details.pop("issues_truncated", None)
        if path is not None:
            details["field_path"] = path
        if record_id is not None:
            details["record_id"] = record_id
        if ref_index is not None:
            details["ref_index"] = ref_index
        if isinstance(ref, dict):
            details.update({key: ref[key] for key in ("source_id", "source_version", "locator")
                            if isinstance(ref.get(key), str)})
        issue = {"code": error.code, "message": str(error), **details}
        key = canonical({key: value for key, value in issue.items() if key != "message"})
        if key not in seen:
            seen.add(key)
            if len(issues) < limit:
                issues.append(issue)
            else:
                truncated = True

    def rejected(code, message, **kwargs):
        add(CaptureError(code, message), **kwargs)

    if not isinstance(output, dict) or not isinstance(output.get("objects"), list):
        if first_error is not None:
            add(first_error)
        else:
            rejected("invalid_model_output", "诊断需要候选对象列表", path="/objects")
        return {"issues": issues, "issues_truncated": truncated}

    # Malformed identities stop their object branch. No path or source is read
    # from a model-supplied name; related snapshots were authorized upstream.
    entries = []
    record_fields = {"id", "type", "title", "source_kind", "sources", "domains", "valid_from", "valid_to",
                     "verification_status", "lifecycle_status", "body"}
    identifiers = set()
    for index, value in enumerate(output["objects"][:21]):
        path = f"/objects/{index}"
        if not isinstance(value, dict):
            rejected("invalid_model_output", "知识对象必须是结构化记录", path=path)
            continue
        if set(value) != record_fields:
            rejected("invalid_model_output", "知识对象字段不完整或超出契约", path=path)
            continue
        for field, allowed in (("type", ("knowledge", "state", "event", "goal", "decision")),
                               ("source_kind", ("user_statement", "external_source", "model_inference")),
                               ("verification_status", ("unverified", "supported", "contested", "undetermined")),
                               ("lifecycle_status", ("undetermined", "current", "historical", "superseded", "retracted"))):
            if value[field] not in allowed:
                rejected("invalid_model_output", "知识对象枚举值无效", path=path + "/" + field,
                         record_id=value.get("id"))
        for field in ("valid_from", "valid_to", "title", "body"):
            try:
                if field in {"valid_from", "valid_to"}:
                    _time(value[field])
                else:
                    _string(value[field], 512 if field == "title" else 65536)
            except CaptureError as error:
                add(error, path=path + "/" + field, record_id=value.get("id"))
        if not isinstance(value["domains"], list) or len(value["domains"]) > 20:
            rejected("invalid_model_output", "分类必须是有界列表", path=path + "/domains", record_id=value.get("id"))
        identifier = value.get("id")
        if isinstance(identifier, str):
            if identifier in identifiers:
                rejected("invalid_object_id", "知识标识重复", path=path + "/id", record_id=identifier)
            identifiers.add(identifier)
        entries.append((path, value, value.get("id")))
    if len(output["objects"]) > 20:
        rejected("invalid_model_output", "知识对象数量超限", path="/objects")
    if "wikis" in output:
        if isinstance(output["wikis"], list):
            for index, value in enumerate(output["wikis"][:21]):
                if isinstance(value, dict):
                    if set(value) != {"id", "title", "body", "sources", "record_ids"}:
                        rejected("invalid_model_output", "主题字段不完整或超出契约", path=f"/wikis/{index}")
                        continue
                    entries.append((f"/wikis/{index}", value, value.get("id")))
                else:
                    rejected("invalid_model_output", "主题必须是结构化记录", path=f"/wikis/{index}")
        else:
            rejected("invalid_model_output", "主题必须是列表", path="/wikis")
    elif isinstance(output.get("wiki"), dict):
        if set(output["wiki"]) != {"id", "title", "body", "sources"}:
            rejected("invalid_model_output", "主题字段不完整或超出契约", path="/wiki")
        entries.append(("/wiki", output["wiki"], output["wiki"].get("id")))

    quality = output.get("quality")
    groups = [(path + "/sources", obj.get("sources"), rid, False)
              for path, obj, rid in entries]
    if isinstance(quality, dict):
        for component, field in (("statistic_claims", "sources"), ("temporal_claims", "source_refs")):
            claims = quality.get(component)
            if not isinstance(claims, list):
                continue
            for index, claim in enumerate(claims):
                if isinstance(claim, dict):
                    refs = claim.get(field)
                    # current_state may legitimately have no source_refs.
                    if component == "temporal_claims" and refs == []:
                        continue
                    groups.append((f"/quality/{component}/{index}/{field}", refs,
                                   claim.get("record_id"), True))
    for path, refs, rid, quality_refs in groups:
        if not isinstance(refs, list) or not 1 <= len(refs) <= 256:
            rejected("invalid_quality_output" if quality_refs else "invalid_model_output",
                     "引用必须是有界列表", path=path, record_id=rid)
            continue
        for index, ref in enumerate(refs):
            ref_path = f"{path}/{index}"
            try:
                _refs([ref], sources, field_path=path, record_id=rid, ref_offset=index)
                if source_views is not None:
                    from .batching import assert_fragment_refs
                    assert_fragment_refs([ref], source_views)
                if quality_refs and quality_context is not None:
                    from .knowledge_quality import _quality_refs
                    _quality_refs([ref], quality_context)
            except CaptureError as error:
                add(error, path=ref_path, record_id=rid, ref_index=index, ref=ref)

    reused = []
    if "reuse_refs" in output:
        try:
            reused = _reuse_objects(output["reuse_refs"], related, sources)
        except CaptureError as error:
            add(error, path="/reuse_refs")
    reused_records = [obj for obj in reused if obj["metadata"]["type"] != "wiki"]
    reused_wikis = [obj for obj in reused if obj["metadata"]["type"] == "wiki"]
    if not 1 <= len(output["objects"]) + len(reused_records) <= 20:
        rejected("invalid_model_output", "知识对象数量无效", path="/objects")
    wiki_entries = [(path, value, rid) for path, value, rid in entries if not path.startswith("/objects/")]
    if not 1 <= len(wiki_entries) + len(reused_wikis) <= (20 if "wikis" in output else 1):
        rejected("invalid_model_output", "主题数量无效", path="/wikis" if "wikis" in output else "/wiki")
    record_ids = {value.get("id") for path, value, _ in entries
                  if path.startswith("/objects/") and isinstance(value.get("id"), str)}
    record_ids.update(obj["metadata"]["id"] for obj in reused_records)
    for path, wiki, rid in wiki_entries:
        for field in ("title", "body"):
            try:
                _string(wiki.get(field), 512 if field == "title" else 65536)
            except CaptureError as error:
                add(error, path=path + "/" + field, record_id=rid)
        if isinstance(rid, str) and rid in identifiers:
            rejected("invalid_object_id", "知识和主题标识重复", path=path + "/id", record_id=rid)
        if isinstance(rid, str):
            identifiers.add(rid)
        if "wikis" in output:
            selected = wiki.get("record_ids")
            if (not isinstance(selected, list) or not selected
                    or any(not isinstance(key, str) for key in selected)
                    or len(set(selected)) != len(selected) or any(key not in record_ids for key in selected)):
                rejected("invalid_wiki_dependency", "主题依赖未对应实际返回记录",
                         path=path + "/record_ids", record_id=rid)

    # Domain and omission checks do not depend on successful quote matching.
    # Build only in-memory identities; this is not a usable candidate object.
    previous = {doc["path"]: doc for item in related for doc in item["documents"]}
    returned = {obj["path"] for obj in reused}
    for path, value, rid in entries:
        if not isinstance(rid, str) or ID_RE.fullmatch(rid) is None:
            rejected("invalid_object_id", "知识标识无效", path=path + "/id")
            continue
        folder = "records" if path.startswith("/objects/") else "topics"
        object_path = f"memory/{folder}/{rid}.md"
        returned.add(object_path)
        if folder != "records" or not isinstance(value.get("domains"), list):
            continue
        if any(not isinstance(label, str) for label in value["domains"]):
            rejected("invalid_model_output", "分类必须是字符串", path=path + "/domains", record_id=rid)
            continue
        for label in value["domains"]:
            try:
                _string(label, 128)
            except CaptureError as error:
                add(error, path=path + "/domains", record_id=rid)
        material = {key: val for key, val in value.items() if key != "body"}
        metadata = {**material, "content_version": _hash({"metadata": material, "body": value.get("body")})}
        probe = {"path": object_path, "metadata": metadata, "body": value.get("body")}
        old = previous.get(object_path)
        # Exact unchanged old metadata is checked by normal _updated_objects;
        # diagnostic objects intentionally cannot claim authoritative reuse.
        operations = {}
        if old is not None and metadata["content_version"] == old.get("content_version"):
            probe = deepcopy({"path": old["path"], "metadata": old["metadata"], "body": old["body"]})
            operations[object_path] = {"action": "reuse"}
        try:
            _validate_domains([probe], related, operations, versions or {})
        except CaptureError as error:
            if error.code not in {"invalid_domains", "invalid_domain_reuse"}:
                raise
            add(error, path=path + "/domains", record_id=rid)
    if check_current:
        required = {path for path in previous if path.startswith("memory/records/")}
        missing = sorted(required - returned)
        if missing:
            add(CaptureError("related_records_omitted", "候选遗漏已选定的旧知识", details={
                "missing_record_paths": missing, "required_record_paths": sorted(required),
                "returned_record_paths": sorted(path for path in returned if path.startswith("memory/records/"))}),
                path="/objects")

    if quality_context is not None:
        from .knowledge_quality import validate_quality_output, collect_temporal_diagnostics
        from .pipeline import _reuse_quality_output
        checked = _reuse_quality_output(output, reused) if "reuse_refs" in output else output
        try:
            validate_quality_output(checked, quality_context)
        except CaptureError as error:
            # An exact quality-reference failure was already located above.
            # Its early stop leaves other quality obligations unverified; do
            # not mistake that partial result for reference-only repair scope.
            if error.code == "quality_source_reference_mismatch":
                rejected("quality_validation_incomplete", "质量引文失败后其余语义对应仍需完整校验",
                         path="/quality")
            if not any(issue["code"] == error.code for issue in issues):
                add(error)
        for diagnostic in collect_temporal_diagnostics(checked, quality_context):
            details = diagnostic.get("details")
            add(CaptureError(diagnostic["code"], diagnostic["message"], details=details))

    if first_error is not None and not any(issue["code"] == first_error.code for issue in issues):
        add(first_error)
    if first_error is not None:
        issues.sort(key=lambda issue: issue["code"] != first_error.code)
    return {"issues": issues, "issues_truncated": truncated}
