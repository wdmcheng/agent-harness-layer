"""Delegation durable evidence、预算与重放 payload 纯函数。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from agent_harness.delegation._service_types import DelegationError
from agent_harness.delegation.models import (
    DelegationChildEvidence,
    DelegationSummary,
)
from agent_harness.models.usage import ModelUsageEvidence
from agent_harness.storage.delegation_repositories import (
    DelegatedChildRunRecord,
    DelegationRecord,
    DelegationUsageEvidenceRecord,
)
from agent_harness.storage.event_capacity_repositories import (
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.storage.repositories import RunRecord


def required_child_id(delegation: DelegationRecord) -> str:
    if delegation.child_run_id is None:
        raise DelegationError("delegation.execution_failed")
    return delegation.child_run_id


def delegation_id_from_child_key(value: str) -> str | None:
    prefix = "delegation:"
    return value[len(prefix) :] if value.startswith(prefix) and len(value) > len(prefix) else None


def published_child_payload(
    *,
    delegation: DelegationRecord,
    result: Mapping[str, object],
) -> dict[str, Any]:
    """从未再更新的 published outbox row 恢复 child-created 原始语义。"""

    child_run_id = result.get("child_run_id")
    status = result.get("status")
    if (
        result.get("delegation_id") != delegation.id
        or result.get("parent_run_id") != delegation.parent_run_id
        or child_run_id != delegation.child_run_id
        or result.get("source_agent_id") != delegation.source_agent_id
        or result.get("target_agent_id") != delegation.target_agent_id
        or result.get("trace_id") != delegation.trace_id
        or not isinstance(child_run_id, str)
        or status not in {"queued", "running", "completed", "failed"}
    ):
        raise DelegationError("delegation.execution_failed")
    return {"status": status, "child_run_id": child_run_id}


def child_evidence(
    *,
    child: RunRecord | DelegatedChildRunRecord,
    rows: list[DelegationUsageEvidenceRecord],
) -> DelegationChildEvidence:
    evidence: list[ModelUsageEvidence] = []
    has_pending = False
    for row in rows:
        try:
            operation_kind = EvidenceOperationKind(row.operation_kind)
        except ValueError as exc:
            raise ValueError("delegation usage evidence operation mismatch") from exc
        if operation_kind not in {
            EvidenceOperationKind.MODEL_USAGE,
            EvidenceOperationKind.EMBEDDING_USAGE,
        } or row.reserved_event_count != operation_event_capacity(operation_kind):
            raise ValueError("delegation usage evidence reservation mismatch")
        if row.state != "published":
            has_pending = True
            continue
        result = row.result
        if not isinstance(result, Mapping) or "evidence" not in result:
            raise ValueError("published delegation usage evidence is incomplete")
        evidence.append(
            ModelUsageEvidence.model_validate(cast(Mapping[str, object], result)["evidence"])
        )
    if not evidence:
        return unknown_child_evidence(child)
    if any(
        item.run_id != child.id
        or item.tenant_id != child.tenant_id
        or item.agent_id != child.agent_id
        or item.trace_id != child.trace_id
        for item in evidence
    ):
        raise ValueError("delegation usage evidence scope mismatch")
    # Cache hit 的 null token/cost 表示 provider usage 不适用，而不是未知。
    # Delegation 聚合必须在这个 evidence 归一化边界把它转换成已知零值；
    # 否则后续 summary 会错误保留整笔 parent reservation 并触发 needs_review。
    cache_hits = [_is_known_zero_cache_hit(item) for item in evidence]
    input_values = [
        0 if cache_hit else item.input_tokens
        for item, cache_hit in zip(evidence, cache_hits, strict=True)
    ]
    output_values = [
        0 if cache_hit else item.output_tokens
        for item, cache_hit in zip(evidence, cache_hits, strict=True)
    ]
    input_complete = not has_pending and all(value is not None for value in input_values)
    output_complete = not has_pending and all(value is not None for value in output_values)
    all_cost = not has_pending and all(
        cache_hit or item.cost_status != "unavailable"
        for item, cache_hit in zip(evidence, cache_hits, strict=True)
    )
    return DelegationChildEvidence(
        run_id=child.id,
        agent_id=child.agent_id,
        status=child.status,
        input_tokens=known_sum(input_values),
        output_tokens=known_sum(output_values),
        input_tokens_complete=input_complete,
        output_tokens_complete=output_complete,
        cost_usd=sum(item.cost_usd or 0 for item in evidence) if all_cost else None,
        cost_status=(
            "estimated"
            if all_cost and any(item.cost_status == "estimated" for item in evidence)
            else "reported"
            if all_cost
            else "unavailable"
        ),
        latency_ms=None if has_pending else sum(item.latency_ms for item in evidence),
        usage_evidence_refs=[row.event_id for row in rows],
        trace_refs=[child.trace_id],
    )


def _is_known_zero_cache_hit(evidence: ModelUsageEvidence) -> bool:
    """识别 DTO 已验证的 cache-hit 语义，供 delegation 聚合映射为已知零。"""

    return (
        evidence.decision.get("cache_status") == "hit"
        and evidence.decision.get("provider_called") is False
    )


def unknown_child_evidence(
    child: RunRecord | DelegatedChildRunRecord,
) -> DelegationChildEvidence:
    return DelegationChildEvidence(
        run_id=child.id,
        agent_id=child.agent_id,
        status=child.status,
        input_tokens=None,
        output_tokens=None,
        input_tokens_complete=False,
        output_tokens_complete=False,
        cost_usd=None,
        cost_status="unavailable",
        latency_ms=None,
        usage_evidence_refs=[],
        trace_refs=[child.trace_id],
    )


def known_sum(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def budget_exceeded(summary: DelegationSummary, reservation: Any) -> bool:
    tokens = (summary.input_tokens or 0) + (summary.output_tokens or 0)
    if tokens > reservation.reserved_tokens:
        return True
    return bool(
        summary.cost_usd is not None
        and reservation.reserved_cost_usd is not None
        and summary.cost_usd > reservation.reserved_cost_usd
    )


def aggregate_reservation_consistent(
    *,
    summary: DelegationSummary,
    aggregate_status: str,
    reservation: Any,
    cost_enabled: bool = True,
) -> bool:
    """聚合状态与预算结算必须来自同一次 durable 状态转换。"""

    if aggregate_status == "needs_review":
        return reservation.state == "needs_review" and summary.budget_status == "incomplete"
    if aggregate_status != "complete" or reservation.state != "settled":
        return False
    if summary.budget_status == "incomplete":
        return False
    return (
        reservation.settled_input_tokens == summary.input_tokens
        and reservation.settled_output_tokens == summary.output_tokens
        and (
            reservation.settled_cost_usd == summary.cost_usd
            if cost_enabled
            else reservation.settled_cost_usd == 0.0 and summary.cost_usd is None
        )
    )


__all__ = [
    "aggregate_reservation_consistent",
    "budget_exceeded",
    "child_evidence",
    "delegation_id_from_child_key",
    "published_child_payload",
    "required_child_id",
    "unknown_child_evidence",
]
