"""Eval Gate API 路由。"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.evals import EvalRunner, EvalService, EvalTraceSource
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyEngine, YamlPolicyProvider
from agent_harness.storage import EvalCaseRecord, EvalScoreRecord
from app.api.dependencies import current_identity, get_eval_service, get_policy_engine
from app.api.routes.runs import request_id_from

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ApiErrorEnvelope},
    403: {"model": ApiErrorEnvelope},
    422: {"model": ApiErrorEnvelope},
    500: {"model": ApiErrorEnvelope},
}

router = APIRouter(prefix="/api/v1", tags=["evals"], responses=ERROR_RESPONSES)


class EvalDraftCreateRequest(HarnessDTO):
    """EVL-001 draft create 请求，actor/tenant 来自认证上下文。"""

    agent_id: str
    run_id: str | None = None
    trace_id: str | None = None
    trigger: str = "failed_run"
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    expected: dict[str, Any] | None = None
    scores: dict[str, float] = Field(default_factory=dict)
    score_threshold: float | None = None
    source_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalApproveRequest(HarnessDTO):
    """EVL-002 approve 请求；case id 来自 URL 边界。"""

    reason: str
    dataset: str = "default"


class EvalRunCreateRequest(HarnessDTO):
    """EVL-003 run 请求，只消费 approved dataset。"""

    agent_id: str
    dataset: str = "default"


class EvalCaseResponse(HarnessDTO):
    """单个 eval case 响应。"""

    request_id: str
    case: EvalCaseRecord
    audit_ref: str | None = None


class EvalCaseListResponse(HarnessDTO):
    """eval case 列表响应。"""

    request_id: str
    cases: list[EvalCaseRecord]


class EvalRunResponse(HarnessDTO):
    """eval run 执行或读取响应。"""

    request_id: str
    eval_run_id: str
    status: str
    case_count: int
    score_summary: dict[str, Any] = Field(default_factory=dict)
    provider_statuses: list[Any] = Field(default_factory=list)
    local_refs: list[str] = Field(default_factory=list)


class EvalScoresResponse(HarnessDTO):
    """eval score 查询响应。"""

    request_id: str
    scores: list[EvalScoreRecord]


def _local_refs_from_summary(score_summary: dict[str, Any]) -> list[str]:
    raw_refs = score_summary.get("local_refs")
    if not isinstance(raw_refs, list):
        return []
    refs = cast(list[object], raw_refs)
    return [str(ref) for ref in refs]


async def _check_approve_permission(
    *,
    policy: PolicyEngine | None,
    identity: IdentityContext,
    case_id: str,
    dataset: str,
    reason: str,
) -> None:
    engine = policy or PolicyEngine(provider=YamlPolicyProvider.default())
    await engine.require_allowed(
        PolicyCheck(
            actor=identity,
            action="eval.case.approve",
            resource=f"eval_case:{case_id}",
            context={"dataset": dataset, "reason": reason},
        )
    )


@router.post("/eval-cases/drafts", response_model=EvalCaseResponse)
async def create_draft_eval_case(
    http_request: Request,
    request: EvalDraftCreateRequest,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[EvalService, Depends(get_eval_service)],
) -> EvalCaseResponse:
    """从 failed/low-score trace 创建 draft case；不会写 approved dataset。"""

    case = await service.draft_from_trace(
        EvalTraceSource(
            tenant_id=identity.tenant_id,
            agent_id=request.agent_id,
            run_id=request.run_id,
            trace_id=request.trace_id,
            trigger=request.trigger,
            input=request.input,
            output=request.output,
            expected=request.expected,
            scores=request.scores,
            source_refs=request.source_refs,
            artifact_refs=request.artifact_refs,
            metadata=request.metadata,
        ),
        score_threshold=request.score_threshold,
    )
    return EvalCaseResponse(request_id=request_id_from(http_request), case=case)


@router.get("/eval-cases/drafts", response_model=EvalCaseListResponse)
async def list_draft_eval_cases(
    http_request: Request,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[EvalService, Depends(get_eval_service)],
    agent_id: str | None = None,
    dataset: str | None = None,
) -> EvalCaseListResponse:
    """列出 draft review queue。"""

    cases = await service.list_cases(
        tenant_id=identity.tenant_id,
        status="draft",
        dataset=dataset,
        agent_id=agent_id,
    )
    return EvalCaseListResponse(request_id=request_id_from(http_request), cases=cases)


@router.post("/eval-cases/{case_id}/approve", response_model=EvalCaseResponse)
async def approve_eval_case(
    http_request: Request,
    case_id: str,
    request: EvalApproveRequest,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[EvalService, Depends(get_eval_service)],
    policy: Annotated[PolicyEngine | None, Depends(get_policy_engine)],
) -> EvalCaseResponse:
    """人工审核并写 approved dataset；自动 detector 不走这个入口。"""

    await _check_approve_permission(
        policy=policy,
        identity=identity,
        case_id=case_id,
        dataset=request.dataset,
        reason=request.reason,
    )
    result = await service.approve_case(
        actor=identity,
        case_id=case_id,
        reason=request.reason,
        dataset=request.dataset,
    )
    return EvalCaseResponse(
        request_id=request_id_from(http_request),
        case=result.case,
        audit_ref=result.audit_ref,
    )


@router.get("/eval-cases/approved", response_model=EvalCaseListResponse)
async def list_approved_eval_cases(
    http_request: Request,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[EvalService, Depends(get_eval_service)],
    agent_id: str | None = None,
    dataset: str | None = None,
) -> EvalCaseListResponse:
    """列出 approved dataset 摘要。"""

    cases = await service.list_cases(
        tenant_id=identity.tenant_id,
        status="approved",
        dataset=dataset,
        agent_id=agent_id,
    )
    return EvalCaseListResponse(request_id=request_id_from(http_request), cases=cases)


@router.post("/evals/runs", response_model=EvalRunResponse)
async def create_eval_run(
    http_request: Request,
    request: EvalRunCreateRequest,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[EvalService, Depends(get_eval_service)],
) -> EvalRunResponse:
    """执行 approved dataset，并返回 score sink 降级摘要。"""

    result = await EvalRunner(service=service, score_sink=service.score_sink).run_approved(
        tenant_id=identity.tenant_id,
        agent_id=request.agent_id,
        dataset=request.dataset,
    )
    return EvalRunResponse(
        request_id=request_id_from(http_request),
        eval_run_id=result.eval_run_id,
        status=result.status,
        case_count=result.case_count,
        score_summary=result.score_summary,
        provider_statuses=[
            status.to_payload() if hasattr(status, "to_payload") else status
            for status in result.provider_statuses
        ],
        local_refs=result.local_refs,
    )


@router.get("/evals/runs/{eval_run_id}", response_model=EvalRunResponse)
async def get_eval_run(
    http_request: Request,
    eval_run_id: str,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[EvalService, Depends(get_eval_service)],
) -> EvalRunResponse:
    """读取 eval run 摘要，不返回 provider 原始对象。"""

    run = await service.get_eval_run(eval_run_id)
    if run.tenant_id != identity.tenant_id:
        raise LookupError(f"eval run not found: {eval_run_id}")
    return EvalRunResponse(
        request_id=request_id_from(http_request),
        eval_run_id=run.eval_run_id,
        status=run.status,
        case_count=run.case_count,
        score_summary=run.score_summary,
        provider_statuses=run.provider_statuses,
        local_refs=_local_refs_from_summary(run.score_summary),
    )


@router.get("/evals/runs/{eval_run_id}/scores", response_model=EvalScoresResponse)
async def list_eval_scores(
    http_request: Request,
    eval_run_id: str,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[EvalService, Depends(get_eval_service)],
) -> EvalScoresResponse:
    """读取 eval score evidence 摘要。"""

    run = await service.get_eval_run(eval_run_id)
    if run.tenant_id != identity.tenant_id:
        raise LookupError(f"eval run not found: {eval_run_id}")
    return EvalScoresResponse(
        request_id=request_id_from(http_request),
        scores=await service.list_scores(eval_run_id),
    )
