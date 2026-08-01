"""从 _invocation_execution.py 拆出的私有职责模块；公共 façade 与顺序语义保持不变。"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import IdentityRuntime
from agent_harness.models.providers import ModelRequest
from agent_harness.models.router import ModelRoutePlan, ModelRouter
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.shared_budget import BudgetReservationRejected

if TYPE_CHECKING:
    from agent_harness.registry.descriptor import AgentModelPolicy


class ModelInvocationPlanningMixin:
    """承载从兼容入口拆出的单一私有职责。"""

    _storage: SQLAlchemyStorage
    _router: ModelRouter
    _shared_budget: IdentityRuntime | None
    _agent_policy_resolver: Callable[[str], AgentModelPolicy] | None

    async def _plan(
        self,
        *,
        request: ModelRequest,
        context: UsageEvidenceContext,
        approved: bool,
    ) -> ModelRoutePlan | ModelRouteChainPlan:
        """依据共享预算树快照或默认路由生成本次调用计划。

        快照、账本和归属在同一个 UoW 中读取，确保路由限制来自同一持久化视图；任何
        缺失或无效快照都转换成稳定预算拒绝，而不是悄悄回退到当前进程配置。
        """

        if self._router.has_controlled_settings and self._agent_policy_resolver is None:
            raise BudgetReservationRejected(reason="snapshot_invalid")
        if self._shared_budget is None:
            if self._router.has_controlled_settings:
                assert self._agent_policy_resolver is not None
                policy = self._agent_policy_resolver(context.agent_id)
                plan = (
                    self._router.plan_chain(request, agent_policy=policy)
                    if policy.fallback_routes
                    else self._router.plan(request, agent_policy=policy)
                )
            else:
                plan = self._router.plan(request)
            if isinstance(plan, ModelRouteChainPlan):
                return plan
            return self._apply_durable_soft_approval(plan) if approved else plan
        # 三项读取必须保持一致，否则并发迁移预算树时可能组合出不存在的路由视图。
        async with self._storage.uow() as uow:
            ownership = await uow.shared_budget.resolve_operation_ownership(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
            )
            ledger = await uow.shared_budget.get_ledger(
                context.tenant_id,
                ownership.budget_owner_run_id,
            )
            snapshot = await uow.shared_budget.get_tree_snapshot(
                context.tenant_id,
                ownership.budget_owner_run_id,
            )
        if ledger is None:
            # `_start_settlement` 负责把 snapshot_invalid 保存为稳定拒绝 evidence。
            try:
                plan = self._router.plan(request)
                return self._apply_durable_soft_approval(plan) if approved else plan
            except KeyError as exc:
                raise BudgetReservationRejected(reason="snapshot_invalid") from exc
        if snapshot is None:
            try:
                plan = self._router.plan(request)
                return self._apply_durable_soft_approval(plan) if approved else plan
            except KeyError as exc:
                raise BudgetReservationRejected(reason="snapshot_invalid") from exc
        if (
            self._router.has_controlled_settings
            and snapshot.get("schema_version") == "budget-tree-v2"
        ):
            try:
                raw_agents = snapshot.get("agents")
                raw_target = (
                    cast(dict[str, object], raw_agents).get(context.agent_id)
                    if isinstance(raw_agents, dict)
                    else None
                )
                raw_policy = (
                    cast(dict[str, object], raw_target).get("model_policy")
                    if isinstance(raw_target, dict)
                    else None
                )
                chain_mode = isinstance(raw_policy, dict) and bool(
                    cast(dict[str, object], raw_policy).get("fallback_routes")
                )
                plan = (
                    self._router.plan_chain_from_snapshot(
                        request,
                        snapshot=snapshot,
                        agent_id=context.agent_id,
                    )
                    if chain_mode
                    else self._router.plan_from_snapshot(
                        request,
                        snapshot=snapshot,
                        agent_id=context.agent_id,
                    )
                )
                if isinstance(plan, ModelRouteChainPlan):
                    return plan
                return self._apply_durable_soft_approval(plan) if approved else plan
            except ValueError as exc:
                # 路由缩权错误保留其公开错误；快照损坏统一映射为安全预算拒绝。
                if getattr(exc, "code", None) != "budget.reservation_rejected":
                    raise
                raise BudgetReservationRejected(reason="snapshot_invalid") from exc
        try:
            config = self._shared_budget.model_router_config(
                snapshot=snapshot,
                agent_id=context.agent_id,
                base=self._router.config,
            )
        except ValueError as exc:
            raise BudgetReservationRejected(reason="snapshot_invalid") from exc
        try:
            plan = self._router.plan(request, config=config)
            return self._apply_durable_soft_approval(plan) if approved else plan
        except KeyError as exc:
            raise BudgetReservationRejected(reason="snapshot_invalid") from exc

    @staticmethod
    def _apply_durable_soft_approval(plan: ModelRoutePlan) -> ModelRoutePlan:
        """只把 Router 明确标记的 soft gate 升级为调用，不触碰 hard eligibility。"""

        if plan.decision.action != "policy_required" or plan.approval_kind != "soft_budget":
            return plan
        decision = plan.decision.model_copy(
            update={
                "action": "call",
                "reason": "durable approval accepted for soft budget threshold",
            }
        )
        return plan.model_copy(update={"decision": decision, "approval_kind": None})
