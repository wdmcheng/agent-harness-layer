"""Delegation 原子 claim、预算预约与幂等重放 repository mixin。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._delegation_records import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationReplayIdentitySeed,
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
    EventSequenceStateInvalid,
    EvidenceOperationKind,
)
from agent_harness.storage.evidence_repositories import EvidenceOutboxRepository
from agent_harness.storage.models import AgentRunModel, RunEvidenceOutboxModel
from agent_harness.storage.shared_budget import OperationIdentity
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    ParentBudgetLedgerModel,
)
from agent_harness.storage.shared_budget_repositories import SharedBudgetRepository


class DelegationClaimRepositoryMixin:
    _session: AsyncSession

    async def claim_and_reserve(self, data: DelegationClaimCreate) -> DelegationClaimResult:
        existing = await self._get_model_by_key(
            tenant_id=data.tenant_id,
            parent_run_id=data.parent_run_id,
            idempotency_key=data.idempotency_key,
        )
        if existing is not None:
            return await self._replay(
                existing,
                expected_request_hash=data.request_hash,
                expected_identity=data.budget_identity,
                validate_request_hash=False,
            )

        shared_budget = SharedBudgetRepository(self._session)
        shared_ledger = await shared_budget.get_ledger(data.tenant_id, data.parent_run_id)
        if shared_ledger is None:
            # 0016 writer 只允许对已有 immutable tree snapshot 的 active root
            # 建立新 delegation。严格 terminal 的 legacy_closed tree 不会再进入
            # 本入口；活动 tree 缺 ledger 必须在 child/queue/evidence 前 fail closed。
            raise DelegationStorageConflict("delegation.execution_failed")
        # direct/allocation/delegation 的新 claim 必须统一先锁共享账本。
        # 若 delegation 先锁 parent run，direct claim 插入时的外键锁会与
        # ledger 锁形成反向等待，导致 PostgreSQL deadlock。
        (
            effective_tokens,
            effective_cost_decimal,
            ledger_state,
        ) = await shared_budget.delegation_reservation(
            tenant_id=data.tenant_id,
            budget_owner_run_id=data.parent_run_id,
            source_agent_id=data.source_agent_id,
            target_agent_id=data.target_agent_id,
        )
        effective_cost = None if effective_cost_decimal is None else float(effective_cost_decimal)

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
            return await self._replay(
                existing,
                expected_request_hash=data.request_hash,
                expected_identity=data.budget_identity,
                validate_request_hash=False,
            )

        legacy_reservations = list(
            await self._session.scalars(
                select(DelegationBudgetReservationModel)
                .where(
                    DelegationBudgetReservationModel.tenant_id == data.tenant_id,
                    DelegationBudgetReservationModel.parent_run_id == data.parent_run_id,
                )
                .with_for_update()
            )
        )
        for legacy_reservation in legacy_reservations:
            # 0016 ledger 是预算真相源，但仍要拒绝与其并存的损坏 0015
            # reservation；否则兼容投影会让恢复与审计看到互相矛盾的状态。
            _reservation_token_impact(legacy_reservation)
            if shared_ledger.cost_limit is not None:
                _reservation_cost_impact(legacy_reservation)
        try:
            await EventCapacityRepository(self._session).assert_sequence_state_valid(
                tenant_id=data.tenant_id,
                run_id=data.parent_run_id,
            )
        except EventSequenceStateInvalid as exc:
            raise DelegationStorageConflict(EventSequenceStateInvalid.code) from exc
        if ledger_state == "needs_review":
            # 序列完整性高于 hard-budget eligibility；锁序仍保持 ledger → parent。
            raise DelegationBudgetExceeded("delegation.budget_exceeded")

        delegation = AgentDelegationModel(
            id=data.delegation_id,
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
            await shared_budget.reserve_delegation(
                tenant_id=data.tenant_id,
                budget_owner_run_id=data.parent_run_id,
                delegation_id=delegation.id,
                request_hash=data.request_hash,
                identity=data.budget_identity,
                token_reservation=effective_tokens,
                cost_reservation=effective_cost_decimal,
            )
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
        expected_request_hash: str,
        expected_identity: OperationIdentity | None,
        validate_request_hash: bool,
    ) -> DelegationClaimResult:
        locked = await self._session.scalar(
            select(AgentDelegationModel)
            .where(AgentDelegationModel.id == model.id)
            .with_for_update()
        )
        if locked is None:
            raise DelegationStorageConflict("delegation.execution_failed")
        if locked.request_hash != expected_request_hash:
            raise DelegationStorageConflict("delegation.idempotency_conflict")
        shared_budget = SharedBudgetRepository(self._session)
        shared_claim = await self._session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.delegation_id == locked.id
            )
        )
        if shared_claim is None:
            managed_ledger = await self._session.scalar(
                select(ParentBudgetLedgerModel.budget_owner_run_id).where(
                    ParentBudgetLedgerModel.tenant_id == locked.tenant_id,
                    ParentBudgetLedgerModel.budget_owner_run_id == locked.parent_run_id,
                )
            )
            if managed_ledger is not None:
                raise DelegationStorageConflict("delegation.execution_failed")
        if shared_claim is not None:
            if shared_claim.request_hash != expected_request_hash:
                raise DelegationStorageConflict("delegation.execution_failed")
            # immutable identity 冲突的优先级高于 reservation/outbox 完整性错误。
            # 这里只比较首次持久化的顶层身份；完整 durable 关系仍在后面统一校验。
            if expected_identity is not None and (
                shared_claim.identity_schema_version != expected_identity.identity_schema_version
                or shared_claim.identity_hash != expected_identity.identity_hash
                or shared_claim.identity_json != expected_identity.to_payload()
            ):
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
            expected_request_hash=expected_request_hash,
            validate_request_hash=validate_request_hash,
        ):
            raise DelegationStorageConflict("delegation.execution_failed")
        if shared_claim is not None:
            cost_reservation = (
                None
                if reservation.reserved_cost_usd is None
                else Decimal(str(reservation.reserved_cost_usd))
            )
            if (
                shared_claim.request_hash != expected_request_hash
                or shared_claim.reserved_tokens != reservation.reserved_tokens
                or shared_claim.reserved_cost != cost_reservation
            ):
                raise DelegationStorageConflict("delegation.execution_failed")
            if (
                expected_identity is not None
                and not await shared_budget.delegation_exact_replay_matches(
                    tenant_id=locked.tenant_id,
                    budget_owner_run_id=locked.parent_run_id,
                    delegation_id=locked.id,
                    request_hash=expected_request_hash,
                    identity=expected_identity,
                    token_reservation=reservation.reserved_tokens,
                    cost_reservation=cost_reservation,
                )
            ):
                raise DelegationStorageConflict("delegation.idempotency_conflict")
        return DelegationClaimResult(
            delegation=_delegation_record(locked),
            reservation=_reservation_record(reservation),
            created=False,
        )

    async def replay_existing(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
        idempotency_key: str,
        request_hash: str,
        expected_identity: OperationIdentity | None,
    ) -> DelegationClaimResult | None:
        """在当前授权、余额和容量之前解析 stable-key exact replay/conflict。"""

        existing = await self._get_model_by_key(
            tenant_id=tenant_id,
            parent_run_id=parent_run_id,
            idempotency_key=idempotency_key,
        )
        if existing is None:
            return None
        return await self._replay(
            existing,
            expected_request_hash=request_hash,
            expected_identity=expected_identity,
            validate_request_hash=True,
        )

    async def replay_identity_seed(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> DelegationReplayIdentitySeed | None:
        """只解析 stable key/hash 和 relation ID，后续再校验完整 durable 状态。

        0016 budget identity 依赖 relation ID，因此 service 需要先取得该 ID。
        这里不得提前读取 reservation/outbox；否则其完整性损坏会覆盖更高优先级的
        immutable identity conflict。
        """

        existing = await self._get_model_by_key(
            tenant_id=tenant_id,
            parent_run_id=parent_run_id,
            idempotency_key=idempotency_key,
        )
        if existing is None:
            return None
        locked = await self._session.scalar(
            select(AgentDelegationModel)
            .where(AgentDelegationModel.id == existing.id)
            .with_for_update()
        )
        if locked is None:
            raise DelegationStorageConflict("delegation.execution_failed")
        if locked.request_hash != request_hash:
            raise DelegationStorageConflict("delegation.idempotency_conflict")
        if (
            locked.tenant_id != tenant_id
            or locked.parent_run_id != parent_run_id
            or locked.idempotency_key != idempotency_key
        ):
            raise DelegationStorageConflict("delegation.execution_failed")
        shared_budget = SharedBudgetRepository(self._session)
        budget_identity = await shared_budget.delegation_replay_identity_seed(
            tenant_id=tenant_id,
            budget_owner_run_id=parent_run_id,
            delegation_id=locked.id,
            request_hash=request_hash,
        )
        return DelegationReplayIdentitySeed(
            delegation=_delegation_record(locked),
            budget_identity=budget_identity,
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
