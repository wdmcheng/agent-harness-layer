"""Eval Gate API 路由。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import Field, StringConstraints, ValidationError

from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.evals import (
    AcceptanceService,
    BehaviorTag,
    EvalExperimentError,
    EvalRunner,
    EvalService,
    EvalTraceSource,
    ExperimentAcceptanceRequest,
    ExperimentComparison,
    ExperimentCreateRequest,
    ExperimentResult,
    ExperimentService,
    ExperimentStatus,
    HarnessVersionManifest,
    RegressionPolicy,
)
from agent_harness.evals.experiment_models import (
    FailureDifference,
    PerTagComparison,
    Recommendation,
    RecommendationReasonCode,
)
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyEngine, YamlPolicyProvider
from agent_harness.storage import EvalCaseRecord, EvalScoreRecord
from app.api.dependencies import (
    current_identity,
    get_acceptance_service,
    get_eval_service,
    get_experiment_service,
    get_policy_engine,
)
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

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
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


class EvalExperimentCreateRequest(HarnessDTO):
    """EVL-004A create body；tenant、request id 和幂等键来自 HTTP 边界。"""

    agent_id: str
    dataset: str
    tags: list[BehaviorTag] = Field(min_length=1)
    split_strategy: Literal["deterministic_multilabel_v1"]
    baseline_harness_version: HarnessVersionManifest
    candidate_harness_version: HarnessVersionManifest | None = None
    optimization_ratio: float = Field(default=0.8, gt=0.0, lt=1.0)
    holdout_ratio: float = Field(default=0.2, gt=0.0, lt=1.0)
    regression_policy: RegressionPolicy = Field(default_factory=RegressionPolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalExperimentResponse(HarnessDTO):
    """EVL-004A/B 公开摘要；comparison 通过独立只读 endpoint 获取。"""

    request_id: str
    experiment_id: str
    status: ExperimentStatus
    agent_id: str
    dataset: str
    tags: list[str]
    optimization_case_count: int
    holdout_case_count: int
    regression_case_count: int
    baseline_harness_version: str
    candidate_harness_version: str | None = None
    baseline_eval_run_ref: str | None
    candidate_eval_run_ref: str | None = None
    local_evidence_refs: list[str]
    provider_statuses: list[dict[str, object]]


class EvalExperimentComparisonResponse(HarnessDTO):
    """EVL-004C 聚合 comparison，不内联完整 case/provider payload。"""

    request_id: str
    experiment_id: str
    candidate_harness_version: str
    per_tag: list[PerTagComparison]
    holdout_delta: float
    regressions: list[FailureDifference]
    new_failures: list[FailureDifference]
    fixed_failures: list[FailureDifference]
    acceptance_recommendation: Recommendation
    recommendation_reason_codes: list[RecommendationReasonCode] = Field(min_length=1)
    local_evidence_refs: list[str]
    provider_statuses: list[dict[str, object]]
    failure_details_ref: str | None = None


class EvalExperimentAcceptanceRequest(HarnessDTO):
    """EVL-004D body；reviewer 只能来自认证 IdentityContext。"""

    decision: Literal["accepted", "rejected"]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    accepted_harness_version: str | None = None
    followup_issue_ref: str | None = None


class EvalExperimentAcceptanceResponse(HarnessDTO):
    """人工 decision、policy、audit 与 production binding 的公开证据。"""

    request_id: str
    experiment_id: str
    decision_id: str
    decision: Literal["accepted", "rejected"]
    reviewer_id: str
    accepted_harness_version: str | None = None
    production_binding: bool
    policy_decision: dict[str, Any]
    audit_ref: str
    evidence_refs: list[str]
    followup_issue_ref: str | None = None


def _require_experiment_permission(identity: IdentityContext, action: str) -> None:
    """GET 不调用带 audit 的 PolicyEngine；可见性只读取认证 permission。"""

    if "*" not in identity.permissions and action not in identity.permissions:
        raise EvalExperimentError(
            "policy.denied",
            "permission missing",
            status_code=403,
        )


def _experiment_response(result: ExperimentResult) -> EvalExperimentResponse:
    payload = result.model_dump(mode="json", exclude_none=False)
    payload.pop("comparison", None)
    return EvalExperimentResponse.model_validate(payload)


def _comparison_response(
    comparison: ExperimentComparison,
) -> EvalExperimentComparisonResponse:
    return EvalExperimentComparisonResponse.model_validate(comparison.to_payload())


def _request_validation_error(exc: ValidationError) -> RequestValidationError:
    return RequestValidationError(exc.errors())


@router.post(
    "/evals/experiments",
    response_model=EvalExperimentResponse,
    status_code=201,
    responses={
        200: {"model": EvalExperimentResponse, "description": "幂等重放"},
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
    },
)
async def create_eval_experiment(
    http_request: Request,
    response: Response,
    request: EvalExperimentCreateRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[ExperimentService, Depends(get_experiment_service)],
) -> EvalExperimentResponse:
    """从当前 tenant 的 approved tagged cases 创建或幂等重放 experiment。"""

    _require_experiment_permission(identity, "eval.experiment.create")
    normalized_key = idempotency_key.strip()
    if not normalized_key:
        raise EvalExperimentError(
            "eval.experiment.idempotency_key_required",
            "Idempotency-Key must not be blank",
            status_code=422,
            field_path="Idempotency-Key",
        )
    request_id = request_id_from(http_request)
    try:
        create_request = ExperimentCreateRequest(
            request_id=request_id,
            tenant_id=identity.tenant_id,
            idempotency_key=normalized_key,
            **request.to_payload(),
        )
    except ValidationError as exc:
        raise _request_validation_error(exc) from exc
    outcome = await service.create(create_request)
    response.status_code = 201 if outcome.created else 200
    return _experiment_response(outcome.result)


@router.get(
    "/evals/experiments/{experiment_id}",
    response_model=EvalExperimentResponse,
    responses={404: {"model": ApiErrorEnvelope}},
)
async def get_eval_experiment(
    http_request: Request,
    experiment_id: str,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[ExperimentService, Depends(get_experiment_service)],
) -> EvalExperimentResponse:
    """读取 tenant-scoped persisted experiment，不重跑 evaluator/provider。"""

    _require_experiment_permission(identity, "eval.experiment.read")
    result = await service.get(
        tenant_id=identity.tenant_id,
        experiment_id=experiment_id,
        request_id=request_id_from(http_request),
    )
    return _experiment_response(result)


@router.get(
    "/evals/experiments/{experiment_id}/comparison",
    response_model=EvalExperimentComparisonResponse,
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
    },
)
async def get_eval_experiment_comparison(
    http_request: Request,
    experiment_id: str,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[ExperimentService, Depends(get_experiment_service)],
) -> EvalExperimentComparisonResponse:
    """读取 create 阶段已持久化的 comparison。"""

    _require_experiment_permission(identity, "eval.experiment.read")
    result = await service.compare(
        tenant_id=identity.tenant_id,
        experiment_id=experiment_id,
        request_id=request_id_from(http_request),
    )
    return _comparison_response(result)


@router.post(
    "/evals/experiments/{experiment_id}/accept",
    response_model=EvalExperimentAcceptanceResponse,
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
    },
)
async def accept_eval_experiment(
    http_request: Request,
    experiment_id: str,
    request: EvalExperimentAcceptanceRequest,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    service: Annotated[AcceptanceService, Depends(get_acceptance_service)],
) -> EvalExperimentAcceptanceResponse:
    """由认证 reviewer 记录唯一 accepted/rejected decision。"""

    try:
        acceptance_request = ExperimentAcceptanceRequest(
            request_id=request_id_from(http_request),
            **request.to_payload(),
        )
    except ValidationError as exc:
        raise _request_validation_error(exc) from exc
    result = await service.decide(
        actor=identity,
        experiment_id=experiment_id,
        request=acceptance_request,
    )
    return EvalExperimentAcceptanceResponse.model_validate(result.to_payload())


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


@router.post(
    "/eval-cases/{case_id}/approve",
    response_model=EvalCaseResponse,
    responses={
        404: {"model": ApiErrorEnvelope},
        409: {"model": ApiErrorEnvelope},
    },
)
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


@router.get(
    "/evals/runs/{eval_run_id}",
    response_model=EvalRunResponse,
    responses={404: {"model": ApiErrorEnvelope}},
)
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


@router.get(
    "/evals/runs/{eval_run_id}/scores",
    response_model=EvalScoresResponse,
    responses={404: {"model": ApiErrorEnvelope}},
)
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
