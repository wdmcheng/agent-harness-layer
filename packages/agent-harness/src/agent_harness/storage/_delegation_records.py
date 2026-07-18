"""Delegation storage DTO、错误与持久化映射不变量。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from pydantic import Field, field_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.delegation_models import (
    AgentDelegationModel,
    DelegationAggregateModel,
    DelegationBudgetReservationModel,
)
from agent_harness.storage.event_capacity_repositories import (
    EVIDENCE_OPERATION_REGISTRY_VERSION,
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.storage.models import AgentRunModel, RunEvidenceOutboxModel


class DelegationStorageError(RuntimeError):
    """只暴露封闭 delegation 错误码，不回显租户或预算内部值。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DelegationStorageConflict(DelegationStorageError):
    pass


class DelegationBudgetExceeded(DelegationStorageError):
    pass


class DelegationClaimCreate(HarnessDTO):
    tenant_id: str = Field(min_length=1)
    parent_run_id: str = Field(min_length=1)
    source_agent_id: str = Field(min_length=1)
    target_agent_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    request_hash: str = Field(min_length=64, max_length=64)
    budget_intent: str = Field(min_length=1)
    child_input: dict[str, Any]
    identity: dict[str, Any]
    trace_id: str = Field(min_length=1)
    request_id: str | None = None
    parent_token_limit: int
    requested_token_reservation: int
    parent_cost_limit: float | None
    requested_cost_reservation: float | None

    @field_validator("parent_token_limit", "requested_token_reservation", mode="before")
    @classmethod
    def validate_token_budget(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("delegation token budget must be a non-negative integer")
        return value

    @field_validator("parent_cost_limit", "requested_cost_reservation", mode="before")
    @classmethod
    def validate_cost_budget(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("delegation cost budget must be numeric or null")
        if not math.isfinite(value) or value < 0:
            raise ValueError("delegation cost budget must be finite and non-negative")
        return value


class DelegationRecord(HarnessDTO):
    id: str
    tenant_id: str
    parent_run_id: str
    child_run_id: str | None
    source_agent_id: str
    target_agent_id: str
    idempotency_key: str
    request_hash: str
    budget_intent: str
    child_input: dict[str, Any]
    identity: dict[str, Any]
    trace_id: str
    request_id: str | None
    status: str
    error_code: str | None
    reserved_event_count: int
    created_at: datetime
    updated_at: datetime


class DelegationBudgetReservationRecord(HarnessDTO):
    id: str
    delegation_id: str
    tenant_id: str
    parent_run_id: str
    reserved_tokens: int
    reserved_cost_usd: float | None
    settled_input_tokens: int | None
    settled_output_tokens: int | None
    settled_cost_usd: float | None
    state: str
    created_at: datetime
    updated_at: datetime


class DelegationClaimResult(HarnessDTO):
    delegation: DelegationRecord
    reservation: DelegationBudgetReservationRecord
    created: bool


class DelegationAggregateRecord(HarnessDTO):
    id: str
    delegation_id: str
    tenant_id: str
    parent_run_id: str
    child_run_id: str
    status: str
    summary: dict[str, Any]
    evidence_refs: list[str]
    created_at: datetime
    updated_at: datetime


class DelegatedChildRunRecord(HarnessDTO):
    """RUN-002 汇总只需要的 durable child 生命周期投影。"""

    id: str
    tenant_id: str
    parent_run_id: str | None
    agent_id: str
    status: str
    trace_id: str
    idempotency_key: str | None


class DelegationSummaryProjectionRecord(HarnessDTO):
    """一条 relation 对应的 child、预算与可选聚合一致性投影。"""

    delegation: DelegationRecord
    child: DelegatedChildRunRecord | None
    reservation: DelegationBudgetReservationRecord | None
    aggregate: DelegationAggregateRecord | None


class DelegationRecoveryCandidate(HarnessDTO):
    """存在可推进 pending event 的 durable delegation operation。"""

    delegation: DelegationRecord
    pending_phases: list[str]


class DelegationUsageEvidenceRecord(HarnessDTO):
    """跨 UoW 返回的可信 usage 快照，禁止泄漏会过期的 ORM 实例。"""

    event_id: str
    operation_kind: str
    state: str
    reserved_event_count: int
    result: dict[str, Any] | None


def _durable_request_hash(model: AgentDelegationModel) -> str | None:
    """在 storage 层重算 service request hash，避免经 delegation package 形成循环导入。"""

    raw_identity: object = cast(object, model.identity_json)
    if not isinstance(raw_identity, Mapping):
        return None
    identity = cast(Mapping[str, object], raw_identity)
    if (
        identity.get("tenant_id") != model.tenant_id
        or not isinstance(identity.get("roles"), list)
        or not isinstance(identity.get("permissions"), list)
    ):
        return None
    roles = cast(list[object], identity["roles"])
    permissions = cast(list[object], identity["permissions"])
    if any(not isinstance(value, str) for value in roles) or any(
        not isinstance(value, str) for value in permissions
    ):
        return None
    payload = {
        "tenant_id": model.tenant_id,
        "identity": {
            "user_id": identity.get("user_id"),
            "session_id": identity.get("session_id"),
            "roles": sorted(cast(list[str], roles)),
            "permissions": sorted(cast(list[str], permissions)),
            "auth_method": identity.get("auth_method"),
        },
        "parent_run_id": model.parent_run_id,
        "source_agent_id": model.source_agent_id,
        "target_agent_id": model.target_agent_id,
        "child_input": model.child_input_json,
        "budget_intent": model.budget_intent,
    }
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replay_integrity_valid(
    *,
    model: AgentDelegationModel,
    reservation: DelegationBudgetReservationModel,
    group: list[RunEvidenceOutboxModel],
    expected_request_hash: str,
    validate_request_hash: bool,
) -> bool:
    """重放前把首次 claim 的不可变语义与当前可信请求、配套状态完整对账。"""

    durable_hash = model.request_hash
    if validate_request_hash:
        durable_hash = _durable_request_hash(model)
        if durable_hash is None:
            return False

    if (
        model.request_hash != expected_request_hash
        or model.request_hash != durable_hash
        or model.event_operation_kind != EvidenceOperationKind.DELEGATION.value
        or model.event_registry_version != EVIDENCE_OPERATION_REGISTRY_VERSION
        or model.reserved_event_count != operation_event_capacity(EvidenceOperationKind.DELEGATION)
        or reservation.delegation_id != model.id
        or reservation.tenant_id != model.tenant_id
        or reservation.parent_run_id != model.parent_run_id
        or len(group) != 3
    ):
        return False
    expected_group_id = delegation_group_id(model.id)
    for row, phase, sequence in zip(
        group,
        ("claimed", "child", "final"),
        (1, 2, 3),
        strict=True,
    ):
        result = row.result_json
        if (
            row.tenant_id != model.tenant_id
            or row.run_id != model.parent_run_id
            or row.group_id != expected_group_id
            or row.event_id != delegation_event_id(model.id, phase)
            or row.operation_kind != EvidenceOperationKind.DELEGATION.value
            or row.sequence_in_group != sequence
            or row.reserved_event_count != 1
            or not isinstance(result, Mapping)
            or result.get("delegation_id") != model.id
            or result.get("parent_run_id") != model.parent_run_id
            or result.get("source_agent_id") != model.source_agent_id
            or result.get("target_agent_id") != model.target_agent_id
            or result.get("trace_id") != model.trace_id
        ):
            return False
    return True


def reservation_token_impact(model: DelegationBudgetReservationModel) -> int:
    if model.state == "released":
        return 0
    if model.state == "settled":
        input_tokens = model.settled_input_tokens
        output_tokens = model.settled_output_tokens
        if (
            not isinstance(input_tokens, int)
            or isinstance(input_tokens, bool)
            or input_tokens < 0
            or not isinstance(output_tokens, int)
            or isinstance(output_tokens, bool)
            or output_tokens < 0
        ):
            raise DelegationBudgetExceeded("delegation.budget_exceeded")
        return input_tokens + output_tokens
    if model.state not in {"reserved", "needs_review"} or model.reserved_tokens < 0:
        raise DelegationBudgetExceeded("delegation.budget_exceeded")
    return model.reserved_tokens


def reservation_cost_impact(model: DelegationBudgetReservationModel) -> float:
    if model.state == "released":
        return 0.0
    if model.state not in {"reserved", "settled", "needs_review"}:
        raise DelegationBudgetExceeded("delegation.budget_exceeded")
    value = model.settled_cost_usd if model.state == "settled" else model.reserved_cost_usd
    if (
        value is None
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise DelegationBudgetExceeded("delegation.budget_exceeded")
    return float(value)


def delegation_group_id(delegation_id: str) -> str:
    return f"delegation:{delegation_id}:evidence"


def delegation_event_id(delegation_id: str, phase: str) -> str:
    return f"delegation:{delegation_id}:{phase}"


def delegation_event_result(
    model: AgentDelegationModel,
    *,
    child_run_id: str | None = None,
) -> dict[str, Any]:
    return {
        "delegation_id": model.id,
        "parent_run_id": model.parent_run_id,
        "child_run_id": child_run_id if child_run_id is not None else model.child_run_id,
        "source_agent_id": model.source_agent_id,
        "target_agent_id": model.target_agent_id,
        "status": model.status,
        "trace_id": model.trace_id,
    }


def delegation_status_from_run(run_status: str) -> str:
    if run_status == "completed":
        return "completed"
    if run_status in {"failed", "cancelled"}:
        return "failed"
    if run_status == "running":
        return "running"
    return "queued"


def child_run_record(model: AgentRunModel) -> DelegatedChildRunRecord:
    return DelegatedChildRunRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        parent_run_id=model.parent_run_id,
        agent_id=model.agent_id,
        status=model.status,
        trace_id=model.trace_id,
        idempotency_key=model.idempotency_key,
    )


def delegation_record(model: AgentDelegationModel) -> DelegationRecord:
    return DelegationRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        parent_run_id=model.parent_run_id,
        child_run_id=model.child_run_id,
        source_agent_id=model.source_agent_id,
        target_agent_id=model.target_agent_id,
        idempotency_key=model.idempotency_key,
        request_hash=model.request_hash,
        budget_intent=model.budget_intent,
        child_input=model.child_input_json,
        identity=model.identity_json,
        trace_id=model.trace_id,
        request_id=model.request_id,
        status=model.status,
        error_code=(
            str(model.error_json["code"])
            if isinstance(model.error_json, dict) and isinstance(model.error_json.get("code"), str)
            else None
        ),
        reserved_event_count=model.reserved_event_count,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def reservation_record(
    model: DelegationBudgetReservationModel,
) -> DelegationBudgetReservationRecord:
    return DelegationBudgetReservationRecord(
        id=model.id,
        delegation_id=model.delegation_id,
        tenant_id=model.tenant_id,
        parent_run_id=model.parent_run_id,
        reserved_tokens=model.reserved_tokens,
        reserved_cost_usd=(
            None if model.reserved_cost_usd is None else float(model.reserved_cost_usd)
        ),
        settled_input_tokens=model.settled_input_tokens,
        settled_output_tokens=model.settled_output_tokens,
        settled_cost_usd=(
            None if model.settled_cost_usd is None else float(model.settled_cost_usd)
        ),
        state=model.state,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def aggregate_record(model: DelegationAggregateModel) -> DelegationAggregateRecord:
    return DelegationAggregateRecord(
        id=model.id,
        delegation_id=model.delegation_id,
        tenant_id=model.tenant_id,
        parent_run_id=model.parent_run_id,
        child_run_id=model.child_run_id,
        status=model.status,
        summary=model.summary_json,
        evidence_refs=model.evidence_refs_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


__all__ = [
    "DelegatedChildRunRecord",
    "DelegationAggregateRecord",
    "DelegationBudgetExceeded",
    "DelegationBudgetReservationRecord",
    "DelegationClaimCreate",
    "DelegationClaimResult",
    "DelegationRecord",
    "DelegationRecoveryCandidate",
    "DelegationStorageConflict",
    "DelegationStorageError",
    "DelegationSummaryProjectionRecord",
    "DelegationUsageEvidenceRecord",
    "aggregate_record",
    "child_run_record",
    "delegation_event_id",
    "delegation_event_result",
    "delegation_group_id",
    "delegation_record",
    "delegation_status_from_run",
    "replay_integrity_valid",
    "reservation_cost_impact",
    "reservation_record",
    "reservation_token_impact",
]
