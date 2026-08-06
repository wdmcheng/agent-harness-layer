"""0018模型工具循环evidence marker的单一写入owner。"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import ModelToolLoopSchemaMarkerModel

MODEL_TOOL_LOOP_MARKER_KEY = "model-tool-loop-v1"


class ModelToolLoopSchemaMarkerRecord(HarnessDTO):
    """公开只读marker投影；不提供reset或delete输入。"""

    marker_key: str
    evidence_seen: bool


async def mark_model_tool_loop_evidence_seen(session: AsyncSession) -> None:
    """在调用方UoW内把marker单调提升为true，缺行或冲突均失败关闭。"""

    result = cast(
        CursorResult[Any],
        await session.execute(
            update(ModelToolLoopSchemaMarkerModel)
            .where(
                ModelToolLoopSchemaMarkerModel.marker_key == MODEL_TOOL_LOOP_MARKER_KEY,
                ModelToolLoopSchemaMarkerModel.evidence_seen.is_(False),
            )
            .values(evidence_seen=True)
        ),
    )
    if result.rowcount not in {0, 1}:
        raise RuntimeError("storage.model_tool_loop_schema_marker_conflict")
    marker = await session.scalar(
        select(ModelToolLoopSchemaMarkerModel).where(
            ModelToolLoopSchemaMarkerModel.marker_key == MODEL_TOOL_LOOP_MARKER_KEY
        )
    )
    if marker is None or marker.evidence_seen is not True:
        raise RuntimeError("storage.model_tool_loop_schema_marker_missing")


class ModelToolLoopSchemaMarkerRepository:
    """只暴露读取与false→true提升，不暴露清零或删除能力。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self) -> ModelToolLoopSchemaMarkerRecord:
        """读取exact marker；缺失时拒绝让runtime猜测schema状态。"""

        marker = await self._session.scalar(
            select(ModelToolLoopSchemaMarkerModel).where(
                ModelToolLoopSchemaMarkerModel.marker_key == MODEL_TOOL_LOOP_MARKER_KEY
            )
        )
        if marker is None:
            raise RuntimeError("storage.model_tool_loop_schema_marker_missing")
        return ModelToolLoopSchemaMarkerRecord(
            marker_key=marker.marker_key,
            evidence_seen=marker.evidence_seen,
        )

    async def mark_evidence_seen(self) -> ModelToolLoopSchemaMarkerRecord:
        """幂等提升marker并返回同一事务内的当前值。"""

        await mark_model_tool_loop_evidence_seen(self._session)
        return await self.get()


__all__ = [
    "MODEL_TOOL_LOOP_MARKER_KEY",
    "ModelToolLoopSchemaMarkerRecord",
    "ModelToolLoopSchemaMarkerRepository",
    "mark_model_tool_loop_evidence_seen",
]
