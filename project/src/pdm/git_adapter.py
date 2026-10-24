"""限定目录的 Git 采集适配器；不会写正式记录或主题页。"""

import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .contracts import CaptureError
from .compatibility import ensure_memory_compatible

ALLOWED_ROOTS = ("memory/sources/conversations/", "memory/sources/inbox/", "memory/runs/captures/")


class GitAdapter:
    def __init__(self, repo: Path):
        self.repo = Path(repo).resolve()
        if self.text("rev-parse", "--show-toplevel") != str(self.repo):
            raise CaptureError("invalid_repository", "必须指定 Git 工作区根目录")
        self._environment_check()

    def _environment_check(self, remote=None):
        marker = self.repo / ".git"
        # Normal legacy clones have no binding. Avoid extra Git subprocesses on
        # every existing low-level adapter construction/check in this case.
        binding = marker / "pdm-environment.json"
        if marker.is_dir() and not binding.exists() and not binding.is_symlink():
            return None
        from .runtime import require_environment
        return require_environment(self.repo, remote=remote)

    def run(self, *args: str, index: str | None = None) -> bytes:
        environment = {k: v for k, v in os.environ.items() if k not in {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        }}
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if index:
            environment["GIT_INDEX_FILE"] = index
        try:
            result = subprocess.run(
                ["git", "-c", "commit.gpgsign=false", *args],
                cwd=self.repo, capture_output=True, timeout=60, check=True,
                env=environment,
            )
            return result.stdout
        except (OSError, subprocess.SubprocessError) as error:
            # Git stderr 可能包含凭证 URL；不复制到用户日志。
            raise CaptureError("git_failed", f"Git {args[0]} 未完成，需要核对已有提交") from error

    def text(self, *args: str) -> str:
        return self.run(*args).decode("utf-8").strip()

    def head(self) -> str:
        return self.text("rev-parse", "--verify", "HEAD")

    def check(self, expected_head: str | None = None) -> str:
        self._environment_check()
        head = self.head()
        if expected_head is not None and head != expected_head:
            raise CaptureError("head_conflict", "仓库基础版本已变化；读取当前版本后重试")
        if self.run("status", "--porcelain", "--untracked-files=all"):
            raise CaptureError("dirty_worktree", "采集要求干净工作区，请先处理现有改动")
        ensure_memory_compatible(self.repo)
        return head

    def path(self, relative: str) -> Path:
        parts = PurePosixPath(relative)
        if parts.is_absolute() or ".." in parts.parts or "\\" in relative or not relative.startswith(ALLOWED_ROOTS):
            raise CaptureError("path_rejected", "采集路径超出允许目录")
        current = self.repo
        for part in parts.parts:
            current = current / part
            if current.is_symlink():
                raise CaptureError("path_rejected", "采集路径不能经过符号链接")
        if not current.resolve().is_relative_to(self.repo):
            raise CaptureError("path_rejected", "采集路径超出工作区")
        return current

    @contextmanager
    def lock(self):
        # Also gates recover(), which intentionally tolerates a dirty worktree.
        self._environment_check()
        git_dir = Path(self.text("rev-parse", "--absolute-git-dir"))
        lock = git_dir / "pdm-capture.lock"
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise CaptureError("capture_locked", "已有采集操作；若进程异常退出，请核对后移除采集锁") from error
        try:
            os.write(descriptor, str(os.getpid()).encode())
            yield
        finally:
            os.close(descriptor)
            lock.unlink()

    def write_commit(self, relative: str, data: bytes, expected_head: str) -> str:
        self.check(expected_head)
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self.path(relative)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        git_dir = self.text("rev-parse", "--absolute-git-dir")
        index_fd, index = tempfile.mkstemp(prefix="pdm-index-", dir=git_dir)
        os.close(index_fd)
        os.unlink(index)
        committed = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            # 独立 index 保证树只含本次路径；update-ref 以旧 SHA 原子条件写入。
            self.run("read-tree", expected_head, index=index)
            blob = self.text("hash-object", "-w", "--no-filters", "--", relative)
            self.run("update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}", index=index)
            tree = self.run("write-tree", index=index).decode().strip()
            commit = self.text("commit-tree", tree, "-p", expected_head, "-m", "capture: persist source or receipt")
            self.run("update-ref", "HEAD", commit, expected_head)
            committed = True
            self.run("reset", "--quiet", commit, "--", relative)
        except Exception:
            # 只清理由本次创建、内容未变化且尚未提交的文件。
            if not committed and target.read_bytes() == data:
                target.unlink()
            raise
        finally:
            Path(index).unlink(missing_ok=True)
        return commit

    def read(self, commit: str, relative: str) -> bytes:
        self.path(relative)
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise CaptureError("invalid_commit", "读取必须指定完整提交标识")
        return self.run("show", f"{commit}:{relative}")

    def source_commit(self, relative: str) -> str:
        self.path(relative)
        return self.text("log", "-1", "--format=%H", "--", relative)

    def sync(self, remote: str, files: dict[str, bytes], commit: str) -> None:
        if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", remote):
            raise CaptureError("invalid_remote", "remote 必须是已配置的远程名称")
        self._environment_check(remote)
        branch = self.text("symbolic-ref", "--quiet", "--short", "HEAD")
        self.run("push", "--porcelain", remote, f"{commit}:refs/heads/{branch}")
        self.run("fetch", "--no-tags", remote, f"refs/heads/{branch}")
        remote_commit = self.text("rev-parse", "FETCH_HEAD")
        self.run("merge-base", "--is-ancestor", commit, remote_commit)
        for relative, data in files.items():
            if self.read(remote_commit, relative) != data:
                raise CaptureError("remote_mismatch", "远程读回内容与本次保存不符")
