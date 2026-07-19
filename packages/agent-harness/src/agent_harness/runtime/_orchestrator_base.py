"""RunOrchestrator 内部依赖容器与跨职责协作契约。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Protocol, runtime_checkable

from agent_harness.embeddings import EmbeddingInvocationService
from agent_harness.events import CanonicalEvent, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.models import ModelInvocationService
from agent_harness.runtime.checkpoints import IdempotencyKey, ResumeToken
from agent_harness.runtime.continuation import idempotency_value
from agent_harness.runtime.executor import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutorResolver,
    RunResult,
)
from agent_harness.runtime.queue import RunQueue
from agent_harness.runtime.state import RunStatus
from agent_harness.runtime.trace import normalize_trace_id
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver


class RunEnqueueUnavailable(RuntimeError):
    """run 已持久化为 enqueue_pending，但 broker 暂时不可用。"""


@runtime_checkable
class PendingDelegationRecovery(Protocol):
    """runtime 只依赖的 delegation 恢复能力，避免形成 runtime/import 环。"""

    async def recover_pending_for_parent(self, *, parent_run_id: str) -> int:
        """补投指定 parent 已提交但尚未发布的委派结果，返回恢复数量。"""

        ...


class OrchestratorState:
    """集中保存 runtime composition，避免各职责模块复制初始化逻辑。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        identity: IdentityContext | None = None,
        executor_resolver: AgentExecutorResolver | None = None,
        executor_services: Mapping[str, object] | None = None,
        queue: RunQueue | None = None,
    ) -> None:
        """保存运行组件并为生产 composition 补齐持久化 trace 解析器。"""

        self._storage = storage
        self._event_bus = event_bus
        if not event_bus.run_trace_resolver_configured:
            # RunOrchestrator 是所有生产 run composition 的共同根；在这里绑定能
            # 保证 API、CLI、local 与 worker 不会因漏配而绕过持久化门禁。
            event_bus.bind_run_trace_resolver(StorageRunTraceResolver(storage))
        self._identity = identity or IdentityContext.local_default()
        self._executor_resolver = executor_resolver
        self._executor_services = dict(executor_services or {})
        self._queue = queue
        self._approval_service: Any | None = None

    def bind_approval_service(self, service: Any) -> None:
        """闭合 runtime/approval 调用环，但不持久化 service object。"""

        self._approval_service = service

    def bind_execution_service(self, name: str, service: object) -> None:
        """闭合创建顺序上的 runtime service 环；禁止静默覆盖既有依赖。"""

        if not name:
            raise ValueError("execution service name must not be empty")
        if name in self._executor_services and self._executor_services[name] is not service:
            raise RuntimeError(f"agent execution service is already configured: {name}")
        self._executor_services[name] = service

    async def recover_pending_usage_evidence(self, *, run_id: str | None = None) -> int:
        """在 worker 启动或 run 重放前只补投已有确定性 usage 结果。"""

        if run_id is None:
            async with self._storage.uow() as uow:
                run_ids = await uow.evidence_outbox.pending_usage_run_ids()
        else:
            run_ids = [run_id]
        model = self._executor_services.get("model_invocation")
        embedding = self._executor_services.get("embedding_invocation")
        if model is not None and not isinstance(model, ModelInvocationService):
            raise RuntimeError("model_invocation service does not support usage recovery")
        if embedding is not None and not isinstance(embedding, EmbeddingInvocationService):
            raise RuntimeError("embedding_invocation service does not support usage recovery")
        recovered = 0
        for pending_run_id in run_ids:
            # `started` 但没有 result+settlement 的窗口必须先持久化为
            # needs_review；后面的 usage recovery 只能补投确定结果，不能重放
            # provider。not_started 则保留给同 identity 的安全执行重试。
            async with self._storage.uow() as uow:
                pending_run = await uow.runs.get(pending_run_id)
                if pending_run is not None:
                    owner_run_id = pending_run.parent_run_id or pending_run.id
                    ledger = await uow.shared_budget.get_ledger(pending_run.tenant_id, owner_run_id)
                    if ledger is not None:
                        recovered += await uow.shared_budget.recover_unknown_started(
                            tenant_id=pending_run.tenant_id,
                            budget_owner_run_id=owner_run_id,
                        )
                        await uow.commit()
            if isinstance(model, ModelInvocationService):
                recovered += await model.recover_pending(run_id=pending_run_id)
            if isinstance(embedding, EmbeddingInvocationService):
                recovered += await embedding.recover_pending(run_id=pending_run_id)
        return recovered

    async def recover_pending_delegation_evidence(self, *, run_id: str) -> int:
        """在 parent retry/WAITING 收口前推进已提交 delegation operation。"""

        service = self._executor_services.get("agent.delegate")
        if service is None:
            return 0
        if not isinstance(service, PendingDelegationRecovery):
            raise RuntimeError("agent.delegate service does not support durable recovery")
        return await service.recover_pending_for_parent(parent_run_id=run_id)

    async def _recover_delegation_after_wait(self, result: RunResult) -> RunResult:
        """pending claim 可在 executor 已退出后恢复；随后返回最新 durable 状态。"""

        if result.status != RunStatus.WAITING:
            return result
        recovered = await self.recover_pending_delegation_evidence(run_id=result.run_id)
        if recovered == 0:
            return result
        async with self._storage.uow() as uow:
            run = await uow.runs.get(result.run_id)
        if run is None:
            raise LookupError(f"run not found: {result.run_id}")
        status = RunStatus(run.status)
        terminal_event = None
        if status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            terminal = await self._event_bus.terminal_event(result.run_id)
            terminal_event = terminal.event_type.value if terminal is not None else None
        return RunResult(
            run_id=result.run_id,
            status=status,
            terminal_event=terminal_event,
            resume_token=result.resume_token if status == RunStatus.WAITING else None,
        )

    @property
    def uses_queue(self) -> bool:
        """报告当前 composition 是否为 service queued mode。"""

        return self._queue is not None

    @asynccontextmanager
    async def coordinate_run_submission(
        self,
        *,
        agent_id: str,
        idempotency_key: IdempotencyKey | str | None,
        trace_id: str,
        identity: IdentityContext | None = None,
    ) -> AsyncGenerator[None]:
        """串行化同一幂等键及全局 trace 的完整副作用窗口。

        Storage 根据后端用 PostgreSQL session advisory lock、file SQLite 文件锁或
        memory SQLite loop lock 实现。调用方先做无副作用预检以固定候选 trace，
        再在这里按固定顺序取得幂等锁和全局 trace 锁；锁内必须重新 prepare，
        并根据 ``replays_existing`` 跳过前置副作用。
        """

        key_value = idempotency_value(idempotency_key)
        canonical_trace_id = normalize_trace_id(trace_id)
        active_identity = identity or self._identity
        async with AsyncExitStack() as stack:
            if key_value is not None:
                idempotency_scope = "\x1f".join(
                    (
                        "idempotency",
                        active_identity.tenant_id,
                        active_identity.session_id,
                        agent_id,
                        key_value,
                    )
                )
                await stack.enter_async_context(
                    self._storage.idempotency_request_lock(idempotency_scope)
                )
            trace_scope = "\x1f".join(("trace", canonical_trace_id))
            await stack.enter_async_context(self._storage.idempotency_request_lock(trace_scope))
            yield

    # 以下声明是 mixin 间的内部协作契约。最终实现由对应职责 mixin 提供，
    # provider-neutral runtime 不依赖 API、worker 或 broker 的具体 composition。
    async def _checkpoint(
        self,
        run_id: str,
        agent_id: str,
        state: dict[str, Any],
        *,
        identity: IdentityContext,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> ResumeToken:
        """由 checkpoint mixin 实现：持久化可续跑状态并返回受限 resume token。"""

        raise NotImplementedError

    async def _fail_execution(
        self,
        run_id: str,
        agent_id: str,
        reason: str,
        *,
        identity: IdentityContext,
        request_id: str | None = None,
        trace_id: str | None = None,
        input: dict[str, Any] | None = None,
        defer_terminal: bool = False,
        approval_recovery: dict[str, Any] | None = None,
    ) -> RunResult:
        """由生命周期 mixin 实现：失败收口、可选延迟终态及审批恢复信息持久化。"""

        raise NotImplementedError

    async def _complete(
        self,
        run_id: str,
        agent_id: str,
        output: dict[str, Any],
        *,
        identity: IdentityContext,
        request_id: str | None = None,
        trace_id: str | None = None,
        input: dict[str, Any] | None = None,
        defer_terminal: bool = False,
    ) -> CanonicalEvent | None:
        """由生命周期 mixin 实现：成功收口并在适用时发布终态事件。"""

        raise NotImplementedError

    async def _defer_pending_delegation_terminal(
        self,
        *,
        run_id: str,
        status: RunStatus,
        identity: IdentityContext,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        approval_recovery: dict[str, Any] | None = None,
    ) -> RunResult | None:
        """由委派生命周期 mixin 实现：存在未完成子操作时延后 parent 终态。"""

        raise NotImplementedError

    async def _apply_execution_result(
        self,
        request: AgentExecutionRequest,
        result: AgentExecutionResult,
        *,
        context: AgentExecutionContext,
        defer_terminal: bool = False,
        approval_recovery: dict[str, Any] | None = None,
    ) -> RunResult:
        """由执行结果 mixin 实现：把 executor 返回值映射为持久化运行状态与事件。"""

        raise NotImplementedError

    async def fail_run(
        self,
        run_id: str,
        *,
        reason: str,
        identity: IdentityContext | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        input: dict[str, Any] | None = None,
        defer_terminal: bool = False,
    ) -> RunResult:
        """由生命周期 mixin 实现的公开失败入口，保留统一身份与终态控制参数。"""

        raise NotImplementedError
