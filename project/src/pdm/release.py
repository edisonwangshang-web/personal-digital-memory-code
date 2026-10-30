"""Build and check development code artifacts without reading working-tree files.

An artifact is not a deployment or a business-acceptance result. Hash verification
checks internal consistency, not publisher authenticity. A trusted caller can pin
the expected artifact hash and/or commit when verifying a downloaded artifact.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import tomllib
from typing import Any, Sequence
import zipfile


MANIFEST_NAME = "RELEASE-MANIFEST.json"
_CODE_ROOT = "project/src/pdm"
_RULE_ROOT = "project/prompts"
_SKILL_ROOT = "project/skills"
_ROOT_FILES = {"pyproject.toml", "README.md", "project/src/README.md"}
_MAX_FILES = 2000
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_FORBIDDEN_PARTS = {"memory", "fixtures", "validation", "__pycache__"}
_SECRET_NAMES = {"secret", "secrets", "credential", "credentials", "password", "passwords"}
_CAPABILITIES = {"source_capture", "reviewed_publication", "scoped_evidence_qa",
                 "multisource_reviewed_publication", "multisource_scoped_qa",
                 "incremental_reviewed_publication", "controlled_current_qa",
                 "correction_deletion_controls", "unified_pending",
                 "daily_incremental_engine", "scoped_reading_packages",
                 "shared_persistent_call_budget", "skill_bound_knowledge_quality"}


class ReleaseError(ValueError):
    """The requested code snapshot or artifact is invalid or unsafe."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_path(name: str) -> bool:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        return False
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return False
    parts = name.split("/")
    return not name.startswith("/") and all(
        p not in {"", ".", ".."} and not p.endswith((" ", ".")) for p in parts
    )


def _in_scope(name: str) -> bool:
    return name in _ROOT_FILES or any(
        name == root or name.startswith(root + "/") for root in (_CODE_ROOT, _RULE_ROOT, _SKILL_ROOT)
    )


def _allowed(name: str) -> bool:
    if not _safe_path(name):
        return False
    parts = PurePosixPath(name).parts
    if any(p.startswith(".") or p.lower() in _FORBIDDEN_PARTS for p in parts):
        return False
    if any(re.split(r"[._-]", p.lower())[0] in _SECRET_NAMES for p in parts):
        return False
    if name in _ROOT_FILES:
        return True
    if name.startswith(_CODE_ROOT + "/"):
        return name.endswith(".py")
    if name.startswith(_SKILL_ROOT + "/"):
        return PurePosixPath(name).suffix.lower() in {".md", ".json"}
    return name.startswith(_RULE_ROOT + "/") and PurePosixPath(name).suffix.lower() in {
        ".md", ".txt"
    }


def _check_payload(name: str, data: bytes) -> None:
    """Reject common credential material; this is not a general secret audit."""
    if re.search(rb"(?m)^-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", data):
        raise ReleaseError(f"Credential material is not allowed in artifact file: {name}")
    if re.search(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-)?[A-Za-z0-9_-]{32,})\b", data):
        raise ReleaseError(f"Credential material is not allowed in artifact file: {name}")
    if re.search(rb'''(?im)^\s*["']?(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password)["']?\s*[:=]\s*["'][^"'\r\n]+["']''', data):
        raise ReleaseError(f"Credential material is not allowed in artifact file: {name}")
    if len(data) > _MAX_FILE_BYTES:
        raise ReleaseError(f"Artifact file is too large: {name}")


def _git(repo: Path, *args: str) -> bytes:
    # No inherited Git setting may redirect the requested repository, index or
    # object database. These operations are local reads and need no Git auth env.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=False,
            env=environment, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(f"Git operation did not complete: {args[0]}") from exc
    if result.returncode:
        # Git errors may contain private paths or credential-bearing remote URLs.
        raise ReleaseError(f"Git operation failed: {args[0]}")
    return result.stdout


def _snapshot(repo: Path, ref: str) -> tuple[str, dict[str, bytes]]:
    if not isinstance(ref, str) or not ref or ref.startswith("-") or any(ord(c) < 32 for c in ref):
        raise ReleaseError("A valid Git commit reference is required")
    commit = _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    if not _COMMIT.fullmatch(commit):
        raise ReleaseError("Git did not resolve a full commit ID")
    files: dict[str, bytes] = {}
    for entry in _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        try:
            name = raw_name.decode("utf-8")
        except UnicodeError as exc:
            raise ReleaseError("Git snapshot contains a non-UTF-8 path") from exc
        if not _in_scope(name):
            continue
        mode, kind, object_id = metadata.decode("ascii").split()
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ReleaseError(f"Symlinks and non-regular entries are forbidden: {name}")
        if not _allowed(name):
            raise ReleaseError(f"File is not allowed in a code artifact: {name}")
        if int(_git(repo, "cat-file", "-s", object_id)) > _MAX_FILE_BYTES:
            raise ReleaseError(f"Artifact file is too large: {name}")
        data = _git(repo, "cat-file", "blob", object_id)
        _check_payload(name, data)
        files[name] = data
    _check_inventory(files)
    return commit, files


def _check_inventory(files: dict[str, bytes]) -> None:
    if "pyproject.toml" not in files or _CODE_ROOT + "/__init__.py" not in files:
        raise ReleaseError("A committed pyproject.toml and pdm/__init__.py are required")
    if len(files) > _MAX_FILES or sum(map(len, files.values())) > _MAX_TOTAL_BYTES:
        raise ReleaseError("Artifact exceeds the size or file-count limit")
    if len({p.casefold() for p in files}) != len(files):
        raise ReleaseError("Case-colliding artifact paths are forbidden")


def _metadata(files: dict[str, bytes]) -> tuple[str, list[str], list[str]]:
    try:
        config = tomllib.loads(files["pyproject.toml"].decode("utf-8"))
        version = config["project"]["version"]
        schemas = config["tool"]["pdm"]["release"]["supported_schema_versions"]
        capabilities = config["tool"]["pdm"]["release"].get("capabilities", ["source_capture"])
        init = ast.parse(files[_CODE_ROOT + "/__init__.py"].decode("utf-8"))
    except (KeyError, TypeError, ValueError, SyntaxError, UnicodeError) as exc:
        raise ReleaseError("Committed package version or schema metadata is missing or invalid") from exc
    if not isinstance(version, str) or not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]*", version):
        raise ReleaseError("Invalid committed package version")
    if not isinstance(schemas, list) or not schemas or any(
        not isinstance(s, str) or not re.fullmatch(r"[0-9][A-Za-z0-9._-]*", s) for s in schemas
    ) or len(set(schemas)) != len(schemas):
        raise ReleaseError("Invalid committed supported_schema_versions")
    if (not isinstance(capabilities, list) or not capabilities
            or any(not isinstance(item, str) or item not in _CAPABILITIES for item in capabilities)
            or len(set(capabilities)) != len(capabilities) or "source_capture" not in capabilities):
        raise ReleaseError("Invalid committed capabilities")
    for node in init.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            if not isinstance(node.value, ast.Constant) or node.value.value != version:
                raise ReleaseError("Package __version__ does not match committed pyproject.toml")
    return version, schemas, sorted(capabilities)


def _manifest(commit: str, files: dict[str, bytes]) -> dict[str, Any]:
    version, schemas, capabilities = _metadata(files)
    if "skill_bound_knowledge_quality" in capabilities:
        _check_skill_assets(files)
    hashes = {name: _sha256(data) for name, data in sorted(files.items())}
    rules = {name: digest for name, digest in hashes.items()
             if name.startswith((_RULE_ROOT + "/", _SKILL_ROOT + "/"))}
    return {
        "format_version": 1,
        "artifact_kind": "pdm-code",
        "code_commit": commit,
        "package_version": version,
        "rules_version": "sha256:" + _sha256(_json_bytes(rules)),
        "rule_files": rules,
        "supported_schema_versions": schemas,
        "capabilities": capabilities,
        "maturity": "development",
        "deployment_status": "none",
        "files": hashes,
    }


def _check_skill_assets(files: dict[str, bytes]) -> None:
    """Verify every registered archived Skill revision without executing code."""
    try:
        tree = ast.parse(files[_CODE_ROOT + "/skill_support.py"].decode("utf-8"))
        names = {"DEFAULT_EXTRACT_SKILL_REF", "DEFAULT_REVIEW_SKILL_REF", "_MANIFEST_HASHES"}
        constants = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in names:
                    constants[name] = ast.literal_eval(node.value)
        pins = constants["_MANIFEST_HASHES"]
        if not isinstance(pins, dict) or not pins:
            raise ValueError("invalid Skill registry")
        policies = {}
        def descriptor(value, root):
            path, digest = value["path"], value["sha256"]
            if not _safe_path(path) or digest != "sha256-" + _sha256(files[root + "/" + path]):
                raise ValueError("missing or mismatched Skill dependency")
        for role in ("extract", "review"):
            ref = constants["DEFAULT_" + role.upper() + "_SKILL_REF"]
            if (not isinstance(ref, str)
                    or not re.fullmatch("knowledge-" + role + r"@[1-9][0-9]*", ref)
                    or ref not in pins):
                raise ValueError("invalid default Skill reference")
        for ref in pins:
            if not isinstance(ref, str) or not re.fullmatch(r"knowledge-(extract|review)@[1-9][0-9]*", ref):
                raise ValueError("invalid registered Skill reference")
            name, revision = ref.split("@")
            role = name.removeprefix("knowledge-")
            manifest_path = name + "/" + revision + "/manifest.json"
            raw = files[_SKILL_ROOT + "/" + manifest_path]
            if pins[ref] != "sha256-" + _sha256(raw):
                raise ValueError("mismatched Skill revision")
            manifest = json.loads(raw, object_pairs_hook=_unique_object)
            if (manifest["schema_version"] != "skill-manifest-v1" or manifest["id"] != name
                    or manifest["version"] != revision or manifest["role"] != role
                    or manifest["quality_contract"] != "skill-quality-v1"
                    or manifest["entrypoint"]["path"] != name + "/" + revision + "/SKILL.md"
                    or manifest["base_prompt"]["path"] != "prompts/" + role + ".md"):
                raise ValueError("invalid Skill manifest")
            descriptors = [manifest["entrypoint"], *manifest["dependencies"]]
            if len({item["path"] for item in descriptors}) != len(descriptors):
                raise ValueError("duplicate Skill dependency")
            for item in descriptors:
                descriptor(item, _SKILL_ROOT)
            descriptor(manifest["base_prompt"], "project")
            policy = manifest["shared_policy"]
            expected_policy = ("knowledge-quality-v3" if ref in {
                "knowledge-extract@16", "knowledge-review@14"} else
                "knowledge-quality-v2" if ref in {
                "knowledge-extract@15", "knowledge-review@13"} else "knowledge-quality-v1")
            if (not isinstance(policy, dict) or set(policy) != {"id", "path", "sha256"}
                    or policy["id"] != expected_policy
                    or policy["path"] != "shared/" + policy["id"] + ".md"):
                raise ValueError("invalid common Skill policy")
            descriptor(policy, _SKILL_ROOT)
            if (sum(item.get("kind") == "policy" for item in manifest["dependencies"]) != 1
                    or sum(item == dict(path=policy["path"], sha256=policy["sha256"], kind="policy")
                           for item in manifest["dependencies"]) != 1):
                raise ValueError("missing common Skill policy")
            policies[ref] = policy
        if (policies[constants["DEFAULT_EXTRACT_SKILL_REF"]]
                != policies[constants["DEFAULT_REVIEW_SKILL_REF"]]):
            raise ValueError("inconsistent common Skill policy")
    except (KeyError, TypeError, ValueError, SyntaxError, UnicodeError) as exc:
        raise ReleaseError("Code artifact does not contain the pinned Skills and all required dependencies") from exc


def build_release(
    repo: str | os.PathLike[str],
    ref: str,
    output: str | os.PathLike[str],
    *,
    package_version: str | None = None,
    supported_schema_versions: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Create a ZIP for one fixed Git commit; output must not already exist.

    Metadata comes from that commit's pyproject.toml. Optional arguments are
    expectations, not overrides. No personal data, deployment or release tag is
    created. An artifact remains a development build pending business acceptance.
    """
    commit, files = _snapshot(Path(repo), ref)
    manifest = _manifest(commit, files)
    if package_version is not None and package_version != manifest["package_version"]:
        raise ReleaseError("Requested package version does not match the committed version")
    if supported_schema_versions is not None and list(supported_schema_versions) != manifest["supported_schema_versions"]:
        raise ReleaseError("Requested schema support does not match the committed declaration")
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".pdm-build-", dir=destination.parent, delete=False) as handle:
            temporary = handle.name
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in sorted({**files, MANIFEST_NAME: _json_bytes(manifest)}.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, content)
        verify_release(temporary, expected_commit=commit)
        # Same-directory hard link publishes complete bytes and never overwrites.
        os.link(temporary, destination)
    except OSError as exc:
        raise ReleaseError("Could not create artifact; output must be a new writable path") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return manifest


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError("Duplicate key in artifact manifest")
        result[key] = value
    return result


def verify_release(
    path: str | os.PathLike[str],
    *,
    expected_commit: str | None = None,
    expected_package_version: str | None = None,
    expected_rules_version: str | None = None,
    expected_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Check ZIP paths, regular-file inventory, hashes and committed metadata.

    This never extracts or executes files. Without a trusted expected artifact
    digest it cannot authenticate a repackaged manifest or a claimed commit ID.
    """
    try:
        if expected_artifact_sha256 is not None:
            with open(path, "rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != expected_artifact_sha256:
                raise ReleaseError("Artifact does not match the trusted SHA256")
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_FILES + 1 or sum(i.file_size for i in infos) > _MAX_TOTAL_BYTES + _MAX_FILE_BYTES:
                raise ReleaseError("Artifact exceeds the size or file-count limit")
            files: dict[str, bytes] = {}
            for info in infos:
                name = info.filename
                if name != info.orig_filename or not _safe_path(name) or name in files:
                    raise ReleaseError("Unsafe or duplicate artifact entry")
                if info.is_dir() or info.create_system != 3 or stat.S_IFMT(info.external_attr >> 16) != stat.S_IFREG:
                    raise ReleaseError("Only regular files are allowed in an artifact")
                if info.flag_bits & 1 or info.file_size > _MAX_FILE_BYTES:
                    raise ReleaseError("Encrypted or oversized artifact entry")
                if name != MANIFEST_NAME and not _allowed(name):
                    raise ReleaseError(f"File is not allowed in a code artifact: {name}")
                data = archive.read(info)
                if name != MANIFEST_NAME:
                    _check_payload(name, data)
                files[name] = data
        if MANIFEST_NAME not in files:
            raise ReleaseError("Artifact manifest is missing")
        manifest = json.loads(files.pop(MANIFEST_NAME), object_pairs_hook=_unique_object)
        if not isinstance(manifest, dict) or not isinstance(manifest.get("code_commit"), str) or not _COMMIT.fullmatch(manifest["code_commit"]):
            raise ReleaseError("Invalid artifact manifest or code_commit")
        _check_inventory(files)
        computed = _manifest(manifest["code_commit"], files)
        if _json_bytes(manifest) != _json_bytes(computed):
            raise ReleaseError("Artifact manifest, metadata or file hashes do not match")
        for key, expected in (
            ("code_commit", expected_commit),
            ("package_version", expected_package_version),
            ("rules_version", expected_rules_version),
        ):
            if expected is not None and manifest[key] != expected:
                raise ReleaseError(f"Artifact {key} does not match the expected version")
        return manifest
    except (OSError, zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, RuntimeError, NotImplementedError) as exc:
        raise ReleaseError("Could not read a valid code artifact") from exc
