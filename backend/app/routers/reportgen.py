"""报告生成接口：维护报告模板，覆盖提交审核、生效发布、停用模板等动作。

模板接口保持原有路径与返回结构；制品接口单独挂在 ``/api/reportgen/artifacts`` 下，
下载地址只以制品身份寻址。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from app.schemas import (
    ActionResult,
    ArtifactClaimPayload,
    ArtifactGeneratePayload,
    EntryPayload,
    PageResult,
)
from app.services import artifacts
from app.services.reportgen import ReportgenConflict, ReportgenService

router = APIRouter(prefix="/api/reportgen", tags=["报告生成"])

service = ReportgenService()

LIST_FIELDS = ["模板编号", "模板名称", "包含图表", "报告周期", "导出格式", "推送渠道", "水印开关", "附件压缩"]
STATUSES = ["草稿", "待审核", "已生效", "已停用"]


def _artifact_error(exc: artifacts.ArtifactError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


# 静态路径必须先于 /{entry_id} 注册，否则会被当成整数模板编号解析。
@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出报告生成清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "reportgen", "total": total, "items": items}


@router.get("/artifacts", response_model=PageResult[dict])
def list_artifacts(
    template_id: int | None = Query(default=None, description="按模板编号 id 过滤"),
    state: str | None = Query(default=None, description="pending/staged/ready/failed/missing/corrupt"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """列出制品元数据；缺文件或校验失败的制品会明确标记为不可投递。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_artifacts(
        template_id=template_id, state=state, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/artifacts/reconcile", response_model=dict)
def reconcile_artifacts() -> dict[str, Any]:
    """手动触发恢复判定：收敛中断的写入并隔离无元数据引用的孤立文件。"""
    return artifacts.reconcile_all()


@router.get("/artifacts/{artifact_id}", response_model=dict)
def get_artifact(artifact_id: str) -> dict:
    try:
        return service.get_artifact(artifact_id)
    except artifacts.ArtifactError as exc:
        raise _artifact_error(exc) from exc


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str) -> FileResponse:
    """下载已确认且校验通过的制品；缺文件或状态不对时拒绝而不是给半成品。"""
    try:
        row, path = artifacts.require_available(artifact_id)
    except artifacts.ArtifactError as exc:
        raise _artifact_error(exc) from exc
    return FileResponse(
        path,
        media_type=str(row["content_type"]),
        filename=str(row["filename"]),
    )


@router.post("/artifacts/{artifact_id}/claim", response_model=dict)
def claim_artifact(artifact_id: str, payload: ArtifactClaimPayload | None = None) -> dict:
    """领取制品：重启后仍按制品身份领取，领取本身不会让制品失效。"""
    payload = payload or ArtifactClaimPayload()
    try:
        return service.claim_artifact(
            artifact_id,
            claimer=payload.claimer,
            channel=payload.channel,
            idempotency_key=payload.idempotency_key,
        )
    except artifacts.ArtifactError as exc:
        raise _artifact_error(exc) from exc


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、待审核、已生效、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤报告生成列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条报告模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"报告模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条报告模板，缺字段时说明原因而不是静默丢弃。"""
    try:
        entry, missing = service.create_entry(payload.values)
    except ReportgenConflict as exc:
        return ActionResult(ok=False, message=str(exc))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="报告模板已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条报告模板执行提交审核、生效发布、停用模板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/artifacts", response_model=dict, status_code=202)
def generate_artifact(entry_id: int, payload: ArtifactGeneratePayload) -> dict:
    """为模板生成并确认一个制品。

    必须携带确认请求号 ``idempotency_key``；同一模板 + 同一请求号只受理一次，
    重复提交返回第一次的结果（``replayed`` 为 true），不会重复落盘。
    """
    key = payload.idempotency_key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="生成制品必须携带确认请求号 idempotency_key")
    try:
        artifact, replayed = service.generate(
            entry_id, idempotency_key=key, format=payload.format
        )
    except artifacts.ArtifactError as exc:
        raise _artifact_error(exc) from exc
    artifact["replayed"] = replayed
    return artifact
