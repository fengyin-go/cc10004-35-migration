"""数据洞察分析工作台 后端服务入口。

启动：uvicorn app.main:app --host 127.0.0.1 --port 8000
健康检查：GET /api/health
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.config import settings
from app.routers import ROUTERS
from app.services import artifacts
from app.store import store

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动时先建库、迁移存量模板，再收敛上次中断的制品写入。

    恢复判定只依据元数据 + 文件摘要：能验证完整的补成 ready，验证不了的回滚为
    failed/corrupt，没有元数据的文件进隔离区。任何情况下都不会自动公开孤立文件。
    """
    created = db.initialize_database()
    summary = artifacts.reconcile_all() if settings.reconcile_on_startup else None
    if not settings.reconcile_on_startup:
        logger.info("已按配置跳过启动恢复判定（RECONCILE_ON_STARTUP=0）")
    elif summary is None:
        logger.info("制品恢复判定由另一个进程执行，本次启动跳过协调")
    else:
        logger.info(
            "制品存储就绪：新建数据库=%s，检查=%s，恢复=%s，回滚=%s，缺失=%s，损坏=%s，孤立隔离=%s",
            created,
            summary["checked"],
            len(summary["recovered"]),
            len(summary["rolled_back"]),
            len(summary["missing"]),
            len(summary["corrupt"]),
            len(summary["orphans_quarantined"]),
        )
    _app.state.storage_boot = {
        "database_created": created,
        "reconcile": summary,
    }
    yield


app = FastAPI(title="数据洞察分析工作台", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for module in ROUTERS:
    app.include_router(module.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """健康检查：确认服务已经监听、示例数据已经就绪、制品存储可读。"""
    return {
        "ok": True,
        "app": settings.app_name,
        "modules": len(store.module_names()),
        "storage": artifacts.storage_summary(),
    }


@app.get("/api/overview")
def overview() -> dict[str, object]:
    """运营概览：把各业务模块的待处理量汇总成看板卡片。"""
    return store.overview()


@app.get("/api/storage")
def storage_state() -> dict[str, object]:
    """存储与恢复状态：重启后排查用，不暴露任何文件系统路径之外的信息。"""
    return artifacts.storage_summary()
