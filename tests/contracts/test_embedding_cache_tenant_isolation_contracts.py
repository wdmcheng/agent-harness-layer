"""Embedding cache tenant 隔离与 0012a 迁移合同。"""

from __future__ import annotations

import json
import sqlite3
from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

from agent_harness.embeddings import EmbeddingRequest, LocalEmbeddingProvider
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.migrations.runner import alembic_config
from agent_harness.storage.repositories import EmbeddingCacheCreate


def sqlite_dsn(path: Path) -> str:
    """生成隔离 SQLite 数据库的异步 DSN。"""

    return f"sqlite+aiosqlite:///{path}"


def migration_config(dsn: str, *, x_args: list[str] | None = None) -> Config:
    """构造可显式传递 Alembic ``-x`` 参数的测试配置。"""

    config = alembic_config(dsn)
    config.cmd_opts = Namespace(x=x_args or [])
    return config


def insert_legacy_cache_row(
    db_path: Path,
    *,
    row_id: str = "cache-legacy",
    tenant_id: str = "tenant-a",
    metadata: object,
) -> None:
    """在 0012 schema 写入旧合同允许的 cache evidence。"""

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "insert or ignore into tenants(id, display_name) values (?, ?)",
            (tenant_id, tenant_id),
        )
        connection.execute(
            """
            insert into embedding_cache(
                id, tenant_id, provider, model, input_hash, vector_ref,
                metadata_json, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                tenant_id,
                "local",
                "mock-small",
                "a" * 64,
                "embedding://legacy/ref",
                json.dumps(metadata),
                "2026-07-01T00:00:00+00:00",
                "2026-07-02T00:00:00+00:00",
            ),
        )


@pytest.mark.asyncio
async def test_embedding_cache_is_tenant_scoped_and_persists_outcomes(tmp_path: Path) -> None:
    """相同输入只能在同 tenant 内复用，命中不得伪造 provider latency。"""

    dsn = sqlite_dsn(tmp_path / "tenant-cache.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.tenants.ensure("tenant-b")
            provider = LocalEmbeddingProvider(cache=uow.embedding_cache)
            a_miss = await provider.embed(EmbeddingRequest(input="same", tenant_id="tenant-a"))
            a_hit = await provider.embed(EmbeddingRequest(input="same", tenant_id="tenant-a"))
            b_miss = await provider.embed(EmbeddingRequest(input="same", tenant_id="tenant-b"))
            await uow.commit()

        async with storage.uow() as uow:
            provider = LocalEmbeddingProvider(cache=uow.embedding_cache)
            b_hit = await provider.embed(EmbeddingRequest(input="same", tenant_id="tenant-b"))
            a_record = await uow.embedding_cache.get(
                tenant_id="tenant-a",
                provider="local",
                model="mock-small",
                input_hash=a_miss.cache.input_hash,
            )
            b_record = await uow.embedding_cache.get(
                tenant_id="tenant-b",
                provider="local",
                model="mock-small",
                input_hash=b_miss.cache.input_hash,
            )
            await uow.commit()
    finally:
        await storage.dispose()

    assert a_miss.cache.hit is False
    assert a_hit.cache.hit is True
    assert b_miss.cache.hit is False
    assert b_hit.cache.hit is True
    assert a_miss.vector_ref == a_hit.vector_ref
    assert b_miss.vector_ref == b_hit.vector_ref
    assert a_miss.vector_ref != b_miss.vector_ref
    assert a_record is not None
    assert b_record is not None
    assert a_record.id != b_record.id
    for record in (a_record, b_record):
        assert record.metadata["cache_status"] == "hit"
        assert record.metadata["vector_ref"] == record.vector_ref
        assert record.metadata["provider_latency_status"] == "recorded"
        assert isinstance(record.metadata["provider_latency_ms"], int | float)
        assert not isinstance(record.metadata["provider_latency_ms"], bool)
        assert record.metadata["provider_latency_ms"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {
            "cache_status": "miss",
            "vector_ref": "embedding://tenant/ref",
            "provider_latency_status": "unavailable",
            "provider_latency_ms": None,
        },
        {
            "cache_status": "miss",
            "vector_ref": "embedding://tenant/ref",
            "provider_latency_status": "recorded",
            "provider_latency_ms": True,
        },
    ],
)
async def test_new_cache_writes_reject_missing_or_invalid_latency(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    """migration 专属 unavailable 状态不能用于 0012a 后的新写入。"""

    dsn = sqlite_dsn(tmp_path / "invalid-cache.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            with pytest.raises(ValueError, match="provider latency"):
                await uow.embedding_cache.put(
                    EmbeddingCacheCreate(
                        tenant_id="tenant-a",
                        provider="local",
                        model="mock-small",
                        input_hash="b" * 64,
                        vector_ref="embedding://tenant/ref",
                        metadata=metadata,
                    )
                )
    finally:
        await storage.dispose()


def test_0012a_upgrade_preserves_legacy_row_and_switches_physical_table(tmp_path: Path) -> None:
    """合法旧 row 无 latency 时无损补 unavailable/null，并切断旧表名。"""

    db_path = tmp_path / "upgrade.db"
    dsn = sqlite_dsn(db_path)
    command.upgrade(alembic_config(dsn), "0012_service_runtime_execution_context")
    insert_legacy_cache_row(db_path, metadata={"custom": "keep", "cache": "miss"})

    # 本合同只验证插入式 0012a；后续 0013 的 head 演进不能污染该边界。
    command.upgrade(alembic_config(dsn), "0012a_embedding_cache_tenant_scope")

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type in ('table', 'view')"
            )
        }
        row = connection.execute(
            """
            select id, tenant_id, vector_ref, metadata_json, created_at, updated_at
            from tenant_embedding_cache
            """
        ).fetchone()
        indexes = {
            item[1] for item in connection.execute("pragma index_list('tenant_embedding_cache')")
        }
        table_sql = connection.execute(
            "select sql from sqlite_master where type='table' and name='tenant_embedding_cache'"
        ).fetchone()
        revision = connection.execute("select version_num from alembic_version").fetchone()

    assert "embedding_cache" not in tables
    assert "tenant_embedding_cache" in tables
    assert row is not None
    assert row[:3] == ("cache-legacy", "tenant-a", "embedding://legacy/ref")
    metadata = json.loads(row[3])
    assert metadata == {
        "custom": "keep",
        "cache": "miss",
        "cache_status": "miss",
        "vector_ref": "embedding://legacy/ref",
        "provider_latency_status": "unavailable",
        "provider_latency_ms": None,
    }
    assert row[4:] == ("2026-07-01T00:00:00+00:00", "2026-07-02T00:00:00+00:00")
    assert table_sql is not None
    assert "uq_tenant_embedding_cache_tenant_provider_model_hash" in table_sql[0]
    assert "ix_tenant_embedding_cache_tenant_id" in indexes
    assert "ix_tenant_embedding_cache_input_hash" in indexes
    assert revision == ("0012a_embedding_cache_tenant_scope",)


@pytest.mark.parametrize(
    "metadata",
    [
        {"cache": "miss", "cache_status": "hit"},
        {"latency_ms": -1},
        {"latency_ms": True},
        {"latency_ms": float("inf")},
        {"provider_latency_status": "recorded", "provider_latency_ms": None},
        ["not-an-object"],
    ],
)
def test_0012a_upgrade_rejects_invalid_metadata_before_mutation(
    tmp_path: Path, metadata: object
) -> None:
    """任一非法 legacy metadata 都必须保留 0012 schema 和原 evidence。"""

    db_path = tmp_path / "invalid-upgrade.db"
    dsn = sqlite_dsn(db_path)
    command.upgrade(alembic_config(dsn), "0012_service_runtime_execution_context")
    insert_legacy_cache_row(db_path, metadata=metadata)

    with pytest.raises(Exception, match="embedding cache metadata"):
        command.upgrade(alembic_config(dsn), "head")

    with sqlite3.connect(db_path) as connection:
        revision = connection.execute("select version_num from alembic_version").fetchone()
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        stored = connection.execute(
            "select metadata_json from embedding_cache where id='cache-legacy'"
        ).fetchone()
    assert revision == ("0012_service_runtime_execution_context",)
    assert "embedding_cache" in tables
    assert "tenant_embedding_cache" not in tables
    assert stored == (json.dumps(metadata),)


def test_0012a_downgrade_requires_exact_opt_in_and_empty_evidence(tmp_path: Path) -> None:
    """空库仍需显式确认，有 evidence 时即使确认也不得降级。"""

    empty_path = tmp_path / "empty.db"
    empty_dsn = sqlite_dsn(empty_path)
    command.upgrade(alembic_config(empty_dsn), "head")
    with pytest.raises(RuntimeError, match="explicit opt-in"):
        command.downgrade(migration_config(empty_dsn), "0012_service_runtime_execution_context")
    with pytest.raises(RuntimeError, match="explicit opt-in"):
        command.downgrade(
            migration_config(
                empty_dsn,
                x_args=[
                    "allow_empty_evidence_downgrade=true",
                    "allow_empty_evidence_downgrade=true",
                ],
            ),
            "0012_service_runtime_execution_context",
        )
    command.downgrade(
        migration_config(empty_dsn, x_args=["allow_empty_evidence_downgrade=true"]),
        "0012_service_runtime_execution_context",
    )
    with sqlite3.connect(empty_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        revision = connection.execute("select version_num from alembic_version").fetchone()
    assert "embedding_cache" in tables
    assert "tenant_embedding_cache" not in tables
    assert revision == ("0012_service_runtime_execution_context",)

    used_path = tmp_path / "used.db"
    used_dsn = sqlite_dsn(used_path)
    command.upgrade(alembic_config(used_dsn), "head")
    storage = SQLAlchemyStorage.from_dsn(used_dsn)

    async def seed() -> None:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await LocalEmbeddingProvider(cache=uow.embedding_cache).embed(
                EmbeddingRequest(input="evidence", tenant_id="tenant-a")
            )
            await uow.commit()

    import asyncio

    asyncio.run(seed())
    asyncio.run(storage.dispose())
    with pytest.raises(RuntimeError, match="cache evidence exists"):
        command.downgrade(
            migration_config(used_dsn, x_args=["allow_empty_evidence_downgrade=true"]),
            "0012_service_runtime_execution_context",
        )


@pytest.mark.asyncio
async def test_new_repository_fails_closed_on_0012_schema(tmp_path: Path) -> None:
    """新代码不得通过旧物理表名继续读取未隔离 evidence。"""

    db_path = tmp_path / "old-schema.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn, "0012_service_runtime_execution_context")
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            with pytest.raises(OperationalError):
                await uow.embedding_cache.get(
                    tenant_id="tenant-a",
                    provider="local",
                    model="mock-small",
                    input_hash="c" * 64,
                )
    finally:
        await storage.dispose()
