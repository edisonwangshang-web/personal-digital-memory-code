"""原文采集：稳定身份、不可变版本、两次提交及可恢复保存回执。"""

from pathlib import Path

from .contracts import CaptureError, SCHEMA_VERSION, markdown, parse_markdown, source_contract, timestamp
from .git_adapter import GitAdapter


def capture(repo: Path, payload: dict, expected_head: str | None = None, *, remote: str | None = None) -> dict:
    """仅采集调用者明确提交的内容。remote=None 不宣称远程保存。

    body 原样保留；调用者负责保留说话者、消息顺序并先去除密钥。
    同 source_key 和实质内容复用快照；改变内容生成新版本。
    本地保存后的远程失败作为 remote_error 返回，可重新调用续传。
    """
    data, source_id, version = source_contract(payload)
    git = GitAdapter(repo)
    with git.lock():
        head = git.check(expected_head)
        folder = "conversations" if data["source_type"] == "conversation" else "inbox"
        relative = f"memory/sources/{folder}/{source_id}/{version}.md"
        receipt_path = f"memory/runs/captures/{source_id}/{version}.md"
        # A deletion control also covers new versions of the same stable source.
        # Decide before opening a previously saved body or creating a new one.
        from .controls import read_controls, source_policy, source_id_policy
        controls = read_controls(repo)
        if (source_id_policy(controls, source_id).get("reason") == "delete"
                or source_policy(controls, relative).get("reason") == "delete"):
            raise CaptureError("source_deleted", "来源已受普通删除控制，采集不能自动恢复")
        target, receipt_target = git.path(relative), git.path(receipt_path)
        reused = target.exists()
        if not reused and (receipt_target.exists() or git.source_commit(relative)):
            raise CaptureError("source_missing", "已有来源版本已被移除；请核对删除原因，采集不能自动重建")
        if reused:
            saved = target.read_bytes()
            metadata, body = parse_markdown(saved)
            original = {key: metadata.get(key) for key in data if key != "body"}
            original["body"] = body
            if (original != data or metadata.get("source_id") != source_id
                    or metadata.get("source_version") != version or metadata.get("schema_version") != SCHEMA_VERSION):
                raise CaptureError("source_mismatch", "已有来源快照与本次内容不符")
            source_commit = git.source_commit(relative)
            if git.read(source_commit, relative) != saved:
                raise CaptureError("source_mismatch", "来源快照未在已知提交中完整保存")
        else:
            metadata = {key: value for key, value in data.items() if key != "body"}
            metadata.update(schema_version=SCHEMA_VERSION, source_id=source_id, source_version=version, captured_at=timestamp())
            saved = markdown(metadata, data["body"])
            source_commit = git.write_commit(relative, saved, head)
            head = source_commit
        if receipt_target.exists():
            receipt_data = receipt_target.read_bytes()
            receipt, _ = parse_markdown(receipt_data)
            if any(receipt.get(k) != v for k, v in {
                "source_id": source_id, "source_version": version, "path": relative,
                "commit_sha": source_commit, "save_result": "local_saved", "schema_version": SCHEMA_VERSION,
            }.items()):
                raise CaptureError("receipt_mismatch", "来源回执与已保存快照不符")
        else:
            receipt_data = markdown({
                "schema_version": SCHEMA_VERSION, "source_id": source_id,
                "source_version": version, "commit_sha": source_commit, "path": relative,
                "save_result": "local_saved", "saved_at": timestamp(),
            })
            head = git.write_commit(receipt_path, receipt_data, head)
        result = {
            "source_id": source_id, "source_version": version, "path": relative,
            "commit_sha": source_commit, "receipt_path": receipt_path, "head": head,
            "local_saved": True, "reused": reused, "remote_status": "not_requested",
            "knowledge_status": "unprocessed",
        }
        if remote is not None:
            try:
                git.sync(remote, {relative: saved, receipt_path: receipt_data}, head)
                result["remote_status"] = "confirmed"
            except CaptureError as error:
                result.update(remote_status="unconfirmed", remote_error=error.code)
        return result
