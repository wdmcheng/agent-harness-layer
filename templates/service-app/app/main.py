"""service-app template 的 FastAPI 应用工厂。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from agent_harness.contracts import ApiErrorEnvelope, ErrorDetail
from agent_harness.events import EventSink
from agent_harness.runtime import InvalidRunTransition, RunOrchestrator
from app.api.routes.runs import get_event_sink, get_run_orchestrator, request_id_from
from app.api.routes.runs import router as runs_router
from app.runtime import RuntimeComponents, build_runtime_components


def api_error_response(
    *,
    request_id: str,
    code: str,
    message: str,
    status_code: int,
) -> JSONResponse:
    envelope = ApiErrorEnvelope(
        error=ErrorDetail(code=code, message=message, request_id=request_id)
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
) -> FastAPI:
    """创建已注册 run routes 的 FastAPI app。

    测试可以传入 orchestrator/event_sink 直接验证 route 适配层；真实 service
    启动则从 profile 构造 RuntimeComponents，并由 lifespan 统一释放 storage engine。
    """

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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        try:
            yield
        finally:
            if components is not None:
                await components.close()

    app = FastAPI(title="Agent Harness Service", lifespan=lifespan)
    app.include_router(runs_router)

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

    async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # 兜底 handler 是 API error envelope 的最后防线。具体异常可以在上面
        # 细分状态码，但不能让内部 RuntimeError/ValueError 退回 FastAPI 默认 detail。
        return api_error_response(
            request_id=request_id_from(request),
            code="api.internal_error",
            message=str(exc),
            status_code=500,
        )

    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(LookupError, lookup_error_handler)
    app.add_exception_handler(InvalidRunTransition, invalid_transition_handler)
    app.add_exception_handler(Exception, internal_error_handler)

    # dependency override 让 route module 保持薄适配层；profile loading、migration
    # 和 engine lifecycle 都留在 application factory。
    app.dependency_overrides[get_run_orchestrator] = lambda: orchestrator
    app.dependency_overrides[get_event_sink] = lambda: event_sink
    return app
