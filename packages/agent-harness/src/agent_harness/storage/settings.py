"""Storage profile 辅助函数。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import make_url

from agent_harness.config.schemas import HarnessSettings


def normalize_async_dsn(dsn: str) -> str:
    """将同步 PostgreSQL/SQLite URL 规范为当前存储层使用的异步驱动 URL。"""

    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    if dsn.startswith("sqlite:///"):
        return dsn.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return dsn


def ensure_sqlite_parent(dsn: str) -> None:
    """为文件型 SQLite 数据库创建父目录；内存库和非 SQLite 连接不触碰文件系统。"""

    url = make_url(normalize_async_dsn(dsn))
    if not url.drivername.startswith("sqlite"):
        return
    database = url.database
    if database and database != ":memory:":
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def sqlite_database_path(dsn: str) -> Path | None:
    """解析可持久化 SQLite 文件路径；内存或其他方言返回 ``None``。"""

    url = make_url(normalize_async_dsn(dsn))
    if not url.drivername.startswith("sqlite"):
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser()


def storage_dsn_from_settings(settings: HarnessSettings) -> str:
    """按 profile 设置解析最终存储 DSN，并拒绝缺少 service PostgreSQL 配置的降级。"""

    if settings.storage.dsn:
        return normalize_async_dsn(settings.storage.dsn)
    if settings.storage.kind in {"sqlite", "filesystem"}:
        root = Path(settings.storage.root or ".agent-harness/local")
        return normalize_async_dsn(f"sqlite:///{root / 'agent_harness.db'}")
    if settings.storage.kind == "postgresql":
        raise ValueError("service profile storage.dsn is required for PostgreSQL")
    raise ValueError(f"unsupported storage kind: {settings.storage.kind}")
