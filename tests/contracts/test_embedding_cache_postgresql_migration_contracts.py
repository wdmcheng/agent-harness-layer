"""Embedding cache 0012a 的 PostgreSQL 升级与租户 identity 合同。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
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

from agent_harness.storage import SQLAlchemyStorage

pytestmark = _postgresql_pytestmark


@pytest.mark.asyncio
async def test_0012a_postgresql_upgrade_preserves_evidence_and_enforces_tenant_identity() -> None:
    """真实 DDL 保留合法四态 metadata，并把 cache identity 收紧到 tenant。"""

    async with _isolated_database("embedding_upgrade") as dsn:
        await _upgrade(dsn, _REVISION_0012)
        engine = create_async_engine(dsn)
        missing_ref = "embedding://legacy/missing"
        equal_ref = "embedding://legacy/equal"
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "insert into tenants(id, display_name) values "
                        "('tenant-a', 'A'), ('tenant-b', 'B')"
                    )
                )
                await _seed_legacy_row(
                    connection,
                    row_id="legacy-missing",
                    tenant_id="tenant-a",
                    input_hash="a" * 64,
                    vector_ref=missing_ref,
                    metadata={"cache": "miss", "custom": {"preserve": True}},
                )
                await _seed_legacy_row(
                    connection,
                    row_id="legacy-equal",
                    tenant_id="tenant-a",
                    input_hash="b" * 64,
                    vector_ref=equal_ref,
                    metadata={
                        "cache": "hit",
                        "cache_status": "hit",
                        "vector_ref": equal_ref,
                        "provider_latency_status": "recorded",
                        "provider_latency_ms": 12,
                        "latency_ms": 12,
                        "custom": "keep-equal",
                    },
                )
            await engine.dispose()

            await _upgrade(dsn, _REVISION_0012A)
            engine = create_async_engine(dsn)
            async with engine.connect() as connection:
                relations, indexes, constraints = await _cache_schema(
                    connection, "tenant_embedding_cache"
                )
                rows = (
                    (
                        await connection.execute(
                            text(
                                "select id, tenant_id, provider, model, input_hash, vector_ref, "
                                "metadata_json, created_at, updated_at "
                                "from tenant_embedding_cache order by id"
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                before_old_query = await _public_snapshot(connection)

            assert relations == (("tenant_embedding_cache", "r"),)
            index_map = {
                str(name): (bool(unique), tuple(columns)) for name, unique, columns in indexes
            }
            assert index_map["uq_tenant_embedding_cache_tenant_provider_model_hash"] == (
                True,
                ("tenant_id", "provider", "model", "input_hash"),
            )
            assert index_map["ix_tenant_embedding_cache_tenant_id"] == (False, ("tenant_id",))
            assert index_map["ix_tenant_embedding_cache_input_hash"] == (False, ("input_hash",))
            assert "uq_embedding_cache_provider_model_hash" not in index_map
            assert "ix_embedding_cache_tenant_id" not in index_map
            assert "ix_embedding_cache_input_hash" not in index_map
            constraint_map = {str(name): str(definition) for name, definition in constraints}
            assert constraint_map["uq_tenant_embedding_cache_tenant_provider_model_hash"] == (
                "UNIQUE (tenant_id, provider, model, input_hash)"
            )
            assert "uq_embedding_cache_provider_model_hash" not in constraint_map

            by_id = {str(row["id"]): row for row in rows}
            missing = by_id["legacy-missing"]
            assert tuple(missing[key] for key in ("tenant_id", "provider", "model")) == (
                "tenant-a",
                "legacy-provider",
                "legacy-model",
            )
            assert missing["input_hash"] == "a" * 64
            assert missing["vector_ref"] == missing_ref
            assert missing["metadata_json"] == {
                "cache": "miss",
                "custom": {"preserve": True},
                "cache_status": "miss",
                "vector_ref": missing_ref,
                "provider_latency_status": "unavailable",
                "provider_latency_ms": None,
            }
            assert missing["created_at"] == datetime(2026, 7, 1, tzinfo=UTC)
            assert missing["updated_at"] == datetime(2026, 7, 2, tzinfo=UTC)
            equal = by_id["legacy-equal"]
            assert equal["metadata_json"] == {
                "cache": "hit",
                "cache_status": "hit",
                "vector_ref": equal_ref,
                "provider_latency_status": "recorded",
                "provider_latency_ms": 12,
                "latency_ms": 12,
                "custom": "keep-equal",
            }

            with pytest.raises(DBAPIError):
                async with engine.connect() as connection:
                    await connection.execute(text("select id from embedding_cache"))
            async with engine.connect() as connection:
                assert await _public_snapshot(connection) == before_old_query

            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "insert into tenant_embedding_cache("
                        "id, tenant_id, provider, model, input_hash, vector_ref, metadata_json"
                        ") values ("
                        "'tenant-b-same', 'tenant-b', 'legacy-provider', 'legacy-model', "
                        ":input_hash, 'embedding://tenant-b/same', "
                        "cast(:metadata as json))"
                    ),
                    {
                        "input_hash": "a" * 64,
                        "metadata": json.dumps(
                            {
                                "cache_status": "miss",
                                "vector_ref": "embedding://tenant-b/same",
                                "provider_latency_status": "recorded",
                                "provider_latency_ms": 1,
                            }
                        ),
                    },
                )
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "insert into tenant_embedding_cache("
                            "id, tenant_id, provider, model, input_hash, vector_ref, metadata_json"
                            ") values ("
                            "'tenant-a-duplicate', 'tenant-a', 'legacy-provider', "
                            "'legacy-model', :input_hash, 'embedding://duplicate', '{}')"
                        ),
                        {"input_hash": "a" * 64},
                    )

            storage = SQLAlchemyStorage.from_dsn(dsn)
            try:
                async with storage.uow() as uow:
                    tenant_a = await uow.embedding_cache.get(
                        tenant_id="tenant-a",
                        provider="legacy-provider",
                        model="legacy-model",
                        input_hash="a" * 64,
                    )
                    tenant_b = await uow.embedding_cache.get(
                        tenant_id="tenant-b",
                        provider="legacy-provider",
                        model="legacy-model",
                        input_hash="a" * 64,
                    )
                    await uow.commit()
            finally:
                await storage.dispose()
            assert tenant_a is not None and tenant_a.id == "legacy-missing"
            assert tenant_b is not None and tenant_b.id == "tenant-b-same"
        finally:
            await engine.dispose()
