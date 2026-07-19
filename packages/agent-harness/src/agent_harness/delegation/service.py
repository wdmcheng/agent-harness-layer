"""受控 `agent.delegate` application service 与 local/service 共享状态机。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast

from agent_harness.contracts import GuardrailDecisionStatus
from agent_harness.delegation._service_evidence import (
    required_child_id as _required_child_id,
)
from agent_harness.delegation._service_publication import _DelegationPublicationMixin
from agent_harness.delegation._service_recovery import DelegationRecoveryMixin
from agent_harness.delegation._service_summary import DelegationSummaryMixin
from agent_harness.delegation._service_types import (
    TERMINAL_RUN_STATUSES as _TERMINAL,
)
from agent_harness.delegation._service_types import (
    DelegationBudgetIdentityRuntime as DelegationBudgetIdentityRuntime,
)
from agent_harness.delegation._service_types import (
    DelegationError as DelegationError,
)
from agent_harness.delegation._service_types import (
    DelegationExecutionResult as DelegationExecutionResult,
)
from agent_harness.delegation._service_types import (
    DelegationMode as DelegationMode,
)
from agent_harness.delegation._service_types import (
    DelegationOrchestrator as DelegationOrchestrator,
)
from agent_harness.delegation._service_types import (
    DelegationPolicy as DelegationPolicy,
)
from agent_harness.delegation.models import (
    DelegationRequest,
    delegation_relation_id,
    delegation_request_bytes,
)
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.delegation_repositories import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationRecord,
    DelegationStorageConflict,
)
from agent_harness.storage.event_capacity_repositories import (
    EventCapacityExceeded,
)
from agent_harness.storage.repositories import RunRecord

if TYPE_CHECKING:
    from agent_harness.storage.shared_budget import OperationIdentity

# 公开类型仍以 application service facade 为身份，避免拆分泄漏私有模块名。
DelegationError.__module__ = __name__
DelegationExecutionResult.__module__ = __name__


@dataclass(frozen=True)
class _FrozenDelegationAuthorization:
    """新 delegation 只消费 root 创建时冻结的 source/target budget catalog。"""

    parent: RunRecord
    source_agent_id: str
    target_agent_id: str
    parent_token_limit: int
    target_token_limit: int
    parent_cost_limit: float | None
    target_cost_limit: float | None
    tree_snapshot_id: str
    snapshot: dict[str, Any]


class DelegationService(
    _DelegationPublicationMixin,
    DelegationSummaryMixin,
    DelegationRecoveryMixin,
):
    """授权后原子 claim，再恢复唯一 child 并从 durable evidence 聚合。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        registry: AgentRegistry,
        policy: DelegationPolicy,
        event_bus: EventBus,
        orchestrator: RunOrchestrator | DelegationOrchestrator,
        shared_budget: DelegationBudgetIdentityRuntime,
        mode: DelegationMode,
    ) -> None:
        """装配授权、持久化、事件和执行器依赖，供 local/service 共用同一状态机。

        `shared_budget` 负责生成可重放的预约身份；此服务只协调调用顺序，
        不在内存中保存 delegation 进度，避免进程重启后产生另一套事实来源。
        """

        self._storage = storage
        self._registry = registry
        self._policy = policy
        self._event_bus = event_bus
        self._orchestrator = orchestrator
        self._shared_budget = shared_budget
        self._mode = mode

    async def delegate(
        self,
        request: DelegationRequest,
        *,
        identity: IdentityContext,
    ) -> DelegationExecutionResult:
        """执行或恢复一个单层 delegation；拒绝路径不创建业务状态。

        先在事务内取得或重放 durable claim，再发布事件和启动 child，保证重试
        不会重复预约预算或发起第二个 child。local 模式会同步收敛 child，
        service 模式则将后续执行交给队列 worker。
        """

        canonical_request_bytes = delegation_request_bytes(request, identity=identity)
        request_hash = hashlib.sha256(canonical_request_bytes).hexdigest()
        scope = f"delegation-parent:{identity.tenant_id}:{request.parent_run_id}"
        try:
            async with self._storage.idempotency_request_lock(scope):
                async with self._storage.uow() as uow:
                    replay_seed = await uow.delegations.replay_identity_seed(
                        tenant_id=identity.tenant_id,
                        parent_run_id=request.parent_run_id,
                        idempotency_key=request.idempotency_key,
                        request_hash=request_hash,
                    )
                    await uow.commit()
                if replay_seed is not None:
                    persisted_budget_identity = replay_seed.budget_identity
                    budget_identity = (
                        None
                        if persisted_budget_identity is None
                        else self._shared_budget.delegation_replay_identity(
                            tenant_id=identity.tenant_id,
                            canonical_request_bytes=canonical_request_bytes,
                            parent_run_id=request.parent_run_id,
                            source_agent_id=request.source_agent_id,
                            target_agent_id=request.target_agent_id,
                            delegation_id=replay_seed.delegation.id,
                            idempotency_key=request.idempotency_key,
                            persisted_identity=persisted_budget_identity,
                        )
                    )
                    async with self._storage.uow() as uow:
                        claim = await uow.delegations.replay_existing(
                            tenant_id=identity.tenant_id,
                            parent_run_id=request.parent_run_id,
                            idempotency_key=request.idempotency_key,
                            request_hash=request_hash,
                            expected_identity=budget_identity,
                        )
                        await uow.commit()
                    if claim is None:
                        raise DelegationStorageConflict("delegation.execution_failed")
                else:
                    authorization = await self._authorize(request=request, identity=identity)
                    parent = authorization.parent
                    delegation_id = delegation_relation_id(
                        tenant_id=identity.tenant_id,
                        parent_run_id=parent.id,
                        idempotency_key=request.idempotency_key,
                    )
                    budget_identity = self._delegation_budget_identity(
                        authorization=authorization,
                        tenant_id=identity.tenant_id,
                        canonical_request_bytes=canonical_request_bytes,
                        delegation_id=delegation_id,
                        idempotency_key=request.idempotency_key,
                    )
                    await self._event_bus.reconcile_local_capacity(run_id=parent.id)
                    async with self._storage.uow() as uow:
                        claim = await uow.delegations.claim_and_reserve(
                            DelegationClaimCreate(
                                delegation_id=delegation_id,
                                tenant_id=identity.tenant_id,
                                parent_run_id=parent.id,
                                source_agent_id=authorization.source_agent_id,
                                target_agent_id=authorization.target_agent_id,
                                idempotency_key=request.idempotency_key,
                                request_hash=request_hash,
                                budget_intent=request.budget_intent,
                                child_input=request.child_input,
                                identity=identity.to_payload(),
                                trace_id=parent.trace_id,
                                request_id=request.request_id,
                                parent_token_limit=authorization.parent_token_limit,
                                requested_token_reservation=authorization.target_token_limit,
                                parent_cost_limit=authorization.parent_cost_limit,
                                requested_cost_reservation=authorization.target_cost_limit,
                                budget_identity=budget_identity,
                            )
                        )
                        await uow.commit()
        except EventCapacityExceeded as exc:
            raise DelegationError("event.sequence_exhausted") from exc
        except DelegationBudgetExceeded as exc:
            raise DelegationError("delegation.budget_exceeded") from exc
        except DelegationStorageConflict as exc:
            raise DelegationError(exc.code) from exc

        delegation = claim.delegation
        await self._publish_claimed(delegation=delegation, identity=identity)
        if delegation.status == "failed" and delegation.child_run_id is None:
            await self._publish_pre_child_failed(delegation=delegation, identity=identity)
            raise DelegationError("delegation.execution_failed")
        delegation = await self._recover_or_launch_child(
            delegation=delegation,
            request=request,
            identity=identity,
        )
        await self._publish_child_created(delegation=delegation, identity=identity)
        if self._mode == "local":
            return await self.reconcile_child(delegation.child_run_id or "")
        return DelegationExecutionResult(
            delegation_id=delegation.id,
            parent_run_id=delegation.parent_run_id,
            child_run_id=_required_child_id(delegation),
            status=delegation.status,
            summary=await self.get_parent_summary(
                tenant_id=delegation.tenant_id,
                parent_run_id=delegation.parent_run_id,
            ),
        )

    async def _authorize(
        self,
        *,
        request: DelegationRequest,
        identity: IdentityContext,
    ) -> _FrozenDelegationAuthorization:
        """校验父 run 与调用身份，并从创建时冻结的树快照推导可授权预算。

        运行中的 registry 或策略配置不得扩大既有 run 的 delegation 图；因此
        先验证 parent/session/tenant 归属，再以持久化快照决定 edge 和预算上限，
        最后才执行实时的 `agent.delegate` 策略检查。
        """

        async with self._storage.uow() as uow:
            parent = await uow.runs.get(request.parent_run_id)
            parent_session = None if parent is None else await uow.sessions.get(parent.session_id)
        if (
            parent is None
            or parent_session is None
            or parent.tenant_id != identity.tenant_id
            or parent.agent_id != request.source_agent_id
            or parent.session_id != identity.session_id
            or parent_session.id != parent.session_id
            or parent_session.tenant_id != identity.tenant_id
            or parent_session.user_id != identity.user_id
        ):
            raise DelegationError("delegation.policy_denied")
        if parent.status in _TERMINAL:
            raise DelegationError("delegation.execution_failed")
        if request.source_agent_id == request.target_agent_id:
            raise DelegationError("delegation.cycle_detected")
        if parent.parent_run_id is not None:
            raise DelegationError("delegation.depth_exceeded")
        async with self._storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger(identity.tenant_id, parent.id)
            snapshot = await uow.shared_budget.get_tree_snapshot(
                identity.tenant_id,
                parent.id,
            )
        authorization = self._frozen_authorization(
            parent=parent,
            request=request,
            snapshot=snapshot,
            tree_snapshot_id="" if ledger is None else ledger.snapshot_id,
        )
        if authorization is None:
            if self._frozen_edge_has_missing_target(snapshot=snapshot, request=request):
                raise DelegationError("delegation.target_not_found")
            raise DelegationError("delegation.edge_denied")
        decision = await self._policy.evaluate(
            PolicyCheck(
                actor=identity,
                action="agent.delegate",
                resource=f"agent:{authorization.target_agent_id}",
                context={
                    "parent_run_id": parent.id,
                    "source_agent_id": authorization.source_agent_id,
                    "target_agent_id": authorization.target_agent_id,
                    "request_id": request.request_id,
                },
            )
        )
        if decision.decision != GuardrailDecisionStatus.ALLOW.value:
            raise DelegationError("delegation.policy_denied")
        return authorization

    def _delegation_budget_identity(
        self,
        *,
        authorization: _FrozenDelegationAuthorization,
        tenant_id: str,
        canonical_request_bytes: bytes,
        delegation_id: str,
        idempotency_key: str,
    ) -> OperationIdentity:
        """构造绑定规范请求、冻结树和实际预约额度的不可变预算身份。

        repository 依靠该身份识别同一业务操作的重放；成本上限先转为 Decimal，
        避免 float 表示差异让等价请求被误判为不同预约。
        """

        trusted_cost_bound = (
            None
            if authorization.target_cost_limit is None
            else Decimal(str(authorization.target_cost_limit))
        )
        return self._shared_budget.delegation_identity(
            tenant_id=tenant_id,
            canonical_request_bytes=canonical_request_bytes,
            parent_run_id=authorization.parent.id,
            source_agent_id=authorization.source_agent_id,
            target_agent_id=authorization.target_agent_id,
            delegation_id=delegation_id,
            idempotency_key=idempotency_key,
            tree_snapshot_id=authorization.tree_snapshot_id,
            snapshot=authorization.snapshot,
            trusted_token_bound=authorization.target_token_limit,
            trusted_cost_bound=trusted_cost_bound,
        )

    @staticmethod
    def _frozen_authorization(
        *,
        parent: RunRecord,
        request: DelegationRequest,
        snapshot: dict[str, Any] | None,
        tree_snapshot_id: str,
    ) -> _FrozenDelegationAuthorization | None:
        """从 frozen catalog 取出 source/target 的授权边和预约上限。

        快照结构或类型不可信时一律返回 ``None``，让上层按拒绝处理；不能退回
        到当前 registry，否则历史 run 会因后续配置变更而获得新的 delegation 权限。
        """

        if not isinstance(snapshot, dict):
            return None
        owner = snapshot.get("owner")
        agents = snapshot.get("agents")
        if not isinstance(owner, dict) or not isinstance(agents, dict):
            return None
        typed_owner = cast(dict[str, object], owner)
        typed_agents = cast(dict[str, object], agents)
        raw_targets = typed_owner.get("delegation_targets")
        if not isinstance(raw_targets, list) or any(
            not isinstance(item, str) for item in cast(list[object], raw_targets)
        ):
            return None
        allowed_targets = cast(list[object], raw_targets)
        source = typed_agents.get(request.source_agent_id)
        target = typed_agents.get(request.target_agent_id)
        if (
            typed_owner.get("agent_id") != request.source_agent_id
            or parent.agent_id != request.source_agent_id
            or request.target_agent_id not in allowed_targets
            or not isinstance(source, dict)
            or not isinstance(target, dict)
        ):
            return None
        source_budget = cast(dict[str, object], source).get("target_budget")
        target_budget = cast(dict[str, object], target).get("target_budget")
        if not isinstance(source_budget, dict) or not isinstance(target_budget, dict):
            return None
        source_limits = DelegationService._frozen_limits(cast(dict[str, object], source_budget))
        target_limits = DelegationService._frozen_limits(cast(dict[str, object], target_budget))
        if source_limits is None or target_limits is None:
            return None
        return _FrozenDelegationAuthorization(
            parent=parent,
            source_agent_id=request.source_agent_id,
            target_agent_id=request.target_agent_id,
            parent_token_limit=source_limits[0],
            target_token_limit=target_limits[0],
            parent_cost_limit=source_limits[1],
            # Frozen target 的 null cost ceiling 表示继承已启用的 owner ceiling，
            # 顶层 immutable identity 必须绑定 repository 实际预约值。
            target_cost_limit=(
                target_limits[1] if target_limits[1] is not None else source_limits[1]
            ),
            tree_snapshot_id=tree_snapshot_id,
            snapshot=snapshot,
        )

    @staticmethod
    def _frozen_edge_has_missing_target(
        *,
        snapshot: dict[str, Any] | None,
        request: DelegationRequest,
    ) -> bool:
        """区分“快照允许该边但 target 丢失”与普通的未授权边。

        这个区分只服务于稳定错误码：前者说明冻结 catalog 已损坏或不完整，
        后者仍是正常的 edge 拒绝，二者都不会回退到实时配置补全信息。
        """

        if not isinstance(snapshot, dict):
            return False
        owner = snapshot.get("owner")
        agents = snapshot.get("agents")
        if not isinstance(owner, dict) or not isinstance(agents, dict):
            return False
        typed_owner = cast(dict[str, object], owner)
        typed_agents = cast(dict[str, object], agents)
        raw_targets = typed_owner.get("delegation_targets")
        return bool(
            isinstance(raw_targets, list)
            and request.target_agent_id in cast(list[object], raw_targets)
            and request.target_agent_id not in typed_agents
        )

    @staticmethod
    def _frozen_limits(budget: dict[str, object]) -> tuple[int, float | None] | None:
        """验证快照中的单次预算，并归一化为 repository 可预约的标量。

        布尔值虽然是 Python 的 ``int`` 子类，但不能作为 token 限额；非有限或
        负数成本同样视为损坏快照，以 fail-closed 方式阻断创建。
        """

        tokens = budget.get("max_tokens_per_run")
        raw_cost = budget.get("max_cost_usd_per_run")
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            return None
        if raw_cost is None:
            return tokens, None
        try:
            cost = Decimal(str(raw_cost))
        except (InvalidOperation, ValueError):
            return None
        if not cost.is_finite() or cost < 0:
            return None
        return tokens, float(cost)

    async def _recover_or_launch_child(
        self,
        *,
        delegation: DelegationRecord,
        request: DelegationRequest,
        identity: IdentityContext,
    ) -> DelegationRecord:
        """恢复已有 child，或在持久化 started 标记后仅启动一次新的 child。

        外部 launcher 可能在超时后已经成功创建 run，因此异常路径先按稳定
        idempotency key 回查。若 started 标记被重放却没有可证明的 child，宁可
        将预算树提升为待人工处理，也绝不能再次调用 launcher。
        """

        child_id = delegation.child_run_id
        if child_id is None:
            async with self._storage.uow() as uow:
                existing = await uow.runs.get_by_idempotency_key(
                    tenant_id=delegation.tenant_id,
                    session_id=identity.session_id,
                    agent_id=delegation.target_agent_id,
                    idempotency_key=f"delegation:{delegation.id}",
                )
            if existing is not None:
                child_id = existing.id
        if child_id is None:
            launcher = (
                self._orchestrator.start_run
                if self._mode == "local"
                else self._orchestrator.submit_run
            )
            async with self._storage.uow() as uow:
                started_claim = await uow.shared_budget.mark_delegation_started(
                    delegation_id=delegation.id
                )
                if started_claim is None:
                    raise DelegationError("delegation.execution_failed")
                if started_claim.replayed:
                    # child/queue 的外部副作用可能已经开始；没有稳定 child 结果时
                    # 只能提升整棵树为 needs_review，绝不能再次调用 launcher。
                    await uow.shared_budget.recover_unknown_started(
                        tenant_id=started_claim.tenant_id,
                        budget_owner_run_id=started_claim.budget_owner_run_id,
                    )
                    await uow.commit()
                    raise DelegationError("delegation.execution_failed")
                await uow.commit()
            try:
                result = await launcher(
                    agent_id=delegation.target_agent_id,
                    input=request.child_input,
                    idempotency_key=f"delegation:{delegation.id}",
                    identity=identity,
                    request_id=delegation.request_id,
                    trace_id=delegation.trace_id,
                    parent_run_id=delegation.parent_run_id,
                )
            except Exception as exc:
                async with self._storage.uow() as uow:
                    existing = await uow.runs.get_by_idempotency_key(
                        tenant_id=delegation.tenant_id,
                        session_id=identity.session_id,
                        agent_id=delegation.target_agent_id,
                        idempotency_key=f"delegation:{delegation.id}",
                    )
                    if existing is None:
                        delegation = await uow.delegations.release_pre_child_failure(
                            delegation_id=delegation.id,
                        )
                        await uow.commit()
                if existing is None:
                    await self._publish_pre_child_failed(
                        delegation=delegation,
                        identity=identity,
                    )
                    raise DelegationError("delegation.execution_failed") from exc
                child_id = existing.id
            else:
                child_id = result.run_id
        async with self._storage.uow() as uow:
            attached = await uow.delegations.attach_child(
                delegation_id=delegation.id,
                child_run_id=child_id,
            )
            await uow.commit()
        return attached


__all__ = [
    "DelegationError",
    "DelegationExecutionResult",
    "DelegationMode",
    "DelegationService",
]
