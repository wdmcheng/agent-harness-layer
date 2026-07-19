"""程序化 Alembic 迁移运行入口，显式区分升级、读取版本与运行时门禁。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Thread

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from agent_harness.storage.settings import (
    ensure_sqlite_parent,
    normalize_async_dsn,
    sqlite_database_path,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent


class SchemaMigrationRequiredError(RuntimeError):
    """普通运行入口发现 schema 未到当前 head 时的稳定失败。"""

    code = "storage.migration_required"

    def __init__(self) -> None:
        """固定对外错误文本，使应用入口可提示显式迁移而不自动修改数据库。"""
        super().__init__("database schema requires explicit migration")


def alembic_config(dsn: str) -> Config:
    """为给定异步 DSN 构造仅指向本包迁移目录的 Alembic 配置。"""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", normalize_async_dsn(dsn))
    return cfg


def run_migrations(dsn: str, revision: str = "head") -> None:
    """显式升级数据库到指定 revision，并兼容在已运行事件循环中调用。

    迁移是有副作用的运维动作：函数只在调用方明确请求时执行。SQLite 父目录在
    此处预先创建，避免 Alembic 把缺失路径误报为与 schema 无关的连接失败。
    """
    resolved = normalize_async_dsn(dsn)
    ensure_sqlite_parent(resolved)
    if _has_running_loop():
        _run_in_thread(lambda: command.upgrade(alembic_config(resolved), revision))
    else:
        command.upgrade(alembic_config(resolved), revision)


def get_head_revision() -> str:
    """返回代码随附 migration 的唯一 head，不访问业务数据库。"""

    head = ScriptDirectory.from_config(
        alembic_config("sqlite+aiosqlite:///:memory:")
    ).get_current_head()
    if head is None:
        raise RuntimeError("migration head is unavailable")
    return head


def _has_running_loop() -> bool:
    """判断当前线程是否已有活动事件循环，以选择不会嵌套 asyncio.run 的执行路径。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run_in_thread[T](callback: Callable[[], T]) -> T:
    """在线程中运行阻塞 Alembic/asyncio 桥接，并将原始异常同步回调用线程。"""
    result: list[T] = []
    errors: list[BaseException] = []

    def target() -> None:
        """执行回调并捕获所有 BaseException，保证 KeyboardInterrupt 等不会丢失。"""
        try:
            result.append(callback())
        except BaseException as exc:  # noqa: BLE001 - cross-thread propagation
            errors.append(exc)

    thread = Thread(target=target)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


async def _current_revision(dsn: str) -> str | None:
    """只读查询数据库当前 Alembic revision；不存在或不可读时返回空值。

    读取失败与未迁移统一视为“不满足运行门禁”，由调用方映射为稳定的迁移要求，
    而不是在健康检查或应用启动中尝试修复数据库。
    """
    resolved = normalize_async_dsn(dsn)
    sqlite_path = sqlite_database_path(resolved)
    if sqlite_path is not None and not sqlite_path.exists():
        return None
    ensure_sqlite_parent(resolved)
    engine = create_async_engine(resolved)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("select version_num from alembic_version"))
            row = result.first()
            return None if row is None else str(row[0])
    except SQLAlchemyError:
        return None
    finally:
        await engine.dispose()


def get_current_revision(dsn: str) -> str | None:
    """同步包装当前 revision 查询，并在异步上下文中使用隔离线程避免嵌套循环。"""
    if _has_running_loop():
        return _run_in_thread(lambda: asyncio.run(_current_revision(dsn)))
    return asyncio.run(_current_revision(dsn))


def require_migration_head(dsn: str) -> str:
    """只校验 schema，不创建数据库、不自动执行 migration。"""

    expected = get_head_revision()
    current = get_current_revision(dsn)
    if current != expected:
        raise SchemaMigrationRequiredError
    return expected
