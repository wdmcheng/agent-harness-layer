"""Model/embedding usage 的 started/final CanonicalEvent 生命周期。"""

from __future__ import annotations

from typing import Any

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus
from agent_harness.models.usage import ModelUsageEvidence


class UsageEvidenceLifecycle:
    """在 provider 副作用前建立可重放的调用级 evidence 生命周期。"""

    def __init__(
        self,
        *,
        event_bus: EventBus,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
    ) -> None:
        """保存本次调用的受信 evidence 与稳定关联标识。

        `usage_call_id` 在 provider 副作用前由 composition 生成；这里仅验证
        其非空并复用它生成 event id，避免重试时产生第二组 started/final 事件。
        """

        if not usage_call_id:
            raise ValueError("usage call id must not be empty")
        self._event_bus = event_bus
        self._evidence = evidence
        self.usage_call_id = usage_call_id

    @property
    def correlation(self) -> dict[str, str]:
        """返回写入 CanonicalEvent payload 的最小调用关联对象。"""

        return {"usage_call_id": self.usage_call_id}

    async def publish_started(self) -> CanonicalEvent:
        """在 provider 副作用前发布 started；重试复用稳定 event id。"""

        return await self._event_bus.publish(
            tenant_id=self._evidence.tenant_id,
            run_id=self._evidence.run_id,
            agent_id=self._evidence.agent_id,
            event_type=CanonicalEventType.MODEL_REQUEST_STARTED,
            payload={
                "correlation": self.correlation,
                "usage": {
                    "usage_kind": self._evidence.usage_kind,
                    "provider": self._evidence.provider,
                    "model": self._evidence.model,
                    "decision": self._evidence.decision,
                },
            },
            request_id=self._evidence.request_id,
            trace_id=self._evidence.trace_id,
            event_id=self._event_id("started"),
        )

    async def publish_final(
        self,
        *,
        outcome: str = "completed",
        error_code: str | None = None,
    ) -> CanonicalEvent:
        """发布恰好一条调用级最终 usage，terminal 永远为 false。"""

        outcome_payload: dict[str, Any] = {"outcome": outcome}
        if error_code is not None:
            outcome_payload["error_code"] = error_code
        return await self._event_bus.publish(
            tenant_id=self._evidence.tenant_id,
            run_id=self._evidence.run_id,
            agent_id=self._evidence.agent_id,
            event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
            payload={
                "correlation": self.correlation,
                "usage": self._evidence.to_payload(),
                **outcome_payload,
            },
            request_id=self._evidence.request_id,
            trace_id=self._evidence.trace_id,
            event_id=self._event_id("final"),
        )

    def _event_id(self, phase: str) -> str:
        """按调用身份和生命周期标识构造可重放的稳定事件标识。"""

        return f"usage:{self._evidence.tenant_id}:{self.usage_call_id}:{phase}"


__all__ = ["UsageEvidenceLifecycle"]
