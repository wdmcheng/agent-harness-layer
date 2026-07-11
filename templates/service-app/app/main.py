"""service-app template 的 FastAPI 应用工厂。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent_harness.approvals import (
    ApprovalEnqueueUnavailable,
    ApprovalService,
    ApprovalStateConflict,
)
from agent_harness.auth import AuthError, TokenVerifier
from agent_harness.config import load_settings
from agent_harness.contracts import ApiErrorEnvelope, ErrorDetail
from agent_harness.evals import (
    AcceptanceService,
    EvalExperimentError,
    EvalService,
    ExperimentService,
)
from agent_harness.events import EventSink
from agent_harness.policy import InputGuardrail, PolicyDeniedError, PolicyEngine
from agent_harness.registry import RegistryLoadError
from agent_harness.runtime import InvalidRunTransition, RunEnqueueUnavailable, RunOrchestrator
from agent_harness.security.redaction import redact_secrets
from app.api.dependencies import (
    get_acceptance_service,
    get_approval_service,
    get_auth_verifier,
    get_eval_service,
    get_experiment_service,
    get_input_guardrail,
    get_optional_approval_service,
    get_policy_engine,
)
from app.api.routes.agents import get_agent_registry as get_agents_route_registry
from app.api.routes.agents import router as agents_router
from app.api.routes.approvals import router as approvals_router
from app.api.routes.evals import router as evals_router
from app.api.routes.health import get_health_summary, health_summary_from_settings
from app.api.routes.health import router as health_router
from app.api.routes.policies import router as policies_router
from app.api.routes.runs import (
    get_agent_registry as get_runs_route_registry,
)
from app.api.routes.runs import get_event_sink, get_run_orchestrator, request_id_from
from app.api.routes.runs import router as runs_router
from app.runtime import RuntimeComponents, build_runtime_components


def api_error_response(
    *,
    request_id: str,
    code: str,
    message: str,
    status_code: int,
    field_path: str | None = None,
    hint: str | None = None,
) -> JSONResponse:
    """把内部结构化异常转换成统一 ApiErrorEnvelope。"""

    envelope = ApiErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=str(redact_secrets(message)),
            request_id=request_id,
            field_path=field_path,
            hint=None if hint is None else str(redact_secrets(hint)),
        )
    )
    return JSONResponse(status_code=status_code, content=envelope.to_payload())


def create_app(
    *,
    orchestrator: RunOrchestrator | None = None,
    event_sink: EventSink | None = None,
    profile: str = "local",
    profiles_dir: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
    registry: object | None = None,
    auth_verifier: TokenVerifier | None = None,
    policy_engine: PolicyEngine | None = None,
    input_guardrail: InputGuardrail | None = None,
    approval_service: ApprovalService | None = None,
    eval_service: EvalService | None = None,
    experiment_service: ExperimentService | None = None,
    acceptance_service: AcceptanceService | None = None,
) -> FastAPI:
    """创建已注册 run routes 的 FastAPI app。

    测试可以传入 orchestrator/event_sink 直接验证 route 适配层；真实 service
    启动则从 profile 构造 RuntimeComponents，并由 lifespan 统一释放 storage engine。
    """

    settings = load_settings(profile=profile, profiles_dir=profiles_dir)
    health_summary = health_summary_from_settings(settings)
    components: RuntimeComponents | None = None
    if orchestrator is None or event_sink is None:
        components = build_runtime_components(
            profile=profile,
            profiles_dir=profiles_dir,
            storage_dsn=storage_dsn,
            events_path=events_path,
        )
        orchestrator = orchestrator or components.orchestrator
        event_sink = event_sink or components.event_sink
        registry = registry or components.registry
        auth_verifier = auth_verifier or components.auth_verifier
        policy_engine = policy_engine or components.policy_engine
        input_guardrail = input_guardrail or components.input_guardrail
        approval_service = approval_service or components.approval_service
        eval_service = eval_service or components.eval_service
        experiment_service = experiment_service or components.experiment_service
        acceptance_service = acceptance_service or components.acceptance_service

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            if components is not None:
                await components.close()

    app = FastAPI(title="Agent Harness Service", lifespan=lifespan)
    app.include_router(agents_router)
    app.include_router(runs_router)
    app.include_router(approvals_router)
    app.include_router(policies_router)
    app.include_router(evals_router)
    app.include_router(health_router)

    async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        http_exc = cast(HTTPException, exc)
        code = "api.not_found" if http_exc.status_code == 404 else "api.http_error"
        return api_error_response(
            request_id=request_id_from(request),
            code=code,
            message=str(http_exc.detail),
            status_code=http_exc.status_code,
        )

    async def lookup_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return api_error_response(
            request_id=request_id_from(request),
            code="api.not_found",
            message=str(exc),
            status_code=404,
        )

    async def invalid_transition_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        return api_error_response(
            request_id=request_id_from(request),
            code="run.invalid_transition",
            message=str(exc),
            status_code=409,
        )

    async def run_enqueue_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return api_error_response(
            request_id=request_id_from(request),
            code="run.enqueue_unavailable",
            message="run queue is temporarily unavailable",
            status_code=503,
        )

    async def auth_error_handler(request: Request, exc: Exception) -> JSONResponse:
        auth_exc = cast(AuthError, exc)
        return api_error_response(
            request_id=request_id_from(request),
            code=auth_exc.code,
            message=str(auth_exc),
            status_code=auth_exc.status_code,
        )

    async def policy_denied_handler(request: Request, exc: Exception) -> JSONResponse:
        policy_exc = cast(PolicyDeniedError, exc)
        return api_error_response(
            request_id=request_id_from(request),
            code=policy_exc.code,
            message=str(policy_exc),
            status_code=policy_exc.status_code,
        )

    async def approval_conflict_handler(request: Request, exc: Exception) -> JSONResponse:
        approval_exc = cast(ApprovalStateConflict, exc)
        return api_error_response(
            request_id=request_id_from(request),
            code=approval_exc.code,
            message=str(approval_exc),
            status_code=approval_exc.status_code,
        )

    async def approval_enqueue_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return api_error_response(
            request_id=request_id_from(request),
            code="approval.enqueue_unavailable",
            message="approval queue is temporarily unavailable",
            status_code=503,
        )

    async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
        validation_exc = cast(RequestValidationError, exc)
        errors = cast(list[dict[str, Any]], validation_exc.errors())
        first_error: dict[str, Any] = errors[0] if errors else {}
        raw_location = first_error.get("loc", ())
        location = (
            cast(list[object] | tuple[object, ...], raw_location)
            if isinstance(raw_location, (list, tuple))
            else ()
        )
        field_path = ".".join(str(part) for part in location) or None
        raw_error_type = first_error.get("type")
        error_type = raw_error_type if isinstance(raw_error_type, str) else None
        return api_error_response(
            request_id=request_id_from(request),
            code="validation_error",
            message="request validation failed",
            status_code=422,
            field_path=field_path,
            hint=None if error_type is None else f"validation type: {error_type}",
        )

    async def eval_experiment_error_handler(request: Request, exc: Exception) -> JSONResponse:
        eval_exc = cast(EvalExperimentError, exc)
        return api_error_response(
            request_id=request_id_from(request),
            code=eval_exc.code,
            message=str(eval_exc),
            status_code=eval_exc.status_code,
            field_path=eval_exc.field_path,
            hint=eval_exc.hint,
        )

    async def registry_load_error_handler(request: Request, exc: Exception) -> JSONResponse:
        registry_exc = cast(RegistryLoadError, exc)
        error = registry_exc.error_details[0]
        status_code = 422
        if error.code == "registry.duplicate_agent_id":
            status_code = 409
        elif error.code == "registry.agent_not_found":
            status_code = 404
        envelope = ApiErrorEnvelope(
            error=ErrorDetail(
                code=error.code,
                message=error.message,
                request_id=request_id_from(request),
                field_path=error.field_path,
                hint=error.hint,
            )
        )
        return JSONResponse(status_code=status_code, content=envelope.to_payload())

    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # 兜底 handler 是 API error envelope 的最后防线。具体异常可以在上面
        # 细分状态码，但不能让内部 RuntimeError/ValueError 退回 FastAPI 默认 detail，
        # 更不能把尚未识别的 DSN、路径或 provider 异常文本放进公开响应。
        return api_error_response(
            request_id=request_id_from(request),
            code="api.internal_error",
            message="internal server error",
            status_code=500,
        )

    app.add_exception_handler(HTTPException, http_exception_handler)
    # Starlette 负责生成未知 route 的 404；同时注册基类才能让未定义
    # `/api/v1/tools` 等路径也保持统一 envelope，而不是退回默认 detail。
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(AuthError, auth_error_handler)
    app.add_exception_handler(PolicyDeniedError, policy_denied_handler)
    app.add_exception_handler(ApprovalStateConflict, approval_conflict_handler)
    app.add_exception_handler(ApprovalEnqueueUnavailable, approval_enqueue_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(EvalExperimentError, eval_experiment_error_handler)
    app.add_exception_handler(LookupError, lookup_error_handler)
    app.add_exception_handler(InvalidRunTransition, invalid_transition_handler)
    app.add_exception_handler(RunEnqueueUnavailable, run_enqueue_error_handler)
    app.add_exception_handler(RegistryLoadError, registry_load_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)

    # dependency override 让 route module 保持薄适配层；profile loading、migration
    # 和 engine lifecycle 都留在 application factory。
    app.dependency_overrides[get_run_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_event_sink] = lambda: event_sink
    app.dependency_overrides[get_agents_route_registry] = lambda: registry
    app.dependency_overrides[get_runs_route_registry] = lambda: registry
    app.dependency_overrides[get_auth_verifier] = lambda: auth_verifier
    app.dependency_overrides[get_policy_engine] = lambda: policy_engine
    app.dependency_overrides[get_input_guardrail] = lambda: input_guardrail
    app.dependency_overrides[get_health_summary] = lambda: health_summary
    if approval_service is not None:
        app.dependency_overrides[get_approval_service] = lambda: approval_service
        app.dependency_overrides[get_optional_approval_service] = lambda: approval_service
    if eval_service is not None:
        app.dependency_overrides[get_eval_service] = lambda: eval_service
    if experiment_service is not None:
        app.dependency_overrides[get_experiment_service] = lambda: experiment_service
    if acceptance_service is not None:
        app.dependency_overrides[get_acceptance_service] = lambda: acceptance_service

    generated_openapi = app.openapi

    def evl_openapi() -> dict[str, Any]:
        """移除 FastAPI 自动追加但不属于 EVL-004 的 GET 422 响应。"""

        schema = generated_openapi()
        paths = cast(dict[str, Any], schema["paths"])
        expected = {
            ("/api/v1/evals/experiments", "post"): {
                "200",
                "201",
                "401",
                "403",
                "404",
                "409",
                "422",
                "500",
            },
            ("/api/v1/evals/experiments/{experiment_id}", "get"): {
                "200",
                "401",
                "403",
                "404",
                "500",
            },
            ("/api/v1/evals/experiments/{experiment_id}/comparison", "get"): {
                "200",
                "401",
                "403",
                "404",
                "409",
                "500",
            },
            ("/api/v1/evals/experiments/{experiment_id}/accept", "post"): {
                "200",
                "401",
                "403",
                "404",
                "409",
                "422",
                "500",
            },
        }
        for (path, method), allowed in expected.items():
            operation = cast(dict[str, Any], paths[path][method])
            responses = cast(dict[str, Any], operation["responses"])
            operation["responses"] = {
                status: payload for status, payload in responses.items() if status in allowed
            }
        return schema

    app.openapi = evl_openapi
    return app
