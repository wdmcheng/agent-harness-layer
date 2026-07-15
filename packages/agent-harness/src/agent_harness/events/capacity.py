"""Canonical usage event 与容量账本之间的可信绑定。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from agent_harness.events.types import CanonicalEvent, CanonicalEventType


class LocalCapacityCommitUncertain(RuntimeError):
    """SQLite commit 结果无法确认；调用方必须保留 durable JSONL 等待重放。"""


@dataclass(frozen=True)
class UsageCapacitySettlement:
    tenant_id: str
    run_id: str
    usage_call_id: str | None
    event_id: str
    operation_kind: str
    state: str
    reserved_event_count: int
    result_json: dict[str, Any] | None
    error_code: str | None


@dataclass(frozen=True)
class UsageCapacityBinding:
    usage_call_id: str
    operation_kind: str
    started_event_id: str
    final_event_id: str
    phase: str


def usage_capacity_binding(event: CanonicalEvent) -> UsageCapacityBinding | None:
    """解析 usage event 的稳定调用身份；形状不完整时在扣预约前拒绝。"""

    phase_by_type = {
        CanonicalEventType.MODEL_REQUEST_STARTED: "started",
        CanonicalEventType.MODEL_USAGE_UPDATED: "final",
    }
    phase = phase_by_type.get(event.event_type)
    if phase is None:
        return None
    payload = event.payload
    if not isinstance(payload, dict):
        raise ValueError("usage event requires a payload")
    correlation = payload.get("correlation")
    usage = payload.get("usage")
    if not isinstance(correlation, dict) or not isinstance(usage, dict):
        raise ValueError("usage event requires correlation and usage payloads")
    correlation_payload = cast(Mapping[str, object], correlation)
    usage_payload = cast(Mapping[str, object], usage)
    usage_call_id = correlation_payload.get("usage_call_id")
    if not isinstance(usage_call_id, str) or not usage_call_id:
        raise ValueError("usage event requires a stable usage_call_id")
    usage_kind = usage_payload.get("usage_kind")
    if usage_kind == "model":
        operation_kind = "model_usage"
    elif usage_kind == "embedding":
        operation_kind = "embedding_usage"
    else:
        raise ValueError("usage event requires a supported usage_kind")
    expected_event_id = f"usage:{event.tenant_id}:{usage_call_id}:{phase}"
    if event.event_id != expected_event_id:
        raise ValueError("usage event id does not match its stable call identity")
    return UsageCapacityBinding(
        usage_call_id=usage_call_id,
        operation_kind=operation_kind,
        started_event_id=f"usage:{event.tenant_id}:{usage_call_id}:started",
        final_event_id=f"usage:{event.tenant_id}:{usage_call_id}:final",
        phase=phase,
    )


def validate_usage_capacity_outbox(
    *,
    event: CanonicalEvent,
    binding: UsageCapacityBinding,
    outbox: UsageCapacitySettlement | None,
    expected_reserved_event_count: int,
) -> int:
    """逐值证明 usage event 属于 durable settlement 后只消费一格。"""

    if outbox is None:
        raise LookupError("usage settlement not found")
    if (
        outbox.tenant_id != event.tenant_id
        or outbox.run_id != event.run_id
        or outbox.usage_call_id != binding.usage_call_id
        or outbox.event_id != binding.final_event_id
        or outbox.operation_kind != binding.operation_kind
        or outbox.reserved_event_count != expected_reserved_event_count
    ):
        raise ValueError("usage event does not match its durable settlement")
    allowed_states = {"started", "result_persisted", "published"}
    if outbox.state not in allowed_states:
        raise RuntimeError("usage settlement state is invalid")
    result = outbox.result_json
    started_payload = result.get("started") if isinstance(result, dict) else None
    if not isinstance(started_payload, Mapping):
        raise RuntimeError("usage settlement is missing its durable started identity")
    # 局部 import 避免 events -> models -> events 的模块环。
    from agent_harness.models.usage import ModelUsageEvidence

    started = ModelUsageEvidence.model_validate(started_payload)
    expected_usage_kind = "model" if binding.operation_kind == "model_usage" else "embedding"
    if (
        started.usage_kind != expected_usage_kind
        or started.tenant_id != event.tenant_id
        or started.run_id != event.run_id
        or started.agent_id != event.agent_id
        or started.request_id != event.request_id
        or started.trace_id != event.trace_id
    ):
        raise ValueError("usage event scope does not match its durable settlement")
    correlation = {"usage_call_id": binding.usage_call_id}
    if binding.phase == "started":
        expected_payload = {
            "correlation": correlation,
            "usage": {
                "usage_kind": started.usage_kind,
                "provider": started.provider,
                "model": started.model,
                "decision": started.decision,
            },
        }
        if event.payload != expected_payload:
            raise ValueError("usage started payload does not match its durable settlement")
        return 1
    if binding.phase == "final" and outbox.state == "started":
        raise RuntimeError("usage final event requires a persisted result")
    final_payload = result.get("evidence") if isinstance(result, dict) else None
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if not isinstance(final_payload, Mapping) or not isinstance(outcome, str) or not outcome:
        raise RuntimeError("usage settlement is missing its durable final result")
    final = ModelUsageEvidence.model_validate(final_payload)
    expected_payload = {
        "correlation": correlation,
        "usage": final.to_payload(),
        "outcome": outcome,
    }
    if outbox.error_code is not None:
        expected_payload["error_code"] = outbox.error_code
    if event.payload != expected_payload:
        raise ValueError("usage final payload does not match its durable settlement")
    return 1


__all__ = [
    "LocalCapacityCommitUncertain",
    "UsageCapacityBinding",
    "UsageCapacitySettlement",
    "usage_capacity_binding",
    "validate_usage_capacity_outbox",
]
