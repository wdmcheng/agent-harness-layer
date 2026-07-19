"""Embedding cache 0012a 的真实 PostgreSQL 迁移合同共享夹具。

升级、双向 schema mismatch 与降级共用同一隔离库生命周期、非法 metadata
矩阵及全量 public snapshot，防止拆分测试后弱化零 mutation 门禁。
"""

from __future__ import annotations

import asyncio
import json
import os
from argparse import Namespace
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from agent_harness.storage import run_migrations
from agent_harness.storage.migrations.runner import alembic_config

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL embedding migration contract runs when service smoke provides a DSN.",
)

REVISION_0012 = "0012_service_runtime_execution_context"
REVISION_0012A = "0012a_embedding_cache_tenant_scope"
OPT_IN = "allow_empty_evidence_downgrade=true"

INVALID_METADATA_CASES: tuple[tuple[str, object], ...] = (
    ("legacy-cache-null", {"cache": None}),
    ("legacy-cache-bool", {"cache": True}),
    ("legacy-cache-invalid", {"cache": "invalid"}),
    ("cache-status-null", {"cache_status": None}),
    ("cache-status-bool", {"cache_status": False}),
    ("cache-status-invalid", {"cache_status": "invalid"}),
    ("cache-status-conflicts-with-legacy", {"cache": "miss", "cache_status": "hit"}),
    ("vector-ref-null", {"vector_ref": None}),
    ("vector-ref-empty", {"vector_ref": ""}),
    ("vector-ref-bool", {"vector_ref": True}),
    ("vector-ref-number", {"vector_ref": 7}),
    ("vector-ref-object", {"vector_ref": {"ref": "embedding://legacy/ref"}}),
    ("vector-ref-conflicts-with-column", {"vector_ref": "embedding://wrong"}),
    ("provider-latency-bool", {"provider_latency_ms": True}),
    ("provider-latency-negative", {"provider_latency_ms": -1}),
    ("provider-latency-nan-string", {"provider_latency_ms": "NaN"}),
    ("provider-latency-infinity-string", {"provider_latency_ms": "Infinity"}),
    ("provider-latency-negative-infinity-string", {"provider_latency_ms": "-Infinity"}),
    ("provider-latency-nonnumeric", {"provider_latency_ms": "slow"}),
    ("provider-latency-object", {"provider_latency_ms": {"milliseconds": 4}}),
    ("provider-latency-null", {"provider_latency_ms": None}),
    ("legacy-latency-bool", {"latency_ms": True}),
    ("legacy-latency-negative", {"latency_ms": -1}),
    ("legacy-latency-nan-string", {"latency_ms": "NaN"}),
    ("legacy-latency-infinity-string", {"latency_ms": "Infinity"}),
    ("legacy-latency-negative-infinity-string", {"latency_ms": "-Infinity"}),
    ("legacy-latency-nonnumeric", {"latency_ms": "slow"}),
    ("legacy-latency-object", {"latency_ms": {"milliseconds": 4}}),
    ("legacy-latency-null", {"latency_ms": None}),
    (
        "provider-and-legacy-latency-conflict",
        {"provider_latency_ms": 3, "latency_ms": 4},
    ),
    (
        "provider-latency-null-with-valid-legacy",
        {"provider_latency_ms": None, "latency_ms": 4},
    ),
    (
        "provider-latency-valid-with-null-legacy",
        {"provider_latency_ms": 4, "latency_ms": None},
    ),
    ("latency-status-null-without-value", {"provider_latency_status": None}),
    (
        "latency-status-null-with-value",
        {"provider_latency_status": None, "provider_latency_ms": 4},
    ),
    ("latency-status-bool", {"provider_latency_status": True}),
    ("latency-status-invalid", {"provider_latency_status": "invalid"}),
    ("latency-recorded-without-value", {"provider_latency_status": "recorded"}),
    (
        "latency-unavailable-with-provider-value",
        {"provider_latency_status": "unavailable", "provider_latency_ms": 4},
    ),
    (
        "latency-unavailable-with-legacy-value",
        {"provider_latency_status": "unavailable", "latency_ms": 4},
    ),
    (
        "latency-unavailable-with-both-values",
        {
            "provider_latency_status": "unavailable",
            "provider_latency_ms": 4,
            "latency_ms": 4,
        },
    ),
    (
        "latency-recorded-with-null-provider-value",
        {"provider_latency_status": "recorded", "provider_latency_ms": None},
    ),
    (
        "latency-recorded-with-null-legacy-value",
        {"provider_latency_status": "recorded", "latency_ms": None},
    ),
    ("metadata-array", ["invalid"]),
    ("metadata-string", "invalid"),
    ("metadata-null", None),
    ("metadata-bool", True),
    ("metadata-number", 7),
)

NON_STANDARD_JSON_LATENCY_CASES: tuple[tuple[str, str], ...] = tuple(
    (
        f"{field_name}-{token.lower().replace('-', 'negative-')}",
        f'{{"{field_name}": {token}}}',
    )
    for field_name in ("provider_latency_ms", "latency_ms")
    for token in ("NaN", "Infinity", "-Infinity")
)

type CacheRelations = tuple[tuple[str, str], ...]
type CacheIndexes = tuple[tuple[str, bool, tuple[str, ...]], ...]
type CacheConstraints = tuple[tuple[str, str], ...]


@asynccontextmanager
async def isolated_database(prefix: str) -> AsyncGenerator[str]:
    """每个合同使用独立 database，避免测试 DDL 或 evidence 污染共享实例。"""

    base_url = make_url(os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"])
    database_name = f"agent_harness_{prefix}_{uuid4().hex}"
    admin_url = base_url.set(database="postgres")
    test_url = base_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    await admin_engine.dispose()
    try:
        yield test_url.render_as_string(hide_password=False)
    finally:
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        await admin_engine.dispose()


def _config(dsn: str, *, x_args: list[str] | None = None) -> Config:
    """为指定隔离库创建 Alembic 配置，并透传显式迁移开关。"""

    config = alembic_config(dsn)
    config.cmd_opts = Namespace(x=x_args or [])
    return config


async def upgrade(dsn: str, revision: str) -> None:
    """在线程中执行同步迁移入口，避免测试事件循环被 Alembic 阻塞。"""

    await asyncio.to_thread(run_migrations, dsn, revision)


async def downgrade(dsn: str, *, x_args: list[str] | None = None) -> None:
    """降回 0012 基线，并允许调用方显式传入受控的 downgrade 开关。"""

    await asyncio.to_thread(command.downgrade, _config(dsn, x_args=x_args), REVISION_0012)


async def seed_legacy_row(
    connection: AsyncConnection,
    *,
    row_id: str,
    tenant_id: str,
    input_hash: str,
    vector_ref: str,
    metadata: object,
) -> None:
    """经 0012 公开 schema 写入 legacy evidence，不依赖 0012a ORM。"""

    await connection.execute(
        text(
            "insert into embedding_cache("
            "id, tenant_id, provider, model, input_hash, vector_ref, metadata_json, "
            "created_at, updated_at"
            ") values ("
            ":row_id, :tenant_id, 'legacy-provider', 'legacy-model', :input_hash, "
            ":vector_ref, cast(:metadata as json), :created_at, :updated_at)"
        ),
        {
            "row_id": row_id,
            "tenant_id": tenant_id,
            "input_hash": input_hash,
            "vector_ref": vector_ref,
            "metadata": json.dumps(metadata),
            "created_at": datetime(2026, 7, 1, tzinfo=UTC),
            "updated_at": datetime(2026, 7, 2, tzinfo=UTC),
        },
    )


async def public_snapshot(connection: AsyncConnection) -> tuple[object, ...]:
    """捕获 mutation 门禁需要保持不变的完整 public schema 与业务数据。"""

    relations = tuple(
        (
            await connection.execute(
                text(
                    "select c.relname, c.relkind from pg_class c "
                    "join pg_namespace n on n.oid=c.relnamespace "
                    "where n.nspname='public' and c.relkind in ('r','p','v','m') "
                    "order by c.relname"
                )
            )
        ).all()
    )
    columns = tuple(
        (
            await connection.execute(
                text(
                    "select table_name, column_name, udt_name, is_nullable, column_default "
                    "from information_schema.columns where table_schema='public' "
                    "order by table_name, ordinal_position"
                )
            )
        ).all()
    )
    indexes = tuple(
        (
            await connection.execute(
                text(
                    "select tablename, indexname, indexdef from pg_indexes "
                    "where schemaname='public' order by tablename, indexname"
                )
            )
        ).all()
    )
    constraints = tuple(
        (
            await connection.execute(
                text(
                    "select conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                    "from pg_constraint where connamespace='public'::regnamespace "
                    "order by conrelid::regclass::text, conname"
                )
            )
        ).all()
    )
    revision = (
        await connection.execute(text("select version_num from alembic_version"))
    ).scalar_one()
    data: list[tuple[str, str]] = []
    for relation_name, relation_kind in relations:
        if relation_kind not in {"r", "p"}:
            continue
        escaped_name = str(relation_name).replace('"', '""')
        rows = (
            await connection.exec_driver_sql(
                "select coalesce("
                "json_agg(row_to_json(snapshot_row) order by row_to_json(snapshot_row)::text)"
                f"::text, '[]') from \"{escaped_name}\" snapshot_row"
            )
        ).scalar_one()
        data.append((str(relation_name), str(rows)))
    return relations, columns, indexes, constraints, revision, tuple(data)


async def cache_schema(
    connection: AsyncConnection,
    table_name: str,
) -> tuple[CacheRelations, CacheIndexes, CacheConstraints]:
    """返回 cache 关系、索引列和 constraint 定义，供精确 schema 断言。"""

    relation_rows = (
        await connection.execute(
            text(
                "select c.relname, c.relkind from pg_class c "
                "join pg_namespace n on n.oid=c.relnamespace "
                "where n.nspname='public' "
                "and c.relname in ('embedding_cache', 'tenant_embedding_cache') "
                "order by c.relname"
            )
        )
    ).all()
    relations: CacheRelations = tuple(
        (
            str(relation_name),
            relation_kind.decode() if isinstance(relation_kind, bytes) else str(relation_kind),
        )
        for relation_name, relation_kind in relation_rows
    )
    index_rows = (
        await connection.execute(
            text(
                "select i.relname, x.indisunique, "
                "array_agg(a.attname order by k.ordinality) "
                "from pg_index x join pg_class t on t.oid=x.indrelid "
                "join pg_namespace n on n.oid=t.relnamespace "
                "join pg_class i on i.oid=x.indexrelid "
                "join unnest(x.indkey) with ordinality k(attnum, ordinality) on true "
                "join pg_attribute a on a.attrelid=t.oid and a.attnum=k.attnum "
                "where n.nspname='public' and t.relname=:table_name "
                "group by i.relname, x.indisunique order by i.relname"
            ),
            {"table_name": table_name},
        )
    ).all()
    indexes: CacheIndexes = tuple(
        (
            str(index_name),
            bool(is_unique),
            tuple(str(column_name) for column_name in column_names),
        )
        for index_name, is_unique, column_names in index_rows
    )
    constraint_rows = (
        await connection.execute(
            text(
                "select conname, pg_get_constraintdef(oid) from pg_constraint "
                "where conrelid=to_regclass(:table_name) order by conname"
            ),
            {"table_name": table_name},
        )
    ).all()
    constraints: CacheConstraints = tuple(
        (str(constraint_name), str(definition)) for constraint_name, definition in constraint_rows
    )
    return relations, indexes, constraints
