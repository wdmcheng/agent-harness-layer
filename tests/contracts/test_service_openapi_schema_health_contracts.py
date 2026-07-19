"""Service OpenAPI schema、health 与安全错误合同测试。"""

from __future__ import annotations

from tests.contracts.test_service_app_template_openapi_contracts import (
    OPERATIONS as OPERATIONS,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    PROFILES as PROFILES,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    Any as Any,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    Path as Path,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    TestClient as TestClient,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    _response_ref as _response_ref,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    asgi_request as asgi_request,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    build_contract_app as build_contract_app,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    cast as cast,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    create_app as create_app,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    descriptor as descriptor,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    get_health_summary as get_health_summary,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    pytest as pytest,
)


def test_api_contract_has_field_level_public_operation_baselines() -> None:
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


def test_service_openapi_has_no_path_method_or_schema_drift(tmp_path: Path) -> None:
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
        success_content = operation["responses"]["200"]["content"]
        assert set(success_content) == {contract.success_media_type}
        if contract.response_schema is None:
            assert success_content[contract.success_media_type]["schema"] == {"type": "string"}
        else:
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
    assert not any("delegation" in path for path in paths)

    components = cast(dict[str, dict[str, Any]], schema["components"]["schemas"])
    run_detail = components["RunDetailResponse"]
    assert set(run_detail["required"]) == {
        "request_id",
        "run_id",
        "agent_id",
        "status",
        "terminal_event",
        "parent_run_id",
        "delegation_summary",
    }
    summary = components["DelegationSummary"]
    assert set(summary["required"]) == {
        "parent_run_id",
        "children",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "cost_usd",
        "budget_status",
        "trace_refs",
    }
    assert summary["properties"]["budget_status"]["enum"] == [
        "within_budget",
        "exceeded",
        "incomplete",
    ]
    assert components["DelegationChildSummary"]["properties"]["status"]["enum"] == [
        "created",
        "running",
        "waiting",
        "completed",
        "failed",
        "cancelled",
    ]


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
