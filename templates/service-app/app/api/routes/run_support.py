"""Run 路由 DTO、依赖与公开 event 过滤工具。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import Request
from pydantic import Field

from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.delegation import DelegationService, DelegationSummary
from agent_harness.events import CanonicalEvent, EventSink
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator, RunStatus


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """为单个 run operation 声明实际可返回的统一错误 envelope。"""

    return {status_code: {"model": ApiErrorEnvelope} for status_code in status_codes}


class RunCreateRequest(HarnessDTO):
    """内部 run create helper 使用的完整请求 DTO。"""

    agent_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class AgentRunCreateRequest(HarnessDTO):
    """agent-scoped HTTP route 的请求体，agent_id 固定来自 URL。"""

    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class RunCreateResponse(HarnessDTO):
    """run create/cancel/resume 共用的公开响应。"""

    request_id: str
    run_id: str
    status: RunStatus
    terminal_event: str | None = None


class RunDetailResponse(HarnessDTO):
    """API Contract 5.31 的 durable run/delegation detail。"""

    request_id: str
    run_id: str
    agent_id: str
    status: RunStatus
    terminal_event: str | None
    parent_run_id: str | None
    delegation_summary: DelegationSummary | None


class RunResumeRequest(HarnessDTO):
    """checkpoint resume 的请求体；token 不会出现在公开响应里。"""

    resume_token: str


class RunEventsResponse(HarnessDTO):
    """按 seq 读取 run event stream 的公开响应。"""

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


def get_delegation_service() -> DelegationService | None:
    """真实 profile 注入 service；隔离 route test 可在无 child 基线下留空。"""

    return None


def request_id_from(request: Request | None) -> str:
    """读取或生成 API request_id。"""

    if request is None:
        return "local"
    return request.headers.get("x-request-id") or str(uuid4())


def public_events(events: list[CanonicalEvent], *, include_internal: bool) -> list[CanonicalEvent]:
    """过滤普通用户默认不可见的事件。"""

    if include_internal:
        return events
    return [event for event in events if event.visibility == "public"]


__all__ = [
    "AgentRunCreateRequest",
    "RunCreateRequest",
    "RunCreateResponse",
    "RunDetailResponse",
    "RunEventsResponse",
    "RunResumeRequest",
    "error_responses",
    "get_agent_registry",
    "get_delegation_service",
    "get_event_sink",
    "get_run_orchestrator",
    "public_events",
    "request_id_from",
]
