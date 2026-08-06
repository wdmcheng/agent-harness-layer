"""绑定运行身份的 provider-neutral 模型工具循环。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import MutableMapping
from datetime import UTC, datetime
from time import monotonic

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events.model_tool_loop import ModelToolLoopEventProducer
from agent_harness.identity import IdentityContext
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.registry.descriptor import AgentModelPolicy, AgentModelToolLoop
from agent_harness.runtime._model_tool_loop_contracts import (
    AgentModelPolicyResolver,
    LoopLimitsResolver,
    LoopStepObserver,
    ModelToolLoopApprovalRequired,
    ModelToolLoopApprovalSnapshot,
    ModelToolLoopError,
    ModelToolLoopLimitOverrides,
    ModelToolLoopLimitState,
    MonotonicClock,
    ToolCatalogResolver,
    ToolRegistryResolver,
    TrustedClock,
    _ContextAssemblyRuntime,
    _ModelToolLoopApprovalStore,
    _ModelToolTurnRuntime,
)
from agent_harness.runtime._model_tool_loop_entry import _ModelToolLoopEntryMixin
from agent_harness.runtime._model_tool_loop_execution import _ModelToolLoopExecutionMixin
from agent_harness.runtime._model_tool_loop_limits import _ModelToolLoopLimitMixin
from agent_harness.runtime._model_tool_loop_persistence import _ModelToolLoopPersistenceMixin
from agent_harness.runtime._model_tool_loop_recovery import _ModelToolLoopRecoveryMixin
from agent_harness.runtime._model_tool_loop_validation import _ModelToolLoopValidationMixin
from agent_harness.storage import SQLAlchemyStorage


class BoundModelToolLoopService(
    _ModelToolLoopEntryMixin,
    _ModelToolLoopExecutionMixin,
    _ModelToolLoopLimitMixin,
    _ModelToolLoopPersistenceMixin,
    _ModelToolLoopRecoveryMixin,
    _ModelToolLoopValidationMixin,
):
    """只向一个可信 tenant/run/agent/request/trace 暴露循环入口。"""

    def __init__(
        self,
        *,
        model_turns: _ModelToolTurnRuntime,
        tool_catalog_resolver: ToolCatalogResolver,
        tool_registry_resolver: ToolRegistryResolver,
        context_assembly: _ContextAssemblyRuntime,
        context: UsageEvidenceContext,
        identity: IdentityContext,
        loop_limits: AgentModelToolLoop | None,
        model_policy: AgentModelPolicy,
        step_observer: LoopStepObserver | None,
        approval_store: _ModelToolLoopApprovalStore | None,
        loop_events: ModelToolLoopEventProducer | None,
        storage: SQLAlchemyStorage | None,
        artifact_store: FileArtifactStore | None,
        trusted_clock: TrustedClock,
        monotonic_clock: MonotonicClock,
        monotonic_deadlines: MutableMapping[tuple[str, str, str, datetime, datetime], float],
    ) -> None:
        """冻结协作者与当前 run 的五项 descriptor 上界，禁止形成无限循环。

        `run()` 前会冻结 exact maxima 与 absolute deadline；调用方只能继续缩小上界，
        随后由耐久 snapshot 保存同一组边界，恢复时不得重置或放大。
        """

        self._model_turns = model_turns
        self._tool_catalog_resolver = tool_catalog_resolver
        self._tool_registry_resolver = tool_registry_resolver
        self._context_assembly = context_assembly
        self._context = context
        self._identity = identity
        self._loop_limits = (
            None
            if loop_limits is None
            else AgentModelToolLoop.model_validate(
                AgentModelToolLoop.model_dump(loop_limits, mode="python")
            ).model_copy(deep=True)
        )
        self._model_policy = AgentModelPolicy.model_validate(
            AgentModelPolicy.model_dump(model_policy, mode="python")
        ).model_copy(deep=True)
        self._step_observer = step_observer
        self._approval_store = approval_store
        self._loop_events = loop_events
        self._storage = storage
        self._artifact_store = artifact_store
        self._trusted_clock = trusted_clock
        self._monotonic_clock = monotonic_clock
        self._monotonic_deadlines = monotonic_deadlines


class ModelToolLoopService:
    """composition 持有的未绑定循环服务；业务可见前必须绑定可信 run。"""

    def __init__(
        self,
        *,
        model_turns: _ModelToolTurnRuntime,
        tool_catalog_resolver: ToolCatalogResolver,
        tool_registry_resolver: ToolRegistryResolver,
        context_assembly: _ContextAssemblyRuntime,
        loop_limits_resolver: LoopLimitsResolver,
        agent_model_policy_resolver: AgentModelPolicyResolver,
        step_observer: LoopStepObserver | None = None,
        approval_store: _ModelToolLoopApprovalStore | None = None,
        loop_events: ModelToolLoopEventProducer | None = None,
        storage: SQLAlchemyStorage | None = None,
        artifact_store: FileArtifactStore | None = None,
        trusted_clock: TrustedClock | None = None,
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        """注入 provider-neutral 协作者，不接受 handler 或 provider callback。"""

        self._model_turns = model_turns
        self._tool_catalog_resolver = tool_catalog_resolver
        self._tool_registry_resolver = tool_registry_resolver
        self._context_assembly = context_assembly
        self._loop_limits_resolver = loop_limits_resolver
        self._agent_model_policy_resolver = agent_model_policy_resolver
        self._step_observer = step_observer
        self._approval_store = approval_store
        self._loop_events = loop_events
        self._storage = storage
        self._artifact_store = artifact_store
        self._trusted_clock = trusted_clock or (lambda: datetime.now(tz=UTC))
        self._monotonic_clock = monotonic_clock or monotonic
        # 该映射只保存同一进程内的deadline guard，不是第二状态机；耐久真相仍是
        # loop row中的absolute UTC deadline。所有bound façade共享它，避免审批恢复
        # 重新绑定时重置授权时长。
        self._monotonic_deadlines: dict[tuple[str, str, str, datetime, datetime], float] = {}

    def bind_execution(
        self,
        *,
        identity: IdentityContext,
        tenant_id: str,
        run_id: str,
        agent_id: str,
        request_id: str | None,
        trace_id: str,
    ) -> BoundModelToolLoopService:
        """在业务取得 façade 前冻结 Agent 上界和全部可信运行身份。"""

        if type(identity) is not IdentityContext or identity.tenant_id != tenant_id:
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        identity_snapshot = IdentityContext.model_validate(
            IdentityContext.model_dump(identity, mode="python")
        ).model_copy(deep=True)
        return BoundModelToolLoopService(
            model_turns=self._model_turns,
            tool_catalog_resolver=self._tool_catalog_resolver,
            tool_registry_resolver=self._tool_registry_resolver,
            context_assembly=self._context_assembly,
            context=UsageEvidenceContext(
                tenant_id=tenant_id,
                run_id=run_id,
                agent_id=agent_id,
                request_id=request_id,
                trace_id=trace_id,
            ),
            identity=identity_snapshot,
            loop_limits=self._loop_limits_resolver(agent_id),
            model_policy=self._agent_model_policy_resolver(agent_id),
            step_observer=self._step_observer,
            approval_store=self._approval_store,
            loop_events=self._loop_events,
            storage=self._storage,
            artifact_store=self._artifact_store,
            trusted_clock=self._trusted_clock,
            monotonic_clock=self._monotonic_clock,
            monotonic_deadlines=self._monotonic_deadlines,
        )


__all__ = [
    "BoundModelToolLoopService",
    "ModelToolLoopApprovalRequired",
    "ModelToolLoopApprovalSnapshot",
    "ModelToolLoopError",
    "ModelToolLoopLimitOverrides",
    "ModelToolLoopLimitState",
    "ModelToolLoopService",
]
