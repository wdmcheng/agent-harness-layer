"""审批公开重试、过期 claim 接管与 fencing 合同测试。"""

from __future__ import annotations

from tests.contracts.test_approval_evidence_recovery_contracts import (
    PROFILES as PROFILES,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    Path as Path,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    TestClient as TestClient,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    build_approval_flow as build_approval_flow,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    create_app as create_app,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    fail_once_on_event as fail_once_on_event,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    pytest as pytest,
)


@pytest.mark.parametrize("decision", ["approved", "denied"])
@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.parametrize("failure_point", ["terminal", "resolution"])
@pytest.mark.asyncio
async def test_public_resolve_retry_reconciles_pending_evidence_before_returning_409(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    mode: str,
    failure_point: str,
) -> None:
    """公开重试先补齐中断的审批证据，再稳定返回 in-progress，不能重放 handler 或终态。"""

    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        """记录真实工具副作用次数，检验不同决策和故障窗口均不会因 HTTP 重试重复执行。"""

        nonlocal calls
        calls += 1
        return arguments

    storage, sink, service, orchestrator, identity, registry, waiting = await build_approval_flow(
        tmp_path, handler=handler
    )
    terminal_type = (
        CanonicalEventType.RUN_COMPLETED
        if decision == "approved"
        else CanonicalEventType.RUN_FAILED
    )
    failure_type = (
        terminal_type if failure_point == "terminal" else CanonicalEventType.APPROVAL_RESOLVED
    )
    monkeypatch.setattr(
        sink,
        "write",
        fail_once_on_event(
            event_type=failure_type,
            mode=mode,
            original_write=sink.write,
        ),
    )
    app = create_app(
        orchestrator=orchestrator,
        event_sink=sink,
        registry=registry,
        approval_service=service,
        profile="local",
        profiles_dir=PROFILES,
    )
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        resolve = service.approve if decision == "approved" else service.deny
        first_request_id = f"req-original-{decision}-{failure_point}-{mode}"
        retry_request_id = f"req-retry-{decision}-{failure_point}-{mode}"
        with pytest.raises(OSError, match=failure_type.value):
            await resolve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
                request_id=first_request_id,
            )

        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/runs/{waiting.run_id}/approvals/{approval.approval_id}",
                json={"decision": decision},
                headers={"X-Request-Id": retry_request_id},
            )

        events = await sink.read(run_id=waiting.run_id)
        async with storage.uow() as uow:
            public = await uow.approvals.get(approval.approval_id)
            private_state = await uow.approvals.get_resolution_state(approval.approval_id)
            run = await uow.runs.get(waiting.run_id)
            audits = await uow.audit_logs.list_for_tenant(identity.tenant_id)
    finally:
        await storage.dispose()

    expected_status = "approved" if decision == "approved" else "denied"
    expected_run = "completed" if decision == "approved" else "failed"
    expected_code = "approval.resolution_in_progress"
    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected_code
    assert public is not None and public.status == expected_status
    assert private_state == ("completed" if decision == "approved" else "denied")
    assert run is not None and run.status == expected_run
    assert calls == (1 if decision == "approved" else 0)
    assert sum(event.terminal for event in events) == 1
    resolution_events = [
        event for event in events if event.event_type == CanonicalEventType.APPROVAL_RESOLVED
    ]
    assert len(resolution_events) == 1
    assert resolution_events[0].request_id == first_request_id
    assert sum(record.action == f"approval.{expected_status}" for record in audits) == 1


@pytest.mark.asyncio
async def test_expired_raw_claim_is_taken_over_and_fenced_by_public_retry(
    tmp_path: Path,
) -> None:
    """过期的原始 resolution lease 可由公开重试接管，旧 lease 之后必须被 fencing 拒绝。"""

    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        """记录接管后唯一允许的一次业务执行，防止恢复逻辑仅靠状态判断掩盖重放。"""

        nonlocal calls
        calls += 1
        return arguments

    storage, sink, service, orchestrator, identity, registry, waiting = await build_approval_flow(
        tmp_path,
        handler=handler,
        recovery_lease_timeout_seconds=0,
    )
    app = create_app(
        orchestrator=orchestrator,
        event_sink=sink,
        registry=registry,
        approval_service=service,
        profile="local",
        profiles_dir=PROFILES,
    )
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
            abandoned = await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=waiting.run_id,
                tenant_id=identity.tenant_id,
                request_id="req-abandoned-lease",
            )
            await uow.commit()
        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/runs/{waiting.run_id}/approvals/{approval.approval_id}",
                json={"decision": "approved"},
                headers={"X-Request-Id": "req-expired-lease-takeover"},
            )
        events = await sink.read(run_id=waiting.run_id)
        async with storage.uow() as uow:
            private = await uow.approvals.get_resolution(approval.approval_id)
            claim = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
            stale_fence = await uow.approvals.fence_resolution_lease(
                approval_id=approval.approval_id,
                run_id=waiting.run_id,
                tenant_id=identity.tenant_id,
                lease_id=abandoned.lease_id,
            )
    finally:
        await storage.dispose()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval.resolution_in_progress"
    assert calls == 1
    assert private is not None and private.state == "completed"
    assert private.lease_id != abandoned.lease_id
    assert claim is not None and claim.execution_state == "completed"
    assert stale_fence is False
    assert sum(event.terminal for event in events) == 1
    assert sum(event.event_type == CanonicalEventType.APPROVAL_RESOLVED for event in events) == 1


@pytest.mark.asyncio
async def test_unexpired_raw_claim_is_not_taken_over_by_concurrent_public_retry(
    tmp_path: Path,
) -> None:
    """未过期 lease 是活跃所有权，外部重试只能得到稳定冲突，不能篡夺或启动工具。"""

    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        """一旦被调用即暴露错误接管；正确分支必须始终保持零次执行。"""

        nonlocal calls
        calls += 1
        return arguments

    storage, sink, service, orchestrator, identity, registry, waiting = await build_approval_flow(
        tmp_path,
        handler=handler,
        recovery_lease_timeout_seconds=300,
    )
    app = create_app(
        orchestrator=orchestrator,
        event_sink=sink,
        registry=registry,
        approval_service=service,
        profile="local",
        profiles_dir=PROFILES,
    )
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
            active = await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=waiting.run_id,
                tenant_id=identity.tenant_id,
                request_id="req-active-lease",
            )
            await uow.commit()
        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/runs/{waiting.run_id}/approvals/{approval.approval_id}",
                json={"decision": "approved"},
                headers={"X-Request-Id": "req-active-lease"},
            )
        events = await sink.read(run_id=waiting.run_id)
        async with storage.uow() as uow:
            private = await uow.approvals.get_resolution(approval.approval_id)
            claim = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
    finally:
        await storage.dispose()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval.resolution_in_progress"
    assert calls == 0
    assert private is not None and private.state == "claimed"
    assert private.lease_id == active.lease_id
    assert claim is None
    assert sum(event.terminal for event in events) == 0
    assert sum(event.event_type == CanonicalEventType.APPROVAL_RESOLVED for event in events) == 0
