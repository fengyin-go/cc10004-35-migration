"""SQLite 持久层：模板定义、制品元数据、领取记录与通知投递记录。

设计要点：

* SQLite 只保存结构化元数据，制品正文放在可配置的持久卷，两者分开备份/挂载。
* 建表与存量模板迁移都做成幂等操作；迁移只复制模板本身，不写入任何生成记录。
* 所有写操作显式事务，连接按操作创建，开启 WAL + FULL 同步，兼顾多线程和重启恢复。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings
from app.seed import SEED_ROWS

logger = logging.getLogger(__name__)

TEMPLATE_MODULE = "reportgen"
CORE_ROW_KEYS = frozenset({"id", "status", "pending", "abnormal"})
SEED_TEMPLATE_MARKER = "reportgen.seed_templates_migrated"

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS report_templates (
        id INTEGER PRIMARY KEY,
        template_code TEXT NOT NULL,
        name TEXT NOT NULL,
        charts TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        pending INTEGER NOT NULL DEFAULT 0,
        abnormal INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_report_templates_code ON report_templates (template_code)",
    """
    CREATE TABLE IF NOT EXISTS report_artifacts (
        artifact_id TEXT PRIMARY KEY,
        template_id INTEGER NOT NULL,
        template_code TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_fingerprint TEXT NOT NULL,
        state TEXT NOT NULL,
        format TEXT NOT NULL,
        content_type TEXT NOT NULL,
        filename TEXT NOT NULL,
        sha256 TEXT,
        size_bytes INTEGER,
        final_relpath TEXT,
        temp_relpath TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        staged_at TEXT,
        ready_at TEXT,
        confirmed_at TEXT,
        verified_at TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE (template_id, idempotency_key),
        FOREIGN KEY (template_id) REFERENCES report_templates (id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_report_artifacts_template ON report_artifacts (template_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_report_artifacts_state ON report_artifacts (state)",
    """
    CREATE TABLE IF NOT EXISTS report_artifact_claims (
        claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
        artifact_id TEXT NOT NULL,
        idempotency_key TEXT,
        claimer TEXT,
        channel TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (artifact_id, idempotency_key),
        FOREIGN KEY (artifact_id) REFERENCES report_artifacts (artifact_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notify_deliveries (
        delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
        delivery_key TEXT UNIQUE,
        rule_id INTEGER,
        artifact_id TEXT NOT NULL,
        artifact_sha256 TEXT NOT NULL,
        channel TEXT NOT NULL,
        recipients TEXT,
        message TEXT,
        status TEXT NOT NULL,
        download_url TEXT NOT NULL,
        error TEXT,
        created_at TEXT NOT NULL,
        delivered_at TEXT,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (artifact_id) REFERENCES report_artifacts (artifact_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_notify_deliveries_artifact ON notify_deliveries (artifact_id)",
)

_init_lock = threading.RLock()
_initialized_path: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database_path() -> Path:
    return Path(settings.sqlite_path)


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """一次操作一个连接，用完即关；sqlite3 的 ``with conn`` 只提交不关闭。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务；失败一定回滚，不把半截状态留给下一次启动。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def payload_from_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in row.items() if key not in CORE_ROW_KEYS}
    return payload


def initialize_database() -> bool:
    """建表 + 一次性迁移存量模板；返回本次是否新建了数据库文件。"""
    global _initialized_path
    path = database_path()
    with _init_lock:
        if _initialized_path == str(path) and path.exists():
            return False
        created_file = not path.exists()
        conn = connect()
        try:
            # 建表与迁移各自包在事务里：迁移失败不会留下半张表或半批模板。
            with transaction(conn):
                for statement in SCHEMA_STATEMENTS:
                    conn.execute(statement)
            migrated = _migrate_seed_templates(conn)
            if migrated:
                logger.info("存量报告模板迁移完成：%s 条（不生成任何制品历史）", migrated)
        finally:
            conn.close()
        _initialized_path = str(path)
        return created_file


def _migrate_seed_templates(conn: sqlite3.Connection) -> int:
    """把存量报告模板复制进 SQLite。

    保留原行 id 与模板编号，状态字段也照搬；制品表完全不写入，避免把没有真实生成过的
    模板伪装成已有生成历史。迁移标记写入后不会重复执行。
    """
    marker = conn.execute(
        "SELECT value FROM schema_meta WHERE key = ?", (SEED_TEMPLATE_MARKER,)
    ).fetchone()
    if marker is not None:
        return 0
    now = utc_now()
    migrated = 0
    with transaction(conn):
        marker = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (SEED_TEMPLATE_MARKER,)
        ).fetchone()
        if marker is not None:
            return 0
        for row in SEED_ROWS.get(TEMPLATE_MODULE, []):
            payload = payload_from_row(dict(row))
            entry_id = int(row.get("id", 0))
            if entry_id <= 0:
                continue
            template_code = str(payload.get("模板编号") or "").strip()
            if not template_code:
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO report_templates
                    (id, template_code, name, charts, payload_json,
                     status, pending, abnormal, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    template_code,
                    str(payload.get("模板名称") or ""),
                    str(payload.get("包含图表") or ""),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    str(row.get("status") or "草稿"),
                    int(bool(row.get("pending"))),
                    int(bool(row.get("abnormal"))),
                    now,
                    now,
                ),
            )
            migrated += cursor.rowcount
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value, updated_at) VALUES (?, ?, ?)",
            (
                SEED_TEMPLATE_MARKER,
                json.dumps(
                    {
                        "source": "app.seed.SEED_ROWS.reportgen",
                        "migrated_at": now,
                        "template_count": migrated,
                        "artifact_history": "none",
                    },
                    ensure_ascii=False,
                ),
                now,
            ),
        )
    return migrated


def seed_migration_state() -> dict[str, Any] | None:
    initialize_database()
    with session() as conn:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = ?", (SEED_TEMPLATE_MARKER,)
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return {"value": row["value"]}


def row_to_template(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    entry: dict[str, Any] = {
        "id": int(row["id"]),
        "status": row["status"],
        "pending": bool(row["pending"]),
        "abnormal": bool(row["abnormal"]),
    }
    for key, value in payload.items():
        entry.setdefault(key, value)
    return entry


def list_templates(
    *,
    keyword: str | None = None,
    status: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    initialize_database()
    clauses: list[str] = []
    params: list[Any] = []
    if keyword:
        clauses.append("instr(template_code, ?) > 0")
        params.append(keyword)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    start = max(page - 1, 0) * size
    with session() as conn:
        total = int(conn.execute(f"SELECT COUNT(*) FROM report_templates{where}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT * FROM report_templates{where} ORDER BY id ASC LIMIT ? OFFSET ?",
            [*params, size, start],
        ).fetchall()
    return [row_to_template(row) for row in rows], total


def get_template(entry_id: int) -> dict[str, Any] | None:
    initialize_database()
    with session() as conn:
        row = conn.execute("SELECT * FROM report_templates WHERE id = ?", (entry_id,)).fetchone()
    return row_to_template(row) if row else None


def list_template_ids() -> list[int]:
    initialize_database()
    with session() as conn:
        rows = conn.execute("SELECT id FROM report_templates ORDER BY id ASC").fetchall()
    return [int(row["id"]) for row in rows]


def create_template(
    payload: dict[str, Any],
    *,
    required_fields: list[str],
) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """登记模板；返回 (条目, 缺失字段, 错误说明)。

    编号是模板的稳定身份，重复编号不会被静默覆盖，也不会自动改写。
    """
    initialize_database()
    missing = [
        field
        for field in required_fields
        if not str(payload.get(field) or "").strip()
    ]
    if missing:
        return None, missing, None
    template_code = str(payload.get("模板编号") or "").strip()
    now = utc_now()
    with _init_lock, session() as conn:
        with transaction(conn):
            next_id = _next_template_id(conn)
            duplicate = conn.execute(
                "SELECT id FROM report_templates WHERE template_code = ?", (template_code,)
            ).fetchone()
            if duplicate is not None:
                return (None, [], f"模板编号 {template_code} 已存在，请使用新的编号")
            conn.execute(
                """
                INSERT INTO report_templates
                    (id, template_code, name, charts, payload_json,
                     status, pending, abnormal, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_id,
                    template_code,
                    str(payload.get("模板名称") or ""),
                    str(payload.get("包含图表") or ""),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "草稿",
                    1,
                    0,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM report_templates WHERE id = ?", (next_id,)).fetchone()
    return row_to_template(row), [], None


def _next_template_id(conn: sqlite3.Connection) -> int:
    """在 ``BEGIN IMMEDIATE`` 事务里分配模板 id，避免并发登记撞号。

    迁移进来的存量模板可能有 id 空洞（例如历史删除），这里只保证递增且不复用。
    """
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM report_templates").fetchone()[0])


def update_template_state(
    entry_id: int,
    *,
    status: str,
    pending: bool,
    abnormal: bool,
) -> dict[str, Any] | None:
    initialize_database()
    with session() as conn:
        with transaction(conn):
            cursor = conn.execute(
                """
                UPDATE report_templates
                   SET status = ?, pending = ?, abnormal = ?, updated_at = ?
                 WHERE id = ?
                """,
                (status, int(pending), int(abnormal), utc_now(), entry_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM report_templates WHERE id = ?", (entry_id,)).fetchone()
    return row_to_template(row)


def template_counts() -> dict[str, int]:
    initialize_database()
    with session() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM report_templates").fetchone()[0])
        active = int(
            conn.execute(
                "SELECT COUNT(*) FROM report_templates WHERE status = ?", ("已生效",)
            ).fetchone()[0]
        )
    return {"total": total, "active": active}


# ---------------------------------------------------------------------------
# 制品元数据
# ---------------------------------------------------------------------------
def insert_artifact_staged(record: dict[str, Any]) -> None:
    initialize_database()
    with session() as conn:
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO report_artifacts (
                    artifact_id, template_id, template_code, idempotency_key,
                    request_fingerprint, state, format, content_type, filename,
                    sha256, size_bytes, final_relpath, temp_relpath, error,
                    created_at, staged_at, ready_at, confirmed_at, verified_at,
                    updated_at
                ) VALUES (
                    :artifact_id, :template_id, :template_code, :idempotency_key,
                    :request_fingerprint, :state, :format, :content_type, :filename,
                    :sha256, :size_bytes, :final_relpath, :temp_relpath, :error,
                    :created_at, :staged_at, :ready_at, :confirmed_at, :verified_at,
                    :updated_at
                )
                """,
                record,
            )


def get_artifact_row(artifact_id: str) -> sqlite3.Row | None:
    initialize_database()
    with session() as conn:
        return conn.execute(
            "SELECT * FROM report_artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()


def find_artifact_by_request(template_id: int, idempotency_key: str) -> sqlite3.Row | None:
    initialize_database()
    with session() as conn:
        return conn.execute(
            """
            SELECT * FROM report_artifacts
             WHERE template_id = ? AND idempotency_key = ?
            """,
            (template_id, idempotency_key),
        ).fetchone()


def update_artifact_staged(
    artifact_id: str,
    *,
    sha256: str,
    size_bytes: int,
    temp_relpath: str,
    final_relpath: str,
) -> None:
    initialize_database()
    with session() as conn:
        with transaction(conn):
            conn.execute(
                """
                UPDATE report_artifacts
                   SET sha256 = ?, size_bytes = ?, temp_relpath = ?, final_relpath = ?,
                       state = 'staged', staged_at = ?, updated_at = ?
                 WHERE artifact_id = ? AND state IN ('pending', 'staged')
                """,
                (sha256, size_bytes, temp_relpath, final_relpath, utc_now(), utc_now(), artifact_id),
            )


def promote_artifact_ready(
    artifact_id: str,
    *,
    sha256: str,
    size_bytes: int,
    final_relpath: str,
) -> None:
    initialize_database()
    now = utc_now()
    with session() as conn:
        with transaction(conn):
            conn.execute(
                """
                UPDATE report_artifacts
                   SET state = 'ready', sha256 = ?, size_bytes = ?, final_relpath = ?,
                       temp_relpath = NULL, error = NULL, ready_at = ?,
                       confirmed_at = COALESCE(confirmed_at, ?), verified_at = ?,
                       updated_at = ?
                 WHERE artifact_id = ? AND state IN ('staged', 'pending', 'missing', 'corrupt')
                """,
                (sha256, size_bytes, final_relpath, now, now, now, now, artifact_id),
            )


def set_artifact_state(
    artifact_id: str,
    *,
    state: str,
    error: str | None = None,
    clear_paths: bool = False,
) -> None:
    initialize_database()
    now = utc_now()
    with session() as conn:
        with transaction(conn):
            if clear_paths:
                conn.execute(
                    """
                    UPDATE report_artifacts
                       SET state = ?, error = ?, temp_relpath = NULL,
                           final_relpath = NULL, updated_at = ?
                     WHERE artifact_id = ?
                    """,
                    (state, error, now, artifact_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE report_artifacts
                       SET state = ?, error = ?, updated_at = ?
                     WHERE artifact_id = ?
                    """,
                    (state, error, now, artifact_id),
                )


def list_artifact_rows(
    *,
    template_id: int | None = None,
    state: str | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[sqlite3.Row], int]:
    initialize_database()
    clauses: list[str] = []
    params: list[Any] = []
    if template_id is not None:
        clauses.append("template_id = ?")
        params.append(template_id)
    if state:
        clauses.append("state = ?")
        params.append(state)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    start = max(page - 1, 0) * size
    with session() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM report_artifacts{where}", params).fetchone()[0]
        )
        rows = conn.execute(
            f"""
            SELECT * FROM report_artifacts{where}
             ORDER BY created_at ASC, artifact_id ASC
             LIMIT ? OFFSET ?
            """,
            [*params, size, start],
        ).fetchall()
    return list(rows), total


def all_artifact_rows() -> list[sqlite3.Row]:
    initialize_database()
    with session() as conn:
        return list(conn.execute("SELECT * FROM report_artifacts ORDER BY created_at ASC").fetchall())


def artifact_state_counts() -> dict[str, int]:
    initialize_database()
    with session() as conn:
        rows = conn.execute(
            "SELECT state, COUNT(*) AS amount FROM report_artifacts GROUP BY state"
        ).fetchall()
    return {str(row["state"]): int(row["amount"]) for row in rows}


def artifact_claim_counts(artifact_id: str) -> int:
    initialize_database()
    with session() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM report_artifact_claims WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()[0]
        )


def find_artifact_claim(artifact_id: str, idempotency_key: str) -> sqlite3.Row | None:
    initialize_database()
    with session() as conn:
        return conn.execute(
            """
            SELECT * FROM report_artifact_claims
             WHERE artifact_id = ? AND idempotency_key = ?
            """,
            (artifact_id, idempotency_key),
        ).fetchone()


def insert_artifact_claim(
    *,
    artifact_id: str,
    idempotency_key: str | None,
    claimer: str | None,
    channel: str | None,
) -> sqlite3.Row:
    initialize_database()
    now = utc_now()
    with session() as conn:
        with transaction(conn):
            cursor = conn.execute(
                """
                INSERT INTO report_artifact_claims
                    (artifact_id, idempotency_key, claimer, channel, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (artifact_id, idempotency_key, claimer, channel, now),
            )
            row = conn.execute(
                "SELECT * FROM report_artifact_claims WHERE claim_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    return row


def list_artifact_claims(artifact_id: str) -> list[sqlite3.Row]:
    initialize_database()
    with session() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM report_artifact_claims
                 WHERE artifact_id = ? ORDER BY claim_id ASC
                """,
                (artifact_id,),
            ).fetchall()
        )


# ---------------------------------------------------------------------------
# 通知投递记录（投递内容引用制品身份，不引用模板名称）
# ---------------------------------------------------------------------------
def find_delivery_by_key(delivery_key: str) -> sqlite3.Row | None:
    initialize_database()
    with session() as conn:
        return conn.execute(
            "SELECT * FROM notify_deliveries WHERE delivery_key = ?", (delivery_key,)
        ).fetchone()


def insert_delivery(record: dict[str, Any]) -> sqlite3.Row:
    initialize_database()
    with session() as conn:
        with transaction(conn):
            cursor = conn.execute(
                """
                INSERT INTO notify_deliveries (
                    delivery_key, rule_id, artifact_id, artifact_sha256, channel,
                    recipients, message, status, download_url, error, created_at,
                    delivered_at, updated_at
                ) VALUES (
                    :delivery_key, :rule_id, :artifact_id, :artifact_sha256, :channel,
                    :recipients, :message, :status, :download_url, :error, :created_at,
                    :delivered_at, :updated_at
                )
                """,
                record,
            )
            row = conn.execute(
                "SELECT * FROM notify_deliveries WHERE delivery_id = ?", (cursor.lastrowid,)
            ).fetchone()
    return row


def list_deliveries(
    *,
    artifact_id: str | None = None,
    rule_id: int | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[list[sqlite3.Row], int]:
    initialize_database()
    clauses: list[str] = []
    params: list[Any] = []
    if artifact_id:
        clauses.append("artifact_id = ?")
        params.append(artifact_id)
    if rule_id is not None:
        clauses.append("rule_id = ?")
        params.append(rule_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    start = max(page - 1, 0) * size
    with session() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM notify_deliveries{where}", params).fetchone()[0]
        )
        rows = conn.execute(
            f"""
            SELECT * FROM notify_deliveries{where}
             ORDER BY delivery_id ASC LIMIT ? OFFSET ?
            """,
            [*params, size, start],
        ).fetchall()
    return list(rows), total
