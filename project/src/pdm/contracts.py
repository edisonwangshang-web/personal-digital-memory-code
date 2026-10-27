"""M0 采集契约；仅定义已实现的来源和保存回执。"""

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

SCHEMA_VERSION = "0.1"
PIPELINE_SCHEMA_VERSION = "0.2"
MULTISOURCE_PIPELINE_SCHEMA_VERSION = "0.3"
M1_SCHEMA_VERSION = "0.4"
BUDGET_SCHEDULE_SCHEMA_VERSION = "0.5"
CONTINUOUS_SCHEMA_VERSION = "0.6"
ORCHESTRATED_SCHEMA_VERSION = "0.7"
DECOMPOSED_SCHEMA_VERSION = "0.8"
REVISION_SCHEMA_VERSION = "0.9"
QUALITY_SCHEMA_VERSION = "0.10"
REUSE_SCHEMA_VERSION = "0.11"
FEEDBACK_SCHEMA_VERSION = "0.12"
QUALITY_RECOVERY_SCHEMA_VERSION = "0.13"
ASSEMBLY_SCHEMA_VERSION = "0.14"
STATISTIC_REPAIR_SCHEMA_VERSION = "0.15"
TIMEOUT_RETRY_SCHEMA_VERSION = "0.16"
QUALITY_SCHEMA_VERSIONS = (QUALITY_SCHEMA_VERSION, REUSE_SCHEMA_VERSION, FEEDBACK_SCHEMA_VERSION, STATISTIC_REPAIR_SCHEMA_VERSION)
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION, PIPELINE_SCHEMA_VERSION, MULTISOURCE_PIPELINE_SCHEMA_VERSION, M1_SCHEMA_VERSION, BUDGET_SCHEDULE_SCHEMA_VERSION, CONTINUOUS_SCHEMA_VERSION, ORCHESTRATED_SCHEMA_VERSION, DECOMPOSED_SCHEMA_VERSION, REVISION_SCHEMA_VERSION, *QUALITY_SCHEMA_VERSIONS, QUALITY_RECOVERY_SCHEMA_VERSION, ASSEMBLY_SCHEMA_VERSION, TIMEOUT_RETRY_SCHEMA_VERSION)
MAX_SOURCE_BYTES = 1024 * 1024


def quality_schema_version(skill_bundle):
    skills = skill_bundle.get("skills", {}) if isinstance(skill_bundle, dict) else {}
    if not isinstance(skills, dict):
        return QUALITY_SCHEMA_VERSION
    if any(isinstance(skills.get(role), dict) and skills[role].get("ref") == ref
           for role, ref in (("extract", "knowledge-extract@11"), ("review", "knowledge-review@9"))):
        return STATISTIC_REPAIR_SCHEMA_VERSION
    if any(isinstance(skills.get(role), dict) and skills[role].get("ref") == ref
           for role, ref in (("extract", "knowledge-extract@10"), ("review", "knowledge-review@8"))):
        return FEEDBACK_SCHEMA_VERSION
    return (REUSE_SCHEMA_VERSION if any(isinstance(skills.get(role), dict) and skills[role].get("ref") == ref
            for role, ref in (("extract", "knowledge-extract@9"), ("review", "knowledge-review@7")))
            else QUALITY_SCHEMA_VERSION)


class CaptureError(ValueError):
    def __init__(self, code: str, message: str, *, details=None):
        super().__init__(message)
        self.code = code
        # Internal diagnostics only. Public CLI errors continue to expose the
        # existing code/message, never a private record or model response.
        self.details = deepcopy(details)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def source_contract(payload: dict) -> tuple[dict, str, str]:
    if not isinstance(payload, dict):
        raise CaptureError("invalid_input", "采集输入必须为对象")
    fields = {"source_key", "source_type", "platform", "original_url", "acquisition_status", "body", "original_time"}
    if set(payload) - fields:
        raise CaptureError("invalid_input", "采集输入含未定义字段")
    data = {key: payload.get(key) for key in sorted(fields)}
    for key in ("source_key", "source_type", "acquisition_status", "body"):
        if not isinstance(data[key], str):
            raise CaptureError("invalid_input", f"{key} 必须为字符串")
    key = data["source_key"]
    if not key or len(key) > 512 or any(ord(c) < 32 for c in key):
        raise CaptureError("invalid_input", "source_key 必须是非空稳定键，最多 512 字符")
    if data["source_type"] not in {"conversation", "note", "article", "video", "attachment_link"}:
        raise CaptureError("invalid_input", "不支持此 source_type")
    for field in ("platform", "original_url", "original_time"):
        if data[field] is not None and (not isinstance(data[field], str) or len(data[field]) > 4096):
            raise CaptureError("invalid_input", f"{field} 必须是字符串或 null")
    if data["original_time"] is not None:
        try:
            raw_time = data["original_time"]
            if re.fullmatch(r"\d{4}-\d{2}", raw_time):
                date.fromisoformat(raw_time + "-01")
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_time):
                date.fromisoformat(raw_time)
            else:
                original_time = datetime.fromisoformat(raw_time)
                if original_time.tzinfo is None or original_time.utcoffset() is None:
                    raise ValueError
        except ValueError as error:
            raise CaptureError("invalid_input", "original_time 必须是合法年月、日期、带时区的 ISO 8601 时间或 null") from error
    url = data["original_url"]
    if url and (urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc):
        raise CaptureError("invalid_input", "original_url 必须是 HTTP(S) 链接或 null")
    if url and (urlsplit(url).username is not None or urlsplit(url).password is not None):
        raise CaptureError("invalid_input", "original_url 不能包含用户名或密码")
    state = data["acquisition_status"]
    if state not in {"link_only", "partial_text", "full_text"}:
        raise CaptureError("invalid_input", "acquisition_status 无效")
    if (state == "link_only" and (not url or data["body"])) or (state != "link_only" and not data["body"]):
        raise CaptureError("invalid_input", "只有链接时正文应为空；取得文本时正文不能为空")
    encoded = canonical(data)
    if len(encoded) > MAX_SOURCE_BYTES:
        raise CaptureError("input_too_large", "单份来源不能超过 1 MiB")
    source_id = "src-" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    version = "sha256-" + hashlib.sha256(encoded).hexdigest()
    return data, source_id, version


def markdown(metadata: dict, body: str = "") -> bytes:
    return b"---\n" + canonical(metadata) + b"\n---\n" + body.encode("utf-8")


def parse_markdown(value: bytes) -> tuple[dict, str]:
    try:
        if not value.startswith(b"---\n"):
            raise ValueError
        header, body = value.decode("utf-8")[4:].split("\n---\n", 1)
        metadata = json.loads(header)
        if not isinstance(metadata, dict):
            raise ValueError
        return metadata, body
    except (UnicodeError, ValueError) as error:
        raise CaptureError("invalid_saved_file", "已有文件不符合来源格式") from error
