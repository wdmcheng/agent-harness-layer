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
    """持久化 usage outbox 的容量结算快照。

    该值对象把数据库行与事件校验解耦，只保留验证某条 usage 事件是否有权
    消费已预约容量所需的字段。`result_json` 仍是受控 outbox 的原始结构，
    由调用方在同一事务快照中读取，避免把不可信事件正文当作结算依据。
    """

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
    """从 CanonicalEvent 提取出的稳定 usage 调用关联。

    `started_event_id` 与 `final_event_id` 是同一调用仅有的两个可写事件标识；
    生命周期标识只用于选择当前事件的校验分支，不能由外部 payload 覆盖。
    """

    usage_call_id: str
    operation_kind: str
    started_event_id: str
    final_event_id: str
    # 此既有公开值对象字段表示 usage evidence 的业务生命周期分支；不能为词面
    # 统一而破坏调用方的解构或序列化兼容性。
    phase: str


def usage_capacity_binding(event: CanonicalEvent) -> UsageCapacityBinding | None:
    """解析 usage 事件的稳定调用关联，拒绝不完整形状。

    非 usage 事件返回 ``None``，让通用 sink 继续走自己的容量路径；两种
    usage 事件则必须同时证明 event type、调用身份和稳定 event id 一致，
    以免伪造正文消耗另一次调用的预约。
    """

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
    # event-id 是容量预约的唯一凭据；只接受由 tenant、调用标识和受控
    # 生命周期标识组成的值，不能让调用方借用其他调用已预约的容量。
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
    """逐值证明 usage 事件属于 durable settlement，并返回应消费的容量数。

    校验先绑定 tenant/run/调用身份和预约数，再重建受信的 started/final
    evidence 以比较事件正文。这样 sink 只在 outbox 已证明的调用上结算，
    不会因重放、篡改或跨 run 事件而错误减少 outstanding reservation。
    """

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
