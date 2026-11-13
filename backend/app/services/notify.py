"""通知分发业务规则：规则状态流转、通知投递记录与按制品身份的下载地址。

投递记录必须引用已确认制品的 ``artifact_id``；模板名称/模板编号只作为可读文案，
不能作为下载地址。跨模块调用统一走 ``deliver_artifact``。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app import db
from app.services import artifacts
from app.store import store

MODULE = "notify"
REQUIRED_FIELDS = ["规则编号", "触发条件", "通知渠道"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用通知": "已启用", "停用通知": "已停用"}
NEGATIVE_ACTIONS = ["停用通知"]


class NotifyService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"通知规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于通知分发可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"通知规则已{action}"

    # ---- 制品投递 ---------------------------------------------------------
    def deliver(
        self,
        *,
        artifact_id: str,
        rule_id: int | None = None,
        channel: str | None = None,
        recipients: str | None = None,
        message: str | None = None,
        delivery_key: str | None = None,
    ) -> dict[str, Any]:
        """按制品身份投递。制品缺文件/未确认时拒绝，不生成可下载的半成品链接。"""
        row, _path = artifacts.require_available(artifact_id)
        sha256 = str(row["sha256"])
        key = (delivery_key or "").strip() or None
        if key is None:
            material = json.dumps(
                {
                    "artifact_id": str(row["artifact_id"]),
                    "artifact_sha256": sha256,
                    "rule_id": rule_id,
                    "channel": channel or "",
                    "recipients": recipients or "",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        existing = db.find_delivery_by_key(key)
        if existing is not None:
            payload = _delivery_payload(existing)
            payload["replayed"] = True
            return payload

        resolved_channel = (channel or "").strip() or _rule_channel(rule_id)
        resolved_message = (message or "").strip() or (
            f"报告制品 {row['artifact_id']} 已确认可用，请按制品身份领取"
        )
        now = db.utc_now()
        record = {
            "delivery_key": key,
            "rule_id": rule_id,
            "artifact_id": str(row["artifact_id"]),
            "artifact_sha256": sha256,
            "channel": resolved_channel,
            "recipients": recipients,
            "message": resolved_message,
            "status": "已投递",
            "download_url": f"/api/reportgen/artifacts/{row['artifact_id']}/download",
            "error": None,
            "created_at": now,
            "delivered_at": now,
            "updated_at": now,
        }
        try:
            delivery = db.insert_delivery(record)
        except Exception:
            # 并发下唯一键冲突时，返回已经落库的那一条。
            existing = db.find_delivery_by_key(key)
            if existing is None:
                raise
            payload = _delivery_payload(existing)
            payload["replayed"] = True
            return payload
        payload = _delivery_payload(delivery)
        payload["replayed"] = False
        return payload

    def list_deliveries(
        self,
        *,
        artifact_id: str | None = None,
        rule_id: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows, total = db.list_deliveries(
            artifact_id=artifact_id, rule_id=rule_id, page=page, size=size
        )
        return [_delivery_payload(row) for row in rows], total


def _rule_channel(rule_id: int | None) -> str:
    if rule_id is None:
        return "站内"
    rule = store.find(MODULE, rule_id)
    if rule is None:
        return "站内"
    return str(rule.get("通知渠道") or "站内")


def _delivery_payload(row: Any) -> dict[str, Any]:
    return {
        "delivery_id": int(row["delivery_id"]),
        "delivery_key": row["delivery_key"],
        "rule_id": int(row["rule_id"]) if row["rule_id"] is not None else None,
        "artifact_id": str(row["artifact_id"]),
        "artifact_sha256": str(row["artifact_sha256"]),
        "channel": str(row["channel"]),
        "recipients": row["recipients"],
        "message": row["message"],
        "status": str(row["status"]),
        "download_url": str(row["download_url"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "delivered_at": row["delivered_at"],
    }
