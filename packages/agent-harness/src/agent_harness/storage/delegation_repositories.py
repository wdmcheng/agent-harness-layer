"""Delegation 幂等 claim、parent 预算与 event capacity 原子 repository。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.delegation_models import (
    AgentDelegationModel,
    DelegationAggregateModel,
    DelegationBudgetReservationModel,
)
from agent_harness.storage.event_capacity_repositories import (
    EVIDENCE_OPERATION_REGISTRY_VERSION,
    EventCapacityRepository,
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.storage.evidence_repositories import EvidenceOutboxRepository
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


class DelegationRepository:
    """调用方在 parent lock 内使用；PostgreSQL 额外锁 parent row。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_and_reserve(self, data: DelegationClaimCreate) -> DelegationClaimResult:
        existing = await self._get_model_by_key(
            tenant_id=data.tenant_id,
            parent_run_id=data.parent_run_id,
            idempotency_key=data.idempotency_key,
        )
        if existing is not None:
            return await self._replay(existing, data=data)

        parent = await self._session.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.id == data.parent_run_id,
                AgentRunModel.tenant_id == data.tenant_id,
            )
            .with_for_update()
        )
        if parent is None or parent.agent_id != data.source_agent_id:
            raise DelegationStorageConflict("delegation.parent_not_found")
        if parent.trace_id != data.trace_id:
            raise DelegationStorageConflict("delegation.parent_trace_conflict")
        if parent.status in {"completed", "failed", "cancelled"}:
            raise DelegationStorageConflict("delegation.execution_failed")

        # parent lock 取得后必须复查；并发同 key 可能刚提交首次 claim。
        existing = await self._get_model_by_key(
            tenant_id=data.tenant_id,
            parent_run_id=data.parent_run_id,
            idempotency_key=data.idempotency_key,
        )
        if existing is not None:
            return await self._replay(existing, data=data)

        reservations = list(
            await self._session.scalars(
                select(DelegationBudgetReservationModel).where(
                    DelegationBudgetReservationModel.tenant_id == data.tenant_id,
                    DelegationBudgetReservationModel.parent_run_id == data.parent_run_id,
                    DelegationBudgetReservationModel.state.in_(
                        ("reserved", "settled", "needs_review")
                    ),
                )
            )
        )
        direct_tokens, direct_cost = await self._parent_direct_usage(
            parent=parent,
            require_cost=data.parent_cost_limit is not None,
        )
        used_tokens = direct_tokens + sum(_reservation_token_impact(item) for item in reservations)
        remaining_tokens = max(data.parent_token_limit - used_tokens, 0)
        # child runtime 没有“缩量后强制执行”的 seam，因此 reservation 必须覆盖
        # descriptor 声明的单次最坏 token 预算；不足时在 claim 写入前整体拒绝。
        if data.requested_token_reservation > remaining_tokens:
            raise DelegationBudgetExceeded("delegation.budget_exceeded")
        effective_tokens = data.requested_token_reservation
        # parent 无限时仍必须保留 target 的有限 ceiling；只有两端都无限时
        # reservation 才能是 null。配置收紧后，旧 null active reservation
        # 会在下面的 impact 计算中 fail closed，不能被当成零。
        effective_cost = data.requested_cost_reservation
        if data.parent_cost_limit is not None:
            used_cost = direct_cost + sum(_reservation_cost_impact(item) for item in reservations)
            remaining_cost = max(data.parent_cost_limit - used_cost, 0.0)
            requested_cost = data.requested_cost_reservation
            # `None` 表示 target 没有成本上限，而不是“继承当前剩余值”。在没有
            # child 执行层尚无成本 fencing，不能把无限 ceiling 静默缩成有限预约。
            if requested_cost is None or requested_cost > remaining_cost:
                raise DelegationBudgetExceeded("delegation.budget_exceeded")
            effective_cost = requested_cost

        delegation = AgentDelegationModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            parent_run_id=data.parent_run_id,
            source_agent_id=data.source_agent_id,
            target_agent_id=data.target_agent_id,
            idempotency_key=data.idempotency_key,
            request_hash=data.request_hash,
            budget_intent=data.budget_intent,
            child_input_json=data.child_input,
            identity_json=data.identity,
            trace_id=data.trace_id,
            request_id=data.request_id,
            status="claimed",
            event_operation_kind=EvidenceOperationKind.DELEGATION.value,
            event_registry_version=EVIDENCE_OPERATION_REGISTRY_VERSION,
            reserved_event_count=1,
        )
        reservation = DelegationBudgetReservationModel(
            id=str(uuid4()),
            delegation_id=delegation.id,
            tenant_id=data.tenant_id,
            parent_run_id=data.parent_run_id,
            reserved_tokens=effective_tokens,
            reserved_cost_usd=effective_cost,
            state="reserved",
        )
        self._session.add_all((delegation, reservation))
        try:
            await self._session.flush()
            delegation.reserved_event_count = await EventCapacityRepository(self._session).reserve(
                run_id=data.parent_run_id,
                operation_kind=EvidenceOperationKind.DELEGATION,
            )
            await EvidenceOutboxRepository(self._session).stage_ordered_group(
                tenant_id=data.tenant_id,
                run_id=data.parent_run_id,
                group_id=_delegation_group_id(delegation.id),
                items=[
                    {
                        "event_id": _delegation_event_id(delegation.id, "claimed"),
                        "operation_kind": EvidenceOperationKind.DELEGATION.value,
                        "sequence_in_group": 1,
                        "reserved_event_count": 1,
                        "result": _delegation_event_result(delegation),
                    },
                    {
                        "event_id": _delegation_event_id(delegation.id, "child"),
                        "operation_kind": EvidenceOperationKind.DELEGATION.value,
                        "sequence_in_group": 2,
                        "reserved_event_count": 1,
                        "result": _delegation_event_result(delegation),
                    },
                    {
                        "event_id": _delegation_event_id(delegation.id, "final"),
                        "operation_kind": EvidenceOperationKind.DELEGATION.value,
                        "sequence_in_group": 3,
                        "reserved_event_count": 1,
                        "result": _delegation_event_result(delegation),
                    },
                ],
            )
            await self._session.flush()
            # event reservation 会再次 UPDATE delegation，server-managed updated_at
            # 因此过期；在 repository 边界内显式刷新，禁止 DTO 转换触发异步懒加载。
            await self._session.refresh(delegation)
            await self._session.refresh(reservation)
        except IntegrityError as exc:
            raise DelegationStorageConflict("delegation.idempotency_conflict") from exc
        return DelegationClaimResult(
            delegation=_delegation_record(delegation),
            reservation=_reservation_record(reservation),
            created=True,
        )

    async def _parent_direct_usage(
        self,
        *,
        parent: AgentRunModel,
        require_cost: bool,
    ) -> tuple[int, float]:
        """在 parent lock 内汇总直接 usage；未知/非法值使新预算 fail closed。"""

        # 局部导入避免 storage adapter 初始化时经 models package 回到 EventBus。
        from agent_harness.models.usage import ModelUsageEvidence

        rows = list(
            await self._session.scalars(
                select(RunEvidenceOutboxModel).where(
                    RunEvidenceOutboxModel.run_id == parent.id,
                    RunEvidenceOutboxModel.operation_kind.in_(
                        (
                            EvidenceOperationKind.MODEL_USAGE.value,
                            EvidenceOperationKind.EMBEDDING_USAGE.value,
                        )
                    ),
                )
            )
        )
        total_tokens = 0
        total_cost = 0.0
        for row in rows:
            result = row.result_json
            if row.state != "published" or not isinstance(result, Mapping):
                raise DelegationBudgetExceeded("delegation.budget_exceeded")
            raw_evidence = result.get("evidence")
            if not isinstance(raw_evidence, Mapping):
                raise DelegationBudgetExceeded("delegation.budget_exceeded")
            try:
                evidence = ModelUsageEvidence.model_validate(raw_evidence)
            except ValueError as exc:
                raise DelegationBudgetExceeded("delegation.budget_exceeded") from exc
            if (
                evidence.tenant_id != parent.tenant_id
                or evidence.run_id != parent.id
                or evidence.agent_id != parent.agent_id
                or evidence.trace_id != parent.trace_id
                or evidence.input_tokens is None
                or evidence.output_tokens is None
            ):
                raise DelegationBudgetExceeded("delegation.budget_exceeded")
            total_tokens += evidence.input_tokens + evidence.output_tokens
            if require_cost:
                if evidence.cost_status == "unavailable" or evidence.cost_usd is None:
                    raise DelegationBudgetExceeded("delegation.budget_exceeded")
                total_cost += evidence.cost_usd
        return total_tokens, total_cost

    async def list_for_parent(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
    ) -> list[DelegationRecord]:
        models = list(
            await self._session.scalars(
                select(AgentDelegationModel)
                .where(
                    AgentDelegationModel.tenant_id == tenant_id,
                    AgentDelegationModel.parent_run_id == parent_run_id,
                )
                .order_by(AgentDelegationModel.created_at, AgentDelegationModel.id)
            )
        )
        return [_delegation_record(model) for model in models]

    async def list_summary_projection_for_parent(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
    ) -> list[DelegationSummaryProjectionRecord]:
        """固定一次联表读取 RUN-002 relation truth，避免逐 child 往返与口径漂移。"""

        rows = list(
            (
                await self._session.execute(
                    select(
                        AgentDelegationModel,
                        AgentRunModel,
                        DelegationBudgetReservationModel,
                        DelegationAggregateModel,
                    )
                    .outerjoin(
                        AgentRunModel,
                        AgentRunModel.id == AgentDelegationModel.child_run_id,
                    )
                    .outerjoin(
                        DelegationBudgetReservationModel,
                        DelegationBudgetReservationModel.delegation_id == AgentDelegationModel.id,
                    )
                    .outerjoin(
                        DelegationAggregateModel,
                        DelegationAggregateModel.delegation_id == AgentDelegationModel.id,
                    )
                    .where(
                        AgentDelegationModel.tenant_id == tenant_id,
                        AgentDelegationModel.parent_run_id == parent_run_id,
                    )
                    .order_by(AgentDelegationModel.created_at, AgentDelegationModel.id)
                )
            ).all()
        )
        return [
            DelegationSummaryProjectionRecord(
                delegation=_delegation_record(delegation),
                child=None if child is None else _child_run_record(child),
                reservation=(None if reservation is None else _reservation_record(reservation)),
                aggregate=None if aggregate is None else _aggregate_record(aggregate),
            )
            for delegation, child, reservation, aggregate in rows
        ]

    async def list_recovery_candidates_for_parent(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
    ) -> list[DelegationRecoveryCandidate]:
        """固定两次查询找出 claim/child/final 可重放项，不按 child 做 N+1。"""

        pending_rows = list(
            await self._session.scalars(
                select(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.tenant_id == tenant_id,
                    RunEvidenceOutboxModel.run_id == parent_run_id,
                    RunEvidenceOutboxModel.operation_kind == EvidenceOperationKind.DELEGATION.value,
                    RunEvidenceOutboxModel.state == "result_persisted",
                )
                .order_by(
                    RunEvidenceOutboxModel.created_at,
                    RunEvidenceOutboxModel.sequence_in_group,
                    RunEvidenceOutboxModel.id,
                )
            )
        )
        phases_by_delegation: dict[str, list[str]] = {}
        for row in pending_rows:
            result = row.result_json
            if not isinstance(result, Mapping):
                raise DelegationStorageConflict("delegation.execution_failed")
            delegation_id = result.get("delegation_id")
            if (
                not isinstance(delegation_id, str)
                or result.get("parent_run_id") != parent_run_id
                or row.group_id != _delegation_group_id(delegation_id)
            ):
                raise DelegationStorageConflict("delegation.execution_failed")
            phase_prefix = f"delegation:{delegation_id}:"
            if not row.event_id.startswith(phase_prefix):
                raise DelegationStorageConflict("delegation.execution_failed")
            phase = row.event_id.removeprefix(phase_prefix)
            if phase not in {"claimed", "child", "final"}:
                raise DelegationStorageConflict("delegation.execution_failed")
            phases_by_delegation.setdefault(delegation_id, []).append(phase)
        if not phases_by_delegation:
            return []

        models = list(
            await self._session.scalars(
                select(AgentDelegationModel)
                .where(
                    AgentDelegationModel.tenant_id == tenant_id,
                    AgentDelegationModel.parent_run_id == parent_run_id,
                    AgentDelegationModel.id.in_(phases_by_delegation),
                )
                .order_by(AgentDelegationModel.created_at, AgentDelegationModel.id)
            )
        )
        if {model.id for model in models} != set(phases_by_delegation):
            raise DelegationStorageConflict("delegation.execution_failed")
        candidates: list[DelegationRecoveryCandidate] = []
        for model in models:
            phases = phases_by_delegation[model.id]
            if (
                model.child_run_id is None
                or "claimed" in phases
                or "child" in phases
                or ("final" in phases and model.status in {"completed", "failed"})
            ):
                candidates.append(
                    DelegationRecoveryCandidate(
                        delegation=_delegation_record(model),
                        pending_phases=phases,
                    )
                )
        return candidates

    async def get(self, delegation_id: str) -> DelegationRecord | None:
        model = await self._session.get(AgentDelegationModel, delegation_id)
        return None if model is None else _delegation_record(model)

    async def get_by_child(self, child_run_id: str) -> DelegationRecord | None:
        model = await self._session.scalar(
            select(AgentDelegationModel).where(AgentDelegationModel.child_run_id == child_run_id)
        )
        return None if model is None else _delegation_record(model)

    async def get_reservation(
        self,
        delegation_id: str,
    ) -> DelegationBudgetReservationRecord:
        model = await self._session.scalar(
            select(DelegationBudgetReservationModel).where(
                DelegationBudgetReservationModel.delegation_id == delegation_id
            )
        )
        if model is None:
            raise LookupError("delegation reservation not found")
        return _reservation_record(model)

    async def attach_child(self, *, delegation_id: str, child_run_id: str) -> DelegationRecord:
        delegation = await self._session.scalar(
            select(AgentDelegationModel)
            .where(AgentDelegationModel.id == delegation_id)
            .with_for_update()
        )
        child = await self._session.get(AgentRunModel, child_run_id)
        if delegation is None or child is None:
            raise DelegationStorageConflict("delegation.execution_failed")
        if delegation.child_run_id is not None and delegation.child_run_id != child_run_id:
            raise DelegationStorageConflict("delegation.idempotency_conflict")
        already_attached = delegation.child_run_id == child_run_id
        if (
            child.tenant_id != delegation.tenant_id
            or child.parent_run_id != delegation.parent_run_id
            or child.agent_id != delegation.target_agent_id
            or child.trace_id != delegation.trace_id
            or child.idempotency_key != f"delegation:{delegation.id}"
        ):
            raise DelegationStorageConflict("delegation.execution_failed")
        if already_attached:
            return _delegation_record(delegation)
        delegation.child_run_id = child.id
        delegation.status = _delegation_status_from_run(child.status)
        await EvidenceOutboxRepository(self._session).update_group_result(
            group_id=_delegation_group_id(delegation.id),
            result=_delegation_event_result(delegation, child_run_id=child.id),
        )
        await self._session.flush()
        await self._session.refresh(delegation)
        return _delegation_record(delegation)

    async def release_pre_child_failure(self, *, delegation_id: str) -> DelegationRecord:
        """在能证明 child 未创建时，幂等释放预算与未使用的 child-event 预约。"""

        delegation = await self._session.scalar(
            select(AgentDelegationModel)
            .where(AgentDelegationModel.id == delegation_id)
            .with_for_update()
        )
        reservation = await self._session.scalar(
            select(DelegationBudgetReservationModel)
            .where(DelegationBudgetReservationModel.delegation_id == delegation_id)
            .with_for_update()
        )
        if delegation is None or reservation is None or delegation.child_run_id is not None:
            raise DelegationStorageConflict("delegation.execution_failed")
        if reservation.state == "released" and delegation.status == "failed":
            return _delegation_record(delegation)
        if reservation.state != "reserved" or delegation.status != "claimed":
            raise DelegationStorageConflict("delegation.execution_failed")
        group_id = _delegation_group_id(delegation.id)
        group = await EvidenceOutboxRepository(self._session).ordered_group(group_id=group_id)
        if len(group) != 3:
            raise DelegationStorageConflict("delegation.execution_failed")
        child_event = group[1]
        if child_event.state != "result_persisted" or child_event.reserved_event_count != 1:
            raise DelegationStorageConflict("delegation.execution_failed")
        # child 尚未创建时不会产生 child-created 事件；以取消终态结清该预约，
        # 不能伪装成已发布，也不能继续阻断 parent terminal。
        child_event.state = "cancelled"
        await EventCapacityRepository(self._session).settle(
            run_id=delegation.parent_run_id,
            reserved_event_count=child_event.reserved_event_count,
            consumed=0,
        )
        reservation.state = "released"
        delegation.status = "failed"
        delegation.error_json = {"code": "delegation.execution_failed"}
        await EvidenceOutboxRepository(self._session).update_group_result(
            group_id=group_id,
            result=_delegation_event_result(delegation),
        )
        await self._session.flush()
        await self._session.refresh(delegation)
        return _delegation_record(delegation)

    async def save_aggregation(
        self,
        *,
        delegation_id: str,
        summary: dict[str, Any],
        evidence_refs: list[str],
        needs_review: bool,
    ) -> DelegationAggregateRecord:
        delegation = await self._session.scalar(
            select(AgentDelegationModel)
            .where(AgentDelegationModel.id == delegation_id)
            .with_for_update()
        )
        reservation = await self._session.scalar(
            select(DelegationBudgetReservationModel)
            .where(DelegationBudgetReservationModel.delegation_id == delegation_id)
            .with_for_update()
        )
        if delegation is None or reservation is None or delegation.child_run_id is None:
            raise DelegationStorageConflict("delegation.execution_failed")
        child = await self._session.scalar(
            select(AgentRunModel)
            .where(AgentRunModel.id == delegation.child_run_id)
            .with_for_update()
        )
        if (
            child is None
            or child.tenant_id != delegation.tenant_id
            or child.parent_run_id != delegation.parent_run_id
            or child.agent_id != delegation.target_agent_id
            or child.trace_id != delegation.trace_id
            or child.idempotency_key != f"delegation:{delegation.id}"
            or reservation.delegation_id != delegation.id
            or reservation.tenant_id != delegation.tenant_id
            or reservation.parent_run_id != delegation.parent_run_id
        ):
            raise DelegationStorageConflict("delegation.execution_failed")
        aggregate = await self._session.scalar(
            select(DelegationAggregateModel).where(
                DelegationAggregateModel.delegation_id == delegation_id
            )
        )
        aggregate_status = "needs_review" if needs_review else "complete"
        if (
            aggregate is not None
            and aggregate.status == aggregate_status
            and aggregate.summary_json == summary
            and aggregate.evidence_refs_json == evidence_refs
        ):
            # final event 已发布后 ordered group 不再可写；相同可信聚合直接重放，
            # 避免 worker redelivery 把已完成 evidence 当成待处理状态。
            return _aggregate_record(aggregate)
        if child.status in {"failed", "cancelled"}:
            delegation.error_json = {"code": "delegation.execution_failed"}
        if aggregate is None:
            aggregate = DelegationAggregateModel(
                id=str(uuid4()),
                delegation_id=delegation.id,
                tenant_id=delegation.tenant_id,
                parent_run_id=delegation.parent_run_id,
                child_run_id=delegation.child_run_id,
                status=aggregate_status,
                summary_json=summary,
                evidence_refs_json=evidence_refs,
            )
            self._session.add(aggregate)
        else:
            aggregate.status = aggregate_status
            aggregate.summary_json = summary
            aggregate.evidence_refs_json = evidence_refs
        if needs_review:
            reservation.state = "needs_review"
            delegation.status = "needs_review"
        else:
            input_tokens = summary.get("input_tokens")
            output_tokens = summary.get("output_tokens")
            cost_usd = summary.get("cost_usd")
            if (
                not isinstance(input_tokens, int)
                or isinstance(input_tokens, bool)
                or input_tokens < 0
            ):
                raise ValueError("complete delegation summary requires input tokens")
            if (
                not isinstance(output_tokens, int)
                or isinstance(output_tokens, bool)
                or output_tokens < 0
            ):
                raise ValueError("complete delegation summary requires output tokens")
            if (
                isinstance(cost_usd, bool)
                or not isinstance(cost_usd, int | float)
                or not math.isfinite(cost_usd)
                or cost_usd < 0
            ):
                raise ValueError("complete delegation summary requires finite cost")
            reservation.settled_input_tokens = input_tokens
            reservation.settled_output_tokens = output_tokens
            reservation.settled_cost_usd = float(cost_usd)
            reservation.state = "settled"
            delegation.status = _delegation_status_from_run(child.status)
        await EvidenceOutboxRepository(self._session).update_group_result(
            group_id=_delegation_group_id(delegation.id),
            result={
                **_delegation_event_result(delegation, child_run_id=delegation.child_run_id),
                "summary": summary,
                "aggregate_status": aggregate_status,
            },
        )
        await self._session.flush()
        await self._session.refresh(aggregate)
        return _aggregate_record(aggregate)

    async def list_aggregates_for_parent(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
    ) -> list[DelegationAggregateRecord]:
        models = list(
            await self._session.scalars(
                select(DelegationAggregateModel)
                .where(
                    DelegationAggregateModel.tenant_id == tenant_id,
                    DelegationAggregateModel.parent_run_id == parent_run_id,
                )
                .order_by(DelegationAggregateModel.created_at, DelegationAggregateModel.id)
            )
        )
        return [_aggregate_record(model) for model in models]

    async def usage_evidence_for_child(
        self,
        child_run_id: str,
    ) -> list[DelegationUsageEvidenceRecord]:
        grouped = await self.usage_evidence_for_children(child_run_ids=[child_run_id])
        return grouped.get(child_run_id, [])

    async def usage_evidence_for_children(
        self,
        *,
        child_run_ids: list[str],
    ) -> dict[str, list[DelegationUsageEvidenceRecord]]:
        """批量读取 RUN-002 对账所需 usage outbox，避免逐 child 查询。"""

        if not child_run_ids:
            return {}
        models = list(
            await self._session.scalars(
                select(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.run_id.in_(child_run_ids),
                    RunEvidenceOutboxModel.operation_kind.in_(
                        (
                            EvidenceOperationKind.MODEL_USAGE.value,
                            EvidenceOperationKind.EMBEDDING_USAGE.value,
                        )
                    ),
                )
                .order_by(
                    RunEvidenceOutboxModel.run_id,
                    RunEvidenceOutboxModel.created_at,
                    RunEvidenceOutboxModel.id,
                )
            )
        )
        grouped: dict[str, list[DelegationUsageEvidenceRecord]] = {}
        for model in models:
            grouped.setdefault(model.run_id, []).append(
                DelegationUsageEvidenceRecord(
                    event_id=model.event_id,
                    operation_kind=model.operation_kind,
                    state=model.state,
                    reserved_event_count=model.reserved_event_count,
                    result=model.result_json,
                )
            )
        return grouped

    async def _replay(
        self,
        model: AgentDelegationModel,
        *,
        data: DelegationClaimCreate,
    ) -> DelegationClaimResult:
        locked = await self._session.scalar(
            select(AgentDelegationModel)
            .where(AgentDelegationModel.id == model.id)
            .with_for_update()
        )
        if locked is None:
            raise DelegationStorageConflict("delegation.execution_failed")
        if locked.request_hash != data.request_hash:
            raise DelegationStorageConflict("delegation.idempotency_conflict")
        reservation = await self._session.scalar(
            select(DelegationBudgetReservationModel)
            .where(DelegationBudgetReservationModel.delegation_id == locked.id)
            .with_for_update()
        )
        if reservation is None:
            raise DelegationStorageConflict("delegation.reservation_missing")
        group = list(
            await self._session.scalars(
                select(RunEvidenceOutboxModel)
                .where(RunEvidenceOutboxModel.group_id == _delegation_group_id(locked.id))
                .order_by(RunEvidenceOutboxModel.sequence_in_group)
                .with_for_update()
            )
        )
        if not _replay_integrity_valid(
            model=locked,
            reservation=reservation,
            group=group,
            data=data,
        ):
            raise DelegationStorageConflict("delegation.execution_failed")
        return DelegationClaimResult(
            delegation=_delegation_record(locked),
            reservation=_reservation_record(reservation),
            created=False,
        )

    async def _get_model_by_key(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
        idempotency_key: str,
    ) -> AgentDelegationModel | None:
        return await self._session.scalar(
            select(AgentDelegationModel).where(
                AgentDelegationModel.tenant_id == tenant_id,
                AgentDelegationModel.parent_run_id == parent_run_id,
                AgentDelegationModel.idempotency_key == idempotency_key,
            )
        )


def _replay_integrity_valid(
    *,
    model: AgentDelegationModel,
    reservation: DelegationBudgetReservationModel,
    group: list[RunEvidenceOutboxModel],
    data: DelegationClaimCreate,
) -> bool:
    """重放前把首次 claim 的不可变语义与当前可信请求、配套状态完整对账。"""

    if (
        model.tenant_id != data.tenant_id
        or model.parent_run_id != data.parent_run_id
        or model.source_agent_id != data.source_agent_id
        or model.target_agent_id != data.target_agent_id
        or model.idempotency_key != data.idempotency_key
        or model.budget_intent != data.budget_intent
        or model.child_input_json != data.child_input
        or model.identity_json != data.identity
        or model.trace_id != data.trace_id
        or model.event_operation_kind != EvidenceOperationKind.DELEGATION.value
        or model.event_registry_version != EVIDENCE_OPERATION_REGISTRY_VERSION
        or model.reserved_event_count != operation_event_capacity(EvidenceOperationKind.DELEGATION)
        or reservation.delegation_id != model.id
        or reservation.tenant_id != data.tenant_id
        or reservation.parent_run_id != data.parent_run_id
        or len(group) != 3
    ):
        return False
    expected_group_id = _delegation_group_id(model.id)
    for row, phase, sequence in zip(
        group,
        ("claimed", "child", "final"),
        (1, 2, 3),
        strict=True,
    ):
        result = row.result_json
        if (
            row.tenant_id != data.tenant_id
            or row.run_id != data.parent_run_id
            or row.group_id != expected_group_id
            or row.event_id != _delegation_event_id(model.id, phase)
            or row.operation_kind != EvidenceOperationKind.DELEGATION.value
            or row.sequence_in_group != sequence
            or row.reserved_event_count != 1
            or not isinstance(result, Mapping)
            or result.get("delegation_id") != model.id
            or result.get("parent_run_id") != data.parent_run_id
            or result.get("source_agent_id") != data.source_agent_id
            or result.get("target_agent_id") != data.target_agent_id
            or result.get("trace_id") != data.trace_id
        ):
            return False
    return True


def _reservation_token_impact(model: DelegationBudgetReservationModel) -> int:
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


def _reservation_cost_impact(model: DelegationBudgetReservationModel) -> float:
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


def _delegation_group_id(delegation_id: str) -> str:
    return f"delegation:{delegation_id}:evidence"


def _delegation_event_id(delegation_id: str, phase: str) -> str:
    return f"delegation:{delegation_id}:{phase}"


def _delegation_event_result(
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


def _delegation_status_from_run(run_status: str) -> str:
    if run_status == "completed":
        return "completed"
    if run_status in {"failed", "cancelled"}:
        return "failed"
    if run_status == "running":
        return "running"
    return "queued"


def _child_run_record(model: AgentRunModel) -> DelegatedChildRunRecord:
    return DelegatedChildRunRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        parent_run_id=model.parent_run_id,
        agent_id=model.agent_id,
        status=model.status,
        trace_id=model.trace_id,
        idempotency_key=model.idempotency_key,
    )


def _delegation_record(model: AgentDelegationModel) -> DelegationRecord:
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


def _reservation_record(
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


def _aggregate_record(model: DelegationAggregateModel) -> DelegationAggregateRecord:
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
    "DelegationRepository",
    "DelegationStorageConflict",
    "DelegationStorageError",
    "DelegationSummaryProjectionRecord",
    "DelegationUsageEvidenceRecord",
]
