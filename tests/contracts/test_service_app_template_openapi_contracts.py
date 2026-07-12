"""service-app P0 管理面与 HTTP 漂移合同测试。

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
    """单个 P0 operation 的 OpenAPI 漂移基线。"""

    path: str
    method: str
    response_schema: str
    error_statuses: tuple[str, ...]


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
        "/api/v1/runs/{run_id}", "get", "RunCreateResponse", ("401", "403", "404", "500")
    ),
    OperationContract(
        "/api/v1/runs/{run_id}/events",
        "get",
        "RunEventsResponse",
        ("401", "403", "404", "422", "500"),
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
    schema = operation["responses"][status]["content"]["application/json"]["schema"]
    return cast(str, schema["$ref"])


def test_api_contract_has_field_level_p0_baselines() -> None:
    """实现前置契约必须保留完整字段表，并明确 eval experiment 排除项。"""

    contract = (ROOT / "API-Contract.md").read_text(encoding="utf-8")
    required_fields = (
        "Contract ID",
        "状态",
        "入口 / 调用方",
        "用途",
        "方法",
        "路径",
        "认证",
        "请求头",
        "Path 参数",
        "URL 参数",
        "请求体",
        "幂等性",
        "副作用",
        "成功响应码",
        "响应头",
        "响应体",
        "错误响应码",
        "状态语义",
        "安全规则",
        "验证要求",
    )
    headings = (
        "### HLT-001 ",
        "### APR-001A ",
        "#### EVL-001A ",
        "#### EVL-001B ",
        "#### EVL-002A ",
        "#### EVL-002B ",
        "#### EVL-003A ",
        "#### EVL-003B ",
        "#### EVL-003C ",
        "#### EVL-004A ",
        "#### EVL-004B ",
        "#### EVL-004C ",
        "#### EVL-004D ",
    )

    for index, heading in enumerate(headings):
        start = contract.index(heading)
        following = [
            contract.find(candidate, start + len(heading)) for candidate in headings[index + 1 :]
        ]
        end_candidates = [position for position in following if position >= 0]
        section = contract[start : min(end_candidates) if end_candidates else len(contract)]
        for field in required_fields:
            assert f"| {field} |" in section, f"{heading} 缺少字段：{field}"

    assert "### EVL-004 eval experiment and harness comparison" in contract
    assert "基础 draft / approve / run 链路之上的 trace/eval 升级契约" in contract


def test_p0_openapi_has_no_path_method_or_schema_drift(tmp_path: Path) -> None:
    """逐 operation 固定 path/method、成功 schema、认证和错误 models。"""

    schema = build_contract_app(tmp_path).openapi()
    paths = cast(dict[str, dict[str, Any]], schema["paths"])
    expected_methods: dict[str, set[str]] = {}
    for contract in OPERATIONS:
        expected_methods.setdefault(contract.path, set()).add(contract.method)

    assert set(paths) == set(expected_methods)
    for path, methods in expected_methods.items():
        assert set(paths[path]) == methods

    for contract in OPERATIONS:
        operation = paths[contract.path][contract.method]
        assert _response_ref(operation, "200").endswith(f"/{contract.response_schema}")
        if (contract.path, contract.method) in {
            ("/api/v1/agents/{agent_id}/runs", "post"),
            ("/api/v1/runs/{run_id}/approvals/{approval_id}", "post"),
        }:
            assert _response_ref(operation, "202").endswith(f"/{contract.response_schema}")
        for status in contract.error_statuses:
            assert _response_ref(operation, status).endswith("/ApiErrorEnvelope")
        if contract.path == "/api/v1/health":
            assert operation.get("security") in (None, [])
        else:
            assert {"HTTPBearer": []} in operation.get("security", [])

    assert "/api/v1/tools" not in paths


@pytest.mark.parametrize(
    ("profile", "storage_kind", "queue_kind", "observability_kind"),
    [
        ("local", "sqlite", "in-memory", "local-jsonl"),
        ("service", "postgresql", "redis", "local-jsonl"),
    ],
)
def test_health_is_public_readonly_profile_summary(
    tmp_path: Path,
    profile: str,
    storage_kind: str,
    queue_kind: str,
    observability_kind: str,
) -> None:
    """health 只报告装配摘要；service profile 也不要求凭据或执行依赖探测。"""

    with TestClient(build_contract_app(tmp_path, profile=profile)) as client:
        response = client.get("/api/v1/health", headers={"X-Request-Id": "req-health"})

    assert response.status_code == 200
    assert response.json() == {
        "request_id": "req-health",
        "status": "ok",
        "profile": profile,
        "storage": {"kind": storage_kind, "status": "configured"},
        "queue": {"kind": queue_kind, "status": "configured"},
        "observability": {"kind": observability_kind, "status": "configured"},
    }


def test_health_does_not_expose_profile_secrets_or_absolute_paths(tmp_path: Path) -> None:
    """即使 profile 包含部署连接信息，health allowlist 也不能回显这些值。"""

    profile_text = (PROFILES / "service.yaml").read_text(encoding="utf-8")
    secret_profile = profile_text.replace("profile: service", "profile: secret-test")
    (tmp_path / "secret-test.yaml").write_text(secret_profile, encoding="utf-8")
    app = create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=AgentRegistry([descriptor()]),
        approval_service=cast(Any, object()),
        eval_service=cast(Any, object()),
        profile="secret-test",
        profiles_dir=tmp_path,
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/health")

    serialized = response.text
    assert response.status_code == 200
    for forbidden in (
        "agent_harness:agent_harness",
        "localhost:55432",
        "localhost:56379",
        "LOGFIRE_TOKEN",
        "LANGFUSE_SECRET_KEY",
        str(tmp_path),
    ):
        assert forbidden not in serialized


def test_health_internal_error_uses_safe_envelope_without_secret_or_path(tmp_path: Path) -> None:
    """app 已启动后的 health 异常只能返回固定安全摘要和 request_id。"""

    app = build_contract_app(tmp_path)

    def fail_health_summary() -> None:
        raise RuntimeError(
            "postgresql://user:secret-pass@localhost/db token=private /tmp/customer/state.db"
        )

    app.dependency_overrides[get_health_summary] = fail_health_summary
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/v1/health",
            headers={"X-Request-Id": "req-health-fail"},
        )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "api.internal_error",
            "message": "internal server error",
            "request_id": "req-health-fail",
        }
    }
    for forbidden in ("secret-pass", "private", "/tmp/customer", "postgresql://"):
        assert forbidden not in response.text


def test_eval_approve_reason_schema_requires_non_blank_text(tmp_path: Path) -> None:
    """EVL-002A 的 request schema 必须把人工 reason 的非空约束发布到 OpenAPI。"""

    schema = build_contract_app(tmp_path).openapi()
    request_schema = schema["components"]["schemas"]["EvalApproveRequest"]

    assert request_schema["required"] == ["reason"]
    assert request_schema["properties"]["reason"]["minLength"] == 1


@pytest.mark.parametrize("reason", ["", "   "])
@pytest.mark.asyncio
async def test_eval_approve_rejects_empty_or_blank_reason(
    tmp_path: Path,
    reason: str,
) -> None:
    """空或纯空白 reason 在 service seam 前返回统一 422 envelope。"""

    status, payload = await asgi_request(
        build_contract_app(tmp_path),
        method="POST",
        path="/api/v1/eval-cases/case-1/approve",
        body={"reason": reason},
    )

    assert status == 422
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["request_id"] == "req-auth-policy-hitl"
    assert "detail" not in payload


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/agents/examples.basic/runs", None),
        ("GET", "/api/v1/runs/run-1/events?after_seq=-1", None),
        ("POST", "/api/v1/runs/run-1/resume", None),
        ("POST", "/api/v1/runs/run-1/approvals/approval-1", None),
        ("POST", "/api/v1/policies/check", None),
        ("POST", "/api/v1/eval-cases/drafts", None),
        ("POST", "/api/v1/eval-cases/case-1/approve", None),
        ("POST", "/api/v1/evals/runs", None),
    ],
)
@pytest.mark.asyncio
async def test_every_validation_capable_operation_uses_api_error_envelope(
    tmp_path: Path,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """所有实际具有 body/query validation 的 P0 operation 都统一返回 422 envelope。"""

    status, payload = await asgi_request(
        build_contract_app(tmp_path),
        method=method,
        path=path,
        body=body,
    )

    assert status == 422
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "validation_error"
    assert payload["error"]["request_id"] == "req-auth-policy-hitl"
    assert "detail" not in payload


def test_swagger_and_redoc_remain_available_without_tool_route(tmp_path: Path) -> None:
    """FastAPI 默认管理面可打开，但不能借 docs 提前暴露远程 tool execution。"""

    with TestClient(build_contract_app(tmp_path)) as client:
        swagger = client.get("/docs")
        redoc = client.get("/redoc")
        openapi = client.get("/openapi.json")
        missing_tools = client.get("/api/v1/tools")

    assert swagger.status_code == 200
    assert redoc.status_code == 200
    assert "text/html" in swagger.headers["content-type"]
    assert "text/html" in redoc.headers["content-type"]
    assert openapi.status_code == 200
    assert "/api/v1/tools" not in openapi.json()["paths"]
    assert missing_tools.status_code == 404
    assert missing_tools.json()["error"]["code"] == "api.not_found"
