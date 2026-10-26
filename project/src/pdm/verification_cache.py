"""Operation-local reuse of immutable Git reads, never of current validity.

The cache only lives for one evidence build. HEAD, the working tree, controls,
and complete proof results remain uncached and are checked normally. A bounded
cache miss falls back to the same Git command; it cannot weaken a gate.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re


MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_ENTRIES = 8192
_CURRENT = ContextVar("pdm_immutable_git_reads", default=None)
_SHA = r"[0-9a-f]{40}(?:[0-9a-f]{24})?"
_COMMIT = re.compile(_SHA + r"(?:\^[0-9]*)?\Z")
_OBJECT = re.compile(_SHA + r"(?::[^\x00\n]+)?\Z")


@contextmanager
def immutable_read_scope():
    """Share only inside a synchronous read operation, then drop every byte."""
    if _CURRENT.get() is not None:
        yield
        return
    token = _CURRENT.set({"entries": {}, "bytes": 0})
    try:
        yield
    finally:
        _CURRENT.reset(token)


def scoped_immutable_reads(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with immutable_read_scope():
            return function(*args, **kwargs)
    return wrapped


def _fixed_command(args):
    """Conservative allowlist: every history anchor must be a full SHA.

    Mutable refs, implicit HEAD, wall-clock filters and unknown options always
    miss. Do not generalize this to arbitrary commands that look read-only.
    """
    if not args or not all(isinstance(arg, str) for arg in args):
        return False
    command, *rest = args
    if command == "show":
        # Whole-commit show renders a configuration-dependent diff. Actual
        # evidence reads always name the raw object at an explicit tree path.
        return len(rest) == 1 and ":" in rest[0] and _OBJECT.fullmatch(rest[0]) is not None
    if command == "cat-file":
        return len(rest) == 2 and rest[0] == "-s" and _OBJECT.fullmatch(rest[1]) is not None
    if command == "rev-parse":
        return len(rest) == 1 and _COMMIT.fullmatch(rest[0]) is not None
    if command == "merge-base":
        return (len(rest) == 3 and rest[0] == "--is-ancestor"
                and all(re.fullmatch(_SHA, item) is not None for item in rest[1:]))
    options = {
        "ls-tree": {"-r", "-z", "--name-only"},
        "rev-list": {"--parents"},
    }
    if command not in options:
        return False
    before_paths = rest[:rest.index("--")] if "--" in rest else rest
    if command == "ls-tree" and "-z" not in before_paths:
        # Without NUL output, core.quotePath changes how names are rendered.
        return False
    # log has several ordinary configuration-dependent traversal modes. Keep
    # every history query live rather than maintaining a configuration cache.
    anchors = 0
    index = 0
    while index < len(before_paths):
        item = before_paths[index]
        if command == "rev-list" and item == "-n" and before_paths[index:index + 2] == ["-n", "1"]:
            index += 2
            continue
        if item in options[command]:
            index += 1
            continue
        if _COMMIT.fullmatch(item):
            anchors += 1
            index += 1
            continue
        return False
    return anchors == 1


def _ordinary_object_graph(repo):
    """A SHA query is cacheable only without mutable history reinterpretation.

    Replacements, grafts and shallow boundaries can change a query's meaning
    without changing its SHA argument. Check before hits as well as misses;
    unfamiliar/worktree layouts just use normal Git instead of this shortcut.
    """
    if any(key in os.environ for key in (
            "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE", "GIT_NAMESPACE")):
        return False
    marker = Path(repo) / ".git"
    try:
        if marker.is_symlink() or not marker.is_dir():
            return False
        for relative in ("shallow", "info/grafts", "refs/replace", "commondir", "reftable"):
            target = marker / relative
            if target.exists() or target.is_symlink():
                return False
        packed = marker / "packed-refs"
        if packed.is_symlink():
            return False
        if packed.exists():
            # An unusually large or unreadable ref list only disables reuse.
            with packed.open("rb") as stream:
                refs = stream.read(1024 * 1024 + 1)
            if len(refs) > 1024 * 1024 or b" refs/replace/" in refs:
                return False
        return True
    except OSError:
        return False


def immutable_git_read(repo, args, index, read):
    """Return successful immutable output; never cache failures or index reads."""
    scope = _CURRENT.get()
    if (scope is None or index is not None or not _fixed_command(args)
            or not _ordinary_object_graph(repo)):
        return read()
    key = (str(repo), tuple(args))
    if key in scope["entries"]:
        return scope["entries"][key]
    result = read()
    if (isinstance(result, bytes) and len(scope["entries"]) < MAX_CACHE_ENTRIES
            and scope["bytes"] + len(result) <= MAX_CACHE_BYTES):
        scope["entries"][key] = result
        scope["bytes"] += len(result)
    return result


def _exact_json_key(value):
    """Preserve key order and JSON types; unknown Python values never hit."""
    active, nodes = set(), 0

    def valid(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > 64 or nodes > 200_000:
            return False
        kind = type(item)
        if kind in (dict, list):
            if id(item) in active:
                return False
            active.add(id(item))
            try:
                if kind is dict:
                    return all(type(key) is str and valid(child, depth + 1) for key, child in item.items())
                return all(valid(child, depth + 1) for child in item)
            finally:
                active.remove(id(item))
        return kind in (str, int, float, bool, type(None))

    try:
        if not valid(value, 0):
            return None
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(raw).digest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def frozen_request_binding(role, payload, render):
    """Reuse only a successful pure Skill rendering within the current read.

    The entire frozen bundle and exact JSON values form the key. This never
    reuses a source/proof validity decision or a legacy prompt read from disk.
    """
    scope = _CURRENT.get()
    digest = _exact_json_key(payload) if scope is not None else None
    if digest is None:
        return render()
    key = ("frozen-request-binding", role, digest)
    if key in scope["entries"]:
        return dict(scope["entries"][key])
    result = render()
    size = len(json.dumps(result).encode("utf-8"))
    if (len(scope["entries"]) < MAX_CACHE_ENTRIES and scope["bytes"] + size <= MAX_CACHE_BYTES):
        scope["entries"][key] = dict(result)
        scope["bytes"] += size
    return result
