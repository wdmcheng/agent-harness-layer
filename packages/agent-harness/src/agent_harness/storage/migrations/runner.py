"""Programmatic Alembic migration helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Thread

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from agent_harness.storage.settings import (
    ensure_sqlite_parent,
    normalize_async_dsn,
    sqlite_database_path,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent


def alembic_config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", normalize_async_dsn(dsn))
    return cfg


def run_migrations(dsn: str, revision: str = "head") -> None:
    resolved = normalize_async_dsn(dsn)
    ensure_sqlite_parent(resolved)
    if _has_running_loop():
        _run_in_thread(lambda: command.upgrade(alembic_config(resolved), revision))
    else:
        command.upgrade(alembic_config(resolved), revision)


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _run_in_thread[T](callback: Callable[[], T]) -> T:
    result: list[T] = []
    errors: list[BaseException] = []

    def target() -> None:
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
    return asyncio.run(_current_revision(dsn))
