"""报告生成业务规则：模板状态流转、字段校验、筛选口径与制品生成都收在这里。

模板定义与生成制品元数据在 SQLite，制品正文在持久卷；本服务只做业务判断，
落盘与提交顺序统一交给 ``app.services.artifacts``。
"""
from __future__ import annotations

from typing import Any

from app import db
from app.services import artifacts

MODULE = "reportgen"


class ReportgenConflict(Exception):
    """模板编号冲突等可读的业务错误。"""


REQUIRED_FIELDS = ["模板编号", "模板名称", "包含图表"]
STATUS_ORDER = ["草稿", "待审核", "已生效", "已停用"]
ACTION_RULES = {"提交审核": "待审核", "生效发布": "已生效", "停用模板": "已停用"}
NEGATIVE_ACTIONS = ["停用模板"]


class ReportgenService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        return db.list_templates(keyword=keyword, status=status, page=page, size=size)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return db.get_template(entry_id)

    def create_entry(
        self, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing, error = db.create_template(values, required_fields=REQUIRED_FIELDS)
        if error:
            raise ReportgenConflict(error)
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = db.get_template(entry_id)
        if entry is None:
            return None, f"报告模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于报告生成可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry = db.update_template_state(
            entry_id,
            status=target,
            pending=target != STATUS_ORDER[-1],
            abnormal=action in NEGATIVE_ACTIONS,
        )
        if entry is None:
            return None, f"报告模板 {entry_id} 不存在或已归档"
        return entry, f"报告模板已{action}"

    # ---- 制品 ------------------------------------------------------------
    def generate(
        self,
        entry_id: int,
        *,
        idempotency_key: str,
        format: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        template = db.get_template(entry_id)
        return artifacts.generate_artifact(
            template=template, idempotency_key=idempotency_key, format=format
        )

    def list_artifacts(
        self,
        *,
        template_id: int | None = None,
        state: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        return artifacts.list_artifacts(
            template_id=template_id, state=state, page=page, size=size
        )

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        return artifacts.get_artifact(artifact_id)

    def claim_artifact(
        self,
        artifact_id: str,
        *,
        claimer: str | None = None,
        channel: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return artifacts.claim_artifact(
            artifact_id,
            claimer=claimer,
            channel=channel,
            idempotency_key=idempotency_key,
        )
