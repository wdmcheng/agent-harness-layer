"""Direct/allocation 对称的 route-chain 状态 CAS 与 reservation 原子转移。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._shared_budget_repository_records import (
    _allocation_record,
    _claim_record,
    _decimal,
)
from agent_harness.storage._shared_budget_route_chain_validation import (
    validate_route_state_mutation,
)
from agent_harness.storage.model_route_chain_state import ModelRouteChainState
from agent_harness.storage.shared_budget import (
    AllocationRecord,
    BudgetOperationConflict,
    BudgetOperationOwnership,
    BudgetReservationRejected,
    ClaimRecord,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
    ParentBudgetLedgerModel,
)


class _SharedBudgetRouteChainMixin:
    """所有 chain mutation 都锁 owner 与 operation row，并对 direct/allocation 对称处理。"""

    _session: AsyncSession

    if TYPE_CHECKING:

        async def resolve_operation_ownership(
            self,
            *,
            tenant_id: str,
            run_id: str,
        ) -> BudgetOperationOwnership: ...

        async def _lock_ledger(
            self,
            tenant_id: str,
            budget_owner_run_id: str,
            *,
            allow_needs_review: bool = False,
        ) -> ParentBudgetLedgerModel: ...

    async def get_model_route_chain_state(
        self,
        *,
        tenant_id: str,
        usage_call_id: str,
        run_id: str | None = None,
    ) -> ModelRouteChainState | None:
        """读取一笔 usage 的 durable chain state；损坏 JSON 直接关闭失败。"""

        if run_id is None:
            direct = await self._session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.tenant_id == tenant_id,
                    BudgetOperationClaimModel.operation_kind == "direct",
                    BudgetOperationClaimModel.usage_call_id == usage_call_id,
                )
            )
            allocation = await self._session.scalar(
                select(DelegationBudgetAllocationModel).where(
                    DelegationBudgetAllocationModel.tenant_id == tenant_id,
                    DelegationBudgetAllocationModel.usage_call_id == usage_call_id,
                )
            )
            if direct is not None and allocation is not None:
                raise BudgetOperationConflict
            model = direct or allocation
        else:
            ownership = await self.resolve_operation_ownership(tenant_id=tenant_id, run_id=run_id)
            model = await self._route_operation_model(
                tenant_id=tenant_id,
                budget_owner_run_id=ownership.budget_owner_run_id,
                delegation_id=ownership.delegation_id,
                usage_call_id=usage_call_id,
                for_update=False,
            )
        if model is None or model.route_chain_state_json is None:
            return None
        return ModelRouteChainState.model_validate(model.route_chain_state_json)

    async def has_waiting_model_route_chain_state(
        self,
        *,
        tenant_id: str,
        run_id: str,
    ) -> bool:
        """只读判断同一 execution 是否有等待审批的 chain，供缺失 checkpoint 关闭失败。"""

        ownership = await self.resolve_operation_ownership(tenant_id=tenant_id, run_id=run_id)
        if ownership.delegation_id is None:
            query = select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.tenant_id == tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == ownership.budget_owner_run_id,
                BudgetOperationClaimModel.operation_kind == "direct",
                BudgetOperationClaimModel.route_chain_state_json.is_not(None),
            )
        else:
            query = select(DelegationBudgetAllocationModel).where(
                DelegationBudgetAllocationModel.tenant_id == tenant_id,
                DelegationBudgetAllocationModel.budget_owner_run_id
                == ownership.budget_owner_run_id,
                DelegationBudgetAllocationModel.delegation_id == ownership.delegation_id,
                DelegationBudgetAllocationModel.route_chain_state_json.is_not(None),
            )
        rows = await self._session.scalars(query)
        return any(
            row.route_chain_state_json is not None
            and ModelRouteChainState.model_validate(
                row.route_chain_state_json
            ).waiting_approval_ordinal
            is not None
            for row in rows
        )

    async def append_model_route_attempt_started(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        state: ModelRouteChainState,
    ) -> ClaimRecord | AllocationRecord:
        """耐久追加一个 started identity；已存在同 attempt 只能 exact replay。"""

        return await self._store_route_chain_state(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
            state=state,
            mutation="attempt_started",
        )

    async def append_model_route_not_started_proof(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        state: ModelRouteChainState,
    ) -> ClaimRecord | AllocationRecord:
        """原子保存 proof，并把同一 lifecycle 从 started 关闭为 proven。"""

        return await self._store_route_chain_state(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
            state=state,
            mutation="proof",
        )

    async def transfer_model_route_reservation(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        state: ModelRouteChainState,
    ) -> ClaimRecord | AllocationRecord:
        """在 owner lock 内替换 reservation 与 active ordinal，不暴露中间释放窗口。"""

        return await self._store_route_chain_state(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
            state=state,
            mutation="transfer",
        )

    async def prove_and_transfer_model_route_reservation(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        proof_state: ModelRouteChainState,
        state: ModelRouteChainState,
    ) -> ClaimRecord | AllocationRecord:
        """在一个 owner UoW 中关闭最后 attempt，并提交后继或终态决定。"""

        return await self._store_route_chain_state(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
            proof_state=proof_state,
            state=state,
            mutation="transfer",
        )

    async def activate_approved_model_route(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        state: ModelRouteChainState,
    ) -> ClaimRecord | AllocationRecord:
        """以同一 claim/usage identity 持久化 approval grant 与目标 reservation。"""

        return await self._store_route_chain_state(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
            state=state,
            mutation="approval",
        )

    async def skip_approved_model_route_balance(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        state: ModelRouteChainState,
    ) -> ClaimRecord | AllocationRecord:
        """获批目标 balance不足时固化双 binding 与零 impact，不写 approved tuple。"""

        return await self._store_route_chain_state(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
            state=state,
            mutation="approval_balance",
        )

    async def close_model_route_attempt(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        state: ModelRouteChainState,
    ) -> ClaimRecord | AllocationRecord:
        """只把当前 started lifecycle 关闭为unknown；可信settled由最终结算持有。"""

        return await self._store_route_chain_state(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
            state=state,
            mutation="unknown_close",
        )

    async def mark_model_route_delta_observed(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        state: ModelRouteChainState,
    ) -> ClaimRecord | AllocationRecord:
        """首个 delta 公开前先固化不可逆的跨候选切换围栏。"""

        return await self._store_route_chain_state(
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
            state=state,
            mutation="delta",
        )

    async def _store_route_chain_state(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        state: ModelRouteChainState,
        mutation: str,
        proof_state: ModelRouteChainState | None = None,
    ) -> ClaimRecord | AllocationRecord:
        """校验不可变前缀后，在同一事务更新 state 与 reservation 影响。"""

        ownership = await self.resolve_operation_ownership(tenant_id=tenant_id, run_id=run_id)
        # PostgreSQL 的 UPDATE 行锁与 SQLite 的 write transaction 都从这里开始，
        # 让 route state读取发生在同一个新鲜串行化视图中；只靠 SELECT FOR UPDATE
        # 在 SQLite 会允许两个 reader 同时把同一 attempt identity 当成首次写入。
        version_bump = await self._session.execute(
            update(ParentBudgetLedgerModel)
            .where(
                ParentBudgetLedgerModel.tenant_id == tenant_id,
                ParentBudgetLedgerModel.budget_owner_run_id == ownership.budget_owner_run_id,
            )
            .values(version=ParentBudgetLedgerModel.version + 1)
        )
        if getattr(version_bump, "rowcount", 0) != 1:
            raise BudgetOperationConflict
        ledger = await self._lock_ledger(tenant_id, ownership.budget_owner_run_id)
        model = await self._route_operation_model(
            tenant_id=tenant_id,
            budget_owner_run_id=ownership.budget_owner_run_id,
            delegation_id=ownership.delegation_id,
            usage_call_id=usage_call_id,
            for_update=True,
        )
        if model is None or model.identity_schema_version != "budget-operation-v2":
            raise BudgetOperationConflict
        if (
            model.identity_json.get("route_chain_digest") != state.chain_id
            or state.usage_call_id != usage_call_id
        ):
            raise BudgetOperationConflict
        previous = (
            None
            if model.route_chain_state_json is None
            else ModelRouteChainState.model_validate(model.route_chain_state_json)
        )
        if previous is not None and previous == state:
            # 上面的 version bump 只用于取得跨数据库一致的写锁；exact replay
            # 不得制造新的 ledger version 事实。
            ledger.version -= 1
            return self._route_operation_record(model, replayed=True)
        if previous is None:
            raise BudgetOperationConflict
        if proof_state is None:
            validate_route_state_mutation(previous, state, mutation=mutation)
        else:
            if proof_state.chain_id != state.chain_id or proof_state.usage_call_id != usage_call_id:
                raise BudgetOperationConflict
            if previous != proof_state:
                validate_route_state_mutation(previous, proof_state, mutation="proof")
            validate_route_state_mutation(proof_state, state, mutation=mutation)
        old_tokens = previous.current_reservation.token_bound
        new_tokens = state.current_reservation.token_bound
        old_cost = Decimal(str(previous.current_reservation.cost_bound or 0))
        new_cost = Decimal(str(state.current_reservation.cost_bound or 0))
        await self._replace_route_reservation(
            ledger=ledger,
            model=model,
            delegation_id=ownership.delegation_id,
            old_tokens=old_tokens,
            old_cost=old_cost,
            new_tokens=new_tokens,
            new_cost=new_cost,
        )
        model.route_chain_state_json = state.to_payload()
        model.reserved_tokens = new_tokens
        model.reserved_cost = None if state.current_reservation.cost_bound is None else new_cost
        if state.attempt_lifecycle:
            # 聚合位只作“是否曾越过 provider 边界”的单调高水位；逐 attempt
            # 是否 proven/unknown/settled 仍完全由 route-chain state 判断。
            model.side_effect_state = "started"
        await self._session.flush()
        return self._route_operation_record(model)

    @staticmethod
    def _settlement_route_chain_state(
        previous_payload: dict[str, Any] | None,
        result: dict[str, Any],
    ) -> ModelRouteChainState | None:
        """从已验证 final evidence或attempt review提取终态并校验。"""

        raw_evidence = result.get("evidence")
        raw_decision = (
            cast(dict[str, object], raw_evidence).get("decision")
            if isinstance(raw_evidence, dict)
            else None
        )
        raw_chain = (
            cast(dict[str, object], raw_decision).get("route_chain")
            if isinstance(raw_decision, dict)
            else None
        )
        raw_state = (
            cast(dict[str, object], raw_chain).get("state") if isinstance(raw_chain, dict) else None
        )
        if previous_payload is None:
            if raw_state is not None:
                raise BudgetOperationConflict
            return None
        try:
            previous = ModelRouteChainState.model_validate(previous_payload)
        except Exception as exc:
            raise BudgetOperationConflict from exc
        if raw_state is None:
            raw_review = result.get("attempt_review")
            if set(result) != {"attempt_review"} or not isinstance(raw_review, dict):
                raise BudgetOperationConflict
            try:
                # 与 usage outbox 复用同一封闭 shape；这里额外绑定 route-chain
                # 最后一个全局 attempt，防止任意 needs-review 结果绕过终态校验。
                from agent_harness.storage.usage_attempt_review_repository import (
                    normalize_attempt_review,
                )

                review = normalize_attempt_review(cast(Mapping[str, object], raw_review))
                review_attempt = cast(list[dict[str, object]], review["attempts"])[0]
            except Exception as exc:
                raise BudgetOperationConflict from exc
            lifecycle = previous.attempt_lifecycle[-1] if previous.attempt_lifecycle else None
            if (
                lifecycle is None
                or lifecycle.lifecycle_state != "unknown"
                or previous.active_ordinal != lifecycle.candidate_ordinal
                or previous.selected_ordinal is not None
                or review["provider_close_state"] != "unknown"
                or review["usage_finality"] != "complete"
                or review["error_code"] != "model.provider_side_effect_unknown"
                or review_attempt["attempt"] != lifecycle.attempt
                or review_attempt["outcome"] != "unknown"
                or review_attempt["error_code"] != "model.provider_side_effect_unknown"
                or review_attempt["http_status"] != lifecycle.http_status
                or review_attempt["completion_observed"] != lifecycle.completion_observed
            ):
                raise BudgetOperationConflict
            return previous
        if not isinstance(raw_state, dict):
            raise BudgetOperationConflict
        try:
            terminal = ModelRouteChainState.model_validate(raw_state)
        except Exception as exc:
            raise BudgetOperationConflict from exc
        if previous == terminal:
            return terminal
        validate_route_state_mutation(previous, terminal, mutation="close")
        terminal_candidate = terminal.candidates[terminal.evidence_route_ordinal - 1]
        completed = (
            terminal_candidate.state == "completed"
            and terminal.selected_ordinal == terminal.evidence_route_ordinal
        )
        cancelled = terminal_candidate.state == "cancelled" and terminal.selected_ordinal is None
        if (
            not (completed or cancelled)
            or terminal.active_ordinal is not None
            or terminal.waiting_approval_ordinal is not None
            or terminal.current_reservation.token_bound != 0
            or terminal.current_reservation.cost_bound is not None
        ):
            raise BudgetOperationConflict
        return terminal

    async def _replace_route_reservation(
        self,
        *,
        ledger: ParentBudgetLedgerModel,
        model: BudgetOperationClaimModel | DelegationBudgetAllocationModel,
        delegation_id: str | None,
        old_tokens: int,
        old_cost: Decimal,
        new_tokens: int,
        new_cost: Decimal,
    ) -> None:
        """验证新 impact 不越 owner/delegation ceiling，再原子替换当前 operation 影响。"""

        if isinstance(model, BudgetOperationClaimModel):
            next_token_impact = ledger.token_impact - old_tokens + new_tokens
            next_cost_impact = ledger.cost_impact - old_cost + new_cost
            if (
                next_token_impact < 0
                or next_token_impact > ledger.token_limit
                or (
                    ledger.cost_enabled
                    and (ledger.cost_limit is None or next_cost_impact > ledger.cost_limit)
                )
            ):
                raise BudgetReservationRejected(reason="balance_insufficient")
            ledger.token_impact = next_token_impact
            ledger.cost_impact = next_cost_impact
            model.token_impact = new_tokens
            model.cost_impact = new_cost
            return
        assert delegation_id is not None
        top = await self._session.scalar(
            select(BudgetOperationClaimModel)
            .where(BudgetOperationClaimModel.delegation_id == delegation_id)
            .with_for_update()
        )
        if top is None:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        sibling_tokens, sibling_cost = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(DelegationBudgetAllocationModel.token_impact), 0),
                    func.coalesce(func.sum(DelegationBudgetAllocationModel.cost_impact), 0),
                ).where(
                    DelegationBudgetAllocationModel.delegation_id == delegation_id,
                    DelegationBudgetAllocationModel.id != model.id,
                )
            )
        ).one()
        if int(sibling_tokens) + new_tokens > top.reserved_tokens or (
            ledger.cost_enabled and Decimal(sibling_cost) + new_cost > _decimal(top.reserved_cost)
        ):
            raise BudgetReservationRejected(reason="balance_insufficient")
        model.token_impact = new_tokens
        model.cost_impact = new_cost

    async def _route_operation_model(
        self,
        *,
        tenant_id: str,
        budget_owner_run_id: str,
        delegation_id: str | None,
        usage_call_id: str,
        for_update: bool,
    ) -> BudgetOperationClaimModel | DelegationBudgetAllocationModel | None:
        """按 ownership 读取同一 usage operation；需要 mutation 时锁定该行。"""

        if delegation_id is None:
            query = select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.tenant_id == tenant_id,
                BudgetOperationClaimModel.budget_owner_run_id == budget_owner_run_id,
                BudgetOperationClaimModel.operation_kind == "direct",
                BudgetOperationClaimModel.usage_call_id == usage_call_id,
            )
        else:
            query = select(DelegationBudgetAllocationModel).where(
                DelegationBudgetAllocationModel.tenant_id == tenant_id,
                DelegationBudgetAllocationModel.budget_owner_run_id == budget_owner_run_id,
                DelegationBudgetAllocationModel.delegation_id == delegation_id,
                DelegationBudgetAllocationModel.usage_call_id == usage_call_id,
            )
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return await self._session.scalar(query)

    @staticmethod
    def _route_operation_record(
        model: BudgetOperationClaimModel | DelegationBudgetAllocationModel,
        *,
        replayed: bool = False,
    ) -> ClaimRecord | AllocationRecord:
        """保持 direct/allocation 既有领域记录类型。"""

        if isinstance(model, BudgetOperationClaimModel):
            return _claim_record(model, replayed=replayed)
        return _allocation_record(model, replayed=replayed)


__all__ = ["_SharedBudgetRouteChainMixin"]
