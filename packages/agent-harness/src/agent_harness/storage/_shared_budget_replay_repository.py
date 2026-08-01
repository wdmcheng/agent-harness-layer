"""共享预算 stable-key replay 与跨记录结算完整性校验。"""

# Mixin 的匹配 helper 由本类提供；最终 repository 组合由全仓 Pyright 校验。
# pyright: reportUnknownMemberType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.model_route_chain_state import ModelRouteChainState
from agent_harness.storage.shared_budget import (
    BudgetOperationConflict,
    BudgetOperationOwnership,
    BudgetOperationReplaySeed,
    OperationIdentity,
    OperationState,
    SideEffectState,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
)


class _SharedBudgetReplayMixin:
    """只读取 durable claim/allocation，自校验身份并判断 crash-window 状态。"""

    _session: AsyncSession

    async def usage_replay_seed(
        self,
        *,
        tenant_id: str,
        usage_call_id: str,
    ) -> BudgetOperationReplaySeed | None:
        """先于当前 owner/snapshot 读取并自校验 durable stable-key identity。"""

        direct = list(
            await self._session.scalars(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.tenant_id == tenant_id,
                    BudgetOperationClaimModel.operation_kind == "direct",
                    BudgetOperationClaimModel.usage_call_id == usage_call_id,
                )
            )
        )
        allocations = list(
            await self._session.scalars(
                select(DelegationBudgetAllocationModel).where(
                    DelegationBudgetAllocationModel.tenant_id == tenant_id,
                    DelegationBudgetAllocationModel.usage_call_id == usage_call_id,
                )
            )
        )
        if len(direct) + len(allocations) > 1:
            raise BudgetOperationConflict
        if direct:
            model = direct[0]
            try:
                identity = OperationIdentity.model_validate(model.identity_json)
            except (TypeError, ValueError) as exc:
                raise BudgetOperationConflict from exc
            if not self._direct_replay_matches(model, identity):
                raise BudgetOperationConflict
            return BudgetOperationReplaySeed(
                operation_kind="direct",
                ownership=BudgetOperationOwnership(
                    kind="direct",
                    budget_owner_run_id=model.budget_owner_run_id,
                ),
                identity=identity,
                state=cast(OperationState, model.state),
                side_effect_state=cast(SideEffectState, model.side_effect_state),
                result=model.result_json,
                route_chain_state=(
                    None
                    if model.route_chain_state_json is None
                    else ModelRouteChainState.model_validate(model.route_chain_state_json)
                ),
            )
        if allocations:
            model = allocations[0]
            try:
                identity = OperationIdentity.model_validate(model.identity_json)
            except (TypeError, ValueError) as exc:
                raise BudgetOperationConflict from exc
            if not self._allocation_replay_matches(model, identity):
                raise BudgetOperationConflict
            return BudgetOperationReplaySeed(
                operation_kind="allocation",
                ownership=BudgetOperationOwnership(
                    kind="allocation",
                    budget_owner_run_id=model.budget_owner_run_id,
                    delegation_id=model.delegation_id,
                ),
                identity=identity,
                state=cast(OperationState, model.state),
                side_effect_state=cast(SideEffectState, model.side_effect_state),
                result=model.result_json,
                route_chain_state=(
                    None
                    if model.route_chain_state_json is None
                    else ModelRouteChainState.model_validate(model.route_chain_state_json)
                ),
            )
        return None

    @staticmethod
    def validate_usage_replay_identity(
        *,
        seed: BudgetOperationReplaySeed,
        expected_identity: OperationIdentity,
    ) -> None:
        """当前语义请求必须重算为同一 opaque identity，才能进入结果重放。"""

        if seed.identity.to_payload() != expected_identity.to_payload():
            raise BudgetOperationConflict

    @staticmethod
    def validate_usage_replay_settlement(
        *,
        seed: BudgetOperationReplaySeed,
        usage_state: str,
        usage_result: dict[str, object] | None,
    ) -> None:
        """预算 claim 与 usage outbox 必须共同证明同一个 crash-window 状态。"""

        if seed.side_effect_state == "not_started":
            valid = (
                seed.state == "reserved"
                and seed.result is None
                and usage_state == "started"
                and isinstance(usage_result, dict)
                and set(usage_result) == {"started"}
            )
        elif seed.side_effect_state == "started":
            valid = (
                seed.state in {"reserved", "needs_review"}
                and seed.result is None
                and usage_state == "started"
                and isinstance(usage_result, dict)
                and set(usage_result) == {"started"}
            )
        else:
            outbox_result = (
                {key: value for key, value in usage_result.items() if key != "started"}
                if isinstance(usage_result, dict)
                else None
            )
            valid = (
                seed.state in {"settled", "needs_review"}
                and seed.result is not None
                and usage_state in {"result_persisted", "published", "needs_review"}
                and outbox_result == seed.result
                and (usage_state != "needs_review" or set(seed.result) == {"attempt_review"})
            )
        if not valid:
            raise BudgetOperationConflict

    @staticmethod
    def _direct_replay_matches(
        model: BudgetOperationClaimModel, requested: OperationIdentity
    ) -> bool:
        """重算 durable identity，并绑定 direct detail 列后才允许 exact replay。"""

        try:
            persisted = OperationIdentity.model_validate(model.identity_json)
        except (TypeError, ValueError):
            return False
        return (
            persisted.to_payload() == requested.to_payload()
            and persisted.ownership_kind == "direct"
            and persisted.delegation_claim_id is None
            and model.operation_kind == "direct"
            and model.delegation_id is None
            and model.run_id == persisted.run_id == model.budget_owner_run_id
            and model.agent_id == persisted.agent_id
            and model.usage_kind == persisted.usage_kind
            and model.identity_schema_version == persisted.identity_schema_version
            and model.identity_hash == persisted.identity_hash
        )

    @staticmethod
    def _allocation_replay_matches(
        model: DelegationBudgetAllocationModel, requested: OperationIdentity
    ) -> bool:
        """Allocation exact replay 还必须绑定 child、delegation 与 stable key。"""

        try:
            persisted = OperationIdentity.model_validate(model.identity_json)
        except (TypeError, ValueError):
            return False
        return (
            persisted.to_payload() == requested.to_payload()
            and persisted.ownership_kind == "allocation"
            and persisted.delegation_claim_id == model.delegation_id
            and model.run_id == persisted.run_id
            and model.agent_id == persisted.agent_id
            and model.usage_kind == persisted.usage_kind
            and model.identity_schema_version == persisted.identity_schema_version
            and model.identity_hash == persisted.identity_hash
        )


__all__ = ["_SharedBudgetReplayMixin"]
