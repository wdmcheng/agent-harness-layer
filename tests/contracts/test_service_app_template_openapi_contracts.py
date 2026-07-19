"""service-app 公开管理面与 HTTP 漂移合同测试。

这些用例只穿过 FastAPI/ASGI/OpenAPI 公开 seam。operation 矩阵显式绑定
`API-Contract.md`，避免新 route 只在代码里存在、却没有认证和错误模型声明。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from tests.contracts.auth_policy_hitl_contract_helpers import (
    ROOT,
    asgi_request,
    descriptor,
)

from agent_harness.events import LocalJsonlEventSink
from agent_harness.registry import AgentRegistry
from app.api.routes.health import get_health_summary
from app.main import create_app

PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


@dataclass(frozen=True, slots=True)
class OperationContract:
    """单个公开 operation 的 OpenAPI 漂移基线。"""

    path: str
    method: str
    response_schema: str | None
    error_statuses: tuple[str, ...]
    success_media_type: str = "application/json"


OPERATIONS = (
    OperationContract(
        "/api/v1/agents", "get", "AgentListResponse", ("401", "403", "409", "422", "500")
    ),
    OperationContract(
        "/api/v1/agents/{agent_id}/runs",
        "post",
        "RunCreateResponse",
        ("400", "401", "403", "404", "409", "422", "500", "503"),
    ),
    OperationContract(
        "/api/v1/runs/{run_id}", "get", "RunDetailResponse", ("401", "403", "404", "500")
    ),
    OperationContract(
        "/api/v1/runs/{run_id}/events",
        "get",
        "RunEventsResponse",
        ("401", "403", "404", "422", "500"),
    ),
    OperationContract(
        "/api/v1/runs/{run_id}/events/stream",
        "get",
        None,
        ("401", "403", "404", "422", "500"),
        "text/event-stream",
    ),
    OperationContract(
        "/api/v1/runs/{run_id}/cancel",
        "post",
        "RunCreateResponse",
        ("401", "403", "404", "409", "500"),
    ),
    OperationContract(
        "/api/v1/runs/{run_id}/resume",
        "post",
        "RunCreateResponse",
        ("401", "403", "404", "409", "422", "500"),
    ),
    OperationContract(
        "/api/v1/runs/{run_id}/approvals",
        "get",
        "ApprovalListResponse",
        ("401", "403", "404", "500"),
    ),
    OperationContract(
        "/api/v1/runs/{run_id}/approvals/{approval_id}",
        "get",
        "ApprovalDetailResponse",
        ("401", "403", "404", "422", "500"),
    ),
    OperationContract(
        "/api/v1/runs/{run_id}/approvals/{approval_id}",
        "post",
        "ApprovalResolveResponse",
        ("401", "403", "404", "409", "422", "500", "503"),
    ),
    OperationContract(
        "/api/v1/policies/check",
        "post",
        "PolicyDecisionResponse",
        ("401", "403", "422", "500"),
    ),
    OperationContract(
        "/api/v1/eval-cases/drafts",
        "post",
        "EvalCaseResponse",
        ("401", "403", "422", "500"),
    ),
    OperationContract(
        "/api/v1/eval-cases/drafts",
        "get",
        "EvalCaseListResponse",
        ("401", "403", "422", "500"),
    ),
    OperationContract(
        "/api/v1/eval-cases/{case_id}/approve",
        "post",
        "EvalCaseResponse",
        ("401", "403", "404", "409", "422", "500"),
    ),
    OperationContract(
        "/api/v1/eval-cases/approved",
        "get",
        "EvalCaseListResponse",
        ("401", "403", "422", "500"),
    ),
    OperationContract(
        "/api/v1/evals/runs",
        "post",
        "EvalRunResponse",
        ("401", "403", "422", "500"),
    ),
    OperationContract(
        "/api/v1/evals/runs/{eval_run_id}",
        "get",
        "EvalRunResponse",
        ("401", "403", "404", "422", "500"),
    ),
    OperationContract(
        "/api/v1/evals/runs/{eval_run_id}/scores",
        "get",
        "EvalScoresResponse",
        ("401", "403", "404", "422", "500"),
    ),
    OperationContract(
        "/api/v1/evals/experiments",
        "post",
        "EvalExperimentResponse",
        ("401", "403", "404", "409", "422", "500"),
    ),
    OperationContract(
        "/api/v1/evals/experiments/{experiment_id}",
        "get",
        "EvalExperimentResponse",
        ("401", "403", "404", "500"),
    ),
    OperationContract(
        "/api/v1/evals/experiments/{experiment_id}/comparison",
        "get",
        "EvalExperimentComparisonResponse",
        ("401", "403", "404", "409", "500"),
    ),
    OperationContract(
        "/api/v1/evals/experiments/{experiment_id}/accept",
        "post",
        "EvalExperimentAcceptanceResponse",
        ("401", "403", "404", "409", "422", "500"),
    ),
    OperationContract("/api/v1/health", "get", "HealthResponse", ("500",)),
)


def build_contract_app(tmp_path: Path, *, profile: str = "local") -> Any:
    """构造不触发真实 storage/provider 的 app，保留完整公开路由表。"""

    return create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / f"{profile}-events.jsonl"),
        registry=AgentRegistry([descriptor()]),
        approval_service=cast(Any, object()),
        eval_service=cast(Any, object()),
        profile=profile,
        profiles_dir=PROFILES,
    )


def _response_ref(operation: dict[str, Any], status: str) -> str:
    """提取 JSON 响应 schema 引用，供多个公开接口断言复用同一漂移判定。"""

    schema = operation["responses"][status]["content"]["application/json"]["schema"]
    return cast(str, schema["$ref"])


__all__ = [
    "AgentRegistry",
    "Any",
    "LocalJsonlEventSink",
    "OPERATIONS",
    "OperationContract",
    "PROFILES",
    "Path",
    "ROOT",
    "TestClient",
    "_response_ref",
    "asgi_request",
    "build_contract_app",
    "cast",
    "create_app",
    "dataclass",
    "descriptor",
    "get_health_summary",
    "pytest",
]
