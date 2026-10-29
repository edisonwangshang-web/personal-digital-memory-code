"""Use actual isolated Git histories; no repository memory or network access."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from git_fixtures import configure_test_repository
from pdm.release import MANIFEST_NAME, ReleaseError, _check_skill_assets, build_release, verify_release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        configure_test_repository(self.repo)
        self.git("config", "user.name", "Artifact Test")
        self.git("config", "user.email", "artifact@example.invalid")
        self.write("pyproject.toml", '[project]\nname = "pdm"\nversion = "0.1.0.dev1"\n[tool.pdm.release]\nsupported_schema_versions = ["0.1"]\n')
        self.write("project/src/pdm/__init__.py", '__version__ = "0.1.0.dev1"\n')
        self.write("project/src/pdm/capture.py", 'VALUE = "committed"\n')
        self.write("project/prompts/capture.md", "Preserve the actual source and speaker.\n")
        self.write("README.md", "Development package.\n")
        self.write("project/src/README.md", "How to run source capture.\n")
        self.write("memory/sources/private.md", "PRIVATE MEMORY MUST NEVER BE PACKAGED\n")
        self.write("project/tests/fixtures/source.json", '{"private": "test fixture"}')
        self.write("project/validation/m0.md", "Private validation evidence\n")
        self.write("fixtures/garmin/memory/sources/private.md", "Synthetic private fixture\n")
        self.write("archive/runtime/project/src/pdm/old.py", "Archived implementation\n")
        self.write("archive/runtime/memory/sources/private.md", "Synthetic archived memory\n")
        self.write(".env", "PRIVATE_CONFIGURATION=not-a-real-secret\n")
        self.commit = self.save("initial")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True).stdout.decode().strip()

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def save(self, message):
        self.git("add", "--all")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def build(self, filename="artifact.zip", ref=None, **kwargs):
        output = self.base / filename
        manifest = build_release(self.repo, ref or self.commit, output, **kwargs)
        return output, manifest

    def rewrite(self, source, *, change=None, add=None, remove=None):
        output = self.base / "changed.zip"
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output, "w") as altered:
            for info in original.infolist():
                if info.filename == remove:
                    continue
                data = original.read(info)
                if change:
                    data = change(info.filename, data)
                altered.writestr(info, data)
            if add:
                name, content, mode = add
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    altered.writestr(info, content)
        return output

    def test_package_contains_only_committed_code_and_declared_metadata(self):
        path, manifest = self.build()
        self.assertEqual(verify_release(path, expected_commit=self.commit), manifest)
        self.assertEqual(manifest["package_version"], "0.1.0.dev1")
        self.assertEqual(manifest["supported_schema_versions"], ["0.1"])
        self.assertEqual(manifest["capabilities"], ["source_capture"])
        self.assertEqual(manifest["maturity"], "development")
        self.assertEqual(manifest["deployment_status"], "none")
        self.assertEqual(set(manifest["rule_files"]), {"project/prompts/capture.md"})
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), set(manifest["files"]) | {MANIFEST_NAME})
            self.assertFalse(any("memory/" in name or "fixtures/" in name or "validation/" in name or "archive/" in name or ".env" in name for name in archive.namelist()))
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(name)).hexdigest(), digest)

    def test_fixed_commit_ignores_dirty_tracked_and_untracked_files(self):
        self.write("project/src/pdm/capture.py", 'VALUE = "later commit"\n')
        self.write("project/prompts/capture.md", "Changed rule\n")
        later = self.save("later")
        self.write("project/src/pdm/capture.py", 'VALUE = "dirty local"\n')
        self.write("pyproject.toml", "invalid dirty TOML")
        self.write("project/src/pdm/untracked.py", "secret local content")
        self.write("project/src/pdm/.env", "local credential configuration")
        path, original = self.build()
        later_path, newer = self.build("later.zip", later)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read("project/src/pdm/capture.py"), b'VALUE = "committed"\n')
            self.assertNotIn("project/src/pdm/untracked.py", archive.namelist())
        self.assertNotEqual(original["rules_version"], newer["rules_version"])
        self.assertEqual(verify_release(later_path)["code_commit"], later)

    def test_skill_assets_are_recoverable_and_affect_rule_digest(self):
        path, original = self.build()
        self.write("project/skills/knowledge-extract/1/SKILL.md", "Synthetic extraction rule.\n")
        self.write("project/skills/knowledge-extract/1/manifest.json", '{"version":"1"}\n')
        self.write("project/skills/shared/quality.md", "Synthetic common policy.\n")
        commit = self.save("include pinned skill assets")
        artifact, manifest = self.build("skills.zip", ref=commit)
        self.assertEqual(verify_release(artifact, expected_commit=commit), manifest)
        self.assertNotEqual(original["rules_version"], manifest["rules_version"])
        self.assertEqual(len([p for p in manifest["rule_files"] if p.startswith("project/skills/")]), 3)
        with zipfile.ZipFile(artifact) as archive:
            self.assertEqual(archive.read("project/skills/knowledge-extract/1/SKILL.md"),
                             b"Synthetic extraction rule.\n")
        self.write("project/skills/unreviewed.sh", "exit 0\n")
        with self.assertRaises(ReleaseError):
            self.build("unsafe-skill.zip", ref=self.save("unapproved executable"))

    def test_quality_capability_requires_complete_pinned_skill_dependencies(self):
        project = Path(__file__).resolve().parents[1]
        for path in (project / "skills").rglob("*"):
            if path.is_file():
                self.write("project/skills/" + str(path.relative_to(project / "skills")),
                           path.read_text())
        self.write("project/src/pdm/skill_support.py", (project / "src/pdm/skill_support.py").read_text())
        for module in ("skill_wire.py", "skill_wire_v2.py", "skill_wire_v3.py"):
            self.write("project/src/pdm/" + module, (project / "src/pdm" / module).read_text())
        for role in ("extract", "review"):
            self.write("project/prompts/" + role + ".md", (project / "prompts" / (role + ".md")).read_text())
        config = (self.repo / "pyproject.toml").read_text()
        self.write("pyproject.toml", config + 'capabilities = ["source_capture", "skill_bound_knowledge_quality"]\n')
        artifact, _ = self.build("complete-skills.zip", ref=self.save("complete quality package"))
        verify_release(artifact)
        registered_paths = ["knowledge-extract/" + revision + "/SKILL.md"
                            for revision in ("1", "2", "3", "4", "5", "6", "7")]
        registered_paths += ["knowledge-review/" + revision + "/SKILL.md"
                             for revision in ("1", "2", "3", "4", "5")]
        with zipfile.ZipFile(artifact) as archive:
            for module in ("skill_wire.py", "skill_wire_v2.py", "skill_wire_v3.py"):
                self.assertEqual(archive.read("project/src/pdm/" + module),
                                 (project / "src/pdm" / module).read_bytes())
            for name in registered_paths:
                self.assertEqual(archive.read("project/skills/" + name),
                                 (project / "skills" / name).read_bytes())
        dependency = self.repo / "project/skills/shared/knowledge-quality-v1.md"
        dependency.unlink()
        with self.assertRaises(ReleaseError):
            self.build("missing-skill-dependency.zip", ref=self.save("missing required dependency"))
        self.write("project/skills/shared/knowledge-quality-v1.md",
                   (project / "skills/shared/knowledge-quality-v1.md").read_text())
        for name in registered_paths:
            with self.subTest(missing=name):
                path = "project/skills/" + name
                (self.repo / path).unlink()
                with self.assertRaises(ReleaseError):
                    self.build("missing-" + name.replace("/", "-") + ".zip",
                               ref=self.save("missing registered Skill " + name))
                self.write(path, (project / "skills" / name).read_text())

    def quality_asset_files(self):
        project = Path(__file__).resolve().parents[1]
        files = {"project/src/pdm/skill_support.py": (project / "src/pdm/skill_support.py").read_bytes()}
        files.update({"project/skills/" + path.relative_to(project / "skills").as_posix(): path.read_bytes()
                      for path in (project / "skills").rglob("*") if path.is_file()})
        files.update({"project/prompts/" + role + ".md": (project / "prompts" / (role + ".md")).read_bytes()
                      for role in ("extract", "review")})
        return files

    def rebound_policy_manifest(self, files, change, ref="knowledge-extract@15"):
        """Isolate policy checks after an explicitly changed fixture registry pin."""
        files = dict(files)
        role, version = ref.split("@")
        name = "project/skills/" + role + "/" + version + "/manifest.json"
        original = files[name]
        manifest = json.loads(original)
        change(manifest)
        replacement = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        registry = "project/src/pdm/skill_support.py"
        old_pin = ("sha256-" + hashlib.sha256(original).hexdigest()).encode()
        new_pin = ("sha256-" + hashlib.sha256(replacement).hexdigest()).encode()
        self.assertEqual(files[registry].count(old_pin), 1)
        files[registry] = files[registry].replace(old_pin, new_pin)
        files[name] = replacement
        return files

    def test_historical_v1_and_current_v2_policies_coexist_in_registered_assets(self):
        files = self.quality_asset_files()
        self.assertIn("project/skills/shared/knowledge-quality-v1.md", files)
        self.assertIn("project/skills/shared/knowledge-quality-v2.md", files)
        _check_skill_assets(files)

    def test_every_historical_and_current_policy_remains_required_and_byte_bound(self):
        files = self.quality_asset_files()
        for version in ("v1", "v2"):
            path = "project/skills/shared/knowledge-quality-" + version + ".md"
            for action in ("missing", "changed"):
                with self.subTest(version=version, action=action):
                    altered = dict(files)
                    if action == "missing":
                        altered.pop(path)
                    else:
                        altered[path] += b"\nChanged artificial policy.\n"
                    with self.assertRaises(ReleaseError):
                        _check_skill_assets(altered)

    def test_default_roles_cannot_mix_historical_and_current_policies(self):
        files = self.quality_asset_files()
        registry = "project/src/pdm/skill_support.py"
        for role, current, previous in (("EXTRACT", "15", "14"), ("REVIEW", "13", "12")):
            with self.subTest(role=role):
                altered = dict(files)
                needle = f'DEFAULT_{role}_SKILL_REF = "knowledge-{role.lower()}@{current}"'.encode()
                replacement = f'DEFAULT_{role}_SKILL_REF = "knowledge-{role.lower()}@{previous}"'.encode()
                self.assertEqual(altered[registry].count(needle), 1)
                altered[registry] = altered[registry].replace(needle, replacement)
                with self.assertRaises(ReleaseError):
                    _check_skill_assets(altered)

    def test_new_default_pair_cannot_both_rebind_to_the_wrong_historical_policy(self):
        files = self.quality_asset_files()
        legacy = json.loads(files["project/skills/knowledge-extract/14/manifest.json"])["shared_policy"]
        def use_legacy_policy(manifest):
            manifest["shared_policy"] = dict(legacy)
            for dependency in manifest["dependencies"]:
                if dependency["kind"] == "policy":
                    dependency.update(path=legacy["path"], sha256=legacy["sha256"])
        for ref in ("knowledge-extract@15", "knowledge-review@13"):
            files = self.rebound_policy_manifest(files, use_legacy_policy, ref)
        with self.assertRaises(ReleaseError):
            _check_skill_assets(files)

    def test_policy_descriptor_and_unique_declared_dependency_are_required(self):
        files = self.quality_asset_files()
        def wrong_id(manifest):
            manifest["shared_policy"]["id"] = "knowledge-quality-v1"
        def extra_field(manifest):
            manifest["shared_policy"]["extra"] = "not allowed"
        def mismatched_dependency(manifest):
            legacy = next(json.loads(files[path])["shared_policy"] for path in files
                          if path == "project/skills/knowledge-extract/14/manifest.json")
            manifest["shared_policy"] = legacy
        def no_policy_dependency(manifest):
            next(item for item in manifest["dependencies"] if item["kind"] == "policy")["kind"] = "reference"
        for change in (wrong_id, extra_field, mismatched_dependency, no_policy_dependency):
            with self.subTest(change=change.__name__), self.assertRaises(ReleaseError):
                _check_skill_assets(self.rebound_policy_manifest(files, change))

    def test_declared_capabilities_come_from_fixed_snapshot_and_are_canonical(self):
        config = (self.repo / "pyproject.toml").read_text()
        self.write("pyproject.toml", config + 'capabilities = ["source_capture", "scoped_evidence_qa", "reviewed_publication"]\n')
        declared = self.save("declared capabilities")
        self.write("pyproject.toml", config + 'capabilities = ["source_capture"]\n')
        path, manifest = self.build(ref=declared)
        self.assertEqual(manifest["capabilities"], ["reviewed_publication", "scoped_evidence_qa", "source_capture"])
        self.assertEqual(verify_release(path, expected_commit=declared), manifest)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(hashlib.sha256(archive.read("pyproject.toml")).hexdigest(), manifest["files"]["pyproject.toml"])
            self.assertEqual(json.loads(archive.read(MANIFEST_NAME))["capabilities"], manifest["capabilities"])
        _, historical = self.build("historical.zip")
        self.assertEqual(historical["capabilities"], ["source_capture"])

    def test_invalid_capability_declarations_are_rejected(self):
        config = (self.repo / "pyproject.toml").read_text()
        for declaration in (
            '["source_capture", "unknown"]', '["source_capture", "source_capture"]',
            '"source_capture"', '["source_capture", 1]', '["source_capture", []]',
            '[]', '["reviewed_publication"]',
        ):
            with self.subTest(declaration=declaration):
                self.write("pyproject.toml", config + "capabilities = " + declaration + "\n")
                invalid = self.save("invalid capabilities")
                with self.assertRaisesRegex(ReleaseError, "capabilities"):
                    self.build(ref=invalid)

    def test_manifest_cannot_override_or_reorder_declared_capabilities(self):
        config = (self.repo / "pyproject.toml").read_text()
        self.write("pyproject.toml", config + 'capabilities = ["source_capture", "reviewed_publication", "scoped_evidence_qa"]\n')
        path, manifest = self.build(ref=self.save("declared capabilities"))
        for capabilities in (["source_capture"], list(reversed(manifest["capabilities"]))):
            def change(name, data):
                if name != MANIFEST_NAME:
                    return data
                value = json.loads(data)
                value["capabilities"] = capabilities
                return json.dumps(value).encode()
            with self.subTest(capabilities=capabilities), self.assertRaises(ReleaseError):
                verify_release(self.rewrite(path, change=change))

    def test_build_is_reproducible_and_does_not_overwrite(self):
        first, _ = self.build("first.zip")
        second, _ = self.build("second.zip")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with self.assertRaises(ReleaseError):
            build_release(self.repo, self.commit, first)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_symlinks_are_rejected_within_each_allowed_root(self):
        for prefix in ("project/src/pdm", "project/prompts"):
            with self.subTest(prefix=prefix):
                link = self.repo / prefix / "linked.py"
                link.symlink_to(self.repo / "memory/sources/private.md")
                bad = self.save("symlink")
                with self.assertRaisesRegex(ReleaseError, "Symlinks"):
                    self.build(ref=bad)
                link.unlink()
                self.save("remove symlink")

    def test_secret_configuration_and_embedded_fixture_paths_are_rejected(self):
        for name in (".env", "secrets.py", "fixtures/source.py", "validation/result.py"):
            with self.subTest(name=name):
                path = "project/src/pdm/" + name
                self.write(path, "not a real secret")
                bad = self.save("forbidden file")
                with self.assertRaises(ReleaseError):
                    self.build(ref=bad)
                (self.repo / path).unlink()
                self.save("remove forbidden file")

    def test_prompt_configuration_and_plaintext_credentials_are_rejected(self):
        self.write("project/prompts/config.yaml", "model: test\n")
        with self.assertRaisesRegex(ReleaseError, "not allowed"):
            self.build(ref=self.save("prompt configuration"))
        (self.repo / "project/prompts/config.yaml").unlink()
        for path in ("project/src/pdm/unsafe.py", "project/prompts/unsafe.md"):
            with self.subTest(path=path):
                self.write(path, 'API_KEY = "not-a-real-secret"\n')
                with self.assertRaisesRegex(ReleaseError, "Credential material"):
                    self.build(ref=self.save("credential assignment"))
                (self.repo / path).unlink()
                self.save("remove credential assignment")

    def test_tampered_payload_missing_and_additional_files_are_rejected(self):
        path, _ = self.build()
        variants = [
            {"change": lambda name, data: b"tampered" if name.endswith("capture.py") else data},
            {"remove": "project/src/pdm/capture.py"},
            {"add": ("project/src/pdm/extra.py", b"extra", stat.S_IFREG | 0o644)},
            {"add": ("memory/private.md", b"private", stat.S_IFREG | 0o644)},
        ]
        for kwargs in variants:
            with self.subTest(kwargs=tuple(kwargs)):
                with self.assertRaises(ReleaseError):
                    verify_release(self.rewrite(path, **kwargs))

    def test_unsafe_and_duplicate_archive_entries_are_rejected(self):
        path, _ = self.build()
        for name, mode in [
            ("../escape.py", stat.S_IFREG | 0o644),
            ("/project/src/pdm/escape.py", stat.S_IFREG | 0o644),
            ("project/src/pdm/../escape.py", stat.S_IFREG | 0o644),
            ("project\\src\\pdm\\escape.py", stat.S_IFREG | 0o644),
            ("project/src/pdm/linked.py", stat.S_IFLNK | 0o777),
            ("project/src/pdm/capture.py", stat.S_IFREG | 0o644),
        ]:
            with self.subTest(name=name):
                with self.assertRaises(ReleaseError):
                    verify_release(self.rewrite(path, add=(name, b"content", mode)))

    def test_manifest_metadata_cannot_override_committed_metadata(self):
        path, _ = self.build()
        for key, value in [("package_version", "99.0"), ("supported_schema_versions", ["99"]), ("rules_version", "made-up"), ("maturity", "production"), ("deployment_status", "deployed")]:
            def change(name, data):
                if name != MANIFEST_NAME:
                    return data
                manifest = json.loads(data)
                manifest[key] = value
                return json.dumps(manifest).encode()
            with self.subTest(key=key):
                with self.assertRaises(ReleaseError):
                    verify_release(self.rewrite(path, change=change))
        with self.assertRaises(ReleaseError):
            self.build("version.zip", package_version="99.0")
        with self.assertRaises(ReleaseError):
            self.build("schema.zip", supported_schema_versions=["99"])

    def test_committed_package_version_disagreement_is_rejected(self):
        self.write("project/src/pdm/__init__.py", '__version__ = "99.0"\n')
        with self.assertRaisesRegex(ReleaseError, "__version__"):
            self.build(ref=self.save("mismatch"))

    def test_expected_versions_and_trusted_artifact_digest(self):
        path, manifest = self.build()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(verify_release(path, expected_artifact_sha256=digest, expected_package_version="0.1.0.dev1", expected_rules_version=manifest["rules_version"]), manifest)
        for kwargs in ({"expected_commit": "0" * 40}, {"expected_package_version": "99.0"}, {"expected_rules_version": "wrong"}, {"expected_artifact_sha256": "0" * 64}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ReleaseError):
                verify_release(path, **kwargs)

    def test_no_prompt_files_is_valid_and_recorded_explicitly(self):
        (self.repo / "project/prompts/capture.md").unlink()
        path, manifest = self.build(ref=self.save("no prompts"))
        self.assertEqual(manifest["rule_files"], {})
        self.assertEqual(verify_release(path), manifest)

    def test_invalid_reference_is_rejected(self):
        for ref in ("does-not-exist", "--help", "HEAD\nother"):
            with self.subTest(ref=ref), self.assertRaises(ReleaseError):
                self.build(ref=ref)

    def test_inherited_git_environment_cannot_redirect_snapshot(self):
        poisoned = {name: str(self.base / "wrong-repository") for name in (
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        )}
        poisoned.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.worktree", GIT_CONFIG_VALUE_0="/wrong")
        with patch.dict(os.environ, poisoned):
            path, manifest = self.build()
        self.assertEqual(manifest["code_commit"], self.commit)
        self.assertEqual(verify_release(path), manifest)

    def test_git_calls_have_a_timeout_and_do_not_expose_process_errors(self):
        error = subprocess.TimeoutExpired("git", 30, stderr=b"private credential-bearing URL")
        with patch("pdm.release.subprocess.run", side_effect=error) as run:
            with self.assertRaisesRegex(ReleaseError, "Git operation did not complete") as raised:
                self.build()
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertNotIn("credential", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
