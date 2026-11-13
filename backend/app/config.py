"""运行配置：端口、跨域、运行环境、SQLite 与制品持久卷位置。

所有路径都可以用环境变量覆盖，本地开发默认落在 ``backend/data``，
容器部署时通常把它们指到命名卷（例如 ``/data``）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]


def _first_env(*names: str) -> str | None:
    """按顺序读取第一个非空环境变量，兼容不同部署习惯的变量名。"""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _resolve_path(value: str | None, default: Path) -> Path:
    path = Path(value).expanduser() if value else default
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


def _default_data_dir() -> Path:
    return _resolve_path(
        _first_env("APP_DATA_DIR", "REPORTGEN_DATA_DIR", "DATA_DIR"),
        BASE_DIR / "data",
    )


@dataclass(frozen=True)
class Settings:
    app_name: str = "数据洞察分析工作台"
    env: str = field(default_factory=lambda: os.environ.get("APP_ENV", "local"))
    port: int = field(default_factory=lambda: int(os.environ.get("APP_PORT", "8000")))
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200

    # 制品与元数据的持久化位置。SQLite 只存模板/元数据，制品内容落在持久卷。
    data_dir: Path = field(default_factory=_default_data_dir)
    sqlite_path: Path = field(
        default_factory=lambda: _resolve_path(
            _first_env(
                "SQLITE_PATH",
                "APP_SQLITE_PATH",
                "APP_DB_PATH",
                "REPORTGEN_DB_PATH",
                "SQLITE_DB_PATH",
            ),
            _default_data_dir() / "app.sqlite3",
        )
    )
    artifact_root: Path = field(
        default_factory=lambda: _resolve_path(
            _first_env(
                "ARTIFACT_STORAGE_DIR",
                "ARTIFACT_ROOT",
                "REPORTGEN_ARTIFACT_DIR",
                "REPORT_ARTIFACT_DIR",
                "ARTIFACT_DIR",
                "ARTIFACT_VOLUME",
            ),
            _default_data_dir() / "artifacts",
        )
    )
    # 失败/损坏文件不暴露下载，只在隔离区保留排查线索。
    artifact_quarantine_dir: Path | None = None
    # 单个制品上限，避免异常模板或请求把持久卷写满。
    artifact_max_bytes: int = field(
        default_factory=lambda: int(os.environ.get("ARTIFACT_MAX_BYTES", str(8 * 1024 * 1024)))
    )
    # 重复确认请求的请求号长度上限。
    idempotency_key_max_length: int = 200
    # 部署可在启动探针期间先不动已确认制品；默认启动即收敛中断写入。
    reconcile_on_startup: bool = field(
        default_factory=lambda: os.environ.get("RECONCILE_ON_STARTUP", "1").strip().lower()
        not in {"0", "false", "no", "off"}
    )

    def __post_init__(self) -> None:
        if self.artifact_quarantine_dir is None:
            object.__setattr__(
                self,
                "artifact_quarantine_dir",
                self.artifact_root / "_quarantine",
            )


def load_settings() -> Settings:
    return Settings()


settings = load_settings()
