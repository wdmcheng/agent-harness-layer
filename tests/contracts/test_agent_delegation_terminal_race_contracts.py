"""真实 PostgreSQL 下 parent terminal 冻结与 child final 的并发合同。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

import pytest
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.delegation.models import delegation_relation_id
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
from agent_harness.storage.shared_budget import LedgerCreate, OperationIdentity

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
                    routes: list[dict[str, object]] = [
                        {
                            "usage_kind": "model",
                            "provider": "fake",
                            "model": "fake-basic",
                            "price_source_ref": "catalog:fake",
                            "price_source_version": "v1",
                            "input_token_price_usd": "0",
                            "output_token_price_usd": "0",
                            "soft_max_tokens_per_call": 100,
                        }
                    ]
                    snapshot_id = f"snapshot:{request.run_id}"
                    snapshot: dict[str, Any] = {
                        "owner": {
                            "agent_id": request.agent_id,
                            "root_run_id": request.run_id,
                            "delegation_targets": ["agent-target"],
                            "max_tokens_per_run": 100,
                            "max_cost_usd_per_run": None,
                            "cost_enabled": False,
                        },
                        "registry_version": "terminal-race-registry-v1",
                        "config_version": "terminal-race-config-v1",
                        "catalog_version": "terminal-race-catalog-v1",
                        "agents": {
                            agent_id: {
                                "agent_id": agent_id,
                                "descriptor_version": f"{agent_id}-v1",
                                "model_policy": {
                                    "provider": "fake",
                                    "default_model": "fake-basic",
                                    "fallback_models": [],
                                },
                                "target_budget": {
                                    "max_tokens_per_run": (
                                        100 if agent_id == request.agent_id else 50
                                    ),
                                    "max_cost_usd_per_run": None,
                                },
                                "routes": routes,
                            }
                            for agent_id in (request.agent_id, "agent-target")
                        },
                    }
                    await uow.shared_budget.create_ledger(
                        LedgerCreate(
                            tenant_id=context.identity.tenant_id,
                            budget_owner_run_id=request.run_id,
                            token_limit=100,
                            cost_limit=None,
                            registry_version="terminal-race-registry-v1",
                            config_version="terminal-race-config-v1",
                            catalog_version="terminal-race-catalog-v1",
                            snapshot_id=snapshot_id,
                            snapshot=snapshot,
                        )
                    )
                    delegation_id = delegation_relation_id(
                        tenant_id=context.identity.tenant_id,
                        parent_run_id=request.run_id,
                        idempotency_key="race-key",
                    )
                    catalog_digest = hashlib.sha256(
                        json.dumps(
                            routes,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode("utf-8")
                    ).hexdigest()
                    await uow.delegations.claim_and_reserve(
                        DelegationClaimCreate(
                            delegation_id=delegation_id,
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
                            budget_identity=OperationIdentity.from_delegation_request(
                                tenant_id=context.identity.tenant_id,
                                fingerprint_key=b"terminal-race-delegation-key",
                                fingerprint_key_version="terminal-race-v1",
                                canonical_request_bytes=b"a" * 64,
                                parent_run_id=request.run_id,
                                source_agent_id=request.agent_id,
                                target_agent_id="agent-target",
                                delegation_claim_id=delegation_id,
                                operation_slot="race-key",
                                tree_snapshot_id=snapshot_id,
                                target_sub_snapshot_id=f"{snapshot_id}:agent-target",
                                target_route_catalog_digest=(f"budget-routes-v1:{catalog_digest}"),
                                cost_enabled=False,
                                trusted_token_bound=50,
                                trusted_cost_bound=None,
                            ),
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
