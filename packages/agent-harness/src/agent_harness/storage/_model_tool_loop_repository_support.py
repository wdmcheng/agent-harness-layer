"""模型工具循环 repository 的 active 围栏与 ORM/DTO 投影。"""
# pyright: reportPrivateUsage=false, reportUnusedFunction=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._model_tool_loop_repository_contracts import (
    ModelToolLoopCumulativeUsage,
    ModelToolLoopFrozenBounds,
    ModelToolLoopRecord,
    ModelToolLoopState,
    ModelToolLoopStorageConflict,
)
from agent_harness.storage.models import ModelToolLoopModel


async def require_model_tool_loop_active(
    session: AsyncSession,
    *,
    tenant_id: str,
    loop_id: str,
) -> None:
    """新tool/context副作用写入前拒绝缺失或已terminal的loop。"""

    status = await session.scalar(
        select(ModelToolLoopModel.status).where(
            ModelToolLoopModel.tenant_id == tenant_id,
            ModelToolLoopModel.loop_id == loop_id,
        )
    )
    if status != "active":
        raise ModelToolLoopStorageConflict


def _record(model: ModelToolLoopModel) -> ModelToolLoopRecord:
    return ModelToolLoopRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        run_id=model.run_id,
        agent_id=model.agent_id,
        loop_id=model.loop_id,
        request_identity_digest=model.request_identity_digest,
        operation_identity_digest=model.operation_identity_digest,
        catalog_digest=model.catalog_digest,
        frozen_bounds=ModelToolLoopFrozenBounds.model_validate(model.frozen_bounds_json),
        cumulative_usage=ModelToolLoopCumulativeUsage.model_validate(model.cumulative_usage_json),
        state=ModelToolLoopState.model_validate(model.state_json),
        owner_lease_digest=model.owner_lease_digest,
        owner_fence=model.owner_fence,
        owner_lease_expires_at=_as_utc(model.owner_lease_expires_at),
        status=cast(
            Literal[
                "active",
                "waiting_approval",
                "completed",
                "failed",
                "cancelled",
                "needs_review",
            ],
            model.status,
        ),
        next_turn_ordinal=model.next_turn_ordinal,
        result_ref=model.result_ref,
        error_ref=model.error_ref,
        version=model.version,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _as_utc(value: datetime) -> datetime:
    """统一SQLite丢失的时区标记与PostgreSQL aware时间，保持租约逐值比较。"""

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["require_model_tool_loop_active"]
