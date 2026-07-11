"""RunOrchestrator 内部依赖容器与跨职责协作契约。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_harness.events import CanonicalEvent, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.runtime.checkpoints import ResumeToken
from agent_harness.runtime.executor import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutorResolver,
    RunResult,
)
from agent_harness.runtime.queue import RunQueue
from agent_harness.storage import SQLAlchemyStorage


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
