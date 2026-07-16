"""Delegation 原子 claim、预算预约与幂等重放 repository mixin。"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._delegation_records import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationStorageConflict,
)
from agent_harness.storage._delegation_records import (
    delegation_event_id as _delegation_event_id,
)
from agent_harness.storage._delegation_records import (
    delegation_event_result as _delegation_event_result,
)
from agent_harness.storage._delegation_records import (
    delegation_group_id as _delegation_group_id,
)
from agent_harness.storage._delegation_records import (
    delegation_record as _delegation_record,
)
from agent_harness.storage._delegation_records import (
    replay_integrity_valid as _replay_integrity_valid,
)
from agent_harness.storage._delegation_records import (
    reservation_cost_impact as _reservation_cost_impact,
)
from agent_harness.storage._delegation_records import (
    reservation_record as _reservation_record,
)
from agent_harness.storage._delegation_records import (
    reservation_token_impact as _reservation_token_impact,
)
from agent_harness.storage.delegation_models import (
    AgentDelegationModel,
    DelegationBudgetReservationModel,
)
from agent_harness.storage.event_capacity_repositories import (
    EVIDENCE_OPERATION_REGISTRY_VERSION,
    EventCapacityRepository,
    EvidenceOperationKind,
)
from agent_harness.storage.evidence_repositories import EvidenceOutboxRepository
from agent_harness.storage.models import AgentRunModel, RunEvidenceOutboxModel


class DelegationClaimRepositoryMixin:
    _session: AsyncSession

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


__all__ = ["DelegationClaimRepositoryMixin"]
