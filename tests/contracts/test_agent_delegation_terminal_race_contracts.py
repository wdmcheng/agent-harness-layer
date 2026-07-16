"""真实 PostgreSQL 下 parent terminal 冻结与 child final 的并发合同。"""

from __future__ import annotations

import asyncio
import os

import pytest
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.events import EventBus, PostgreSQLEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    RunOrchestrator,
    RunStatus,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_repositories import DelegationClaimCreate
from agent_harness.storage.event_capacity_repositories import EvidenceOperationKind
from agent_harness.storage.repositories import (
    CheckpointCreate,
    CheckpointRecord,
    CheckpointRepository,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="delegation terminal 竞争合同需要真实 PostgreSQL。",
)


@pytest.mark.asyncio
async def test_child_final_cannot_cross_terminal_intent_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """child final 必须等 parent checkpoint 提交，不能错过唯一恢复边沿。"""

    checkpoint_entered = asyncio.Event()
    allow_checkpoint = asyncio.Event()
    original_create = CheckpointRepository.create

    async def blocking_create(
        self: CheckpointRepository,
        data: CheckpointCreate,
    ) -> CheckpointRecord:
        checkpoint_entered.set()
        await allow_checkpoint.wait()
        return await original_create(self, data)

    monkeypatch.setattr(CheckpointRepository, "create", blocking_create)
    identity = IdentityContext(
        tenant_id="tenant-race",
        user_id="user-race",
        session_id="session-race",
        roles=["operator"],
        permissions=["agent.delegate"],
        auth_method="test",
    )

    async with isolated_database("delegation_terminal_race") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)

        class DelegatingExecutor:
            """从公开 executor seam 创建 pending delegation 后返回终态意图。"""

            parent_run_id: str | None = None

            async def run(
                self,
                request: AgentExecutionRequest,
                context: AgentExecutionContext,
            ) -> AgentExecutionResult:
                self.parent_run_id = request.run_id
                async with storage.uow() as uow:
                    await uow.delegations.claim_and_reserve(
                        DelegationClaimCreate(
                            tenant_id=context.identity.tenant_id,
                            parent_run_id=request.run_id,
                            source_agent_id=request.agent_id,
                            target_agent_id="agent-target",
                            idempotency_key="race-key",
                            request_hash="a" * 64,
                            budget_intent="inherit_parent",
                            child_input={"prompt": "safe"},
                            identity=context.identity.to_payload(),
                            trace_id=str(context.trace_id),
                            parent_token_limit=100,
                            requested_token_reservation=50,
                            parent_cost_limit=None,
                            requested_cost_reservation=None,
                        )
                    )
                    await uow.commit()
                return AgentExecutionResult.completed({"ok": True})

            async def resume(
                self,
                request: AgentExecutionRequest,
                context: AgentExecutionContext,
                grant: ApprovalGrant,
            ) -> AgentExecutionResult:
                raise AssertionError("本合同不应进入 approval resume")

        executor = DelegatingExecutor()
        orchestrator = RunOrchestrator(
            storage=storage,
            event_bus=EventBus(sink=PostgreSQLEventSink(storage)),
            identity=identity,
            executor_resolver=lambda _agent_id: executor,
        )
        try:
            freeze_task = asyncio.create_task(
                orchestrator.start_run(
                    agent_id="agent-source",
                    input={"prompt": "race"},
                    identity=identity,
                    trace_id="trace-delegation-terminal-race",
                )
            )
            await asyncio.wait_for(checkpoint_entered.wait(), timeout=2)
            assert executor.parent_run_id is not None
            parent_run_id = executor.parent_run_id

            async def settle_last_child() -> None:
                async with storage.uow() as uow:
                    locked = await uow.runs.get_for_update(parent_run_id)
                    assert locked is not None
                    rows = await uow.evidence_outbox.list_for_run(run_id=parent_run_id)
                    for row in rows:
                        if row.operation_kind == "delegation":
                            await uow.evidence_outbox.mark_event_published(event_id=row.event_id)
                    await uow.commit()

            child_final_task = asyncio.create_task(settle_last_child())
            await asyncio.sleep(0.05)
            assert not child_final_task.done()

            allow_checkpoint.set()
            frozen, _ = await asyncio.gather(freeze_task, child_final_task)
            async with storage.uow() as uow:
                persisted_parent = await uow.runs.get(parent_run_id)
                checkpoint = await uow.checkpoints.get_latest(parent_run_id)
                pending = await uow.evidence_outbox.has_pending_operation(
                    run_id=parent_run_id,
                    operation_kind=EvidenceOperationKind.DELEGATION,
                )
        finally:
            await storage.dispose()

    assert frozen is not None and frozen.status == RunStatus.WAITING
    assert persisted_parent is not None and persisted_parent.status == "waiting"
    assert checkpoint is not None and checkpoint.state["kind"] == "delegation_terminal"
    assert pending is False
