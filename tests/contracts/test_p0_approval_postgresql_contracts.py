"""PostgreSQL 上的审批仲裁、租约 fencing 与唯一执行 claim 合同。"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from agent_harness.approvals import ApprovalService
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage, ToolInvocationCreate, run_migrations
from agent_harness.storage.access_repositories import ApprovalResolutionRepositoryConflict
from agent_harness.tools import hash_tool_arguments


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL claim contract runs when service smoke provides a DSN.",
)
@pytest.mark.asyncio
async def test_postgresql_approval_arbitration_and_unique_tool_claim(tmp_path: Path) -> None:
    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    identity = IdentityContext(
        tenant_id=f"contract-{uuid4()}",
        user_id="service-contract-user",
        session_id=str(uuid4()),
    )
    sink = LocalJsonlEventSink(tmp_path / "postgres-events.jsonl")
    orchestrator = RunOrchestrator(storage=storage, event_bus=EventBus(sink=sink))
    service = ApprovalService(
        storage=storage,
        event_bus=EventBus(sink=sink),
        orchestrator=orchestrator,
    )
    try:
        waiting = await orchestrator.start_run(
            agent_id="examples.postgres",
            input={},
            checkpoint_state={"reason": "postgres contract"},
            identity=identity,
        )
        approval = await service.require_approval(
            actor=identity,
            run_id=waiting.run_id,
            agent_id="examples.postgres",
            action="shell.execute",
            resource="tool:shell",
            reason="postgres contract",
            resume_token=waiting.resume_token,
        )
        async with storage.uow() as uow:
            lease = await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
            )
            await uow.commit()

        async with storage.uow() as uow:
            active_takeover = await uow.approvals.takeover_expired_resolution(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                expired_before=datetime.now(tz=UTC) - timedelta(seconds=1),
            )
        assert active_takeover is None

        async with storage.uow() as uow:
            takeover = await uow.approvals.takeover_expired_resolution(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                expired_before=datetime.now(tz=UTC) + timedelta(seconds=1),
            )
            await uow.commit()
        assert takeover is not None and takeover.lease_id != lease.lease_id

        claim = ToolInvocationCreate(
            tenant_id=identity.tenant_id,
            agent_id=approval.agent_id,
            run_id=approval.run_id,
            tool_name="shell.execute",
            args_ref="artifact://postgres-args",
            status="executing",
            approval_id=approval.approval_id,
            arguments_hash=hash_tool_arguments({"command": "echo postgres"}),
            execution_state="executing",
            metadata={"lease_id": takeover.lease_id},
        )
        async with storage.uow() as uow:
            stale_fence = await uow.approvals.fence_resolution_lease(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                lease_id=lease.lease_id,
            )
            current_fence = await uow.approvals.fence_resolution_lease(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                lease_id=takeover.lease_id,
            )
            await uow.tool_invocations.create(claim)
            await uow.commit()
        assert stale_fence is False
        assert current_fence is True

        async with storage.uow() as uow:
            claimed_takeover = await uow.approvals.takeover_expired_resolution(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                expired_before=datetime.now(tz=UTC) + timedelta(seconds=1),
            )
        assert claimed_takeover is None

        with pytest.raises(ApprovalResolutionRepositoryConflict) as conflict:
            async with storage.uow() as uow:
                await uow.approvals.deny_waiting(
                    approval_id=approval.approval_id,
                    run_id=approval.run_id,
                    tenant_id=approval.tenant_id,
                    resolved_by=identity.user_id,
                )
        assert conflict.value.code == "approval.resolution_in_progress"

        with pytest.raises(IntegrityError):
            async with storage.uow() as uow:
                await uow.tool_invocations.create(claim)
                await uow.commit()
    finally:
        await storage.dispose()
