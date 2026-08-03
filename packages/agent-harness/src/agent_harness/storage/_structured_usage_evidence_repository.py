"""Structured usage claim 的耐久 replay seed 绑定职责。"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.models import RunEvidenceOutboxModel


class StructuredUsageEvidenceRepositoryMixin:
    """在首次 started claim 上冻结 exact structured replay seed。"""

    _session: AsyncSession

    async def bind_structured_started_replay_seed(
        self,
        *,
        tenant_id: str,
        usage_call_id: str,
        replay_seed: Mapping[str, object],
    ) -> None:
        """只允许幂等重绑同一seed；身份漂移必须在事务内关闭失败。"""

        row = await self._session.scalar(
            select(RunEvidenceOutboxModel)
            .where(
                RunEvidenceOutboxModel.tenant_id == tenant_id,
                RunEvidenceOutboxModel.usage_call_id == usage_call_id,
            )
            .with_for_update()
        )
        if row is None or row.state != "started" or not isinstance(row.result_json, Mapping):
            raise ValueError("structured started replay seed requires a started usage claim")
        result = dict(row.result_json)
        normalized_seed = dict(replay_seed)
        existing = result.get("structured_replay_seed")
        if existing is not None and existing != normalized_seed:
            raise ValueError("structured started replay seed conflicts with durable identity")
        result["structured_replay_seed"] = normalized_seed
        row.result_json = result


__all__ = ["StructuredUsageEvidenceRepositoryMixin"]
