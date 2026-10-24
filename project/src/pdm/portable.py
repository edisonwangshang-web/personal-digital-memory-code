"""A fixed, explicit reading package shared with separately authorized models.

Preparing or verifying a package never calls a model. Integrity checks bind its
bytes; they are not an attestation that a receiver actually read or answered it.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .contracts import CaptureError, M1_SCHEMA_VERSION, canonical, timestamp
from .transaction import PipelineGit

MAX_PACKAGE_BYTES = 256_000
FRAGMENT_SCHEMA = "0.6"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _fail(code="invalid_reading_package"):
    raise CaptureError(code, "限定读取包校验失败；未调用模型或写入个人记忆")


def _questions(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 10
            or any(not isinstance(item, str) or not item.strip()
                   or len(item.encode("utf-8")) > 4096 for item in value)
            or len(set(value)) != len(value)):
        _fail("invalid_reading_questions")
    return value


def _file_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            _fail()
        value = stream.read(MAX_PACKAGE_BYTES + 1)
    if len(value) > MAX_PACKAGE_BYTES:
        _fail("reading_package_limit")
    return value


def _fragment_evidence(evidence, paths, context):
    """Validate exported coordinates without requiring unprovided source text.

    An external SHA pin attests the original export. These checks prevent a
    structurally truncated package from pretending its declared ranges exist.
    They do not authenticate a publisher who replaces both data and the pin.
    """
    if context is None:
        if ("batch_context" in evidence or any("source_view" in entry
                or entry.get("source", {}).get("content_scope") == "fragment" for entry in evidence["sources"])):
            _fail()
        for entry in evidence["sources"]:
            body = entry["source"].get("body")
            if not isinstance(body, str) or entry.get("lines") != [
                    {"line": number, "text": text} for number, text in enumerate(body.splitlines(), 1)]:
                _fail()
        return
    from .batching import _bounds
    from .evidence import _missing_line_ranges
    if (not isinstance(context, dict) or set(context) != {"plan_id", "batch_id", "source_ranges", "parent_batch_ids"}
            or not isinstance(context["plan_id"], str) or not re.fullmatch(r"plan-[0-9a-f]{64}", context["plan_id"])
            or not isinstance(context["batch_id"], str) or not re.fullmatch(r"batch-[0-9a-f]{64}", context["batch_id"])
            or not isinstance(context["parent_batch_ids"], list) or len(set(context["parent_batch_ids"])) != len(context["parent_batch_ids"])
            or any(not isinstance(item, str) or not re.fullmatch(r"batch-[0-9a-f]{64}", item) for item in context["parent_batch_ids"])
            or context["batch_id"] in context["parent_batch_ids"]
            or evidence.get("batch_context") != context
            or not isinstance(context["source_ranges"], list) or not 1 <= len(context["source_ranges"]) <= 10000):
        _fail()
    descriptors, identifiers = {}, set()
    for value in context["source_ranges"]:
        if (not isinstance(value, dict) or set(value) != {"path", "source_id", "source_version", "locator", "fragment_id"}
                or value["path"] not in paths or not isinstance(value["source_id"], str)
                or not re.fullmatch(r"src-[0-9a-f]{64}", value["source_id"])
                or not isinstance(value["source_version"], str) or not re.fullmatch(r"sha256-[0-9a-f]{64}", value["source_version"])
                or not isinstance(value["fragment_id"], str) or not re.fullmatch(r"fragment-[0-9a-f]{64}", value["fragment_id"])
                or value["fragment_id"] in identifiers):
            _fail()
        _bounds(value["locator"])
        identifiers.add(value["fragment_id"])
        descriptors.setdefault(value["path"], []).append(value)
    coverage = evidence["coverage"]
    if coverage.get("provided_source_ranges") != context["source_ranges"]:
        _fail()
    for entry in evidence["sources"]:
        source, marker = entry["source"], entry.get("source_view")
        if (source.get("body") != "" or source.get("content_scope") != "fragment"
                or not isinstance(marker, dict) or set(marker) != {"kind", "source_line_count", "fragments"}
                or marker["kind"] != "fragment" or type(marker["source_line_count"]) is not int
                or not 1 <= marker["source_line_count"] <= 1_048_576
                or not isinstance(marker["fragments"], list) or not marker["fragments"]
                or not isinstance(entry.get("lines"), list)):
            _fail()
        lines = {}
        for line in entry["lines"]:
            if (not isinstance(line, dict) or set(line) != {"line", "text"} or type(line["line"]) is not int
                    or not 1 <= line["line"] <= marker["source_line_count"] or not isinstance(line["text"], str)
                    or line["line"] in lines):
                _fail()
            lines[line["line"]] = line["text"]
        expected = descriptors.get(source["path"], [])
        if sorted(marker["fragments"], key=lambda fragment: fragment["locator"]) != sorted(
                [{key: value[key] for key in ("fragment_id", "locator")} for value in expected], key=lambda fragment: fragment["locator"]):
            _fail()
        used = set()
        for value in expected:
            if any(source.get(key) != value[key] for key in ("source_id", "source_version")):
                _fail()
            start, end = _bounds(value["locator"])
            if end - start + 1 > len(lines) or not all(number in lines for number in range(start, end + 1)):
                _fail()
            fragment_lines = [{"line": number, "text": lines[number]} for number in range(start, end + 1)]
            text_hash = "sha256-" + hashlib.sha256(canonical(fragment_lines)).hexdigest()
            material = {key: value[key] for key in ("path", "source_id", "source_version", "locator")}
            material["text_sha256"] = text_hash
            if value["fragment_id"] != "fragment-" + hashlib.sha256(canonical(material)).hexdigest():
                _fail()
            used.update(range(start, end + 1))
        if set(lines) != used:
            _fail()
        missing = _missing_line_ranges(marker["source_line_count"], lines)
        if missing != coverage.get("fragment_missing_ranges", {}).get(source["path"], []):
            _fail()
        if bool(missing) != (source["path"] in coverage.get("fragment_incomplete_source_paths", [])):
            _fail()


def _provided_references(evidence):
    """Formal text and full pending questions may only use provided evidence.

    An internally rehashed file is not authenticated. These checks nevertheless
    reject hiding a required source or range while retaining its derived text;
    the external SHA pin remains the authenticity boundary.
    """
    source_pattern = re.compile(r"memory/sources/(?:conversations|inbox)/(src-[0-9a-f]{64})/(sha256-[0-9a-f]{64})\.md\Z")
    object_pattern = re.compile(r"memory/(?:records|topics)/[a-z][a-z0-9-]{0,95}\.md\Z")
    digest_pattern = re.compile(r"sha256-[0-9a-f]{64}\Z")
    sources = {}
    for entry in evidence["sources"]:
        source = entry["source"]
        match = source_pattern.fullmatch(source["path"])
        if match is None or (source["source_id"], source["source_version"]) != match.groups():
            _fail()
        identity = (source["source_id"], source["source_version"])
        if identity in sources:
            _fail()
        sources[identity] = entry
    for publication in evidence["publications"]:
        files, documents = publication.get("files"), publication.get("documents")
        if (not isinstance(files, dict) or not 1 <= len(files) <= 256
                or any(not isinstance(path, str) or not object_pattern.fullmatch(path)
                       or not isinstance(digest, str) or not digest_pattern.fullmatch(digest) for path, digest in files.items())
                or not isinstance(documents, list) or len(documents) > len(files)):
            _fail()
        seen = set()
        for document in documents:
            if (not isinstance(document, dict) or document.get("path") not in files
                    or document["path"] in seen or document.get("sha256") != files[document["path"]]
                    or not isinstance(document.get("metadata"), dict) or not isinstance(document.get("body"), str)):
                _fail()
            seen.add(document["path"])
            refs = document["metadata"].get("sources")
            if not isinstance(refs, list) or not refs:
                _fail()
            for ref in refs:
                if not isinstance(ref, dict):
                    _fail()
                entry = sources.get((ref.get("source_id"), ref.get("source_version")))
                locator, quote = ref.get("locator"), ref.get("quote")
                match = re.fullmatch(r"L([1-9][0-9]*)-L([1-9][0-9]*)", locator) if isinstance(locator, str) else None
                if (entry is None or entry["source"].get("metadata", {}).get("acquisition_status") == "link_only"
                        or match is None or any(len(value) > 9 for value in match.groups())
                        or not isinstance(quote, str) or not quote.strip()):
                    _fail()
                lines = {line["line"]: line["text"] for line in entry["lines"]}
                start, end = map(int, match.groups())
                if not 1 <= start <= end or end - start + 1 > len(lines) or not all(number in lines for number in range(start, end + 1)):
                    _fail()
                if entry["source"].get("content_scope") == "fragment":
                    # Fragment records preserve line text, not original newline
                    # bytes. Normalize only newline spelling for this check.
                    given = "\n".join(lines[number] for number in range(start, end + 1)) + "\n"
                    if quote.replace("\r\n", "\n").replace("\r", "\n") not in given:
                        _fail()
                elif quote not in "".join(entry["source"]["body"].splitlines(keepends=True)[start - 1:end]):
                    _fail()
    pending = evidence.get("pending")
    if pending is not None:
        if not isinstance(pending, dict) or not isinstance(pending.get("items"), list):
            _fail()
        for item in pending["items"]:
            for ref in [*item["source_refs"], *item["reply_sources"]]:
                entry = sources.get((ref["source_id"], ref["source_version"]))
                if entry is None or entry["source"]["path"] != ref["path"]:
                    _fail()


def _parse(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                _fail()
            result[key] = item
        return result

    def invalid_constant(item):
        _fail()

    try:
        package = json.loads(value, object_pairs_hook=unique, parse_constant=invalid_constant)
        if not isinstance(package, dict) or set(package) != {"payload", "payload_sha256"}:
            _fail()
        payload = package["payload"]
        if (not isinstance(payload, dict) or set(payload) != {
                "schema_version", "kind", "data_head", "scope", "questions", "evidence",
                "answer_rules", "answer_rules_sha256", "created_at"}
                or payload["schema_version"] not in {M1_SCHEMA_VERSION, FRAGMENT_SCHEMA}
                or payload["kind"] != "scoped_reading_package"
                or not isinstance(payload["data_head"], str)
                or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", payload["data_head"])
                or not isinstance(payload["evidence"], dict)
                or not isinstance(payload["scope"], dict)
                or set(payload["scope"]) != ({"source_paths", "run_ids", "batch_context"}
                                             if payload["schema_version"] == FRAGMENT_SCHEMA else {"source_paths", "run_ids"})
                or not isinstance(payload["answer_rules"], str)):
            _fail()
        from .pipeline import RUN_RE, normalize_source_paths
        paths = payload["scope"]["source_paths"]
        maximum = 256 if payload["schema_version"] == FRAGMENT_SCHEMA else 8
        if normalize_source_paths(paths, allow_versions=True, max_sources=maximum) != paths:
            _fail()
        runs = payload["scope"]["run_ids"]
        if (not isinstance(runs, list) or len(runs) > maximum or runs != sorted(set(runs))
                or any(not isinstance(item, str) or not RUN_RE.fullmatch(item) for item in runs)):
            _fail()
        evidence = payload["evidence"]
        coverage = evidence.get("coverage")
        if (evidence.get("head") != payload["data_head"]
                or evidence.get("scope") != "explicit_current_view"
                or not isinstance(coverage, dict)
                or coverage.get("requested_source_paths") != paths
                or coverage.get("requested_runs") != runs
                or not isinstance(evidence.get("sources"), list)
                or not isinstance(evidence.get("publications"), list)):
            _fail()
        source_paths = [item["source"]["path"] for item in evidence["sources"]]
        if (len(source_paths) != len(set(source_paths)) or not set(source_paths) <= set(paths)
                or set(source_paths) != set(coverage.get("read_source_paths", []))
                or any(not isinstance(item, dict) or item.get("run_id") not in runs
                       for item in evidence["publications"])):
            _fail()
        if payload["schema_version"] == FRAGMENT_SCHEMA:
            _fragment_evidence(evidence, paths, payload["scope"]["batch_context"])
        else:
            # Legacy packages never carried fragment coordinates. Rehashing a
            # downgraded envelope must not bypass their structural validation.
            _fragment_evidence(evidence, paths, None)
        _provided_references(evidence)
        _questions(payload["questions"])
        from datetime import datetime
        recorded = datetime.fromisoformat(payload["created_at"])
        if recorded.tzinfo is None or recorded.utcoffset() is None:
            _fail()
        if hashlib.sha256(payload["answer_rules"].encode()).hexdigest() != payload["answer_rules_sha256"]:
            _fail()
        if hashlib.sha256(canonical(payload)).hexdigest() != package["payload_sha256"]:
            _fail()
        if canonical(package) + b"\n" != value:
            _fail()
        return package
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, CaptureError):
            raise
        _fail()


def prepare_reading_package(repo, source_paths, questions, run_ids=None, *, output,
                            source_views=None, batch_context=None):
    """Export only selected current evidence, questions and rules, without answers."""
    from .evidence import build_current_evidence
    from .pipeline import RUN_RE, normalize_source_paths
    paths = normalize_source_paths(source_paths, allow_versions=True, max_sources=256)
    _questions(questions)
    runs = [] if run_ids is None else run_ids
    if (not isinstance(runs, list) or len(runs) > 256
            or any(not isinstance(item, str) or not RUN_RE.fullmatch(item) for item in runs)
            or len(set(runs)) != len(runs)):
        _fail()
    runs = sorted(runs)
    git = PipelineGit(repo)
    head = git.head()
    if git.run("status", "--porcelain", "--untracked-files=all"):
        _fail("dirty_worktree")
    evidence = build_current_evidence(repo, paths, runs or None, source_views=source_views, batch_context=batch_context)
    rules_path = Path(__file__).resolve().parents[2] / "prompts" / "answer.md"
    if rules_path.is_symlink():
        _fail()
    rules = rules_path.read_text(encoding="utf-8")
    if git.head() != head or git.run("status", "--porcelain", "--untracked-files=all"):
        _fail("head_conflict")
    extended = source_views is not None or batch_context is not None or len(paths) > 8 or len(runs) > 8
    scope = {"source_paths": paths, "run_ids": runs}
    if extended:
        scope["batch_context"] = batch_context
    payload = {"schema_version": FRAGMENT_SCHEMA if extended else M1_SCHEMA_VERSION, "kind": "scoped_reading_package",
               "data_head": head, "scope": scope,
               "questions": questions, "evidence": evidence, "answer_rules": rules,
               "answer_rules_sha256": hashlib.sha256(rules.encode()).hexdigest(),
               "created_at": timestamp()}
    package = {"payload": payload, "payload_sha256": hashlib.sha256(canonical(payload)).hexdigest()}
    value = canonical(package) + b"\n"
    if len(value) > MAX_PACKAGE_BYTES:
        _fail("reading_package_limit")
    _parse(value)
    destination = Path(output).absolute()
    if destination.resolve().is_relative_to(git.repo):
        _fail("reading_package_output_scope")
    # Open every parent without following links, then create relative to that
    # descriptor. A changed symlink cannot redirect a prepared private package.
    parent = os.open(destination.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in destination.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(destination.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)
    return {"status": "prepared", "path": str(destination), "sha256": hashlib.sha256(value).hexdigest(),
            "payload_sha256": package["payload_sha256"], "bytes": len(value), "data_head": head,
            "source_paths": paths, "run_ids": runs, "questions": len(questions),
            "model_calls": 0, "second_model_reading": "unverified"}


def verify_reading_package(artifact, *, expected_sha256=None):
    """Verify a pinned export's bytes, without granting access to any model."""
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not _DIGEST.fullmatch(expected_sha256)):
        _fail()
    value = _file_bytes(artifact)
    digest = hashlib.sha256(value).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        _fail("reading_package_mismatch")
    package = _parse(value)
    payload = package["payload"]
    return {"status": "integrity_verified", "sha256": digest, "payload_sha256": package["payload_sha256"],
            "bytes": len(value), "data_head": payload["data_head"], **payload["scope"],
            "questions": len(payload["questions"]), "model_calls": 0,
            "publisher_authenticity": "not_attested", "second_model_reading": "unverified"}
