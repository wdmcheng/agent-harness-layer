"""Embedding cache 0012a 的 PostgreSQL 失败原子性与降级合同。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    INVALID_METADATA_CASES as _INVALID_METADATA_CASES,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    NON_STANDARD_JSON_LATENCY_CASES as _NON_STANDARD_JSON_LATENCY_CASES,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    OPT_IN as _OPT_IN,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    REVISION_0012 as _REVISION_0012,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    REVISION_0012A as _REVISION_0012A,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    cache_schema as _cache_schema,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    downgrade as _downgrade,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database as _isolated_database,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    public_snapshot as _public_snapshot,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    pytestmark as _postgresql_pytestmark,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    seed_legacy_row as _seed_legacy_row,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    upgrade as _upgrade,
)

from agent_harness.storage import SQLAlchemyStorage, get_current_revision

pytestmark = _postgresql_pytestmark


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_name", "metadata"),
    _INVALID_METADATA_CASES,
    ids=[case_name for case_name, _ in _INVALID_METADATA_CASES],
)
async def test_0012a_postgresql_invalid_metadata_is_zero_mutation(
    case_name: str,
    metadata: object,
) -> None:
    """冲突、非法及 latency status/value 不一致均在首个 mutation 前整批拒绝。"""

    async with _isolated_database("embedding_preflight") as dsn:
        await _upgrade(dsn, _REVISION_0012)
        engine = create_async_engine(dsn)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("insert into tenants(id, display_name) values ('tenant-a', 'A')")
                )
                await _seed_legacy_row(
                    connection,
                    row_id=f"invalid-{case_name[:28]}",
                    tenant_id="tenant-a",
                    input_hash="c" * 64,
                    vector_ref="embedding://legacy/ref",
                    metadata=metadata,
                )
            async with engine.connect() as connection:
                before = await _public_snapshot(connection)
            await engine.dispose()
            with pytest.raises(RuntimeError, match="embedding cache metadata is invalid"):
                await _upgrade(dsn, _REVISION_0012A)
            engine = create_async_engine(dsn)
            async with engine.connect() as connection:
                after = await _public_snapshot(connection)
            assert after == before, case_name
            assert after[4] == _REVISION_0012
        finally:
            await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_name", "raw_metadata"),
    _NON_STANDARD_JSON_LATENCY_CASES,
    ids=[case_name for case_name, _ in _NON_STANDARD_JSON_LATENCY_CASES],
)
async def test_0012_postgresql_rejects_non_standard_json_latency_before_storage(
    case_name: str,
    raw_metadata: str,
) -> None:
    """PostgreSQL JSON parser 拒绝非标准数值，不能伪造成 migration 已预检的 row。"""

    async with _isolated_database("embedding_json_parser") as dsn:
        await _upgrade(dsn, _REVISION_0012)
        engine = create_async_engine(dsn)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("insert into tenants(id, display_name) values ('tenant-a', 'A')")
                )
            async with engine.connect() as connection:
                before = await _public_snapshot(connection)
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text(
                            "insert into embedding_cache("
                            "id, tenant_id, provider, model, input_hash, vector_ref, "
                            "metadata_json, created_at, updated_at"
                            ") values ("
                            ":row_id, 'tenant-a', 'legacy-provider', 'legacy-model', "
                            ":input_hash, 'embedding://legacy/ref', cast(:metadata as json), "
                            ":created_at, :updated_at)"
                        ),
                        {
                            "row_id": "invalid-json",
                            "input_hash": "d" * 64,
                            "metadata": raw_metadata,
                            "created_at": datetime(2026, 7, 1, tzinfo=UTC),
                            "updated_at": datetime(2026, 7, 2, tzinfo=UTC),
                        },
                    )
                await connection.rollback()
                after = await _public_snapshot(connection)
            assert after == before, case_name
            assert after[4] == _REVISION_0012
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_0012a_postgresql_new_repository_fails_closed_on_0012_schema() -> None:
    """新 repository 连接旧 schema 时不得返回或改写 legacy cache evidence。"""

    async with _isolated_database("embedding_new_app_old_schema") as dsn:
        await _upgrade(dsn, _REVISION_0012)
        engine = create_async_engine(dsn)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("insert into tenants(id, display_name) values ('tenant-a', 'A')")
                )
                await _seed_legacy_row(
                    connection,
                    row_id="legacy-only",
                    tenant_id="tenant-a",
                    input_hash="d" * 64,
                    vector_ref="embedding://legacy/only",
                    metadata={"cache": "miss"},
                )
            async with engine.connect() as connection:
                before = await _public_snapshot(connection)

            storage = SQLAlchemyStorage.from_dsn(dsn)
            try:
                with pytest.raises(DBAPIError):
                    async with storage.uow() as uow:
                        await uow.embedding_cache.get(
                            tenant_id="tenant-a",
                            provider="legacy-provider",
                            model="legacy-model",
                            input_hash="d" * 64,
                        )
            finally:
                await storage.dispose()
            async with engine.connect() as connection:
                assert await _public_snapshot(connection) == before
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_0012a_postgresql_downgrade_opt_in_and_evidence_gates() -> None:
    """真实 PostgreSQL downgrade 只允许精确 opt-in 的空 evidence schema。"""

    async with _isolated_database("embedding_downgrade_empty") as dsn:
        await _upgrade(dsn, _REVISION_0012A)
        engine = create_async_engine(dsn)
        try:
            for label, x_args in (
                ("missing", []),
                ("invalid", ["allow_empty_evidence_downgrade=false"]),
                ("wrong-case", ["allow_empty_evidence_downgrade=True"]),
                ("duplicate", [_OPT_IN, _OPT_IN]),
            ):
                async with engine.connect() as connection:
                    before = await _public_snapshot(connection)
                with pytest.raises(RuntimeError, match="explicit opt-in is required"):
                    await _downgrade(dsn, x_args=x_args)
                async with engine.connect() as connection:
                    assert await _public_snapshot(connection) == before, label

            await engine.dispose()
            await _downgrade(dsn, x_args=[_OPT_IN])
            engine = create_async_engine(dsn)
            async with engine.connect() as connection:
                relations, indexes, constraints = await _cache_schema(connection, "embedding_cache")
                revision = (
                    await connection.execute(text("select version_num from alembic_version"))
                ).scalar_one()
            assert relations == (("embedding_cache", "r"),)
            index_map = {
                str(name): (bool(unique), tuple(columns)) for name, unique, columns in indexes
            }
            assert index_map["uq_embedding_cache_provider_model_hash"] == (
                True,
                ("provider", "model", "input_hash"),
            )
            assert index_map["ix_embedding_cache_tenant_id"] == (False, ("tenant_id",))
            assert index_map["ix_embedding_cache_input_hash"] == (False, ("input_hash",))
            assert "uq_tenant_embedding_cache_tenant_provider_model_hash" not in index_map
            constraint_map = {str(name): str(definition) for name, definition in constraints}
            assert constraint_map["uq_embedding_cache_provider_model_hash"] == (
                "UNIQUE (provider, model, input_hash)"
            )
            assert revision == _REVISION_0012
            assert await asyncio.to_thread(get_current_revision, dsn) == _REVISION_0012
        finally:
            await engine.dispose()

    async with _isolated_database("embedding_downgrade_evidence") as dsn:
        await _upgrade(dsn, _REVISION_0012A)
        engine = create_async_engine(dsn)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("insert into tenants(id, display_name) values ('tenant-a', 'A')")
                )
                await connection.execute(
                    text(
                        "insert into tenant_embedding_cache("
                        "id, tenant_id, provider, model, input_hash, vector_ref, metadata_json"
                        ") values ('evidence', 'tenant-a', 'provider', 'model', :input_hash, "
                        "'embedding://evidence', cast(:metadata as json))"
                    ),
                    {
                        "input_hash": "e" * 64,
                        "metadata": json.dumps(
                            {
                                "cache_status": "miss",
                                "vector_ref": "embedding://evidence",
                                "provider_latency_status": "recorded",
                                "provider_latency_ms": 1,
                            }
                        ),
                    },
                )
            async with engine.connect() as connection:
                before = await _public_snapshot(connection)
            with pytest.raises(RuntimeError, match="cache evidence exists"):
                await _downgrade(dsn, x_args=[_OPT_IN])
            async with engine.connect() as connection:
                after = await _public_snapshot(connection)
            assert after == before
            assert after[4] == _REVISION_0012A
        finally:
            await engine.dispose()
