"""Explicit, pinned Skill loading with self-contained historical snapshots.

Only the two registered roles are supported. No source text, user-wide Skill
directory, plugin discovery or model-generated dependency is consulted.
"""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re


QUALITY_CONTRACT = "skill-quality-v1"
BUNDLE_SCHEMA = "skill-bundle-v1"
DEFAULT_EXTRACT_SKILL_REF = "knowledge-extract@14"
DEFAULT_REVIEW_SKILL_REF = "knowledge-review@12"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = PROJECT_ROOT / "skills"
MAX_FILE_BYTES = 64_000
MAX_BUNDLE_BYTES = 256_000

# A revision is immutable. Updating rules requires a new registered revision,
# not replacing a file while continuing to claim the old reference.
_MANIFEST_HASHES = {
    "knowledge-extract@1": "sha256-24b630ca3beeb3c76d1e56f7434ecee5b7dc5c655f807d239bed59f19e70779b",
    "knowledge-extract@2": "sha256-e84a4f0cd7b8a6806cc09b3b4dfaf4677af8484d569c7a283a60377ce606b81d",
    "knowledge-review@1": "sha256-65e8e19f7bd07b3cfe5d86934c1c8f2b8310e6e287b3130f1e7052b611d402de",
    "knowledge-extract@3": "sha256-3b936abdcdf68e5bbfb639de829541ca1a06ff36c27e5ed061eb4f9ccee7967d",
    "knowledge-review@2": "sha256-2b6252dbf8ba20dfad6d6eefbc922e6585c0537fb2518501cd22f6133742ad6e",
    "knowledge-extract@4": "sha256-ea950601cf18b3d9b4dc8f43991c6e072f40f6dc3b0f2cd6f4a1b7d1860d4582",
    "knowledge-review@3": "sha256-738e2e06c0887ac23f4170e91d513de100d085dbb9a40a707a819618bb23d0d2",
    "knowledge-extract@5": "sha256-2617d0b0f4aaad2d4ddd4675870d8166119038b2950a659752030bc9b5513ad7",
    "knowledge-review@4": "sha256-3492541a935ed41b8840639c0f9758bb019c66d78bda9f72fdd4306d7f39abb5",
    "knowledge-extract@6": "sha256-6a16ead2dcdac9b77cc8a8a0855fdfc2d3db599d62996846606368455f6f7c27",
    "knowledge-review@5": "sha256-103a647f68995fc999a7a5a42334ce9b02075265af23e3a39893cdf89665d97d",
    "knowledge-extract@7": "sha256-b5699d9b8624a8b7839739e08aa7f3e006bc6e6ae2285f4bb722ea0f70c48080",
    "knowledge-extract@8": "sha256-86ca4b3d20c2f0b3f9b50fec69658bfde10da0ecd3f55a217f930c7f8b5d766a",
    "knowledge-review@6": "sha256-5cce6e93792bf6b99efd1eae417ef8cba92cb590a7dfcc3553b79f4e3b8cb980",
    "knowledge-extract@9": "sha256-c4433237a8b21a99770b2724a8ae9069a6171dcb35fadaf5fa828306702906bf",
    "knowledge-review@7": "sha256-247df9b6b74940b393fa2ef4c0c4ad36f5963b21e9752606eb27428619b7b348",
    "knowledge-extract@10": "sha256-0e58d98a748f4c85882b1c54c74b2dc4b27a5936af154bbc76c977d4a8350816",
    "knowledge-review@8": "sha256-b6314501bcf69f42d5d7c6336354bfc45db1ac176baa266f6efe49d7d1a8044c",
    "knowledge-extract@11": "sha256-763660e25629a48644c6e06364c0fa3f4766f6f96d384ca90ce9906f82961133",
    "knowledge-review@9": "sha256-e225a117fc552bff0bee58dbf4b81cd7bc177f03ca8099a5a56c4557690c847a",
    "knowledge-extract@12": "sha256-6b538f1014cd5ad9f35adfbe007b7350b95433816b4443101985677d46cd656c",
    "knowledge-review@10": "sha256-03f3b3a21c3ba8683bc4854d396e3cb89e9dfd650650f967ba7ecde4f2965558",
    "knowledge-extract@13": "sha256-9a923703a4913a7a59409d41613cf5baaf7746089576aadf228719d2868451f6",
    "knowledge-review@11": "sha256-9eee2fef44350495531c3c8c3244c61e140025c94cdaaec09caf6241347e9325",
    "knowledge-extract@14": "sha256-9e384d9389642ac6287aa067a1ca3348f149bbb419a5becc7b24e4265c1f3d0c",
    "knowledge-review@12": "sha256-71085225c3977f7ae9c2be19c615f5e3bab65057bf23c8aa264c985cb12930aa",
}
_ROLES = ("extract", "review")
_SNAPSHOT_KEYS = {"path", "content", "sha256", "bytes"}
_MANIFEST_KEYS = {
    "schema_version", "id", "version", "role", "quality_contract",
    "entrypoint", "dependencies", "shared_policy", "base_prompt",
}


class SkillError(ValueError):
    """A bounded diagnostic; never embeds rules, source data or filesystem paths."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _sha(raw):
    return "sha256-" + hashlib.sha256(raw).hexdigest()


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise SkillError("invalid_skill_bundle") from None


def _path(value):
    if (not isinstance(value, str) or not value or "\\" in value or
            "\x00" in value or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise SkillError("invalid_skill_path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value:
        raise SkillError("invalid_skill_path")
    return value


def _reference(ref, role):
    if (not isinstance(ref, str) or
            re.fullmatch(r"knowledge-" + role + r"@[1-9][0-9]*", ref) is None or
            ref not in _MANIFEST_HASHES):
        raise SkillError("unsupported_skill_reference")
    name, version = ref.rsplit("@", 1)
    return name, version


def _snapshot(path, raw):
    if len(raw) > MAX_FILE_BYTES:
        raise SkillError("skill_file_limit")
    try:
        content = raw.decode("utf-8")
    except UnicodeError:
        raise SkillError("invalid_skill_encoding") from None
    if not content.strip() or "\x00" in content:
        raise SkillError("invalid_skill_content")
    return {"path": _path(path), "content": content, "sha256": _sha(raw), "bytes": len(raw)}


def _read(root, relative):
    """No symbolic links or paths outside the explicit package are accepted."""
    relative = _path(relative)
    root = Path(root)
    path = root / relative
    try:
        if root.is_symlink():
            raise SkillError("skill_symlink")
        cursor = root
        for part in PurePosixPath(relative).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise SkillError("skill_symlink")
        if not path.resolve().is_relative_to(root.resolve()):
            raise SkillError("invalid_skill_path")
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise SkillError("skill_file_unavailable") from None
    return _snapshot(relative, raw)


def _validate_snapshot(value):
    if not isinstance(value, dict) or set(value) != _SNAPSHOT_KEYS:
        raise SkillError("invalid_skill_snapshot")
    if not isinstance(value["content"], str) or type(value["bytes"]) is not int:
        raise SkillError("invalid_skill_snapshot")
    try:
        actual = _snapshot(value["path"], value["content"].encode("utf-8"))
    except UnicodeError:
        raise SkillError("invalid_skill_encoding") from None
    if actual != value:
        raise SkillError("skill_snapshot_hash_mismatch")
    return value


def _json(text):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise SkillError("invalid_skill_manifest")
            value[key] = item
        return value

    def constant(_):
        raise SkillError("invalid_skill_manifest")

    try:
        return json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    except (ValueError, TypeError, RecursionError):
        raise SkillError("invalid_skill_manifest") from None


def _file_descriptor(value):
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise SkillError("invalid_skill_manifest")
    _path(value["path"])
    if (not isinstance(value["sha256"], str) or
            re.fullmatch(r"sha256-[0-9a-f]{64}", value["sha256"]) is None):
        raise SkillError("invalid_skill_manifest")


def _manifest(snapshot, ref, role):
    name, version = _reference(ref, role)
    _validate_snapshot(snapshot)
    if (snapshot["path"] != "{}/{}/manifest.json".format(name, version) or
            snapshot["sha256"] != _MANIFEST_HASHES[ref]):
        raise SkillError("skill_revision_mismatch")
    value = _json(snapshot["content"])
    if (not isinstance(value, dict) or set(value) != _MANIFEST_KEYS or
            value["schema_version"] != "skill-manifest-v1" or
            value["id"] != name or value["version"] != version or
            value["role"] != role or value["quality_contract"] != QUALITY_CONTRACT):
        raise SkillError("invalid_skill_manifest")
    _file_descriptor(value["entrypoint"])
    _file_descriptor(value["base_prompt"])
    if (value["entrypoint"]["path"] != "{}/{}/SKILL.md".format(name, version) or
            value["base_prompt"]["path"] != "prompts/{}.md".format(role)):
        raise SkillError("invalid_skill_manifest")
    deps = value["dependencies"]
    if not isinstance(deps, list) or not 1 <= len(deps) <= 16:
        raise SkillError("invalid_skill_dependencies")
    seen = {value["entrypoint"]["path"]}
    for dep in deps:
        if (not isinstance(dep, dict) or set(dep) != {"path", "sha256", "kind"} or
                dep["kind"] not in {"policy", "reference"}):
            raise SkillError("invalid_skill_dependencies")
        _file_descriptor({key: dep[key] for key in ("path", "sha256")})
        if dep["path"] in seen:
            raise SkillError("invalid_skill_dependencies")
        seen.add(dep["path"])
    policy = value["shared_policy"]
    if (not isinstance(policy, dict) or set(policy) != {"id", "path", "sha256"} or
            policy["id"] != "knowledge-quality-v1"):
        raise SkillError("invalid_shared_policy")
    _file_descriptor({key: policy[key] for key in ("path", "sha256")})
    if sum(dep == dict(path=policy["path"], sha256=policy["sha256"], kind="policy")
           for dep in deps) != 1:
        raise SkillError("invalid_shared_policy")
    return value


def _validate_role(value, role):
    if (not isinstance(value, dict) or
            set(value) != {"ref", "role", "version", "manifest", "files", "base_prompt"}):
        raise SkillError("invalid_skill_role")
    manifest = _manifest(value["manifest"], value["ref"], role)
    if value["role"] != role or value["version"] != manifest["version"]:
        raise SkillError("invalid_skill_role")
    descriptors = [manifest["entrypoint"]] + manifest["dependencies"]
    if not isinstance(value["files"], list) or len(value["files"]) != len(descriptors):
        raise SkillError("incomplete_skill_dependencies")
    for snapshot, descriptor in zip(value["files"], descriptors):
        _validate_snapshot(snapshot)
        if (snapshot["path"] != descriptor["path"] or
                snapshot["sha256"] != descriptor["sha256"]):
            raise SkillError("skill_dependency_mismatch")
    _validate_snapshot(value["base_prompt"])
    if any(value["base_prompt"][key] != manifest["base_prompt"][key]
           for key in ("path", "sha256")):
        raise SkillError("skill_base_prompt_mismatch")
    return manifest


def validate_skill_bundle(bundle):
    """Validate all frozen bytes, roles and declared dependencies without disk IO.

    Authenticity of a run's chosen bundle is additionally bound by the caller's
    candidate/review Git and request hashes. This function never substitutes
    current files when a historical snapshot is incomplete.
    """
    raw = _canonical(bundle)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise SkillError("skill_bundle_limit")
    if (not isinstance(bundle, dict) or
            set(bundle) != {"schema_version", "quality_contract", "skills",
                           "shared_policy", "sha256"} or
            bundle["schema_version"] != BUNDLE_SCHEMA or
            bundle["quality_contract"] != QUALITY_CONTRACT or
            not isinstance(bundle["skills"], dict) or set(bundle["skills"]) != set(_ROLES)):
        raise SkillError("invalid_skill_bundle")
    expected = _sha(_canonical({key: value for key, value in bundle.items() if key != "sha256"}))
    if bundle["sha256"] != expected:
        raise SkillError("skill_bundle_hash_mismatch")
    manifests = [_validate_role(bundle["skills"][role], role) for role in _ROLES]
    if ((bundle["skills"]["extract"]["ref"] == "knowledge-extract@9")
            != (bundle["skills"]["review"]["ref"] == "knowledge-review@7")):
        raise SkillError("invalid_skill_pair")
    if ((bundle["skills"]["extract"]["ref"] == "knowledge-extract@10")
            != (bundle["skills"]["review"]["ref"] == "knowledge-review@8")):
        raise SkillError("invalid_skill_pair")
    if ((bundle["skills"]["extract"]["ref"] == "knowledge-extract@11")
            != (bundle["skills"]["review"]["ref"] == "knowledge-review@9")):
        raise SkillError("invalid_skill_pair")
    if ((bundle["skills"]["extract"]["ref"] == "knowledge-extract@12")
            != (bundle["skills"]["review"]["ref"] == "knowledge-review@10")):
        raise SkillError("invalid_skill_pair")
    if ((bundle["skills"]["extract"]["ref"] == "knowledge-extract@13")
            != (bundle["skills"]["review"]["ref"] == "knowledge-review@11")):
        raise SkillError("invalid_skill_pair")
    if ((bundle["skills"]["extract"]["ref"] == "knowledge-extract@14")
            != (bundle["skills"]["review"]["ref"] == "knowledge-review@12")):
        raise SkillError("invalid_skill_pair")
    if any(manifest["shared_policy"] != bundle["shared_policy"] for manifest in manifests):
        raise SkillError("shared_policy_mismatch")
    return bundle


def statistic_repair_enabled(bundle):
    """Registered 11/9 and later pairs retain the frozen-text quality repair."""
    skills = bundle.get("skills", {}) if isinstance(bundle, dict) else {}
    return incremental_repair_enabled(bundle) or (isinstance(skills, dict)
            and all(isinstance(skills.get(role), dict) and skills[role].get("ref") == ref
                    for role, ref in (("extract", "knowledge-extract@11"),
                                      ("review", "knowledge-review@9"))))


def incremental_repair_enabled(bundle):
    """Explicit 12/10 and 13/11 retain pinned incremental materialization."""
    skills = bundle.get("skills", {}) if isinstance(bundle, dict) else {}
    return revision_coverage_enabled(bundle) or (isinstance(skills, dict) and all(isinstance(skills.get(role), dict)
            and skills[role].get("ref") == ref for role, ref in
            (("extract", "knowledge-extract@12"), ("review", "knowledge-review@10"))))


def revision_coverage_enabled(bundle):
    """Registered 13/11 and later retain current-draft coverage obligations."""
    skills = bundle.get("skills", {}) if isinstance(bundle, dict) else {}
    return (isinstance(skills, dict) and any(all(isinstance(skills.get(role), dict)
            and skills[role].get("ref") == ref for role, ref in pair) for pair in (
                (("extract", "knowledge-extract@13"), ("review", "knowledge-review@11")),
                (("extract", "knowledge-extract@14"), ("review", "knowledge-review@12")))))


def statistic_evidence_enabled(bundle):
    """Registered 13/11 and later require Review-pinned missing-pair evidence."""
    return revision_coverage_enabled(bundle)


def csv_bom_normalization_enabled(bundle):
    """Only 14/12 uses the frozen source-quality-v2 parse policy."""
    skills = bundle.get("skills", {}) if isinstance(bundle, dict) else {}
    return (isinstance(skills, dict) and all(isinstance(skills.get(role), dict)
            and skills[role].get("ref") == ref for role, ref in
            (("extract", "knowledge-extract@14"), ("review", "knowledge-review@12"))))


def pending_coverage_priority_enabled(bundle):
    """Only 14/12 prioritizes final incomplete-source coverage Review."""
    return csv_bom_normalization_enabled(bundle)


def resolve_skill_refs(extract_skill_ref=None, review_skill_ref=None, *,
                       skills_root=None, prompts_root=None):
    """Resolve registered refs; omitted refs select the current role defaults.

    Alternate roots are for controlled installation/testing, not source-selected
    locations; the same immutable manifest and dependency hashes still apply.
    """
    refs = {
        "extract": DEFAULT_EXTRACT_SKILL_REF if extract_skill_ref is None else extract_skill_ref,
        "review": DEFAULT_REVIEW_SKILL_REF if review_skill_ref is None else review_skill_ref,
    }
    root = SKILLS_ROOT if skills_root is None else Path(skills_root)
    prompt_root = PROJECT_ROOT / "prompts" if prompts_root is None else Path(prompts_root)
    loaded = {}
    for role, ref in refs.items():
        name, version = _reference(ref, role)
        snapshot = _read(root, "{}/{}/manifest.json".format(name, version))
        manifest = _manifest(snapshot, ref, role)
        files = [_read(root, item["path"])
                 for item in [manifest["entrypoint"]] + manifest["dependencies"]]
        prompt = _read(prompt_root, role + ".md")
        prompt["path"] = manifest["base_prompt"]["path"]
        loaded[role] = {
            "ref": ref, "role": role, "version": version, "manifest": snapshot,
            "files": files, "base_prompt": prompt,
        }
    result = {
        "schema_version": BUNDLE_SCHEMA, "quality_contract": QUALITY_CONTRACT,
        "skills": loaded,
        "shared_policy": _json(loaded["extract"]["manifest"]["content"])["shared_policy"],
    }
    result["sha256"] = _sha(_canonical(result))
    return validate_skill_bundle(result)


def skill_request_parts(role, bundle):
    """Return frozen base instructions and explicitly expanded role rules."""
    validate_skill_bundle(bundle)
    if role not in _ROLES:
        raise SkillError("unsupported_skill_role")
    skill = bundle["skills"][role]
    sections = [
        "以下为程序显式校验并加载的 {}（bundle {}）；仅这些固定规则作为 Skill 指令。"
        .format(skill["ref"], bundle["sha256"]),
    ]
    # Shared dependencies precede the role entrypoint, which explains how the
    # versioned contract extends the legacy base prompt.
    for snapshot in skill["files"][1:] + skill["files"][:1]:
        sections.append("规则文件 {}（{}）：\n{}".format(
            snapshot["path"], snapshot["sha256"], snapshot["content"]))
    return skill["base_prompt"]["content"], "\n\n".join(sections)
