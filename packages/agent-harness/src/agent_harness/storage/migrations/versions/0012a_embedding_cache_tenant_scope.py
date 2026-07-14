"""把 embedding cache 收紧为 tenant-scoped evidence。"""

from __future__ import annotations

import json
from math import isfinite
from typing import Any, NoReturn, cast

import sqlalchemy as sa
from alembic import context, op

revision = "0012a_embedding_cache_tenant_scope"
down_revision = "0012_service_runtime_execution_context"
branch_labels = None
depends_on = None

_OPT_IN = "allow_empty_evidence_downgrade=true"


def upgrade() -> None:
    """预检并保留旧 evidence，再原子切换物理表与唯一性。"""

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            select id, vector_ref, metadata_json
            from embedding_cache
            order by id
            """
        )
    ).mappings()
    normalized = [
        (str(row["id"]), _normalize_metadata(row["metadata_json"], str(row["vector_ref"])))
        for row in rows
    ]

    metadata_parameter = sa.bindparam("metadata_json", type_=sa.JSON())
    statement = sa.text(
        "update embedding_cache set metadata_json = :metadata_json where id = :row_id"
    ).bindparams(metadata_parameter)
    for row_id, metadata in normalized:
        connection.execute(statement, {"metadata_json": metadata, "row_id": row_id})

    op.rename_table("embedding_cache", "tenant_embedding_cache")
    op.drop_index(
        "ix_embedding_cache_tenant_id",
        table_name="tenant_embedding_cache",
    )
    op.drop_index(
        "ix_embedding_cache_input_hash",
        table_name="tenant_embedding_cache",
    )
    with op.batch_alter_table("tenant_embedding_cache") as batch_op:
        batch_op.drop_constraint(
            "uq_embedding_cache_provider_model_hash",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_tenant_embedding_cache_tenant_provider_model_hash",
            ["tenant_id", "provider", "model", "input_hash"],
        )
    op.create_index(
        "ix_tenant_embedding_cache_tenant_id",
        "tenant_embedding_cache",
        ["tenant_id"],
    )
    op.create_index(
        "ix_tenant_embedding_cache_input_hash",
        "tenant_embedding_cache",
        ["input_hash"],
    )


def downgrade() -> None:
    """仅在显式确认且没有任何 cache evidence 时恢复 0012 schema。"""

    _require_exact_empty_evidence_opt_in()
    connection = op.get_bind()
    evidence_count = int(
        connection.execute(sa.text("select count(*) from tenant_embedding_cache")).scalar_one()
    )
    if evidence_count:
        raise RuntimeError("0012a downgrade refused: cache evidence exists")

    op.drop_index(
        "ix_tenant_embedding_cache_input_hash",
        table_name="tenant_embedding_cache",
    )
    op.drop_index(
        "ix_tenant_embedding_cache_tenant_id",
        table_name="tenant_embedding_cache",
    )
    with op.batch_alter_table("tenant_embedding_cache") as batch_op:
        batch_op.drop_constraint(
            "uq_tenant_embedding_cache_tenant_provider_model_hash",
            type_="unique",
        )
        batch_op.create_unique_constraint(
            "uq_embedding_cache_provider_model_hash",
            ["provider", "model", "input_hash"],
        )
    op.rename_table("tenant_embedding_cache", "embedding_cache")
    op.create_index("ix_embedding_cache_tenant_id", "embedding_cache", ["tenant_id"])
    op.create_index("ix_embedding_cache_input_hash", "embedding_cache", ["input_hash"])


def _normalize_metadata(raw: object, vector_ref: str) -> dict[str, Any]:
    """把旧 metadata 增量规范化；任何冲突都在首个 mutation 前拒绝。"""

    metadata = _metadata_object(raw)
    has_legacy_cache = "cache" in metadata
    has_cache_status = "cache_status" in metadata
    legacy_cache = metadata.get("cache")
    cache_status = metadata.get("cache_status")
    if has_legacy_cache and legacy_cache not in {"hit", "miss"}:
        _invalid_metadata()
    if has_cache_status and cache_status not in {"hit", "miss"}:
        _invalid_metadata()
    if has_legacy_cache and has_cache_status and legacy_cache != cache_status:
        _invalid_metadata()
    metadata["cache_status"] = (
        cache_status if has_cache_status else legacy_cache if has_legacy_cache else "miss"
    )

    has_metadata_vector_ref = "vector_ref" in metadata
    metadata_vector_ref = metadata.get("vector_ref")
    if has_metadata_vector_ref and (
        not isinstance(metadata_vector_ref, str)
        or not metadata_vector_ref
        or metadata_vector_ref != vector_ref
    ):
        _invalid_metadata()
    metadata["vector_ref"] = vector_ref

    has_provider_latency = "provider_latency_ms" in metadata
    has_legacy_latency = "latency_ms" in metadata
    provider_latency = metadata.get("provider_latency_ms")
    legacy_latency = metadata.get("latency_ms")
    has_latency_status = "provider_latency_status" in metadata
    latency_status = metadata.get("provider_latency_status")

    if not has_provider_latency and not has_legacy_latency:
        if has_latency_status and latency_status != "unavailable":
            _invalid_metadata()
        metadata["provider_latency_status"] = "unavailable"
        metadata["provider_latency_ms"] = None
        return metadata

    if has_provider_latency and provider_latency is None:
        if not has_legacy_latency and latency_status == "unavailable":
            return metadata
        _invalid_metadata()
    if has_provider_latency:
        _validate_latency(provider_latency)
    if has_legacy_latency:
        _validate_latency(legacy_latency)
    if has_provider_latency and has_legacy_latency and provider_latency != legacy_latency:
        _invalid_metadata()
    if has_latency_status and latency_status != "recorded":
        _invalid_metadata()
    metadata["provider_latency_status"] = "recorded"
    metadata["provider_latency_ms"] = provider_latency if has_provider_latency else legacy_latency
    return metadata


def _metadata_object(raw: object) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            _invalid_metadata()
    if not isinstance(raw, dict):
        _invalid_metadata()
    return cast(dict[str, Any], raw)


def _validate_latency(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(value)
        or value < 0
    ):
        _invalid_metadata()


def _invalid_metadata() -> NoReturn:
    raise RuntimeError("embedding cache metadata is invalid")


def _require_exact_empty_evidence_opt_in() -> None:
    arguments = context.get_x_argument(as_dictionary=False)
    matches = [item for item in arguments if item.startswith("allow_empty_evidence_downgrade")]
    if matches != [_OPT_IN]:
        raise RuntimeError("0012a downgrade refused: explicit opt-in is required")
