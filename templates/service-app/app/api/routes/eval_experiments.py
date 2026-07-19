"""评测实验的创建、只读比较与人工验收 API 路由。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import Field, StringConstraints, ValidationError

from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.evals import (
    AcceptanceService,
    BehaviorTag,
    EvalExperimentError,
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
from app.api.dependencies import (
    current_identity,
    get_acceptance_service,
    get_experiment_service,
)
from app.api.routes.runs import request_id_from

router = APIRouter()


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
    """投影实验结果为公共摘要，刻意不在创建或读取响应内联 comparison 明细。"""
    payload = result.model_dump(mode="json", exclude_none=False)
    payload.pop("comparison", None)
    return EvalExperimentResponse.model_validate(payload)


def _comparison_response(
    comparison: ExperimentComparison,
) -> EvalExperimentComparisonResponse:
    """将已持久化比较结果转换为响应模型，不重新运行评测或 Provider。"""
    return EvalExperimentComparisonResponse.model_validate(comparison.to_payload())


def _request_validation_error(exc: ValidationError) -> RequestValidationError:
    """把领域 DTO 的校验错误交给 FastAPI 统一的 422 错误信封。"""
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


__all__ = ["router"]
