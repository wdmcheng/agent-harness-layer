"""由 RunOrchestrator 支撑的 run API 适配层。"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventSink
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator, RunStatus

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiErrorEnvelope},
    404: {"model": ApiErrorEnvelope},
    409: {"model": ApiErrorEnvelope},
    500: {"model": ApiErrorEnvelope},
}

router = APIRouter(prefix="/api/v1", tags=["runs"], responses=ERROR_RESPONSES)


class RunCreateRequest(HarnessDTO):
    agent_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class AgentRunCreateRequest(HarnessDTO):
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class RunCreateResponse(HarnessDTO):
    request_id: str
    run_id: str
    status: RunStatus
    terminal_event: str | None = None
    resume_token: str | None = None


class RunResumeRequest(HarnessDTO):
    resume_token: str


class RunEventsResponse(HarnessDTO):
    request_id: str
    events: list[CanonicalEvent]


def get_run_orchestrator() -> RunOrchestrator:
    """由应用工厂注入的 RunOrchestrator 依赖。"""

    raise RuntimeError("RunOrchestrator dependency is not configured")


def get_event_sink() -> EventSink:
    """由应用工厂注入的 event stream 读取依赖。"""

    raise RuntimeError("EventSink dependency is not configured")


def get_agent_registry() -> AgentRegistry:
    """由应用工厂注入的 AgentRegistry 依赖。"""

    raise RuntimeError("AgentRegistry dependency is not configured")


def request_id_from(request: Request | None) -> str:
    """读取或生成 API request_id。"""

    if request is None:
        return "local"
    return request.headers.get("x-request-id") or str(uuid4())


def public_events(events: list[CanonicalEvent], *, include_internal: bool) -> list[CanonicalEvent]:
    """过滤普通用户默认不可见的事件。"""

    if include_internal:
        return events
    # Product-Spec 明确 `reasoning.delta` 默认不对普通用户暴露。其他 internal
    # evidence 仍可由后续 auth/role 策略细分；这里先锁最危险的思维流泄漏边界。
    return [event for event in events if event.event_type != CanonicalEventType.REASONING_DELTA]


async def create_run_with_orchestrator(
    request: RunCreateRequest,
    *,
    orchestrator: RunOrchestrator,
    request_id: str = "local",
) -> RunCreateResponse:
    # route 必须保持薄适配层。idempotency、状态转换、事件写入和 storage
    # transaction boundary 都归 runtime 管。
    result = await orchestrator.start_run(
        agent_id=request.agent_id,
        input=request.input,
        idempotency_key=request.idempotency_key,
    )
    return RunCreateResponse(
        request_id=request_id,
        run_id=result.run_id,
        status=result.status,
        terminal_event=result.terminal_event,
        resume_token=result.resume_token.value if result.resume_token is not None else None,
    )


async def get_run_with_orchestrator(
    run_id: str,
    *,
    orchestrator: RunOrchestrator,
    request_id: str = "local",
) -> RunCreateResponse:
    result = await orchestrator.get_run(run_id)
    return RunCreateResponse(
        request_id=request_id,
        run_id=result.run_id,
        status=result.status,
        terminal_event=result.terminal_event,
        resume_token=result.resume_token.value if result.resume_token is not None else None,
    )


@router.post("/agents/{agent_id}/runs", response_model=RunCreateResponse)
async def create_agent_run(
    http_request: Request,
    agent_id: str,
    request: AgentRunCreateRequest,
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
    registry: Annotated[AgentRegistry, Depends(get_agent_registry)],
) -> RunCreateResponse:
    """创建 agent-scoped run，agent_id 来自稳定 URL 边界。"""

    registry.get(agent_id)
    return await create_run_with_orchestrator(
        RunCreateRequest(
            agent_id=agent_id,
            input=request.input,
            idempotency_key=request.idempotency_key,
        ),
        orchestrator=orchestrator,
        request_id=request_id_from(http_request),
    )


@router.get("/runs/{run_id}", response_model=RunCreateResponse)
async def get_run(
    http_request: Request,
    run_id: str,
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
) -> RunCreateResponse:
    """读取 run detail，不把 ORM model 暴露给 API 调用方。"""

    return await get_run_with_orchestrator(
        run_id,
        orchestrator=orchestrator,
        request_id=request_id_from(http_request),
    )


@router.get("/runs/{run_id}/events", response_model=RunEventsResponse)
async def read_run_events(
    http_request: Request,
    run_id: str,
    event_sink: Annotated[EventSink, Depends(get_event_sink)],
    after_seq: int = Query(default=0, ge=0),
    include_internal: bool = Query(default=False),
) -> RunEventsResponse:
    """按 seq 读取 event stream，供 SSE/API resume 共用。"""

    # 这里读取 CanonicalEvent DTO，而不是返回 storage/event sink 私有对象。
    events = await event_sink.read(run_id=run_id, after_seq=after_seq)
    return RunEventsResponse(
        request_id=request_id_from(http_request),
        events=public_events(events, include_internal=include_internal),
    )


@router.post("/runs/{run_id}/cancel", response_model=RunCreateResponse)
async def cancel_run(
    http_request: Request,
    run_id: str,
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
) -> RunCreateResponse:
    """取消尚未 terminal 的 run。"""

    result = await orchestrator.cancel_run(run_id)
    return RunCreateResponse(
        request_id=request_id_from(http_request),
        run_id=result.run_id,
        status=result.status,
        terminal_event=result.terminal_event,
    )


@router.post("/runs/{run_id}/resume", response_model=RunCreateResponse)
async def resume_run(
    http_request: Request,
    run_id: str,
    request: RunResumeRequest,
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
) -> RunCreateResponse:
    """使用 resume token 恢复 checkpointed run。"""

    result = await orchestrator.resume_run(request.resume_token, expected_run_id=run_id)
    return RunCreateResponse(
        request_id=request_id_from(http_request),
        run_id=result.run_id,
        status=result.status,
        terminal_event=result.terminal_event,
    )


# 兼容 contract tests 和 template examples：它们可以直接调用同一段适配逻辑，
# 不必为了证明 route 逻辑而启动完整 FastAPI app。
create_run_for_test = create_run_with_orchestrator
