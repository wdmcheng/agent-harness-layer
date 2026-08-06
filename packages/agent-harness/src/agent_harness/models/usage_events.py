"""Model/embedding usage 的 started/final CanonicalEvent 生命周期。"""

from __future__ import annotations

from typing import Any

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus
from agent_harness.models.usage import ModelUsageEvidence, usage_event_correlation


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
        marker_present = "usage_event_identity" in evidence.decision
        marker = evidence.decision.get("usage_event_identity")
        expected_marker = {"ref": "stream-usage", "version": "v1"}
        if marker_present and marker != expected_marker:
            raise ValueError("stream usage identity marker is invalid")
        if marker_present:
            if evidence.usage_kind != "model":
                raise ValueError("stream usage identity marker only supports model usage")
            # 局部 import 避免 models 初始化时引入 storage -> models 循环。
            from agent_harness.storage.stream_evidence_repositories import (
                stream_usage_event_id,
            )

            stream_usage_event_id(usage_call_id, "started")
        correlation = usage_event_correlation(evidence, usage_call_id=usage_call_id)
        self._event_bus = event_bus
        self._evidence = evidence
        self.usage_call_id = usage_call_id
        self._stream_identity = marker_present
        self._correlation = correlation

    @property
    def correlation(self) -> dict[str, Any]:
        """返回写入 CanonicalEvent payload 的最小调用关联对象。"""

        return dict(self._correlation)

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

        if self._stream_identity:
            from agent_harness.storage.stream_evidence_repositories import (
                stream_usage_event_id,
            )

            return stream_usage_event_id(self.usage_call_id, phase)
        return f"usage:{self._evidence.tenant_id}:{self.usage_call_id}:{phase}"


__all__ = ["UsageEvidenceLifecycle"]
