"""RunOrchestrator 内部依赖容器与跨职责协作契约。"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from agent_harness.events import CanonicalEvent, EventBus
from agent_harness.identity import IdentityContext
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
from agent_harness.runtime.trace import normalize_trace_id
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver


class RunEnqueueUnavailable(RuntimeError):
    """run 已持久化为 enqueue_pending，但 broker 暂时不可用。"""


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
    ) -> RunResult:
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
    ) -> CanonicalEvent:
        raise NotImplementedError

    async def _apply_execution_result(
        self,
        request: AgentExecutionRequest,
        result: AgentExecutionResult,
        *,
        context: AgentExecutionContext,
    ) -> RunResult:
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
    ) -> RunResult:
        raise NotImplementedError
