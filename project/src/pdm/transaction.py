"""Foundation 多文件 Git 事务、显式条件更新与受限恢复。"""

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .contracts import CaptureError
from .git_adapter import ALLOWED_ROOTS, GitAdapter
from .storage_limits import LEGACY_FILE_BYTES, read_limit, validate_file_bytes

WRITE_ROOTS = ("memory/staging/", "memory/records/", "memory/topics/", "memory/runs/processes/",
               "memory/runs/controls/", "memory/runs/pending/", "memory/runs/schedules/")
WRITE_FILES = {"memory/pending.md"}
READ_ROOTS = ALLOWED_ROOTS + WRITE_ROOTS
MAX_FILES = 256
MAX_FILE_BYTES = LEGACY_FILE_BYTES
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_BYTES = 128 * 1024
_HEX = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class PipelineGit(GitAdapter):
    """调用者持有继承的 lock；不扩大采集适配器本身的写入范围。"""

    def run(self, *args: str, index: str | None = None) -> bytes:
        from .verification_cache import immutable_git_read
        return immutable_git_read(self.repo, args, index,
                                  lambda: super(PipelineGit, self).run(*args, index=index))

    def write_commit(self, relative: str, data: bytes, expected_head: str) -> str:
        raise CaptureError("invalid_transaction", "加工适配器只允许带恢复日志的 write_files 事务")

    @staticmethod
    def _validate_storage(relative: str, value: bytes, code="invalid_transaction") -> None:
        try:
            validate_file_bytes(relative, value)
        except CaptureError as error:
            raise CaptureError(code, "事务内容超出该路径和固定候选合同的存储边界") from error

    def _storage_blob(self, revision: str, relative: str, code="transaction_recovery_conflict") -> bytes:
        # Check immutable blob size before loading it; the path only grants a
        # provisional bound, never the candidate exception by itself.
        size = self.text("cat-file", "-s", revision + ":" + relative)
        if not size.isdigit() or int(size) > read_limit(relative):
            raise CaptureError(code, "事务 blob 超出该目标的读取上界")
        value = self.read(revision, relative)
        if len(value) != int(size):
            raise CaptureError(code, "事务 blob 长度不符")
        self._validate_storage(relative, value, code)
        return value

    def path(self, relative: str) -> Path:
        if not isinstance(relative, str) or len(relative) > 4096:
            raise CaptureError("path_rejected", "加工路径无效")
        parts = PurePosixPath(relative)
        if (parts.is_absolute() or str(parts) != relative
                or not (relative.startswith(READ_ROOTS) or relative in WRITE_FILES)
                or not relative.endswith(".md")
                or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) for part in parts.parts)
                or ".." in parts.parts):
            raise CaptureError("path_rejected", "加工路径超出允许范围")
        current = self.repo
        for part in parts.parts:
            current = current / part
            if current.is_symlink():
                raise CaptureError("path_rejected", "加工路径不能经过符号链接")
        if not current.resolve().is_relative_to(self.repo):
            raise CaptureError("path_rejected", "加工路径超出工作区")
        return current

    def _write_path(self, relative: str) -> Path:
        target = self.path(relative)
        if not (relative.startswith(WRITE_ROOTS) or relative in WRITE_FILES):
            raise CaptureError("path_rejected", "加工事务不能改写原始来源或采集回执")
        return target

    def _journal_path(self) -> Path:
        return Path(self.text("rev-parse", "--absolute-git-dir")) / "pdm-pipeline-transaction.json"

    def _reference(self) -> str:
        # 当前分支绑定到日志；恢复时不能把另一个 checkout 的资料写进来。
        try:
            return self.text("symbolic-ref", "--quiet", "HEAD")
        except CaptureError:
            return "HEAD"

    @contextmanager
    def _parent(self, relative: str, *, create: bool = False):
        self.path(relative)
        descriptor = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in PurePosixPath(relative).parts[:-1]:
                if create:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            yield descriptor, PurePosixPath(relative).name
        except (NotADirectoryError, OSError) as error:
            if isinstance(error, FileNotFoundError):
                raise
            raise CaptureError("path_rejected", "加工路径不能经过符号链接或非目录") from error
        finally:
            os.close(descriptor)

    def _existing(self, relative: str) -> bytes | None:
        try:
            with self._parent(relative) as (parent, name):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                with os.fdopen(descriptor, "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise CaptureError("path_rejected", "事务目标必须是普通文件")
                    value = stream.read(read_limit(relative) + 1)
                self._validate_storage(relative, value, "transaction_conflict")
                return value
        except FileNotFoundError:
            return None

    def _tree_blob(self, commit: str, relative: str) -> str | None:
        entry = self.run("ls-tree", "-z", commit, "--", relative)
        if not entry:
            return None
        try:
            header, saved = entry[:-1].decode("utf-8").split("\t")
            mode, kind, blob = header.split()
            if not entry.endswith(b"\0") or saved != relative or mode != "100644" or kind != "blob" or not _HEX.fullmatch(blob):
                raise ValueError
            return blob
        except (UnicodeError, ValueError) as error:
            raise CaptureError("transaction_conflict", "事务目标不是普通 Git 文件") from error

    def _write_journal(self, journal: dict) -> None:
        path = self._journal_path()
        encoded = json.dumps(journal, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_JOURNAL_BYTES:
            raise CaptureError("transaction_too_large", "事务日志超出边界")
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError as error:
            raise CaptureError("transaction_recovery_required", "已有待核对事务，请先恢复") from error
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def _load_journal(self) -> dict | None:
        path = self._journal_path()
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise CaptureError("transaction_recovery_conflict", "事务日志不可安全读取") from error
        try:
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError
                encoded = stream.read(MAX_JOURNAL_BYTES + 1)
            if len(encoded) > MAX_JOURNAL_BYTES:
                raise ValueError

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            journal = json.loads(encoded, object_pairs_hook=unique)
            if (not isinstance(journal, dict) or set(journal) != {"version", "base", "commit", "reference", "files", "progress"}
                    or type(journal["version"]) is not int or journal["version"] not in {2, 3}
                    or not isinstance(journal["files"], dict)
                    or not isinstance(journal["progress"], dict) or set(journal["progress"]) != set(journal["files"])
                    or any(not isinstance(value, str) or value not in (
                        {"pending", "linking", "linked", "indexing", "done"} if journal["version"] == 2
                        else {"pending", "moving", "moved", "linking", "linked", "indexing", "done"}
                    ) for value in journal["progress"].values())
                    or not 1 <= len(journal["files"]) <= MAX_FILES
                    or not isinstance(journal["reference"], str)
                    or not (journal["reference"] == "HEAD" or re.fullmatch(r"refs/heads/[A-Za-z0-9/_\-.]+", journal["reference"]))):
                raise ValueError
            for key in ("base", "commit"):
                if not isinstance(journal[key], str) or not _HEX.fullmatch(journal[key]):
                    raise ValueError
            for relative, details in journal["files"].items():
                self._write_path(relative)
                if journal["version"] == 3:
                    if (not isinstance(details, dict)
                            or set(details) != {"before_blob", "before_sha256", "after_blob", "after_sha256"}):
                        raise ValueError
                    for prefix in ("before", "after"):
                        blob, digest = details[prefix + "_blob"], details[prefix + "_sha256"]
                        if ((blob is None) != (digest is None)
                                or (blob is not None and (not isinstance(blob, str) or not _HEX.fullmatch(blob)
                                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)))):
                            raise ValueError
                    continue
                if (not isinstance(details, dict) or set(details) != {"sha256", "blob"}
                        or not isinstance(details["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", details["sha256"])
                        or not isinstance(details["blob"], str) or not _HEX.fullmatch(details["blob"])):
                    raise ValueError
            return journal
        except (ValueError, UnicodeError, TypeError, CaptureError) as error:
            raise CaptureError("transaction_recovery_conflict", "事务日志无效；未改写资料") from error

    def _progress(self, journal: dict, relative: str, phase: str) -> None:
        """先持久记录副作用边界；不确定窗口保持暂停，不猜测人工删除。"""
        updated = {**journal, "progress": {**journal["progress"], relative: phase}}
        encoded = json.dumps(updated, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        path = self._journal_path()
        if path.is_symlink() or len(encoded) > MAX_JOURNAL_BYTES:
            raise CaptureError("transaction_recovery_conflict", "事务日志无法安全更新")
        descriptor, temporary = tempfile.mkstemp(prefix="pdm-journal-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            journal["progress"] = updated["progress"]
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _materialize(self, relative: str, data: bytes, blob: str, journal: dict) -> None:
        phase = journal["progress"][relative]
        if phase == "done":
            return
        if phase == "indexing":
            # _preflight 已验证文件和 index 均完整，确认之前的原子 index 更新。
            self._progress(journal, relative, "done")
            return
        with self._parent(relative, create=True) as (parent, name):
            existing = self._existing(relative)
            temporary = self._temporary_name(relative, blob)
            leftover = self._temporary_content(parent, temporary, relative)
            if leftover is not None and leftover != data:
                raise CaptureError("transaction_recovery_conflict", "事务临时文件未完整写入；保持暂停")
            if existing is None:
                if phase != "pending":
                    raise CaptureError("transaction_recovery_conflict", "已开始物化的目标缺失；未重建")
                # 同目录临时文件先完整写入，hard link 仅在目标不存在时成功。
                created = False
                try:
                    if leftover is None:
                        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                        created = True
                        with os.fdopen(descriptor, "wb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                    self._progress(journal, relative, "linking")
                    os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    os.fsync(parent)
                    self._progress(journal, relative, "linked")
                finally:
                    if created or leftover is not None:
                        os.unlink(temporary, dir_fd=parent)
            elif existing != data:
                raise CaptureError("transaction_recovery_conflict", "事务目标已被修改，未覆盖")
            elif phase == "pending":
                raise CaptureError("transaction_recovery_conflict", "未开始物化的目标已存在；未覆盖")
            elif leftover is not None:
                os.unlink(temporary, dir_fd=parent)
            if journal["progress"][relative] == "linking":
                self._progress(journal, relative, "linked")
        self._progress(journal, relative, "indexing")
        self.run("update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}")
        self._progress(journal, relative, "done")

    @staticmethod
    def _temporary_name(relative: str, blob: str) -> str:
        return ".pdm-transaction-" + hashlib.sha256((relative + blob).encode()).hexdigest()[:32]

    @staticmethod
    def _temporary_content(parent: int, name: str, relative: str) -> bytes | None:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CaptureError("path_rejected", "事务临时路径必须是普通文件")
            value = stream.read(read_limit(relative) + 1)
        PipelineGit._validate_storage(relative, value, "transaction_recovery_conflict")
        return value

    def _preflight(self, journal: dict) -> dict[str, bytes]:
        if self._reference() != journal["reference"]:
            raise CaptureError("transaction_recovery_conflict", "事务分支已变化，未改写资料")
        commit, base, head = journal["commit"], journal["base"], self.head()
        if self.text("rev-list", "--parents", "-n", "1", commit).split() != [commit, base]:
            raise CaptureError("transaction_recovery_conflict", "事务提交的父版本不符")
        self.run("merge-base", "--is-ancestor", commit, head)
        actual = self.run("diff-tree", "--no-commit-id", "--name-status", "-r", "-z", base, commit).decode().split("\0")
        expected = [item for relative in sorted(journal["files"]) for item in ("A", relative)] + [""]
        if actual != expected:
            raise CaptureError("transaction_recovery_conflict", "事务提交含未授权的变化")
        values, total = {}, 0
        for relative, details in journal["files"].items():
            if self._tree_blob(commit, relative) != details["blob"] or self._tree_blob(head, relative) != details["blob"]:
                raise CaptureError("transaction_recovery_conflict", "当前版本已修改或移除事务目标")
            value = self._storage_blob(commit, relative)
            total += len(value)
            if total > MAX_TOTAL_BYTES or hashlib.sha256(value).hexdigest() != details["sha256"]:
                raise CaptureError("transaction_recovery_conflict", "事务内容校验失败")
            existing = self._existing(relative)
            if existing is not None and existing != value:
                raise CaptureError("transaction_recovery_conflict", "事务目标已被修改，未覆盖")
            try:
                with self._parent(relative) as (parent, _):
                    leftover = self._temporary_content(parent, self._temporary_name(relative, details["blob"]), relative)
                    if leftover is not None and leftover != value:
                        raise CaptureError("transaction_recovery_conflict", "事务临时文件未完整写入；保持暂停")
            except FileNotFoundError:
                pass
            entries = self.run("ls-files", "--stage", "-z", "--", relative)
            expected_entry = f"100644 {details['blob']} 0\t{relative}\0".encode()
            if entries and entries != expected_entry:
                raise CaptureError("transaction_recovery_conflict", "事务目标已被暂存修改，未覆盖")
            phase = journal["progress"][relative]
            if phase == "pending":
                if existing is not None or entries:
                    raise CaptureError("transaction_recovery_conflict", "未开始物化的目标已被外部写入")
            elif existing is None:
                raise CaptureError("transaction_recovery_conflict", "已开始物化的目标缺失；未重建")
            elif phase in {"indexing", "done"} and entries != expected_entry:
                raise CaptureError("transaction_recovery_conflict", "事务目标 index 已移除或更新结果不明；未重建")
            elif phase in {"linking", "linked"} and entries:
                raise CaptureError("transaction_recovery_conflict", "尚未暂存的目标被外部修改了 index")
            values[relative] = value
        return values

    def recover(self) -> dict:
        """需在 lock 内调用；只恢复日志绑定的提交，保留其他工作区改动。"""
        self._environment_check()
        journal = self._load_journal()
        if journal is None:
            return {"status": "clean", "commit_sha": None, "files": 0}
        if journal["version"] == 3:
            return self._recover_changes(journal)
        if self._reference() != journal["reference"]:
            raise CaptureError("transaction_recovery_conflict", "事务分支已变化，未改写资料")
        if self.head() == journal["base"]:
            # CAS 未发布；不恢复文件，也不尝试重新执行未完成的写入。
            self._journal_path().unlink()
            return {"status": "aborted", "commit_sha": None, "files": 0}
        try:
            values = self._preflight(journal)
            for relative, value in values.items():
                self._materialize(relative, value, journal["files"][relative]["blob"], journal)
            self._preflight(journal)
            self._journal_path().unlink()
        except (OSError, CaptureError) as error:
            raise CaptureError("transaction_recovery_conflict", "事务恢复未完成；未覆盖已有改动") from error
        return {"status": "recovered", "commit_sha": journal["commit"], "files": len(values)}

    def write_files(self, files: dict[str, bytes], expected_head: str, message: str) -> str:
        """在一个 create-only Git 提交中发布整批；调用者持有共享 lock。"""
        if self._journal_path().exists() or self._journal_path().is_symlink():
            raise CaptureError("transaction_recovery_required", "已有待核对事务，请先恢复")
        if (not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES
                or any(not isinstance(value, bytes) for value in files.values())
                or sum(len(value) for value in files.values()) > MAX_TOTAL_BYTES
                or not isinstance(message, str) or not 1 <= len(message) <= 1024 or "\0" in message):
            raise CaptureError("invalid_transaction", "加工事务输入无效或超出边界")
        for relative, value in files.items():
            self._validate_storage(relative, value)
        self.check(expected_head)
        reference = self._reference()
        for relative in files:
            target = self._write_path(relative)
            if target.exists() or self._tree_blob(expected_head, relative) is not None:
                raise CaptureError("target_exists", "加工事务不能覆盖已有文件")
        git_dir = self._journal_path().parent
        journal = None
        journal_written = False
        committed = False
        with tempfile.TemporaryDirectory(prefix="pdm-pipeline-", dir=git_dir) as temporary:
            index = str(Path(temporary) / "index")
            self.run("read-tree", expected_head, index=index)
            details = {}
            for number, (relative, value) in enumerate(sorted(files.items())):
                temporary_blob = Path(temporary) / f"blob-{number}"
                temporary_blob.write_bytes(value)
                temporary_blob.chmod(0o600)
                blob = self.text("hash-object", "-w", "--no-filters", "--", str(temporary_blob))
                self.run("update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}", index=index)
                details[relative] = {"sha256": hashlib.sha256(value).hexdigest(), "blob": blob}
            tree = self.run("write-tree", index=index).decode().strip()
            commit = self.text("commit-tree", tree, "-p", expected_head, "-m", message)
            journal = {"version": 2, "base": expected_head, "commit": commit, "reference": reference, "files": details,
                       "progress": {relative: "pending" for relative in details}}
            try:
                self._write_journal(journal)
                journal_written = True
                if self.head() != expected_head or self._reference() != reference:
                    raise CaptureError("head_conflict", "仓库基础版本已变化；读取当前版本后重试")
                self.run("update-ref", reference, commit, expected_head)
                committed = True
                values = self._preflight(journal)
                for relative, value in values.items():
                    self._materialize(relative, value, details[relative]["blob"], journal)
                self._preflight(journal)
                self._journal_path().unlink()
            except Exception as error:
                if not committed:
                    try:
                        current = self.text("rev-parse", reference)
                        if current == commit:
                            committed = True
                        elif current != expected_head:
                            try:
                                self.run("merge-base", "--is-ancestor", commit, current)
                                committed = True
                            except CaptureError:
                                pass
                    except CaptureError:
                        # Git 结果无法确认时保留日志；不能报告未提交。
                        committed = True
                if committed:
                    raise CaptureError("transaction_recovery_required", "事务提交结果需恢复核对；请先执行 recover") from error
                if journal_written and self._journal_path().exists():
                    self._journal_path().unlink()
                raise
        return commit

    @staticmethod
    def _change_names(relative: str, details: dict) -> tuple[str, str]:
        binding = relative + str(details["before_sha256"]) + str(details["after_sha256"])
        token = hashlib.sha256(binding.encode()).hexdigest()[:32]
        return ".pdm-change-before-" + token, ".pdm-change-after-" + token

    def _index_entry(self, relative: str) -> bytes:
        return self.run("ls-files", "--stage", "-z", "--", relative)

    @staticmethod
    def _expected_entry(relative: str, blob: str | None) -> bytes:
        return b"" if blob is None else f"100644 {blob} 0\t{relative}\0".encode()

    def _check_change_before(self, expected_files: dict[str, bytes | None], expected_head: str) -> dict:
        """在创建 Git 对象或日志之前检查所有目标，包括被 status 忽略的文件。"""
        self.check(expected_head)
        details = {}
        for relative, value in expected_files.items():
            self._write_path(relative)
            blob = self._tree_blob(expected_head, relative)
            if ((blob is None) != (value is None)
                    or (blob is not None and self._storage_blob(expected_head, relative, "transaction_conflict") != value)
                    or self._existing(relative) != value
                    or self._index_entry(relative) != self._expected_entry(relative, blob)):
                raise CaptureError("transaction_conflict", "事务旧值与基础版本、工作区或 index 不一致")
            details[relative] = {"before_blob": blob,
                                 "before_sha256": None if value is None else hashlib.sha256(value).hexdigest()}
        return details

    def _change_temporary(self, relative: str, details: dict, before: bytes | None, after: bytes | None,
                          phase: str) -> None:
        try:
            with self._parent(relative) as (parent, _):
                old_name, new_name = self._change_names(relative, details)
                old, new = self._temporary_content(parent, old_name, relative), self._temporary_content(parent, new_name, relative)
                if ((old is not None and old != before) or (new is not None and new != after)
                        or (phase == "pending" and old is not None)
                        or (phase == "linked" and before is not None and before != after and old is None)):
                    raise CaptureError("transaction_recovery_conflict", "条件事务临时文件不符；保持暂停")
        except FileNotFoundError:
            if phase != "pending" and (before is not None or after is not None):
                raise CaptureError("transaction_recovery_conflict", "条件事务目标目录缺失；未重建")

    def _preflight_changes(self, journal: dict, *, committed: bool = True) -> dict:
        """完整验证 v3 的父边、允许的 A/M/D、前后 blob 和所有物化阶段。"""
        if self._reference() != journal["reference"]:
            raise CaptureError("transaction_recovery_conflict", "条件事务分支已变化")
        base, commit, head = journal["base"], journal["commit"], self.head()
        if self.text("rev-list", "--parents", "-n", "1", commit).split() != [commit, base]:
            raise CaptureError("transaction_recovery_conflict", "条件事务提交父版本不符")
        if committed:
            self.run("merge-base", "--is-ancestor", commit, head)
        elif head != base:
            raise CaptureError("transaction_recovery_conflict", "未发布条件事务的基础版本已变化")
        expected_diff, values, total = [], {}, 0
        for relative, details in sorted(journal["files"].items()):
            self._write_path(relative)
            before_blob, after_blob = details["before_blob"], details["after_blob"]
            if (self._tree_blob(base, relative) != before_blob
                    or self._tree_blob(commit, relative) != after_blob
                    or (committed and self._tree_blob(head, relative) != after_blob)):
                raise CaptureError("transaction_recovery_conflict", "条件事务目标已在 Git 中修改或移除")
            pair = []
            for prefix, revision, blob in (("before", base, before_blob), ("after", commit, after_blob)):
                value = None if blob is None else self._storage_blob(revision, relative)
                if value is not None:
                    total += len(value)
                    if hashlib.sha256(value).hexdigest() != details[prefix + "_sha256"]:
                        raise CaptureError("transaction_recovery_conflict", "条件事务内容摘要不符")
                pair.append(value)
            before, after = pair
            if before_blob != after_blob:
                expected_diff.extend(("A" if before_blob is None else "D" if after_blob is None else "M", relative))
            phase = journal["progress"][relative]
            # 路径移动/链接与日志之间不能辨别外部删除；不自动猜测或复活。
            if phase in {"moving", "moved", "linking"} or (not committed and phase != "pending"):
                raise CaptureError("transaction_recovery_conflict", "条件事务物化结果不确定；保持暂停")
            expected_value = before if phase == "pending" else after
            expected_blob = before_blob if phase in {"pending", "linked"} else after_blob
            if self._existing(relative) != expected_value or self._index_entry(relative) != self._expected_entry(relative, expected_blob):
                raise CaptureError("transaction_recovery_conflict", "条件事务工作区或 index 已改变；未覆盖")
            self._change_temporary(relative, details, before, after, phase)
            if not committed:
                try:
                    with self._parent(relative) as (parent, _):
                        if any(self._temporary_content(parent, name, relative) is not None
                               for name in self._change_names(relative, details)):
                            raise CaptureError("transaction_recovery_conflict", "未发布事务含物化痕迹；保持暂停")
                except FileNotFoundError:
                    pass
            values[relative] = (before, after)
        if total > MAX_TOTAL_BYTES or not expected_diff:
            raise CaptureError("transaction_recovery_conflict", "条件事务内容超限或没有授权变化")
        actual = self.run("diff-tree", "--no-commit-id", "--no-renames", "--name-status", "-r", "-z", base, commit).decode().split("\0")
        if actual != expected_diff + [""]:
            raise CaptureError("transaction_recovery_conflict", "条件事务提交含未授权变化")
        return values

    def _change_reference(self, journal: dict, expected_current: str) -> None:
        if self._reference() != journal["reference"] or self.head() != expected_current:
            raise CaptureError("transaction_recovery_conflict", "条件事务执行期间分支或 HEAD 已变化")

    @staticmethod
    def _move_noreplace(parent: int, source: str, destination: str) -> None:
        """原子捕获当前目录项且不覆盖快照；平台缺失时不使用 link/unlink 代替。"""
        try:
            rename = ctypes.CDLL(None, use_errno=True).renameat2
        except (AttributeError, OSError) as error:
            raise CaptureError("atomic_move_unavailable", "平台缺少安全条件更新所需的原子移动能力") from error
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(parent, os.fsencode(source), parent, os.fsencode(destination), 1) != 0:
            error = ctypes.get_errno()
            if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                raise CaptureError("atomic_move_unavailable", "文件系统不支持不覆盖目标的原子移动")
            raise OSError(error, os.strerror(error))

    def _check_atomic_moves(self, files: dict, expected_files: dict) -> None:
        """全部旧值通过核对后，在涉及的目录验证原语；不触及任何资料路径。"""
        checked = set()
        for relative, before in expected_files.items():
            if before is None or before == files[relative]:
                continue
            with self._parent(relative) as (parent, _):
                identity = os.fstat(parent)
                if (identity.st_dev, identity.st_ino) in checked:
                    continue
                directory = ".pdm-move-probe-" + secrets.token_hex(16)
                os.mkdir(directory, mode=0o700, dir_fd=parent)
                probe = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    for name in ("source", "destination"):
                        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                             0o600, dir_fd=probe)
                        os.close(descriptor)
                    source = os.stat("source", dir_fd=probe, follow_symlinks=False)
                    destination = os.stat("destination", dir_fd=probe, follow_symlinks=False)
                    try:
                        self._move_noreplace(probe, "source", "destination")
                    except FileExistsError:
                        pass
                    else:
                        raise CaptureError("atomic_move_unavailable", "原子移动未保留既存目标")
                    if (os.stat("source", dir_fd=probe).st_ino != source.st_ino
                            or os.stat("destination", dir_fd=probe).st_ino != destination.st_ino):
                        raise CaptureError("atomic_move_unavailable", "原子移动能力探测结果不符")
                    os.unlink("destination", dir_fd=probe)
                    self._move_noreplace(probe, "source", "destination")
                    if os.stat("destination", dir_fd=probe, follow_symlinks=False).st_ino != source.st_ino:
                        raise CaptureError("atomic_move_unavailable", "原子移动能力探测结果不符")
                finally:
                    for name in ("source", "destination"):
                        try:
                            os.unlink(name, dir_fd=probe)
                        except FileNotFoundError:
                            pass
                    os.close(probe)
                    os.rmdir(directory, dir_fd=parent)
                checked.add((identity.st_dev, identity.st_ino))

    def _change_index(self, relative: str, before_blob: str | None, after_blob: str | None) -> None:
        """持有标准 index.lock 后复制并更新 index，保留无关暂存内容。"""
        git_dir = self._journal_path().parent
        index, lock = git_dir / "index", git_dir / "index.lock"

        def index_bytes():
            try:
                source = os.open(index, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                return None
            with os.fdopen(source, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise CaptureError("transaction_recovery_conflict", "index 必须是普通文件")
                value = stream.read(64 * 1024 * 1024 + 1)
            if len(value) > 64 * 1024 * 1024:
                raise CaptureError("transaction_recovery_conflict", "index 超出安全复制边界")
            return value

        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        owned = os.fstat(descriptor)
        try:
            old = index_bytes()
            if self._index_entry(relative) != self._expected_entry(relative, before_blob):
                raise CaptureError("transaction_recovery_conflict", "条件事务 index 旧值已改变")
            with tempfile.TemporaryDirectory(prefix="pdm-change-index-", dir=git_dir) as temporary:
                private = Path(temporary) / "index"
                if old is not None:
                    private.write_bytes(old)
                else:
                    self.run("read-tree", "--empty", index=str(private))
                if after_blob is None:
                    self.run("update-index", "--force-remove", "--", relative, index=str(private))
                else:
                    self.run("update-index", "--add", "--cacheinfo", f"100644,{after_blob},{relative}", index=str(private))
                new = private.read_bytes()
            if index_bytes() != old:
                raise CaptureError("transaction_recovery_conflict", "条件事务 index 在锁外被修改")
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(new)
                stream.flush()
                os.fsync(stream.fileno())
            saved = lock.lstat()
            if (saved.st_dev, saved.st_ino) != (owned.st_dev, owned.st_ino):
                raise CaptureError("transaction_recovery_conflict", "条件事务 index 锁已改变")
            os.replace(lock, index)
            directory = os.open(git_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.close(descriptor)
            try:
                saved = lock.lstat()
                if (saved.st_dev, saved.st_ino) == (owned.st_dev, owned.st_ino):
                    lock.unlink()
            except FileNotFoundError:
                pass

    def _materialize_change(self, relative: str, before: bytes | None, after: bytes | None,
                            journal: dict, expected_current: str) -> None:
        self._change_reference(journal, expected_current)
        details, phase = journal["files"][relative], journal["progress"][relative]
        if phase == "done":
            return
        if phase == "indexing":
            self._progress(journal, relative, "done")
            return
        if before == after:
            self._progress(journal, relative, "done")
            return
        if phase == "pending":
            with self._parent(relative, create=after is not None) as (parent, name):
                if (self._existing(relative) != before
                        or self._index_entry(relative) != self._expected_entry(relative, details["before_blob"])):
                    raise CaptureError("transaction_recovery_conflict", "条件事务目标在物化前已改变")
                old_name, new_name = self._change_names(relative, details)
                leftover = self._temporary_content(parent, new_name, relative)
                if after is not None and leftover is None:
                    descriptor = os.open(new_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(after)
                        stream.flush()
                        os.fsync(stream.fileno())
                elif leftover is not None and leftover != after:
                    raise CaptureError("transaction_recovery_conflict", "条件事务临时新值不符")
                self._change_reference(journal, expected_current)
                if self._existing(relative) != before:
                    raise CaptureError("transaction_recovery_conflict", "条件事务旧值在移动前已改变")
                if before is not None:
                    # 原子捕获实际目录项。若最后核对后有人 replace，捕获的是外部
                    # 对象，后续核对会暂停且完整保留该对象，不按旧路径 unlink。
                    previous = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    self._progress(journal, relative, "moving")
                    self._move_noreplace(parent, name, old_name)
                    os.fsync(parent)
                    saved = os.stat(old_name, dir_fd=parent, follow_symlinks=False)
                    if ((previous.st_dev, previous.st_ino) != (saved.st_dev, saved.st_ino)
                            or not stat.S_ISREG(saved.st_mode)
                            or self._temporary_content(parent, old_name, relative) != before):
                        raise CaptureError("transaction_recovery_conflict", "捕获的旧文件已并发替换或修改；保留快照并暂停")
                    self._progress(journal, relative, "moved")
                if after is not None:
                    self._progress(journal, relative, "linking")
                    if before is not None and self._temporary_content(parent, old_name, relative) != before:
                        raise CaptureError("transaction_recovery_conflict", "条件事务旧快照已改变")
                    os.link(new_name, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    os.fsync(parent)
                self._progress(journal, relative, "linked")
        self._change_reference(journal, expected_current)
        if self._existing(relative) != after:
            raise CaptureError("transaction_recovery_conflict", "条件事务新值已被外部修改")
        self._progress(journal, relative, "indexing")
        self._change_index(relative, details["before_blob"], details["after_blob"])
        self._progress(journal, relative, "done")

    def _finish_changes(self, journal: dict, values: dict, expected_current: str) -> None:
        for relative, (before, after) in values.items():
            self._preflight_changes(journal)
            self._materialize_change(relative, before, after, journal, expected_current)
        self._preflight_changes(journal)
        for relative, (before, after) in values.items():
            try:
                with self._parent(relative) as (parent, _):
                    for name, expected in zip(self._change_names(relative, journal["files"][relative]), (before, after)):
                        actual = self._temporary_content(parent, name, relative)
                        if actual is not None:
                            if actual != expected:
                                raise CaptureError("transaction_recovery_conflict", "条件事务清理发现外部临时文件改动")
                            os.unlink(name, dir_fd=parent)
                    os.fsync(parent)
            except FileNotFoundError:
                pass
        self._change_reference(journal, expected_current)
        self._preflight_changes(journal)
        self._journal_path().unlink()

    def _recover_changes(self, journal: dict) -> dict:
        try:
            if self.head() == journal["base"]:
                self._preflight_changes(journal, committed=False)
                self._journal_path().unlink()
                return {"status": "aborted", "commit_sha": None, "files": 0}
            current = self.head()
            values = self._preflight_changes(journal)
            pending = {relative: pair for relative, pair in values.items()
                       if journal["progress"][relative] == "pending"}
            self._check_atomic_moves({relative: pair[1] for relative, pair in pending.items()},
                                     {relative: pair[0] for relative, pair in pending.items()})
            self._finish_changes(journal, values, current)
        except (OSError, CaptureError) as error:
            raise CaptureError("transaction_recovery_conflict", "条件事务恢复未完成；保留冲突和日志") from error
        return {"status": "recovered", "commit_sha": journal["commit"], "files": len(values)}

    def write_changes(self, files: dict[str, bytes | None], expected_files: dict[str, bytes | None],
                      expected_head: str, message: str) -> str:
        """同一提交条件创建、更新和删除；None 分别表示不存在和删除。调用者持锁。"""
        if self._journal_path().exists() or self._journal_path().is_symlink():
            raise CaptureError("transaction_recovery_required", "已有待核对事务，请先恢复")
        if (not isinstance(files, dict) or not isinstance(expected_files, dict)
                or set(files) != set(expected_files) or not 1 <= len(files) <= MAX_FILES
                or any(value is not None and not isinstance(value, bytes)
                       for value in (*files.values(), *expected_files.values()))
                or sum(len(value) for value in (*files.values(), *expected_files.values()) if isinstance(value, bytes)) > MAX_TOTAL_BYTES
                or not any(files[path] != expected_files[path] for path in files)
                or not isinstance(expected_head, str) or not _HEX.fullmatch(expected_head)
                or not isinstance(message, str) or not 1 <= len(message) <= 1024 or "\0" in message):
            raise CaptureError("invalid_transaction", "条件事务输入无效或超出边界")
        for mapping in (files, expected_files):
            for relative, value in mapping.items():
                if value is not None:
                    self._validate_storage(relative, value)
        # 固定不可变 bytes 映射，避免调用方的回调修改已核对的发布内容。
        files, expected_files = dict(files), dict(expected_files)
        details = self._check_change_before(expected_files, expected_head)
        reference = self._reference()
        for relative, value in files.items():
            details[relative].update(after_blob=None, after_sha256=None if value is None else hashlib.sha256(value).hexdigest())
            self._change_temporary(relative, details[relative], expected_files[relative], value, "pending")
        self._check_atomic_moves(files, expected_files)
        git_dir = self._journal_path().parent
        journal_written, committed = False, False
        with tempfile.TemporaryDirectory(prefix="pdm-pipeline-", dir=git_dir) as temporary:
            index = str(Path(temporary) / "index")
            self.run("read-tree", expected_head, index=index)
            for number, (relative, value) in enumerate(sorted(files.items())):
                if value is None:
                    self.run("update-index", "--force-remove", "--", relative, index=index)
                else:
                    path = Path(temporary) / f"blob-{number}"
                    path.write_bytes(value)
                    path.chmod(0o600)
                    blob = self.text("hash-object", "-w", "--no-filters", "--", str(path))
                    details[relative]["after_blob"] = blob
                    self.run("update-index", "--add", "--cacheinfo", f"100644,{blob},{relative}", index=index)
            tree = self.run("write-tree", index=index).decode().strip()
            commit = self.text("commit-tree", tree, "-p", expected_head, "-m", message)
            journal = {"version": 3, "base": expected_head, "commit": commit, "reference": reference,
                       "files": details, "progress": {relative: "pending" for relative in details}}
            try:
                self._write_journal(journal)
                journal_written = True
                self._change_reference(journal, expected_head)
                self._check_change_before(expected_files, expected_head)
                self.run("update-ref", reference, commit, expected_head)
                committed = True
                values = self._preflight_changes(journal)
                self._finish_changes(journal, values, commit)
            except Exception as error:
                if not committed:
                    try:
                        current = self.text("rev-parse", reference)
                        if current == commit:
                            committed = True
                        elif current != expected_head:
                            try:
                                self.run("merge-base", "--is-ancestor", commit, current)
                                committed = True
                            except CaptureError:
                                pass
                    except CaptureError:
                        committed = True
                if committed:
                    raise CaptureError("transaction_recovery_required", "条件事务提交结果需恢复核对；请先执行 recover") from error
                if journal_written and self._journal_path().exists():
                    self._journal_path().unlink()
                raise
        return commit
