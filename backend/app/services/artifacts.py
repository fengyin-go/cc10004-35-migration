"""报告制品存储：文件落持久卷，元数据落 SQLite。

提交顺序（每一步都可由重启后的 reconcile 判定）：

1. 先算出正文与摘要，在 SQLite 里以 ``pending`` 状态预留制品身份和幂等请求号；
2. 把正文写到 ``tmp/<artifact_id>.part`` 并 fsync；
3. 元数据推进到 ``staged``（记录 sha256/size）；
4. 以 ``os.replace`` 原子改名进 ``blobs/<artifact_id>/<sha256>.<ext>``；
5. 重新校验文件后元数据推进到 ``ready``，此时才可下载/领取/投递。

恢复判定：

* ``pending``/``staged`` 的临时文件按元数据里的 sha256/size 校验，
  匹配则补完改名与 ready，不匹配则删除并回滚为 ``failed``；
* 已经改名但元数据还没到 ready 时，用最终文件补齐 ready；
* ``ready`` 但文件缺失 -> ``missing``，文件摘要不符 -> 移入隔离区并转 ``corrupt``；
* 任何没有元数据引用的文件都只进隔离区，绝不会凭空变成可投递制品。

路径完全由服务内部生成：artifact_id 是 UUID、最终路径由 sha256 派生；
模板名称、模板编号和用户输入都不参与路径拼接。
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app import db
from app.config import settings

try:  # Linux 容器/本地部署的标准能力；其他平台退化为进程内串行。
    import fcntl
except ImportError:  # pragma: no cover - Windows 开发机
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

ARTIFACT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
FORMAT_ALIASES = {
    "json": "json",
    "md": "md",
    "markdown": "md",
    "html": "html",
    "htm": "html",
    "txt": "txt",
    "text": "txt",
}
CONTENT_TYPES = {
    "json": "application/json; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}
EXTENSIONS = {"json": "json", "md": "md", "html": "html", "txt": "txt"}

STATE_PENDING = "pending"
STATE_STAGED = "staged"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_MISSING = "missing"
STATE_CORRUPT = "corrupt"

DELIVERABLE_STATES = (STATE_READY,)

_reconcile_lock = threading.RLock()
# 同一进程内的生成/重放串行化：保证重放检查不会读到另一个线程写了一半的中间状态。
_generate_lock = threading.RLock()


def _storage_lock_path() -> Path:
    return _root() / ".storage.lock"


@contextmanager
def _cross_process_lock(*, blocking: bool = True, shared: bool = False) -> Iterator[bool]:
    """制品卷上的跨进程锁：多 worker 或滚动重启时协调写入与恢复判定。

    SQLite 的元数据操作本身有事务隔离；这个锁保护“文件系统 + 元数据”的复合
    判定：提交写独占，恢复判定也独占，两者不会交错；领取/读取用共享锁，
    避免读到一个正在被协调的中间状态。
    """
    _ensure_dirs()
    if fcntl is None:  # pragma: no cover
        with _reconcile_lock:
            yield True
        return
    path = _storage_lock_path()
    handle = path.open("a+")
    try:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        if not blocking:
            mode |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), mode)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class ArtifactError(Exception):
    """制品操作的可读业务错误。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ArtifactUnavailable(ArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409)


class ArtifactNotFound(ArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=404)


class ArtifactConflict(ArtifactError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409)


def _root() -> Path:
    return Path(settings.artifact_root)


def _tmp_dir() -> Path:
    return _root() / "tmp"


def _blob_dir() -> Path:
    return _root() / "blobs"


def _quarantine_root() -> Path:
    return Path(settings.artifact_quarantine_dir or (_root() / "_quarantine"))


def _ensure_dirs() -> None:
    _root().mkdir(parents=True, exist_ok=True)
    _tmp_dir().mkdir(parents=True, exist_ok=True)
    _blob_dir().mkdir(parents=True, exist_ok=True)
    _quarantine_root().mkdir(parents=True, exist_ok=True)


def _validate_artifact_id(artifact_id: str) -> str:
    value = str(artifact_id or "").strip()
    if not ARTIFACT_ID_RE.match(value):
        raise ArtifactNotFound("制品编号格式不合法")
    return value


def _safe_relative_path(relpath: str | None) -> Path | None:
    """只接受位于制品根目录内的相对路径，拒绝绝对路径与 .. 逃逸。"""
    if not relpath:
        return None
    if os.path.isabs(relpath):
        raise ArtifactError("制品路径必须使用相对路径")
    root = _root().resolve()
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ArtifactError("制品路径越出了持久卷范围") from exc
    return candidate


def _relative(path: Path) -> str:
    return str(path.resolve().relative_to(_root().resolve()))


def _temp_path(artifact_id: str) -> Path:
    return _tmp_dir() / f"{_validate_artifact_id(artifact_id)}.part"


def _temp_relpath(artifact_id: str) -> str:
    return f"tmp/{_validate_artifact_id(artifact_id)}.part"


def _final_path(artifact_id: str, sha256: str, fmt: str) -> Path:
    return _blob_dir() / _validate_artifact_id(artifact_id) / f"{sha256}.{EXTENSIONS[fmt]}"


def _final_relpath(artifact_id: str, sha256: str, fmt: str) -> str:
    return f"blobs/{_validate_artifact_id(artifact_id)}/{sha256}.{EXTENSIONS[fmt]}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _verify_file(path: Path, sha256: str | None, size_bytes: int | None) -> bool:
    if not path.is_file():
        return False
    actual_sha, actual_size = _file_digest(path)
    if sha256 and actual_sha != sha256:
        return False
    if size_bytes is not None and actual_size != int(size_bytes):
        return False
    return True


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        # 同一制品身份只允许一个写入者；落盘前先清理遗留的半成品。
        path.unlink(missing_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    _fsync_dir(path.parent)


def _quarantine(path: Path, reason: str) -> Path | None:
    if not path.exists():
        return None
    root = _quarantine_root()
    root.mkdir(parents=True, exist_ok=True)
    unique = uuid.uuid4().hex[:10]
    target = root / f"{path.name}.{unique}.quarantine"
    shutil.move(str(path), str(target))
    marker = target.with_suffix(target.suffix + ".json")
    try:
        marker.write_text(
            json.dumps(
                {"original": str(path), "reason": reason, "quarantined_at": db.utc_now()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("隔离文件说明写入失败：%s", target)
    return target


def _normalize_format(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    fmt = FORMAT_ALIASES.get(candidate)
    if fmt is None:
        # 模板里的“导出格式”是业务描述时，不擅自猜格式，默认 JSON。
        fmt = "json"
    return fmt


def _request_fingerprint(template: dict[str, Any], idempotency_key: str, fmt: str) -> str:
    material = json.dumps(
        {
            "template_id": int(template.get("id", 0)),
            "template_code": str(template.get("模板编号") or ""),
            "template_name": str(template.get("模板名称") or ""),
            "template_payload": {
                key: value
                for key, value in template.items()
                if key not in {"id", "status", "pending", "abnormal"}
            },
            "idempotency_key": idempotency_key,
            "format": fmt,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(material.encode("utf-8"))


def _render_report(template: dict[str, Any], artifact_id: str, fmt: str, generated_at: str) -> bytes:
    fields = {
        key: value
        for key, value in template.items()
        if key not in {"id", "pending", "abnormal"}
    }
    if fmt == "json":
        document = {
            "artifact_id": artifact_id,
            "format": "json",
            "generated_at": generated_at,
            "template": fields,
            "note": "该制品由报告生成模块在本地确认并校验后落盘。",
        }
        return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

    title = str(template.get("模板名称") or template.get("模板编号") or "分析报告")
    if fmt == "html":
        rows = "".join(
            f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
            for key, value in fields.items()
        )
        body = (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title></head><body>"
            f"<h1>{html.escape(title)}</h1>"
            f"<p>制品编号：{html.escape(artifact_id)}；生成时间：{html.escape(generated_at)}</p>"
            f"<table>{rows}</table></body></html>"
        )
        return body.encode("utf-8")

    if fmt == "md":
        lines = [
            f"# {title}",
            "",
            f"- 制品编号：{artifact_id}",
            f"- 生成时间：{generated_at}",
            "",
            "| 字段 | 值 |",
            "| --- | --- |",
        ]
        for key, value in fields.items():
            lines.append(f"| {str(key).replace('|', '/')} | {str(value).replace('|', '/')} |")
        return ("\n".join(lines) + "\n").encode("utf-8")

    lines = [f"报告：{title}", f"制品编号：{artifact_id}", f"生成时间：{generated_at}", ""]
    for key, value in fields.items():
        lines.append(f"{key}：{value}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _artifact_payload(row: Any, *, available: bool | None = None) -> dict[str, Any]:
    state = str(row["state"])
    is_available = state in DELIVERABLE_STATES if available is None else available
    artifact_id = str(row["artifact_id"])
    return {
        "artifact_id": artifact_id,
        "template_id": int(row["template_id"]),
        "template_code": str(row["template_code"]),
        "state": state,
        "available": bool(is_available),
        "format": str(row["format"]),
        "content_type": str(row["content_type"]),
        "filename": str(row["filename"]),
        "sha256": row["sha256"],
        "size_bytes": int(row["size_bytes"]) if row["size_bytes"] is not None else None,
        "created_at": row["created_at"],
        "ready_at": row["ready_at"],
        "confirmed_at": row["confirmed_at"],
        "verified_at": row["verified_at"],
        "error": row["error"],
        "idempotency_key": row["idempotency_key"],
        # 下载地址只认制品身份，不拼模板名称或模板编号。
        "download_url": f"/api/reportgen/artifacts/{artifact_id}/download" if is_available else None,
        "claim_url": f"/api/reportgen/artifacts/{artifact_id}/claim" if is_available else None,
    }


def list_artifacts(
    *,
    template_id: int | None = None,
    state: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    rows, total = db.list_artifact_rows(
        template_id=template_id, state=state, page=page, size=size
    )
    # 只读请求先拿共享锁；发现需要就地降级时，收集起来在独占锁下写元数据。
    needs_downgrade: list[Any] = []
    with _cross_process_lock(shared=True):
        for row in rows:
            if row["state"] == STATE_READY and not _row_file_ok(row):
                needs_downgrade.append(row)
    if needs_downgrade:
        with _cross_process_lock():
            for row in needs_downgrade:
                current = db.get_artifact_row(str(row["artifact_id"]))
                if current is not None and current["state"] == STATE_READY:
                    reconcile_row(current)
        rows = [db.get_artifact_row(str(row["artifact_id"])) or row for row in rows]
    return [
        _artifact_payload(
            row, available=row["state"] in DELIVERABLE_STATES and _row_file_ok(row)
        )
        for row in rows
    ], total


def get_artifact(artifact_id: str) -> dict[str, Any]:
    value = _validate_artifact_id(artifact_id)
    with _cross_process_lock(shared=True):
        row = db.get_artifact_row(value)
        if row is None:
            raise ArtifactNotFound(f"制品 {artifact_id} 不存在")
        needs_downgrade = (
            row["state"] in DELIVERABLE_STATES and not _row_file_ok(row)
        )
    if needs_downgrade:
        with _cross_process_lock():
            current = db.get_artifact_row(value)
            if current is not None and current["state"] in DELIVERABLE_STATES and not _row_file_ok(current):
                reconcile_row(current)
            row = db.get_artifact_row(value) or row
    return _artifact_payload(row, available=row["state"] in DELIVERABLE_STATES and _row_file_ok(row))


def _row_file_ok(row: Any) -> bool:
    if row is None or row["state"] != STATE_READY:
        return False
    path = _safe_relative_path(row["final_relpath"])
    return bool(path and _verify_file(path, row["sha256"], row["size_bytes"]))


def require_available(artifact_id: str) -> tuple[Any, Path]:
    """返回可用制品行和已验证的最终文件；缺文件时先把元数据降级再拒绝。"""
    value = _validate_artifact_id(artifact_id)
    with _cross_process_lock(shared=True):
        row = db.get_artifact_row(value)
        path = _safe_relative_path(row["final_relpath"]) if row is not None else None
        if row is not None and row["state"] in DELIVERABLE_STATES and path is not None:
            if _verify_file(path, row["sha256"], row["size_bytes"]):
                return row, path
        needs_downgrade = row is not None and row["state"] in DELIVERABLE_STATES
    if not needs_downgrade:
        _raise_unavailable(value, row)
    # 降级需要写元数据/移动文件，必须在独占锁下完成。
    with _cross_process_lock():
        row = db.get_artifact_row(value)
        path = _safe_relative_path(row["final_relpath"]) if row is not None else None
        if row is not None and row["state"] in DELIVERABLE_STATES and path is not None:
            if _verify_file(path, row["sha256"], row["size_bytes"]):
                return row, path
            # 明确降级，绝不把缺文件或摘要漂移的制品展示成可投递。
            state = STATE_MISSING if not path.exists() else STATE_CORRUPT
            if state == STATE_CORRUPT:
                _quarantine(path, "download verification failed")
                db.set_artifact_state(
                    value, state=state, error="制品文件摘要校验失败", clear_paths=True
                )
            else:
                db.set_artifact_state(value, state=state, error="制品文件缺失", clear_paths=False)
            row = db.get_artifact_row(value)
    _raise_unavailable(value, row)


def _raise_unavailable(value: str, row: Any) -> None:
    if row is None:
        raise ArtifactNotFound(f"制品 {value} 不存在")
    if row["state"] not in DELIVERABLE_STATES:
        raise ArtifactUnavailable(
            f"制品 {value} 当前状态为 {row['state']}，只有已确认且校验通过的制品才可投递"
        )
    raise ArtifactUnavailable(f"制品 {value} 的文件不可用（{row['state']}），已标记为不可投递")


def claim_artifact(
    artifact_id: str,
    *,
    claimer: str | None = None,
    channel: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """领取已确认制品；只记录领取回执，不改变制品可用性。"""
    row, path = require_available(artifact_id)
    key = (idempotency_key or "").strip() or None
    existing = db.find_artifact_claim(str(row["artifact_id"]), key) if key else None
    if existing is None:
        try:
            existing = db.insert_artifact_claim(
                artifact_id=str(row["artifact_id"]),
                idempotency_key=key,
                claimer=(claimer or "").strip() or None,
                channel=(channel or "").strip() or None,
            )
        except sqlite3.IntegrityError:
            # 并发重复领取命中唯一键：返回已经落库的那条回执。
            existing = db.find_artifact_claim(str(row["artifact_id"]), key)
            if existing is None:
                raise
    payload = _artifact_payload(row, available=True)
    payload.update(
        {
            "claim_id": int(existing["claim_id"]),
            "claimed_at": existing["created_at"],
            "claimer": existing["claimer"],
            "channel": existing["channel"],
            "file_verified": True,
            "file_sha256": row["sha256"],
            "file_size_bytes": row["size_bytes"],
        }
    )
    return payload


def _reserve(
    *,
    template: dict[str, Any],
    idempotency_key: str,
    fmt: str,
) -> tuple[Any, bytes, str, int] | dict[str, Any]:
    """预留制品身份。

    正常情况下返回元数据字典与正文；若同一请求号已被并发请求抢先落库，
    返回带 ``__replayed__`` 标记的字典，由调用方复用已有制品身份。
    """
    template_id = int(template.get("id", 0))
    artifact_id = uuid.uuid4().hex
    created_at = db.utc_now()
    content = _render_report(template, artifact_id, fmt, created_at)
    sha256 = _sha256(content)
    size_bytes = len(content)
    if size_bytes > settings.artifact_max_bytes:
        raise ArtifactError(
            f"制品大小 {size_bytes} 字节超过上限 {settings.artifact_max_bytes} 字节"
        )
    final_relpath = _final_relpath(artifact_id, sha256, fmt)
    record = {
        "artifact_id": artifact_id,
        "template_id": template_id,
        "template_code": str(template.get("模板编号") or ""),
        "idempotency_key": idempotency_key,
        "request_fingerprint": _request_fingerprint(template, idempotency_key, fmt),
        "state": STATE_PENDING,
        "format": fmt,
        "content_type": CONTENT_TYPES[fmt],
        "filename": f"artifact-{artifact_id}.{EXTENSIONS[fmt]}",
        "sha256": sha256,
        "size_bytes": size_bytes,
        "final_relpath": final_relpath,
        "temp_relpath": _temp_relpath(artifact_id),
        "error": None,
        "created_at": created_at,
        "staged_at": None,
        "ready_at": None,
        "confirmed_at": None,
        "verified_at": None,
        "updated_at": created_at,
    }
    try:
        db.insert_artifact_staged(record)
    except sqlite3.IntegrityError as exc:
        # 唯一约束是“同一模板 + 同一确认请求号只受理一次”的最终裁定点。
        existing = db.find_artifact_by_request(template_id, idempotency_key)
        if existing is None:
            raise ArtifactConflict(f"确认请求 {idempotency_key} 与已有制品冲突") from exc
        artifact, _replayed = _replay(existing, template, idempotency_key, fmt)
        return {"__replayed__": True, "artifact": artifact}
    return record, content, sha256, size_bytes


def generate_artifact(
    *,
    template: dict[str, Any] | None,
    idempotency_key: str,
    format: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """生成并确认一个制品。

    返回 (制品描述, 是否为幂等重放)。同一模板 + 同一确认请求号只受理一次：
    重放会返回第一次受理的制品状态，不会重新写文件。
    """
    if template is None:
        raise ArtifactNotFound("模板不存在或已归档")
    key = str(idempotency_key or "").strip()
    if not key:
        raise ArtifactError("生成制品必须携带确认请求号 idempotency_key")
    if len(key) > settings.idempotency_key_max_length:
        raise ArtifactError("确认请求号过长")
    fmt = _normalize_format(format)

    template_id = int(template.get("id", 0))
    _ensure_dirs()
    # 生成、重放与恢复判定在同一进程内串行；跨进程再叠加文件锁。
    # 这样重放请求永远不会读到另一个写入者留下的中间态，也不会把正在写的
    # 临时文件误判为半成品。
    with _generate_lock, _cross_process_lock():
        existing = db.find_artifact_by_request(template_id, key)
        if existing is not None:
            return _replay(existing, template, key, fmt)

        reserved = _reserve(template=template, idempotency_key=key, fmt=fmt)
        if isinstance(reserved, dict):
            # 极端情况下唯一键冲突已被处理：直接复用已有制品身份。
            return reserved["artifact"], True
        record, content, sha256, size_bytes = reserved
        artifact_id = record["artifact_id"]

        try:
            _write_temp(_temp_path(artifact_id), content)
            db.update_artifact_staged(
                artifact_id,
                sha256=sha256,
                size_bytes=size_bytes,
                temp_relpath=record["temp_relpath"],
                final_relpath=record["final_relpath"],
            )
            _commit_file(record["temp_relpath"], record["final_relpath"])
            final_path = _safe_relative_path(record["final_relpath"])
            if not final_path or not _verify_file(final_path, sha256, size_bytes):
                # 落盘后立刻复核：校验不通过就隔离并回滚，绝不标记 ready。
                if final_path is not None:
                    _quarantine(final_path, "post-commit verification failed")
                raise ArtifactError("制品落盘后校验失败，已转入隔离区")
            db.promote_artifact_ready(
                artifact_id,
                sha256=sha256,
                size_bytes=size_bytes,
                final_relpath=record["final_relpath"],
            )
        except Exception as exc:
            _rollback(artifact_id, exc)
            raise ArtifactError(f"制品生成失败，已回滚：{exc}", status_code=500) from exc

        row = db.get_artifact_row(artifact_id)
        available = _row_file_ok(row)
        return _artifact_payload(row, available=available), False


def _replay(existing: Any, template: dict[str, Any], key: str, fmt: str) -> tuple[dict[str, Any], bool]:
    expected = _request_fingerprint(template, key, fmt)
    if str(existing["request_fingerprint"]) != expected:
        raise ArtifactConflict(
            f"确认请求 {key} 已用于其他模板内容，不能重复使用同一请求号"
        )
    # 重放请求先把第一次受理留下的中间状态收敛，再返回同一个制品身份。
    reconcile_row(existing, quarantine_unknown=False)
    row = db.get_artifact_row(str(existing["artifact_id"])) or existing
    payload = _artifact_payload(row, available=_row_file_ok(row))
    payload["replayed"] = True
    if row["state"] not in DELIVERABLE_STATES:
        raise ArtifactUnavailable(
            f"确认请求 {key} 已受理过，但制品当前状态为 {row['state']}；请使用新的请求号重试"
        )
    return payload, True


def _commit_file(temp_relpath: str, final_relpath: str) -> None:
    temp_path = _safe_relative_path(temp_relpath)
    final_path = _safe_relative_path(final_relpath)
    if temp_path is None or final_path is None:
        raise ArtifactError("制品路径缺失")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp_path, final_path)
    _fsync_dir(final_path.parent)
    _fsync_dir(temp_path.parent)


def _rollback(artifact_id: str, error: Exception) -> None:
    temp_path = _temp_path(artifact_id)
    if temp_path.exists():
        temp_path.unlink(missing_ok=True)
    db.set_artifact_state(
        artifact_id, state=STATE_FAILED, error=f"{type(error).__name__}: {error}"[:500]
    )


def reconcile_row(row: Any, *, quarantine_unknown: bool = True) -> str:
    """把单条制品收敛到确定状态；返回收敛后的状态。"""
    artifact_id = str(row["artifact_id"])
    state = str(row["state"])
    expected_sha = row["sha256"]
    expected_size = row["size_bytes"]
    temp_path = _safe_relative_path(row["temp_relpath"]) if row["temp_relpath"] else None
    final_path = _safe_relative_path(row["final_relpath"]) if row["final_relpath"] else None

    if state in (STATE_PENDING, STATE_STAGED):
        if temp_path is not None and temp_path.exists():
            if _verify_file(temp_path, expected_sha, expected_size):
                try:
                    _commit_file(row["temp_relpath"], row["final_relpath"])
                except Exception as exc:
                    _rollback(artifact_id, exc)
                    return STATE_FAILED
                db.promote_artifact_ready(
                    artifact_id,
                    sha256=str(expected_sha),
                    size_bytes=int(expected_size or 0),
                    final_relpath=str(row["final_relpath"]),
                )
                return STATE_READY
            # 半成品无法证明完整：删除并回滚，绝不改名发布。
            temp_path.unlink(missing_ok=True)
            db.set_artifact_state(
                artifact_id,
                state=STATE_FAILED,
                error="写入中断或摘要不一致，已回滚",
                clear_paths=False,
            )
            return STATE_FAILED

        if final_path is not None and final_path.exists() and _verify_file(
            final_path, expected_sha, expected_size
        ):
            db.promote_artifact_ready(
                artifact_id,
                sha256=str(expected_sha),
                size_bytes=int(expected_size or 0),
                final_relpath=str(row["final_relpath"]),
            )
            return STATE_READY

        if final_path is not None and final_path.exists() and quarantine_unknown:
            _quarantine(final_path, "staged file does not match metadata")
            db.set_artifact_state(
                artifact_id,
                state=STATE_CORRUPT,
                error="最终文件与元数据不一致",
                clear_paths=True,
            )
            return STATE_CORRUPT

        db.set_artifact_state(
            artifact_id,
            state=STATE_FAILED,
            error="文件与元数据均缺失，已回滚",
            clear_paths=False,
        )
        return STATE_FAILED

    if state == STATE_READY:
        if final_path is not None and _verify_file(final_path, expected_sha, expected_size):
            return STATE_READY
        if final_path is None or not final_path.exists():
            db.set_artifact_state(
                artifact_id,
                state=STATE_MISSING,
                error="最终文件缺失",
                clear_paths=False,
            )
            return STATE_MISSING
        if quarantine_unknown:
            _quarantine(final_path, "ready file checksum mismatch")
        db.set_artifact_state(
            artifact_id,
            state=STATE_CORRUPT,
            error="最终文件摘要不符",
            clear_paths=True,
        )
        return STATE_CORRUPT

    if state == STATE_MISSING and final_path is not None:
        # 文件后来被恢复且摘要/大小都对得上：可以重新认领为 ready；
        # 没恢复或校验不符时保持 missing，仍然不可投递。
        if final_path.exists() and _verify_file(final_path, expected_sha, expected_size):
            db.promote_artifact_ready(
                artifact_id,
                sha256=str(expected_sha),
                size_bytes=int(expected_size or 0),
                final_relpath=str(row["final_relpath"]),
            )
            return STATE_READY
        return STATE_MISSING

    # failed/corrupt 不自动公开；只清理可以确定归属的临时半成品。
    if temp_path is not None and temp_path.exists():
        temp_path.unlink(missing_ok=True)
    return state


def _referenced_paths() -> set[str]:
    """被元数据引用的最终/临时文件；并发恢复时不能误删正在写的临时文件。"""
    referenced: set[str] = set()
    for row in db.all_artifact_rows():
        for relpath in (row["final_relpath"], row["temp_relpath"]):
            if not relpath:
                continue
            try:
                referenced.add(str(_safe_relative_path(str(relpath)).resolve()))
            except (OSError, ArtifactError):
                continue
    return referenced


def _sweep_orphans(summary: dict[str, Any]) -> None:
    """未登记的文件只做隔离，不生成元数据、不进入可投递集合。"""
    referenced = _referenced_paths()
    for base in (_blob_dir(), _tmp_dir()):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            resolved = str(path.resolve())
            if resolved in referenced:
                continue
            if path.suffix == ".part":
                # 没有任何元数据引用的临时文件才是写入中断的残留，直接清理。
                path.unlink(missing_ok=True)
                summary["temp_removed"].append(_relative(path))
                continue
            target = _quarantine(path, "orphan file without artifact metadata")
            if target is not None:
                summary["orphans_quarantined"].append(
                    {"path": _relative(path), "quarantine": str(target)}
                )


def reconcile_all(*, sweep_orphans: bool = True) -> dict[str, Any] | None:
    """启动与手动恢复入口：收敛所有制品状态并隔离孤立文件。

    拿不到跨进程锁时说明另一个 worker 正在协调，本次直接跳过，避免两边同时
    判定同一批中间状态。
    """
    with _cross_process_lock(blocking=False) as acquired:
        if not acquired:
            return None
        return _reconcile_all_locked(sweep_orphans=sweep_orphans)


def _reconcile_all_locked(*, sweep_orphans: bool = True) -> dict[str, Any]:
    with _reconcile_lock:
        _ensure_dirs()
        db.initialize_database()
        summary: dict[str, Any] = {
            "checked": 0,
            "recovered": [],
            "rolled_back": [],
            "missing": [],
            "corrupt": [],
            "orphans_quarantined": [],
            "temp_removed": [],
        }
        for row in db.all_artifact_rows():
            summary["checked"] += 1
            before = str(row["state"])
            after = reconcile_row(row)
            if after == STATE_READY and before in (STATE_PENDING, STATE_STAGED):
                summary["recovered"].append(str(row["artifact_id"]))
            elif after == STATE_FAILED and before in (STATE_PENDING, STATE_STAGED):
                summary["rolled_back"].append(str(row["artifact_id"]))
            elif after == STATE_MISSING:
                summary["missing"].append(str(row["artifact_id"]))
            elif after == STATE_CORRUPT:
                summary["corrupt"].append(str(row["artifact_id"]))
        if sweep_orphans:
            _sweep_orphans(summary)
        return summary


def storage_summary() -> dict[str, Any]:
    root = _root()
    return {
        "artifact_root": str(root),
        "artifact_root_exists": root.exists(),
        "sqlite_path": str(settings.sqlite_path),
        "states": db.artifact_state_counts(),
        "quarantine_dir": str(_quarantine_root()),
    }
