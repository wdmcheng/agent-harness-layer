"""Service OpenAPI validation envelope 与文档入口合同测试。"""

from __future__ import annotations

from tests.contracts.test_service_app_template_openapi_contracts import (
    Any as Any,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    Path as Path,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    TestClient as TestClient,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    asgi_request as asgi_request,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    build_contract_app as build_contract_app,
)
from tests.contracts.test_service_app_template_openapi_contracts import (
    pytest as pytest,
)


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
    """所有实际具有 body/query validation 的公开 operation 都统一返回 422 envelope。"""

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
