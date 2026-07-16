"""审批恢复失败时的 waiting 状态合同测试。"""

from __future__ import annotations

from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    IdentityContext as IdentityContext,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    InvalidRunTransition as InvalidRunTransition,
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
    pytest as pytest,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_auth_policy_hitl_approval_contracts import (
    sqlite_dsn as sqlite_dsn,
)


@pytest.mark.asyncio
async def test_failed_runtime_resume_keeps_approval_waiting(tmp_path: Path) -> None:
    from agent_harness.approvals import ApprovalService
    from agent_harness.audit import AuditService

    db_path = tmp_path / "approval-runtime-failure.db"
    events_path = tmp_path / "events.jsonl"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_bus = EventBus(sink=LocalJsonlEventSink(events_path))
    orchestrator = RunOrchestrator(storage=storage, event_bus=event_bus)
    identity = IdentityContext.local_default(session_id="runtime-failure-session")
    approval_service = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        audit=AuditService(storage=storage),
    )
    waiting = await orchestrator.start_run(
        agent_id="examples.basic",
        input={"prompt": "pause then cancel"},
        checkpoint_state={"reason": "shell.execute"},
    )
    assert waiting.resume_token is not None
    approval = await approval_service.require_approval(
        actor=identity,
        run_id=waiting.run_id,
        agent_id="examples.basic",
        action="shell.execute",
        resource="tool:shell",
        reason="dangerous action requires approval",
        resume_token=waiting.resume_token,
    )
    await orchestrator.cancel_run(waiting.run_id)

    try:
        with pytest.raises(InvalidRunTransition):
            await approval_service.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
            )
        async with storage.uow() as uow:
            row = await uow.approvals.get(approval.approval_id)
            run = await uow.runs.get(waiting.run_id)
    finally:
        await storage.dispose()

    assert row is not None
    assert row.status == "waiting"
    assert row.resolved_by is None
    assert run is not None
    assert run.status == "cancelled"
