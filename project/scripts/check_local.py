#!/usr/bin/env python3
"""Run the existing push CI checks locally, against a clean Git commit."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SHARDS = ("decomposed", "other-a", "other-b")


def environment():
    # Do not inherit a caller's alternate repository or Python import path.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
               GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull)
    return env


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args],
                                   env=environment(), stderr=subprocess.PIPE,
                                   timeout=60).decode().strip()


def resolve_python(command: str, expected: str) -> dict:
    path = shutil.which(command)
    if path is None:
        raise ValueError(f"Python {expected} not found: {command}")
    result = subprocess.check_output(
        [path, "-I", "-c", "import json,sys;print(json.dumps(list(sys.version_info[:3])))"],
        env=environment(), timeout=15)
    version = json.loads(result)
    if version[:2] != [int(x) for x in expected.split(".")]:
        raise ValueError(f"Expected Python {expected}, got {version}: {command}")
    return {"path": str(Path(path).resolve()), "version": ".".join(map(str, version))}


def validate_manifests(paths: list[Path]) -> int:
    reports = [json.loads(path.read_text()) for path in paths]
    if len(reports) != 3 or {r["selected_shard"] for r in reports} != set(SHARDS):
        raise ValueError("All three distinct test shards are required")
    first = reports[0]
    for report in reports:
        if any(report[key] != first[key] for key in
               ("schema_version", "routing", "total_tests", "discovery_sha256", "partitions")):
            raise ValueError("Test shards came from different discoveries")
        expected = report["partitions"][report["selected_shard"]]["tests"]
        if report["selected_tests"] != expected or expected <= 0:
            raise ValueError("Shard test count mismatch")
    if sum(r["selected_tests"] for r in reports) != first["total_tests"]:
        raise ValueError("Incomplete test coverage")
    return first["total_tests"]


def check_archive(path: Path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
    excluded = ("memory/", "project/validation/", "fixtures/", "archive/")
    if any(name.startswith(excluded) for name in names):
        raise ValueError("Private data appeared in the source archive")
    if not {"project/src/pdm/cli.py", "pyproject.toml"} <= set(names):
        raise ValueError("Source archive is missing required code")


def source_state(repo):
    files = {str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in (repo / "memory").rglob("*") if p.is_file()}
    return {"commit": git(repo, "rev-parse", "HEAD"),
            "status": git(repo, "status", "--porcelain", "--untracked-files=all"),
            "memory_sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


class Checks:
    def __init__(self, output, report, timeout):
        self.output, self.report, self.timeout = output, report, timeout
        self.lock = threading.Lock()
        self.processes = set()
        self.stopped = threading.Event()

    def save(self):
        with self.lock:
            target = self.output / "report.json"
            temporary = self.output / "report.json.tmp"
            temporary.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n")
            temporary.chmod(0o600)
            temporary.replace(target)

    def run(self, name, argv, cwd, env=None):
        if self.stopped.is_set():
            raise RuntimeError("Checks interrupted")
        entry = {"name": name, "argv": list(map(str, argv)), "status": "running",
                 "log": name + ".log"}
        with self.lock:
            self.report["steps"].append(entry)
        self.save()
        print(f"START {name}", flush=True)
        started = time.monotonic()
        process = None
        try:
            with (self.output / entry["log"]).open("xb") as log:
                os.chmod(log.name, 0o600)
                process = subprocess.Popen(entry["argv"], cwd=cwd, env=env or environment(),
                                           stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
                with self.lock:
                    self.processes.add(process)
                    if self.stopped.is_set() and process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                try:
                    code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    entry["error"] = "timeout"
                    code = -1
            entry.update(exit_code=code, status="passed" if code == 0 else "failed")
        except KeyboardInterrupt:
            entry.update(status="interrupted", exit_code=130, error="Interrupted by user")
            raise
        except Exception as error:
            entry.update(status="failed", exit_code=1, error=str(error))
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            with self.lock:
                self.processes.discard(process)
            entry["seconds"] = round(time.monotonic() - started, 3)
            self.save()
        print(f"{entry['status'].upper()} {name} ({entry['seconds']}s)", flush=True)
        if entry["status"] != "passed":
            raise RuntimeError(f"Failed: {name}; see {entry['log']}")

    def stop(self):
        self.stopped.set()
        with self.lock:
            for process in self.processes:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def checkout(self, repo, commit, dest, name):
        self.run(name + "-clone", ["git", "clone", "--quiet", "--no-local", "--no-checkout",
                                  str(repo), str(dest)], repo)
        self.run(name + "-checkout", ["git", "checkout", "--quiet", "--detach", commit], dest)
        if git(dest, "rev-parse", "HEAD") != commit or git(dest, "rev-parse", "--is-shallow-repository") != "false":
            raise ValueError("The checkout is not the complete pinned commit")
        if (dest / ".git/objects/info/alternates").exists():
            raise ValueError("The checkout shares external objects")
        self.run(name + "-disable-gc", ["git", "config", "gc.auto", "0"], dest)
        self.run(name + "-disable-maintenance", ["git", "config", "maintenance.auto", "false"], dest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python311", default="python3.11", help="Python 3.11 executable")
    parser.add_argument("--python312", default="python3.12", help="Python 3.12 executable")
    parser.add_argument("--output", type=Path, help="New report directory outside the repository")
    parser.add_argument("--jobs", type=int, default=2, choices=range(1, 7), metavar="1..6")
    parser.add_argument("--timeout", type=int, default=3600, help="Timeout in seconds for each step")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        parser.error("Run on Linux or Windows WSL2; native Windows is not supported")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = (args.output or ROOT.parent / ".cache" / ROOT.name / "local-checks" / stamp).resolve()
    git_dir = Path(git(ROOT, "rev-parse", "--absolute-git-dir")).resolve()
    if output.is_relative_to(ROOT) or output.is_relative_to(git_dir):
        parser.error("Report directory must be outside the source repository and its Git directory")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    report = {"schema_version": 1, "status": "running", "started_utc": stamp,
              "source": str(ROOT), "scope": "push engineering checks",
              "github_status_updated": False, "pr_promotion_checked": False,
              "real_model_calls": 0, "steps": [], "test_counts": {}}
    checks = Checks(output, report, args.timeout)
    checks.save()
    before = None
    pool = None
    temporary = None
    try:
        before = source_state(ROOT)
        report["source_before"] = before
        if before["status"]:
            raise ValueError("Commit or save source changes first: checks require a clean worktree")
        if git(ROOT, "rev-parse", "--is-shallow-repository") != "false":
            raise ValueError("A full Git clone is required")
        report["interpreters"] = {
            "3.11": resolve_python(args.python311, "3.11"),
            "3.12": resolve_python(args.python312, "3.12")}
        commit = before["commit"]
        report["commit"] = commit
        config = tomllib.loads(git(ROOT, "show", f"{commit}:pyproject.toml"))
        report["package_version"] = config["project"]["version"]
        checks.save()
        temporary = tempfile.TemporaryDirectory(prefix="checkouts-", dir=output)
        work = Path(temporary.name)

        def shard(version, shard_name):
            name = f"python{version}-{shard_name}"
            checkout = work / name
            checks.checkout(ROOT, commit, checkout, name)
            env = environment()
            env["PYTHONPATH"] = str(checkout / "project/src")
            checks.run(name + "-tests", [report["interpreters"][version]["path"],
                       ".github/scripts/run_test_shard.py", "--shard", shard_name,
                       "--manifest", str(output / (name + ".json"))], checkout, env)

        pool = ThreadPoolExecutor(max_workers=args.jobs)
        futures = [pool.submit(shard, version, name) for name in SHARDS
                   for version in report["interpreters"]]
        errors = []
        for future in futures:
            try:
                future.result()
            except Exception as error:
                errors.append(str(error))
        pool.shutdown(wait=True)
        pool = None
        if errors:
            raise RuntimeError("; ".join(errors))
        for version in report["interpreters"]:
            report["test_counts"][version] = validate_manifests(
                [output / f"python{version}-{name}.json" for name in SHARDS])
            name = f"python{version}-foundation"
            checkout = work / name
            checks.checkout(ROOT, commit, checkout, name)
            env = environment()
            env["PYTHONPATH"] = str(checkout / "project/src")
            python = report["interpreters"][version]["path"]
            checks.run(name + "-doctor", [python, "-m", "pdm", "doctor", "--repo", "."], checkout, env)
            archive = output / f"python{version}-source.zip"
            checks.run(name + "-archive", ["git", "archive", "--format=zip", commit,
                                          "--output", str(archive)], checkout)
            check_archive(archive)
            artifact = output / f"python{version}-code.zip"
            checks.run(name + "-build", [python, "-m", "pdm", "release", "build", "--repo", ".",
                       "--ref", commit, "--output", str(artifact)], checkout, env)
            checks.run(name + "-verify", [python, "-m", "pdm", "release", "verify", str(artifact),
                       "--expected-commit", commit, "--expected-package-version",
                       report["package_version"]], checkout, env)
        report["status"] = "passed"
    except KeyboardInterrupt:
        report.update(status="interrupted", error="Interrupted by user")
    except Exception as error:
        report.update(status="failed", error=str(error))
    finally:
        cleanup_errors = []
        for cleanup in (checks.stop,
                        lambda: pool.shutdown(wait=True, cancel_futures=True) if pool else None,
                        lambda: temporary.cleanup() if temporary else None):
            try:
                cleanup()
            except Exception as error:
                cleanup_errors.append(str(error))
        try:
            if before is not None:
                report["source_after"] = source_state(ROOT)
                report["source_unchanged"] = report["source_after"] == before
                if not report["source_unchanged"]:
                    cleanup_errors.append("Source repository changed during checks")
        except Exception as error:
            cleanup_errors.append(str(error))
        if cleanup_errors:
            report.update(status="failed", cleanup_errors=cleanup_errors)
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        checks.save()
    print(f"{report['status'].upper()}: {output / 'report.json'}", flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
