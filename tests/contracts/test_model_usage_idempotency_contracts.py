"""稳定 usage_call_id 的连续与并发重试合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_harness.embeddings import (
    EmbeddingCacheInfo,
    EmbeddingInvocationService,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
    stable_usage_call_id,
)
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations


async def _seed_run(storage: SQLAlchemyStorage) -> str:
    """创建拥有租户、会话和 trace 的最小运行，供用量结算落库。"""

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-a",
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id="session-a",
                agent_id="agent-a",
                trace_id="trace-a",
            )
        )
        await uow.commit()
        return run.id


def _context(run_id: str) -> UsageEvidenceContext:
    """构造与夹具运行一致的用量证据上下文，固定请求与追踪身份。"""

    return UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )


async def _resolve_trace(**_: object) -> str:
    """为本地事件总线返回稳定 trace，避免测试依赖运行时查询实现。"""

    return "trace-a"


class CountingModelProvider(FakeModelProvider):
    """记录 provider 调用次数的假模型，用于证明重复请求不会重放副作用。"""

    def __init__(self) -> None:
        """初始化调用计数；其余响应语义复用基础 fake provider。"""

        self.calls = 0

    async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
        """记录一次实际模型执行后返回确定性 fake 响应。"""

        self.calls += 1
        return await super().complete(request, plan=plan)


class CountingEmbeddingProvider:
    """可选阻塞的 embedding provider 替身，用于观察并发预约与实际调用次数。"""

    provider = "counting-embedding"
    model = "embedding-model"

    def __init__(self, *, release: asyncio.Event | None = None) -> None:
        """保存可选释放闸门，并初始化调用与开始信号。"""

        self.calls = 0
        self.started = asyncio.Event()
        self.release = release

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """标记真实 provider 路径已进入，必要时阻塞后返回固定 cache miss 响应。"""

        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return EmbeddingResponse(
            provider=self.provider,
            model=self.model,
            vector_ref="embedding://counting/result",
            vector=[0.25],
            cache=EmbeddingCacheInfo(
                hit=False,
                input_hash="hash-a",
                vector_ref="embedding://counting/result",
            ),
            latency_ms=1,
        )


class FailFinalOnceSink:
    """让 provider 结果先落 outbox，再在最终 event 写前制造可恢复中断。"""

    manages_event_capacity = False

    def __init__(self, delegate: LocalJsonlEventSink) -> None:
        """包装真实本地 sink，仅对第一个用量终态写入注入故障。"""

        self.delegate = delegate
        self.failed = False

    async def write(self, event: CanonicalEvent) -> CanonicalEvent:
        """首次目标事件失败，之后委托给真实 sink 以验证恢复补投。"""

        if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED and not self.failed:
            self.failed = True
            raise OSError("injected final write failure")
        return await self.delegate.write(event)

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        """转发读取协议，使替身可作为完整事件 sink 使用。"""

        return await self.delegate.read(run_id=run_id, after_seq=after_seq)

    async def latest_seq(self, run_id: str) -> int:
        """转发最新序号查询，供容量与恢复逻辑保持真实行为。"""

        return await self.delegate.latest_seq(run_id)

    async def has_terminal(self, run_id: str) -> bool:
        """转发运行终态查询，不让故障注入改变其他事件语义。"""

        return await self.delegate.has_terminal(run_id)


async def _assert_settled_once(
    *, storage: SQLAlchemyStorage, sink: LocalJsonlEventSink, run_id: str
) -> None:
    """断言一次调用最终只留下成对用量事件、已发布 outbox 与已释放容量。"""

    # event、outbox 和容量快照共同证明恢复没有重复 provider 或遗留 reservation。
    events = await sink.read(run_id=run_id)
    assert [event.event_type.value for event in events] == [
        "model.request.started",
        "model.usage.updated",
    ]
    async with storage.uow() as uow:
        capacity = await uow.event_capacity.snapshot(run_id)
        outbox = await uow.evidence_outbox.list_for_run(run_id=run_id)
        outbox_states = [item.state for item in outbox]
    assert outbox_states == ["published"]
    assert capacity.outstanding_reserved_event_count == 0
    assert capacity.highest_persisted_seq == 2
    assert capacity.terminal_reservation == 1


__all__ = [
    "CanonicalEvent",
    "CanonicalEventType",
    "CountingEmbeddingProvider",
    "CountingModelProvider",
    "EmbeddingCacheInfo",
    "EmbeddingInvocationService",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "EventBus",
    "FailFinalOnceSink",
    "FakeModelProvider",
    "LocalJsonlEventSink",
    "ModelInvocationService",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ModelRouterConfig",
    "Path",
    "RunCreate",
    "RunOrchestrator",
    "SQLAlchemyStorage",
    "SessionCreate",
    "UsageEvidenceContext",
    "_assert_settled_once",
    "_context",
    "_resolve_trace",
    "_seed_run",
    "asyncio",
    "pytest",
    "run_migrations",
    "stable_usage_call_id",
]
