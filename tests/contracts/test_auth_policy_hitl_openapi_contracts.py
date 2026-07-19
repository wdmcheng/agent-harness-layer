"""认证、OpenAPI 与 local identity 合同测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import (
    ROOT,
    asgi_request,
    descriptor,
    sqlite_dsn,
    table_count,
)

from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.main import create_app


def test_api_contract_documents_auth_policy_approval_endpoints() -> None:
    """API 合约必须记录认证、策略与审批入口及已落地的 trace 约束，避免文档承诺滞后实现。"""

    contract = (ROOT / "API-Contract.md").read_text(encoding="utf-8")

    assert "### APR-001 列出 run approvals" in contract
    assert "### APR-002 resolve approval" in contract
    assert "### POL-001 policy check" in contract
    assert "### 5.12 `PolicyCheckRequest`" in contract
    assert "### 5.15 `ApprovalRecord`" in contract
    assert (
        "当前生产 DTO、OpenAPI 与 `0013_run_trace_correlation -> "
        "0013a_run_trace_event_hardening` schema 均要求 `trace_id` 必填且非空" in contract
    )
    assert "当前生产 DTO/OpenAPI 仍允许 `trace_id=null`" not in contract
    assert "| `trace_id` | string | 目标 Yes；当前 No |" not in contract
    assert "GET /api/v1/agents" in contract
    assert "认证/策略/HITL contract tests 还必须覆盖 401/403 和身份可见性过滤" in contract
    assert "| `APR-001` | 规划中 | Auth / Policy / HITL |" not in contract
    assert "| `POL-001` | 规划中 | Auth / Policy / HITL |" not in contract


def test_openapi_exposes_auth_policy_hitl_paths_security_and_error_envelopes(
    tmp_path: Path,
) -> None:
    """OpenAPI 应为所有受保护入口声明 Bearer 安全方案与统一错误封套，并精确投影公开 DTO。"""

    from agent_harness.auth import StaticTokenVerifier

    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier({"valid-token": IdentityContext.local_default()}),
        policy_engine=PolicyEngine(provider=YamlPolicyProvider.default()),
    )
    openapi = app.openapi()
    paths = openapi["paths"]
    run_response_properties = openapi["components"]["schemas"]["RunCreateResponse"]["properties"]
    approval_resolve_run_schema = openapi["components"]["schemas"]["ApprovalResolveResponse"][
        "properties"
    ]["run"]
    approval_public_schema = openapi["components"]["schemas"]["ApprovalPublicRecord"]
    canonical_event_schema = openapi["components"]["schemas"]["CanonicalEvent"]
    policy_response_schema = openapi["components"]["schemas"]["PolicyDecisionResponse"]

    assert "HTTPBearer" in openapi["components"]["securitySchemes"]
    assert "resume_token" not in run_response_properties
    assert "RunCreateResponse" in json.dumps(approval_resolve_run_schema)
    assert "RunResult" not in json.dumps(approval_resolve_run_schema)
    assert "trace_id" in approval_public_schema["required"]
    assert approval_public_schema["properties"]["trace_id"]["type"] == "string"
    assert "anyOf" not in approval_public_schema["properties"]["trace_id"]
    assert canonical_event_schema["properties"]["record_scope"]["enum"] == [
        "run",
        "non_run",
    ]
    assert {
        branch.get("type") for branch in canonical_event_schema["properties"]["trace_id"]["anyOf"]
    } == {
        "string",
        "null",
    }
    assert any("if" in branch and "then" in branch for branch in canonical_event_schema["allOf"])
    for response_schema in (
        "ApprovalListResponse",
        "ApprovalDetailResponse",
        "ApprovalResolveResponse",
    ):
        assert "ApprovalPublicRecord" in json.dumps(
            openapi["components"]["schemas"][response_schema]
        )
    assert "audit_ref" in policy_response_schema["required"]
    assert "get" in paths["/api/v1/agents"]
    assert "get" in paths["/api/v1/runs/{run_id}/approvals"]
    assert "get" in paths["/api/v1/runs/{run_id}/approvals/{approval_id}"]
    assert "post" in paths["/api/v1/runs/{run_id}/approvals/{approval_id}"]
    assert "post" in paths["/api/v1/policies/check"]
    approval_list_params = paths["/api/v1/runs/{run_id}/approvals"]["get"]["parameters"]
    assert any(
        param["name"] == "status" and param["in"] == "query" for param in approval_list_params
    )

    for path, method in [
        ("/api/v1/agents", "get"),
        ("/api/v1/runs/{run_id}/approvals", "get"),
        ("/api/v1/runs/{run_id}/approvals/{approval_id}", "get"),
        ("/api/v1/runs/{run_id}/approvals/{approval_id}", "post"),
        ("/api/v1/policies/check", "post"),
    ]:
        operation = paths[path][method]
        assert {"HTTPBearer": []} in operation.get("security", [])
        for status in ("401", "403", "500"):
            schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("/ApiErrorEnvelope")
        if method == "post":
            validation_schema = operation["responses"]["422"]["content"]["application/json"][
                "schema"
            ]
            assert validation_schema["$ref"].endswith("/ApiErrorEnvelope")

    conflict_schema = paths["/api/v1/runs/{run_id}/approvals/{approval_id}"]["post"]["responses"][
        "409"
    ]["content"]["application/json"]["schema"]
    assert conflict_schema["$ref"].endswith("/ApiErrorEnvelope")
    for path, method in [
        ("/api/v1/agents/{agent_id}/runs", "post"),
        ("/api/v1/runs/{run_id}/resume", "post"),
        ("/api/v1/runs/{run_id}/events", "get"),
    ]:
        validation_schema = paths[path][method]["responses"]["422"]["content"]["application/json"][
            "schema"
        ]
        assert validation_schema["$ref"].endswith("/ApiErrorEnvelope")


@pytest.mark.asyncio
async def test_invalid_bearer_token_rejects_agents_and_run_without_side_effects(
    tmp_path: Path,
) -> None:
    """无效 token 必须在列举 agent 和创建 run 前返回 401，不能留下运行、审批或审计副作用。"""

    from agent_harness.auth import StaticTokenVerifier

    db_path = tmp_path / "auth.db"
    events_path = tmp_path / "events.jsonl"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=LocalJsonlEventSink(events_path)),
    )
    app = create_app(
        orchestrator=orchestrator,
        event_sink=LocalJsonlEventSink(events_path),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier({"valid-token": IdentityContext.local_default()}),
        policy_engine=PolicyEngine(provider=YamlPolicyProvider.default()),
    )
    invalid_auth = [(b"authorization", b"Bearer invalid-token")]

    try:
        agents_status, agents_body = await asgi_request(
            cast(Any, app),
            method="GET",
            path="/api/v1/agents",
            headers=invalid_auth,
        )
        run_status, run_body = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/agents/examples.basic/runs",
            body={"input": {"prompt": "hello"}},
            headers=invalid_auth,
        )
    finally:
        await storage.dispose()

    assert agents_status == 401
    assert run_status == 401
    assert agents_body["error"]["code"] == "auth.invalid_token"
    assert run_body["error"]["code"] == "auth.invalid_token"
    assert run_body["error"]["request_id"] == "req-auth-policy-hitl"
    assert table_count(db_path, "agent_runs") == 0
    assert table_count(db_path, "checkpoints") == 0
    assert table_count(db_path, "approvals") == 0
    assert table_count(db_path, "audit_logs") == 0


@pytest.mark.asyncio
async def test_valid_token_without_run_create_permission_cannot_create_run(
    tmp_path: Path,
) -> None:
    """身份有效不等于有创建权限；策略拒绝必须在 run/checkpoint/approval 持久化之前发生。"""

    from agent_harness.audit import AuditService
    from agent_harness.auth import StaticTokenVerifier
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider

    db_path = tmp_path / "run-create-policy.db"
    events_path = tmp_path / "events.jsonl"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    low_privilege = IdentityContext(
        tenant_id="default",
        user_id="run-create-low-privilege",
        session_id="run-create-low-session",
        roles=[],
        permissions=[],
        auth_method="api-key",
    )
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=LocalJsonlEventSink(events_path)),
        identity=low_privilege,
    )
    app = create_app(
        orchestrator=orchestrator,
        event_sink=LocalJsonlEventSink(events_path),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier({"low-token": low_privilege}),
        policy_engine=PolicyEngine(
            provider=YamlPolicyProvider.default(),
            audit=AuditService(storage=storage),
        ),
    )

    try:
        status, body = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/agents/examples.basic/runs",
            body={"input": {"prompt": "hello"}},
            headers=[(b"authorization", b"Bearer low-token")],
        )
    finally:
        await storage.dispose()

    assert status == 403
    assert body["error"]["code"] == "policy.denied"
    assert table_count(db_path, "agent_runs") == 0
    assert table_count(db_path, "checkpoints") == 0
    assert table_count(db_path, "approvals") == 0


@pytest.mark.asyncio
async def test_env_example_local_profile_uses_default_identity_without_authorization(
    tmp_path: Path,
) -> None:
    """本地示例环境保留无授权的开发体验，但只限 local profile，不能改变 service 的认证边界。"""

    from app.main import create_app

    service_root = tmp_path / "service-app"
    service_root.mkdir()
    (service_root / ".env").write_text(
        (ROOT / "templates" / "service-app" / ".env.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (service_root / "configs").symlink_to(ROOT / "templates" / "service-app" / "configs")
    (service_root / "agents").symlink_to(ROOT / "templates" / "service-app" / "agents")
    dsn = sqlite_dsn(tmp_path / "env-example-local.db")
    events_path = tmp_path / "env-example-events.jsonl"
    run_migrations(dsn)
    app = create_app(
        profile="local",
        profiles_dir=service_root / "configs" / "profiles",
        storage_dsn=dsn,
        events_path=events_path,
    )

    status, body = await asgi_request(cast(Any, app), method="GET", path="/api/v1/agents")

    assert status == 200
    assert body["agents"][0]["agent_id"] == "examples.basic"


@pytest.mark.asyncio
async def test_invalid_resume_token_is_not_echoed_in_error_envelope(
    tmp_path: Path,
) -> None:
    """无效恢复令牌即使随请求提交也不得出现在 404 错误封套，避免敏感 continuation 泄漏。"""

    from agent_harness.auth import StaticTokenVerifier

    db_path = tmp_path / "resume-redaction.db"
    events_path = tmp_path / "events.jsonl"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=LocalJsonlEventSink(events_path)),
    )
    app = create_app(
        orchestrator=orchestrator,
        event_sink=LocalJsonlEventSink(events_path),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier({"valid-token": IdentityContext.local_default()}),
        policy_engine=PolicyEngine(provider=YamlPolicyProvider.default()),
    )
    try:
        status, body = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/runs/not-a-run/resume",
            body={"resume_token": "resume-secret-token"},
            headers=[(b"authorization", b"Bearer valid-token")],
        )
    finally:
        await storage.dispose()

    assert status == 404
    assert body["error"]["code"] == "api.not_found"
    assert "resume-secret-token" not in json.dumps(body)
