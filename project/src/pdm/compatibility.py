"""Bounded, read-only schema guard for the implemented managed-data locations.

This checks Markdown metadata headers, not source bodies, evidence semantics or
the whole memory tree. JSON data is deliberately unsupported and never opened.
"""

import json
import os
from pathlib import Path
import stat

from .contracts import CaptureError, SUPPORTED_SCHEMA_VERSIONS

MAX_HEADER_BYTES = 65536
MAX_SCAN_ENTRIES = 10000
MAX_SCAN_DEPTH = 32
MANAGED_DIRECTORIES = ("sources", "staging", "records", "topics", "runs")
_KNOWN_VERSIONS = frozenset({*SUPPORTED_SCHEMA_VERSIONS, "pdm-1"})
_IGNORED_NAMES = frozenset({"readme.md", ".gitkeep"})


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate metadata key")
        value[key] = item
    return value


def _header_version(descriptor):
    # No buffering: a metadata-only read must not prefetch private body bytes.
    with os.fdopen(descriptor, "rb", buffering=0) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        if stream.readline(4) != b"---\n":
            return None
        header = bytearray()
        while len(header) <= MAX_HEADER_BYTES:
            # Even at the limit, allow reading the four-byte closing marker.
            line = stream.readline(max(4, MAX_HEADER_BYTES - len(header) + 1))
            if not line:
                return None
            if line == b"---\n":
                metadata = json.loads(header.decode("utf-8"), object_pairs_hook=_unique_object)
                if isinstance(metadata, dict) and isinstance(metadata.get("schema_version"), str):
                    return metadata["schema_version"]
                return None
            header.extend(line)
    return None


def inspect_memory_schema(repo: Path) -> dict:
    """Inspect only managed metadata; unknown or inaccessible data blocks writes.

    README.md and .gitkeep inside these locations are documentation, not data.
    Root-level personal/domain templates are outside this probe's scope. No
    filenames, arbitrary metadata values or private body text are returned.
    """
    result = {
        "status": "compatible",
        "writable": True,
        "supported_schema_versions": list(SUPPORTED_SCHEMA_VERSIONS),
        "detected_known_versions": [],
        "scanned_metadata_files": 0,
        "unknown_files": 0,
        "ignored_files": 0,
        "scope": ["memory/" + name for name in MANAGED_DIRECTORIES] + ["memory/pending.md"],
        "path_rejected": False,
    }
    detected = set()
    entries_seen = 0
    exhausted = False
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow
    file_flags = os.O_RDONLY | nofollow | nonblock

    def unknown():
        result["unknown_files"] += 1

    def visit(parent, name, depth, *, directory=False, regular_file=False, optional=False):
        nonlocal entries_seen, exhausted
        if exhausted:
            return
        entries_seen += 1
        if entries_seen > MAX_SCAN_ENTRIES or depth > MAX_SCAN_DEPTH:
            unknown()
            exhausted = True
            return
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            if not optional:
                unknown()
            return
        except OSError:
            unknown()
            return
        try:
            if stat.S_ISLNK(info.st_mode):
                result["path_rejected"] = True
                unknown()
                return
            if stat.S_ISDIR(info.st_mode):
                if regular_file:
                    unknown()
                    return
                descriptor = os.open(name, directory_flags, dir_fd=parent)
                try:
                    with os.scandir(descriptor) as children:
                        for child in children:
                            visit(descriptor, child.name, depth + 1)
                            if exhausted:
                                break
                finally:
                    os.close(descriptor)
                return
            if directory or not stat.S_ISREG(info.st_mode):
                unknown()
                return
            if name.lower() in _IGNORED_NAMES:
                result["ignored_files"] += 1
                return
            result["scanned_metadata_files"] += 1
            if not name.lower().endswith(".md"):
                # Includes .json: do not read candidate bodies to find a schema.
                unknown()
                return
            descriptor = os.open(name, file_flags, dir_fd=parent)
            # _header_version also verifies and closes the opened object.
            version = _header_version(descriptor)
            if version in _KNOWN_VERSIONS:
                detected.add(version)
            if version not in SUPPORTED_SCHEMA_VERSIONS:
                if version not in _KNOWN_VERSIONS:
                    unknown()
        except (OSError, UnicodeError, ValueError, RecursionError):
            unknown()

    try:
        root = Path(repo).resolve()
        root_descriptor = os.open(root, directory_flags)
        try:
            # The memory directory itself must not redirect this bounded probe.
            try:
                info = os.stat("memory", dir_fd=root_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                info = None
            if info is not None:
                if stat.S_ISLNK(info.st_mode):
                    result["path_rejected"] = True
                    unknown()
                elif not stat.S_ISDIR(info.st_mode):
                    unknown()
                else:
                    memory = os.open("memory", directory_flags, dir_fd=root_descriptor)
                    try:
                        for name in MANAGED_DIRECTORIES:
                            visit(memory, name, 0, directory=True, optional=True)
                        visit(memory, "pending.md", 0, regular_file=True, optional=True)
                    finally:
                        os.close(memory)
        finally:
            os.close(root_descriptor)
    except (OSError, ValueError, RuntimeError):
        unknown()
    result["detected_known_versions"] = sorted(detected)
    if detected - set(SUPPORTED_SCHEMA_VERSIONS):
        result["status"] = "incompatible"
    elif result["unknown_files"]:
        result["status"] = "unknown"
    result["writable"] = result["status"] == "compatible"
    return result


def ensure_memory_compatible(repo: Path) -> dict:
    result = inspect_memory_schema(repo)
    if result["path_rejected"]:
        raise CaptureError("path_rejected", "记忆数据路径不能经过符号链接")
    if result["status"] == "incompatible":
        raise CaptureError("incompatible_memory_schema", "记忆包含本程序不支持的结构版本，已停止写入")
    if not result["writable"]:
        raise CaptureError("unknown_memory_schema", "记忆结构版本无法可靠确认，已停止写入")
    return result
