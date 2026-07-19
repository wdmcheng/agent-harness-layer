"""Service approval worker 的 evidence 补偿与 poison-message 恢复合同。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.contracts.approval_evidence_contract_helpers import fail_once_on_event
from tests.contracts.test_approval_execution_contracts import build_approval_flow

from agent_harness.adapters.runtime import DBOSOperationOutcome
from agent_harness.approvals import ApprovalService, ApprovalStateConflict
from agent_harness.audit import AuditService
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus
from agent_harness.runtime import InMemoryRunQueue
from app.workers.runtime_worker import consume_one


@pytest.mark.asyncio
async def test_service_approve_retry_reconciles_resolution_evidence_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次补发审批证据失败后，重试只能收敛持久化状态，不能重复执行业务 handler。"""

    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        """记录业务副作用次数，作为“补证据不重放 continuation”的可观察边界。"""

        nonlocal calls
        calls += 1
        return arguments

    queue = InMemoryRunQueue()
    storage, sink, _local, orchestrator, identity, _registry, waiting = await build_approval_flow(
        tmp_path, handler=handler, queue=queue
    )
    service = ApprovalService(
        storage=storage,
        event_bus=EventBus(sink=sink),
        orchestrator=orchestrator,
        audit=AuditService(storage),
        queue=queue,
    )
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        await service.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
            request_id="request-service-approve",
        )
        delivery = await queue.pickup(consumer_id="service-worker")
        assert delivery is not None
        async with storage.uow() as uow:
            assert await uow.approvals.claim_resolution_execution(
                approval_id=approval.approval_id,
                tenant_id=delivery.message.tenant_id,
                run_id=delivery.message.run_id,
                lease_id=delivery.message.resolution_lease_id or "",
                operation_id=delivery.message.operation_id,
                request_id=delivery.message.request_id,
                message_id=delivery.receipt.message_id,
                workflow_owner_id="service-worker",
                workflow_id="approval-workflow",
            )
            await uow.commit()
        monkeypatch.setattr(
            sink,
            "write",
            fail_once_on_event(
                event_type=CanonicalEventType.APPROVAL_RESOLVED,
                mode="before",
                original_write=sink.write,
            ),
        )
        with pytest.raises(OSError, match="approval.resolved"):
            await service.execute_queued_approval(
                approval_id=approval.approval_id,
                tenant_id=delivery.message.tenant_id,
                run_id=delivery.message.run_id,
                operation_id=delivery.message.operation_id,
                lease_id=delivery.message.resolution_lease_id or "",
            )
        with pytest.raises(ApprovalStateConflict):
            await service.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
                request_id="request-service-approve-retry",
            )
        events = await sink.read(run_id=waiting.run_id)
    finally:
        await storage.dispose()

    assert calls == 1
    assert sum(event.terminal for event in events) == 1
    assert sum(event.event_type == CanonicalEventType.APPROVAL_RESOLVED for event in events) == 1


@pytest.mark.asyncio
async def test_worker_acks_recovery_pending_approval_without_replaying_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DBOS ERROR 对应已落库 continuation 时，worker 补证据后确认原消息。"""

    calls = 0

    def handler(arguments: dict[str, object]) -> dict[str, object]:
        """记录 continuation 的真实执行次数，证明 worker 恢复不会重放已完成的业务动作。"""

        nonlocal calls
        calls += 1
        return arguments

    clock = [0.0]
    queue = InMemoryRunQueue(clock=lambda: clock[0])
    storage, sink, _local, orchestrator, identity, _registry, waiting = await build_approval_flow(
        tmp_path, handler=handler, queue=queue
    )
    service = ApprovalService(
        storage=storage,
        event_bus=EventBus(sink=sink),
        orchestrator=orchestrator,
        audit=AuditService(storage),
        queue=queue,
    )

    class DeterministicFailedDBOS:
        """模拟 DBOS 已返回确定失败、但前一次 continuation 已写入恢复状态的运行环境。"""

        calls = 0

        async def execute(self, operation: object) -> DBOSOperationOutcome:
            """第一次注入落库后的证据故障，后续调用保持同一确定失败结果以触发恢复分支。"""

            self.calls += 1
            if self.calls > 1:
                return DBOSOperationOutcome(
                    status="deterministic_failed",
                    error_code="dbos.error",
                )
            message = cast(Any, operation)
            try:
                await service.execute_queued_approval(
                    approval_id=message.approval_id,
                    tenant_id=message.tenant_id,
                    run_id=message.run_id,
                    operation_id=message.operation_id,
                    lease_id=message.resolution_lease_id,
                )
            except OSError as exc:
                assert "run.completed" in str(exc)
                return DBOSOperationOutcome(
                    status="deterministic_failed",
                    error_code="dbos.error",
                )
            raise AssertionError("failure injection did not interrupt continuation")

    class DelegationService:
        """提供最小委派 seam，明确本合同只验证本地审批恢复而非子运行协调。"""

        calls = 0

        async def reconcile_child_if_delegated(self, _run_id: str) -> bool:
            """记录 worker 是否检查过委派关系，并稳定声明当前运行并非委派子运行。"""

            self.calls += 1
            return False

    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        await service.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
            request_id="request-worker-recovery",
        )
        original_write = sink.write
        terminal_failures = 0

        async def fail_terminal_twice(event: CanonicalEvent) -> CanonicalEvent:
            """仅拦截前两次终态事件写入，构造可恢复但不会掩盖重复执行的故障窗口。"""

            nonlocal terminal_failures
            if event.event_type == CanonicalEventType.RUN_COMPLETED and terminal_failures < 2:
                terminal_failures += 1
                raise OSError("run.completed sink unavailable")
            return await original_write(event)

        monkeypatch.setattr(sink, "write", fail_terminal_twice)
        delegation_service = DelegationService()
        components = SimpleNamespace(
            queue=queue,
            storage=storage,
            orchestrator=orchestrator,
            approval_service=service,
            delegation_service=delegation_service,
        )
        dbos = DeterministicFailedDBOS()
        with pytest.raises(OSError, match="run.completed"):
            await consume_one(
                cast(Any, components),
                cast(Any, dbos),
                consumer_id="approval-recovery-worker-a",
            )
        async with storage.uow() as uow:
            pending_state = await uow.approvals.get_resolution_queue_state(approval.approval_id)
        assert pending_state is not None
        assert pending_state.resolution_state == "recovery_pending"

        clock[0] += 31
        consumed = await consume_one(
            cast(Any, components),
            cast(Any, dbos),
            consumer_id="approval-recovery-worker-b",
        )
        events = await sink.read(run_id=waiting.run_id)
        async with storage.uow() as uow:
            public = await uow.approvals.get(approval.approval_id)
            state = await uow.approvals.get_resolution_queue_state(approval.approval_id)
            audits = await uow.audit_logs.list_for_tenant(identity.tenant_id)
        pending = await queue.pickup(consumer_id="after-recovery")
    finally:
        await storage.dispose()

    assert consumed == waiting.run_id
    assert pending is None
    assert calls == 1
    assert dbos.calls == 2
    assert delegation_service.calls == 1
    assert terminal_failures == 2
    assert public is not None and public.status == "approved"
    assert state is not None and state.resolution_state == "completed"
    assert sum(event.terminal for event in events) == 1
    assert sum(event.event_type == CanonicalEventType.APPROVAL_RESOLVED for event in events) == 1
    assert sum(record.action == "approval.approved" for record in audits) == 1
