"""RUN-003 JSON events 与 RUN-006 SSE transport routes。"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from agent_harness.events import EventReader, EventSink
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import RunOrchestrator
from app.api.dependencies import current_identity, get_policy_engine
from app.api.routes.run_support import (
    RunEventsResponse,
    error_responses,
    get_event_sink,
    get_run_orchestrator,
    public_events,
    request_id_from,
)
from app.api.sse import MAX_EVENT_SEQ, stream_run_events, validate_stream_cursor

router = APIRouter(prefix="/api/v1", tags=["runs"])


async def _check_internal_event_permission(
    *,
    policy: PolicyEngine | None,
    identity: IdentityContext,
    run_id: str,
) -> None:
    """internal visibility 只做策略判定，不写业务 audit/evidence。"""

    engine = policy or PolicyEngine(provider=YamlPolicyProvider.default())
    await engine.require_allowed_readonly(
        PolicyCheck(
            actor=identity,
            action="events.read_internal",
            resource=f"run:{run_id}:events",
            context={"include_internal": True},
        )
    )


@router.get(
    "/runs/{run_id}/events",
    response_model=RunEventsResponse,
    responses=error_responses(401, 403, 404, 422, 500),
)
async def read_run_events(
    http_request: Request,
    run_id: str,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    event_sink: Annotated[EventSink, Depends(get_event_sink)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
    policy: Annotated[PolicyEngine | None, Depends(get_policy_engine)],
    after_seq: int = Query(default=0, ge=0),
    include_internal: bool = Query(default=False),
) -> RunEventsResponse:
    """按 seq 读取 JSON event，保持 RUN-003 的兼容响应。"""

    await orchestrator.authorize_run_read(run_id, identity=identity)
    if include_internal:
        await _check_internal_event_permission(policy=policy, identity=identity, run_id=run_id)
    events = await event_sink.read(run_id=run_id, after_seq=after_seq)
    return RunEventsResponse(
        request_id=request_id_from(http_request),
        events=public_events(events, include_internal=include_internal),
    )


@router.get(
    "/runs/{run_id}/events/stream",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "CanonicalEvent SSE stream",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **error_responses(401, 403, 404, 422, 500),
    },
)
async def stream_run_event_frames(
    http_request: Request,
    run_id: str,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    event_sink: Annotated[EventSink, Depends(get_event_sink)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
    policy: Annotated[PolicyEngine | None, Depends(get_policy_engine)],
    last_event_id: Annotated[
        int,
        Header(alias="Last-Event-ID", ge=0, le=MAX_EVENT_SEQ),
    ] = 0,
    include_internal: bool = Query(default=False),
    _accept: Annotated[str, Header(alias="Accept")] = "text/event-stream",
) -> StreamingResponse:
    """建立 RUN-006；所有可判定错误都在握手前完成。"""

    authorization = await orchestrator.authorize_run_read(run_id, identity=identity)
    if include_internal:
        await _check_internal_event_permission(policy=policy, identity=identity, run_id=run_id)

    reader = cast(EventReader, event_sink)
    await validate_stream_cursor(
        request=http_request,
        event_sink=reader,
        run_id=run_id,
        last_event_id=last_event_id,
        include_internal=include_internal,
    )
    return StreamingResponse(
        stream_run_events(
            request=http_request,
            event_sink=reader,
            run_id=run_id,
            after_seq=last_event_id,
            include_internal=include_internal,
            request_id=request_id_from(http_request),
            trace_id=authorization.trace_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


__all__ = ["router"]
