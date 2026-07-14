"""审批确定性结果与 terminal/resolution evidence 故障恢复合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.contracts.approval_evidence_contract_helpers import fail_once_on_event
from tests.contracts.test_p0_approval_execution_contracts import build_approval_flow

from agent_harness.approvals import ApprovalStateConflict
from agent_harness.events import CanonicalEventType
from agent_harness.runtime import RunStatus
from agent_harness.storage import AuditLogCreate
from agent_harness.storage.audit_repositories import AuditLogRepository
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.asyncio
async def test_terminal_event_failure_keeps_claim_recoverable_without_replaying_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return arguments

    (
        storage,
        sink,
        service,
        _orchestrator,
        identity,
        _registry,
        waiting,
    ) = await build_approval_flow(tmp_path, handler=handler)
    original_write = sink.write
    monkeypatch.setattr(
        sink,
        "write",
        fail_once_on_event(
            event_type=CanonicalEventType.RUN_COMPLETED,
            mode=mode,
            original_write=original_write,
        ),
    )
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        with pytest.raises(OSError, match="run.completed"):
            await service.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
            )
        async with storage.uow() as uow:
            private_after_failure = await uow.approvals.get_resolution(approval.approval_id)
            run_after_failure = await uow.runs.get(waiting.run_id)
            claim_after_failure = await uow.tool_invocations.get_by_approval_id(
                approval.approval_id
            )

        recovered = await service.recover_claimed(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
        )
        events = await sink.read(run_id=waiting.run_id)
        async with storage.uow() as uow:
            public = await uow.approvals.get(approval.approval_id)
            audits = await uow.audit_logs.list_for_tenant(identity.tenant_id)
    finally:
        await storage.dispose()

    assert calls == 1
    assert private_after_failure is not None and private_after_failure.state == "recovery_pending"
    assert run_after_failure is not None and run_after_failure.status == "completed"
    assert claim_after_failure is not None and claim_after_failure.execution_state == "completed"
    assert recovered.run is not None and recovered.run.status == RunStatus.COMPLETED
    assert public is not None and public.status == "approved"
    assert sum(event.terminal for event in events) == 1
    assert sum(event.event_type == CanonicalEventType.APPROVAL_RESOLVED for event in events) == 1
    assert sum(record.action == "approval.approved" for record in audits) == 1


@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.asyncio
async def test_pre_executor_event_failure_retries_without_duplicate_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return arguments

    storage, sink, service, _orchestrator, identity, _registry, waiting = await build_approval_flow(
        tmp_path, handler=handler
    )
    monkeypatch.setattr(
        sink,
        "write",
        fail_once_on_event(
            event_type=CanonicalEventType.RUN_RESUMED,
            mode=mode,
            original_write=sink.write,
        ),
    )
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        with pytest.raises(OSError, match="run.resumed"):
            await service.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
            )
        async with storage.uow() as uow:
            pending = await uow.approvals.get_resolution(approval.approval_id)
        assert pending is not None and pending.state == "recovery_pending"
        assert pending.approval.resume_token is not None
        with pytest.raises(ApprovalStateConflict) as exc_info:
            await service.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
                request_id=f"req-pre-executor-{mode}",
            )
        events = await sink.read(run_id=waiting.run_id)
        async with storage.uow() as uow:
            recovered = await uow.approvals.get(approval.approval_id)
            claim = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
    finally:
        await storage.dispose()

    assert exc_info.value.code == "approval.resolution_in_progress"
    assert recovered is not None and recovered.status == "approved"
    assert claim is not None and claim.execution_state == "completed"
    assert calls == 1
    assert sum(event.event_type == CanonicalEventType.RUN_RESUMED for event in events) == 1


@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.asyncio
async def test_resolution_event_failure_is_idempotently_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return arguments

    (
        storage,
        sink,
        service,
        _orchestrator,
        identity,
        _registry,
        waiting,
    ) = await build_approval_flow(tmp_path, handler=handler)
    original_write = sink.write
    monkeypatch.setattr(
        sink,
        "write",
        fail_once_on_event(
            event_type=CanonicalEventType.APPROVAL_RESOLVED,
            mode=mode,
            original_write=original_write,
        ),
    )
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        with pytest.raises(OSError, match="approval.resolved"):
            await service.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
            )

        recovered = await service.recover_claimed(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
        )
        events = await sink.read(run_id=waiting.run_id)
        async with storage.uow() as uow:
            public = await uow.approvals.get(approval.approval_id)
            audits = await uow.audit_logs.list_for_tenant(identity.tenant_id)
    finally:
        await storage.dispose()

    assert calls == 1
    assert recovered.approval.status == "approved"
    assert public is not None and public.status == "approved"
    assert sum(event.terminal for event in events) == 1
    assert sum(event.event_type == CanonicalEventType.APPROVAL_RESOLVED for event in events) == 1
    assert sum(record.action == "approval.approved" for record in audits) == 1


@pytest.mark.asyncio
async def test_approval_audit_failure_rolls_back_finalize_and_recovers_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return arguments

    storage, sink, service, _orchestrator, identity, _registry, waiting = await build_approval_flow(
        tmp_path, handler=handler
    )
    original_create = AuditLogRepository.create
    failed = False

    async def fail_approved_audit_once(
        self: AuditLogRepository,
        data: AuditLogCreate,
    ) -> object:
        nonlocal failed
        if not failed and data.action == "approval.approved":
            failed = True
            raise OSError("approval audit unavailable")
        return await original_create(self, data)

    monkeypatch.setattr(AuditLogRepository, "create", fail_approved_audit_once)
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        with pytest.raises(OSError, match="approval audit unavailable"):
            await service.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
            )
        async with storage.uow() as uow:
            after_failure = await uow.approvals.get_resolution(approval.approval_id)

        recovered = await service.recover_claimed(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
        )
        events = await sink.read(run_id=waiting.run_id)
        async with storage.uow() as uow:
            audits = await uow.audit_logs.list_for_tenant(identity.tenant_id)
    finally:
        await storage.dispose()

    assert calls == 1
    assert after_failure is not None
    assert after_failure.approval.status == "waiting"
    assert after_failure.state == "recovery_pending"
    assert recovered.approval.status == "approved"
    assert sum(event.event_type == CanonicalEventType.APPROVAL_RESOLVED for event in events) == 1
    assert sum(record.action == "approval.approved" for record in audits) == 1


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
    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
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
    expected_code = (
        "approval.resolution_in_progress"
        if decision == "approved" and failure_point == "terminal"
        else "approval.invalid_transition"
    )
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
    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
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
    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
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
