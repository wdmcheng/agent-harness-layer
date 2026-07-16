"""由 RunOrchestrator 支撑的 run API 适配层。"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import Field

from agent_harness.approvals import ApprovalService
from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.delegation import DelegationService, DelegationSummary
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventSink
from agent_harness.identity import IdentityContext
from agent_harness.policy import (
    InputGuardrail,
    PolicyCheck,
    PolicyDeniedError,
    PolicyEngine,
    YamlPolicyProvider,
)
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator, RunStatus
from app.api.dependencies import (
    current_identity,
    get_input_guardrail,
    get_optional_approval_service,
    get_policy_engine,
)

router = APIRouter(prefix="/api/v1", tags=["runs"])


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
    """run create/detail/cancel/resume 共用的公开响应。"""

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


async def create_run_with_orchestrator(
    request: RunCreateRequest,
    *,
    orchestrator: RunOrchestrator,
    identity: IdentityContext | None = None,
    policy: PolicyEngine | None = None,
    input_guardrail: InputGuardrail | None = None,
    approval_service: ApprovalService | None = None,
    request_id: str = "local",
    trace_id: str | None = None,
) -> RunCreateResponse:
    """API 和测试共用的 run create 适配逻辑。"""

    run_method = orchestrator.submit_run if orchestrator.uses_queue else orchestrator.start_run
    preflight_trace = await orchestrator.prepare_trace(
        agent_id=request.agent_id,
        identity=identity,
        idempotency_key=request.idempotency_key,
        trace_id=trace_id,
    )
    async with orchestrator.coordinate_run_submission(
        agent_id=request.agent_id,
        idempotency_key=request.idempotency_key,
        trace_id=preflight_trace,
        identity=identity,
    ):
        # 必须在锁内重新 prepare。并发 loser 看到首次 run 后只走 runtime replay，
        # 不重复 permission、guardrail、audit、approval 或 queue/provider 副作用。
        canonical_trace = await orchestrator.prepare_trace(
            agent_id=request.agent_id,
            identity=identity,
            idempotency_key=request.idempotency_key,
            trace_id=preflight_trace,
        )
        checkpoint_state: dict[str, Any] | None = None
        guardrail_payload: dict[str, Any] | None = None
        if not canonical_trace.replays_existing and identity is not None:
            await _check_run_create_permission(
                policy=policy,
                identity=identity,
                agent_id=request.agent_id,
                request_id=request_id,
            )
        if (
            not canonical_trace.replays_existing
            and identity is not None
            and input_guardrail is not None
        ):
            guardrail = await input_guardrail.check(
                actor=identity,
                agent_id=request.agent_id,
                input=request.input,
            )
            guardrail_payload = guardrail.to_payload()
            if guardrail.decision == GuardrailDecisionStatus.DENY.value:
                raise PolicyDeniedError(guardrail.reason)
            if guardrail.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
                checkpoint_state = {
                    "reason": guardrail.reason,
                    "policy": guardrail_payload,
                }

        run_arguments: dict[str, Any] = {
            "agent_id": request.agent_id,
            "input": request.input,
            "idempotency_key": request.idempotency_key,
            "checkpoint_state": checkpoint_state,
            "identity": identity,
            "request_id": request_id,
            "trace_id": canonical_trace,
        }
        if not orchestrator.uses_queue:
            run_arguments["pre_run_events"] = (
                [(CanonicalEventType.INPUT_GUARDRAIL_CHECKED, guardrail_payload)]
                if guardrail_payload is not None
                else None
            )
        result = await run_method(**run_arguments)
        if orchestrator.uses_queue and identity is not None and guardrail_payload is not None:
            await orchestrator.record_guardrail_check(
                run_id=result.run_id,
                agent_id=request.agent_id,
                identity=identity,
                payload=guardrail_payload,
                request_id=request_id,
                trace_id=canonical_trace,
            )
        if (
            identity is not None
            and approval_service is not None
            and checkpoint_state is not None
            and result.resume_token is not None
        ):
            await approval_service.require_approval(
                actor=identity,
                run_id=result.run_id,
                agent_id=request.agent_id,
                action="input.prompt_injection",
                resource=f"agent:{request.agent_id}:input",
                reason=checkpoint_state["reason"],
                resume_token=result.resume_token,
                request_id=request_id,
                trace_id=canonical_trace,
            )
    return RunCreateResponse(
        request_id=request_id,
        run_id=result.run_id,
        status=result.status,
        terminal_event=result.terminal_event,
    )


async def get_run_with_orchestrator(
    run_id: str,
    *,
    orchestrator: RunOrchestrator,
    identity: IdentityContext | None = None,
    delegation_service: DelegationService | None = None,
    request_id: str = "local",
) -> RunDetailResponse:
    """API 和测试共用的 run detail 适配逻辑。"""

    result = await orchestrator.get_run_detail(run_id, identity=identity)
    summary = (
        None
        if identity is None or delegation_service is None
        else await delegation_service.get_parent_summary(
            tenant_id=identity.tenant_id,
            parent_run_id=run_id,
        )
    )
    return RunDetailResponse(
        request_id=request_id,
        run_id=result.run_id,
        agent_id=result.agent_id,
        status=result.status,
        terminal_event=result.terminal_event,
        parent_run_id=result.parent_run_id,
        delegation_summary=summary,
    )


@router.post(
    "/agents/{agent_id}/runs",
    response_model=RunCreateResponse,
    responses={
        202: {"model": RunCreateResponse},
        **error_responses(400, 401, 403, 404, 409, 422, 500, 503),
    },
)
async def create_agent_run(
    http_request: Request,
    response: Response,
    agent_id: str,
    request: AgentRunCreateRequest,
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
    registry: Annotated[AgentRegistry, Depends(get_agent_registry)],
    identity: Annotated[IdentityContext, Depends(current_identity)],
    input_guardrail: Annotated[InputGuardrail | None, Depends(get_input_guardrail)],
    approval_service: Annotated[ApprovalService | None, Depends(get_optional_approval_service)],
    policy: Annotated[PolicyEngine | None, Depends(get_policy_engine)],
    trace_id: Annotated[str | None, Header(alias="X-Trace-Id")] = None,
) -> RunCreateResponse:
    """创建 agent-scoped run，agent_id 来自稳定 URL 边界。"""

    registry.get(agent_id)
    result = await create_run_with_orchestrator(
        RunCreateRequest(
            agent_id=agent_id,
            input=request.input,
            idempotency_key=request.idempotency_key,
        ),
        orchestrator=orchestrator,
        identity=identity,
        policy=policy,
        input_guardrail=input_guardrail,
        approval_service=approval_service,
        request_id=request_id_from(http_request),
        trace_id=trace_id,
    )
    if orchestrator.uses_queue and result.status == RunStatus.CREATED:
        response.status_code = 202
    return result


@router.get(
    "/runs/{run_id}",
    response_model=RunDetailResponse,
    responses=error_responses(401, 403, 404, 500),
)
async def get_run(
    http_request: Request,
    run_id: str,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
    delegation_service: Annotated[
        DelegationService | None,
        Depends(get_delegation_service),
    ],
) -> RunDetailResponse:
    """读取 run detail，不把 ORM model 暴露给 API 调用方。"""

    return await get_run_with_orchestrator(
        run_id,
        orchestrator=orchestrator,
        identity=identity,
        delegation_service=delegation_service,
        request_id=request_id_from(http_request),
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
    """按 seq 读取 event stream，供 SSE/API resume 共用。"""

    # 这里读取 CanonicalEvent DTO，而不是返回 storage/event sink 私有对象。
    await orchestrator.get_run(run_id, identity=identity)
    if include_internal:
        await _check_internal_event_permission(policy=policy, identity=identity, run_id=run_id)
    events = await event_sink.read(run_id=run_id, after_seq=after_seq)
    return RunEventsResponse(
        request_id=request_id_from(http_request),
        events=public_events(events, include_internal=include_internal),
    )


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunCreateResponse,
    responses=error_responses(401, 403, 404, 409, 500),
)
async def cancel_run(
    http_request: Request,
    run_id: str,
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
    identity: Annotated[IdentityContext, Depends(current_identity)],
    delegation_service: Annotated[
        DelegationService | None,
        Depends(get_delegation_service),
    ],
) -> RunCreateResponse:
    """取消尚未 terminal 的 run。"""

    request_id = request_id_from(http_request)
    # 先以公开 runtime seam 完成 tenant/identity 校验，再做内部补偿。这样重试
    # 可恢复“child 已 terminal、上次 aggregation 失败”，又不会让猜测 run_id
    # 的调用方触发跨租户写入。
    await orchestrator.get_run(run_id, identity=identity)
    if delegation_service is not None:
        await delegation_service.reconcile_child_if_delegated(run_id)
    result = await orchestrator.cancel_run(
        run_id,
        identity=identity,
        request_id=request_id,
    )
    if delegation_service is not None:
        # cancel 已持久化 child 终态；必须在响应前结算 parent aggregation，
        # 让重复请求只重放同一 final evidence，而不是遗留永久 reservation。
        await delegation_service.reconcile_child_if_delegated(run_id)
    return RunCreateResponse(
        request_id=request_id,
        run_id=result.run_id,
        status=result.status,
        terminal_event=result.terminal_event,
    )


@router.post(
    "/runs/{run_id}/resume",
    response_model=RunCreateResponse,
    responses=error_responses(401, 403, 404, 409, 422, 500),
)
async def resume_run(
    http_request: Request,
    run_id: str,
    request: RunResumeRequest,
    orchestrator: Annotated[RunOrchestrator, Depends(get_run_orchestrator)],
    identity: Annotated[IdentityContext, Depends(current_identity)],
    delegation_service: Annotated[
        DelegationService | None,
        Depends(get_delegation_service),
    ],
) -> RunCreateResponse:
    """使用 resume token 恢复 checkpointed run。"""

    await orchestrator.get_run(run_id, identity=identity)
    if delegation_service is not None:
        # resume token 可能已在上次请求中消费；先补偿 terminal child，随后即使
        # runtime 返回 token conflict，durable parent 状态也已经收敛。
        await delegation_service.reconcile_child_if_delegated(run_id)
    result = await orchestrator.resume_run(
        request.resume_token,
        expected_run_id=run_id,
        identity=identity,
    )
    if delegation_service is not None and result.status in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }:
        await delegation_service.reconcile_child_if_delegated(run_id)
    return RunCreateResponse(
        request_id=request_id_from(http_request),
        run_id=result.run_id,
        status=result.status,
        terminal_event=result.terminal_event,
    )


# 兼容 contract tests 和 template examples：它们可以直接调用同一段适配逻辑，
# 不必为了证明 route 逻辑而启动完整 FastAPI app。
create_run_for_test = create_run_with_orchestrator


async def _check_internal_event_permission(
    *,
    policy: PolicyEngine | None,
    identity: IdentityContext,
    run_id: str,
) -> None:
    engine = policy or PolicyEngine(provider=YamlPolicyProvider.default())
    await engine.require_allowed(
        PolicyCheck(
            actor=identity,
            action="events.read_internal",
            resource=f"run:{run_id}:events",
            context={"include_internal": True},
        )
    )


async def _check_run_create_permission(
    *,
    policy: PolicyEngine | None,
    identity: IdentityContext,
    agent_id: str,
    request_id: str,
) -> None:
    engine = policy or PolicyEngine(provider=YamlPolicyProvider.default())
    await engine.require_allowed(
        PolicyCheck(
            actor=identity,
            action="run.create",
            resource=f"agent:{agent_id}:run",
            context={"agent_id": agent_id, "request_id": request_id},
        )
    )
