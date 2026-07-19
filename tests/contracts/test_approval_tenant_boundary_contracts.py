"""审批跨租户可见性与解决权限合同测试。"""

from __future__ import annotations

from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    Any as Any,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    IdentityContext as IdentityContext,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    Path as Path,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    asgi_request as asgi_request,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    cast as cast,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    create_app as create_app,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    descriptor as descriptor,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    pytest as pytest,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    sqlite_dsn as sqlite_dsn,
)


@pytest.mark.asyncio
async def test_approval_api_rejects_cross_tenant_visibility_and_resolution(
    tmp_path: Path,
) -> None:
    """验证审批 API 区分跨租户隐藏与同租户权限拒绝两类安全边界。

    跨租户主体不能借列表、详情或决策接口探测资源；同租户但无策略权限的
    主体则必须收到显式拒绝，避免把资源隔离与授权判断混为同一种结果。
    """
    from agent_harness.approvals import ApprovalService
    from agent_harness.audit import AuditService
    from agent_harness.auth import StaticTokenVerifier
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider

    db_path = tmp_path / "tenant-approval.db"
    events_path = tmp_path / "events.jsonl"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_bus = EventBus(sink=LocalJsonlEventSink(events_path))
    owner = IdentityContext.local_default(session_id="owner-session")
    intruder = IdentityContext(
        tenant_id="other",
        user_id="intruder",
        session_id="intruder-session",
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )
    low_privilege = IdentityContext(
        tenant_id="default",
        user_id="same-tenant-low-privilege",
        session_id="low-privilege-session",
        roles=[],
        permissions=[],
        auth_method="api-key",
    )
    orchestrator = RunOrchestrator(storage=storage, event_bus=event_bus, identity=owner)
    audit = AuditService(storage=storage)
    approval_service = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        audit=audit,
    )
    waiting = await orchestrator.start_run(
        agent_id="examples.basic",
        input={"prompt": "pause for tenant approval"},
        checkpoint_state={"reason": "shell.execute"},
        identity=owner,
    )
    assert waiting.resume_token is not None
    approval = await approval_service.require_approval(
        actor=owner,
        run_id=waiting.run_id,
        agent_id="examples.basic",
        action="shell.execute",
        resource="tool:shell",
        reason="dangerous action requires approval",
        resume_token=waiting.resume_token,
    )
    app = create_app(
        orchestrator=orchestrator,
        event_sink=LocalJsonlEventSink(events_path),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier(
            {
                "owner-token": owner,
                "intruder-token": intruder,
                "low-token": low_privilege,
            }
        ),
        policy_engine=PolicyEngine(provider=YamlPolicyProvider.default(), audit=audit),
        approval_service=approval_service,
    )
    intruder_auth = [(b"authorization", b"Bearer intruder-token")]
    low_auth = [(b"authorization", b"Bearer low-token")]

    try:
        list_status, list_body = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/runs/{waiting.run_id}/approvals",
            headers=intruder_auth,
        )
        detail_status, detail_body = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/runs/{waiting.run_id}/approvals/{approval.approval_id}",
            headers=intruder_auth,
        )
        approve_status, approve_body = await asgi_request(
            cast(Any, app),
            method="POST",
            path=f"/api/v1/runs/{waiting.run_id}/approvals/{approval.approval_id}",
            body={"decision": "approved"},
            headers=intruder_auth,
        )
        low_list_status, low_list_body = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/runs/{waiting.run_id}/approvals",
            headers=low_auth,
        )
        low_detail_status, low_detail_body = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/runs/{waiting.run_id}/approvals/{approval.approval_id}",
            headers=low_auth,
        )
        low_status, low_body = await asgi_request(
            cast(Any, app),
            method="POST",
            path=f"/api/v1/runs/{waiting.run_id}/approvals/{approval.approval_id}",
            body={"decision": "approved"},
            headers=low_auth,
        )
        owner_status, owner_body = await asgi_request(
            cast(Any, app),
            method="GET",
            path=f"/api/v1/runs/{waiting.run_id}/approvals",
            headers=[(b"authorization", b"Bearer owner-token")],
        )
    finally:
        await storage.dispose()

    assert list_status == 200
    assert list_body["approvals"] == []
    assert detail_status == 404
    assert detail_body["error"]["code"] == "api.not_found"
    assert approve_status == 404
    assert approve_body["error"]["code"] == "api.not_found"
    assert low_list_status == 403
    assert low_list_body["error"]["code"] == "policy.denied"
    assert low_detail_status == 403
    assert low_detail_body["error"]["code"] == "policy.denied"
    assert low_status == 403
    assert low_body["error"]["code"] == "policy.denied"
    assert owner_status == 200
    assert owner_body["approvals"][0]["status"] == "waiting"
