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


@dataclass(frozen=True)
class StreamCapacitySettlement:
    """stream outbox 行在 sink 事务中的最小可信快照。"""

    tenant_id: str
    run_id: str
    event_id: str
    operation_kind: str
    state: str
    reserved_event_count: int
    group_id: str | None
    sequence_in_group: int | None
    result_json: dict[str, Any] | None


@dataclass(frozen=True)
class StreamCapacityBinding:
    """从公开 delta/completed 提取的稳定 outbox 关联。"""

    usage_call_id: str
    group_id: str
    sequence_in_group: int


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
    decision = usage_payload.get("decision")
    marker_present = isinstance(decision, Mapping) and "usage_event_identity" in decision
    decision_payload = (
        cast(Mapping[str, object], decision) if isinstance(decision, Mapping) else None
    )
    marker = decision_payload.get("usage_event_identity") if decision_payload is not None else None
    if marker_present:
        if usage_kind != "model" or marker != {"ref": "stream-usage", "version": "v1"}:
            raise ValueError("stream usage identity marker is invalid")
        # 局部 import 避免 events/storage 在模块初始化阶段形成依赖环。
        from agent_harness.storage.stream_evidence_repositories import (
            stream_usage_event_id,
        )

        started_event_id = stream_usage_event_id(usage_call_id, "started")
        final_event_id = stream_usage_event_id(usage_call_id, "final")
    else:
        started_event_id = f"usage:{event.tenant_id}:{usage_call_id}:started"
        final_event_id = f"usage:{event.tenant_id}:{usage_call_id}:final"
    # event-id 是容量预约的唯一凭据；stream 仅由 durable marker 选择新版本，
    # 缺 marker 的历史行继续使用 tenant-scoped legacy identity。
    expected_event_id = started_event_id if phase == "started" else final_event_id
    if event.event_id != expected_event_id:
        raise ValueError("usage event id does not match its stable call identity")
    return UsageCapacityBinding(
        usage_call_id=usage_call_id,
        operation_kind=operation_kind,
        started_event_id=started_event_id,
        final_event_id=final_event_id,
        phase=phase,
    )


def stream_capacity_binding(event: CanonicalEvent) -> StreamCapacityBinding | None:
    """解析普通文本流事件；其他 CanonicalEvent 不进入 stream 预约分支。"""

    if event.event_type not in {
        CanonicalEventType.MODEL_OUTPUT_DELTA,
        CanonicalEventType.MODEL_OUTPUT_COMPLETED,
    }:
        return None
    payload = event.payload
    payload_mapping = cast(Mapping[str, object], payload) if isinstance(payload, Mapping) else None
    correlation = payload_mapping.get("correlation") if payload_mapping is not None else None
    correlation_mapping = (
        cast(Mapping[str, object], correlation) if isinstance(correlation, Mapping) else None
    )
    usage_call_id = (
        correlation_mapping.get("usage_call_id") if correlation_mapping is not None else None
    )
    if not isinstance(usage_call_id, str):
        raise ValueError("stream event requires a stable usage_call_id")
    from agent_harness.storage.stream_evidence_repositories import (
        stream_completed_event_id,
        stream_delta_event_id,
        stream_group_id,
    )

    if event.event_type == CanonicalEventType.MODEL_OUTPUT_DELTA:
        ordinal = payload.get("chunk_ordinal") if isinstance(payload, Mapping) else None
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise ValueError("stream delta requires an integer chunk_ordinal")
        expected_event_id = stream_delta_event_id(usage_call_id, ordinal)
        sequence = ordinal
    else:
        expected_event_id = stream_completed_event_id(usage_call_id)
        sequence = 65
    if event.event_id != expected_event_id:
        raise ValueError("stream event id does not match its stable call identity")
    return StreamCapacityBinding(
        usage_call_id=usage_call_id,
        group_id=stream_group_id(usage_call_id),
        sequence_in_group=sequence,
    )


def validate_stream_capacity_outbox(
    *,
    event: CanonicalEvent,
    binding: StreamCapacityBinding,
    outbox: StreamCapacitySettlement | None,
) -> int:
    """逐值核对 stream identity、顺序和已固化 intent，再授权消费一个槽位。"""

    if outbox is None:
        raise LookupError("stream evidence placeholder not found")
    if (
        outbox.tenant_id != event.tenant_id
        or outbox.run_id != event.run_id
        or outbox.event_id != event.event_id
        or outbox.operation_kind != "model_stream"
        or outbox.reserved_event_count != 1
        or outbox.group_id != binding.group_id
        or outbox.sequence_in_group != binding.sequence_in_group
    ):
        raise ValueError("stream event does not match its durable placeholder")
    if outbox.state not in {"result_persisted", "published"}:
        raise RuntimeError("stream event result is not durable")
    result = outbox.result_json
    result_mapping = cast(Mapping[str, object], result) if isinstance(result, Mapping) else None
    stream = result_mapping.get("stream") if result_mapping is not None else None
    intent = result_mapping.get("event") if result_mapping is not None else None
    stream_mapping = cast(Mapping[str, object], stream) if isinstance(stream, Mapping) else None
    expected_kind = "delta" if binding.sequence_in_group <= 64 else "completed"
    if stream_mapping is None or (
        stream_mapping.get("usage_call_id") != binding.usage_call_id
        or stream_mapping.get("kind") != expected_kind
        or stream_mapping.get("ordinal") != binding.sequence_in_group
    ):
        raise ValueError("stream event does not match its durable binding")
    if not isinstance(intent, Mapping):
        raise RuntimeError("stream event is missing its durable intent")
    intent_mapping = cast(Mapping[str, object], intent)
    current = event.to_payload()
    stable_fields = {
        "event_id",
        "tenant_id",
        "run_id",
        "user_id",
        "agent_id",
        "parent_run_id",
        "event_type",
        "event_version",
        "payload",
        "payload_ref",
        "payload_checksum",
        "raw_event_ref",
        "terminal",
        "visibility",
        "request_id",
        "trace_id",
        "record_scope",
        "span_id",
    }
    if any(intent_mapping.get(field) != current.get(field) for field in stable_fields):
        raise ValueError("event does not match durable stream intent")
    return 1


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
    from agent_harness.models.usage import ModelUsageEvidence, usage_event_correlation

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
    if binding.phase == "started":
        correlation = usage_event_correlation(
            started,
            usage_call_id=binding.usage_call_id,
        )
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
    correlation = usage_event_correlation(
        final,
        usage_call_id=binding.usage_call_id,
    )
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
    "StreamCapacityBinding",
    "StreamCapacitySettlement",
    "stream_capacity_binding",
    "validate_stream_capacity_outbox",
    "usage_capacity_binding",
    "validate_usage_capacity_outbox",
]
