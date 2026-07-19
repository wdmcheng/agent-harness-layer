"""审批证据发布失败与幂等恢复合同测试。"""

from __future__ import annotations

from tests.contracts.test_approval_evidence_recovery_contracts import (
    ApprovalStateConflict as ApprovalStateConflict,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    AuditLogCreate as AuditLogCreate,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    AuditLogRepository as AuditLogRepository,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    Path as Path,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    RunStatus as RunStatus,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    build_approval_flow as build_approval_flow,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    fail_once_on_event as fail_once_on_event,
)
from tests.contracts.test_approval_evidence_recovery_contracts import (
    pytest as pytest,
)


@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.asyncio
async def test_terminal_event_failure_keeps_claim_recoverable_without_replaying_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """验证 terminal 发布前后失败都保留可恢复 claim，恢复时绝不重复执行工具 handler。"""

    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        """记录一次真实 handler 副作用并原样返回参数，便于断言恢复的幂等性。"""

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
            ordered_group = [
                (item.event_id, item.sequence_in_group, item.state)
                for item in await uow.evidence_outbox.ordered_group(
                    group_id=f"approval:{approval.approval_id}:resolution"
                )
            ]
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
    assert ordered_group == [
        (f"approval-resolution:{approval.approval_id}", 1, "published"),
        (f"run-terminal:{waiting.run_id}", 2, "published"),
    ]


@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.asyncio
async def test_pre_executor_event_failure_retries_without_duplicate_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """验证 resumed 事件发布失败后重试只补偿状态，不会再次执行已完成 handler。"""

    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        """记录执行次数的最小 handler，用于证明 pre-executor 恢复不会重复副作用。"""

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
    """验证 approval.resolved 发布失败可由 recovery 幂等补齐且保持单一终态。"""

    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        """记录工具执行次数，确保 resolution evidence 补偿不触发业务重放。"""

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
    """验证审批审计失败回滚公开 finalize，并由恢复路径仅收口一次。"""

    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        """提供可计数的成功 handler，隔离本场景与工具实现细节。"""

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
        """只在首次 approved 审计写入失败，之后委托真实仓储验证可恢复收口。"""

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
