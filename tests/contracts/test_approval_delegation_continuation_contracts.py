"""Approval resume 与 delegation terminal checkpoint 的组合恢复合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from agent_harness.delegation import (
    DelegationRequest,
    delegation_relation_id,
    delegation_request_bytes,
    delegation_request_hash,
)
from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import (
    AgentApprovalRequest,
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    RunStatus,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_repositories import DelegationClaimCreate
from app.runtime import build_runtime_components

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


class _ApprovalThenDelegationExecutor:
    """审批前不执行；审批后只建立 durable delegation claim。"""

    def __init__(self) -> None:
        self.storage: SQLAlchemyStorage | None = None
        self.shared_budget: SharedBudgetRuntime | None = None

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        del request, context
        return AgentExecutionResult.waiting(
            AgentApprovalRequest(
                action="agent.delegate",
                resource="agent:examples.ticket_triage",
                reason="delegation requires review",
                arguments_ref="artifact://delegation-arguments",
                arguments_hash="a" * 64,
                continuation={"kind": "delegation"},
            )
        )

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        del grant
        if self.storage is None or self.shared_budget is None:
            raise RuntimeError("test runtime dependencies are not bound")
        storage = self.storage
        delegation_request = DelegationRequest(
            parent_run_id=request.run_id,
            source_agent_id=request.agent_id,
            target_agent_id="examples.ticket_triage",
            child_input={"text": "approved"},
            idempotency_key="approval-delegation-key",
            request_id=context.request_id,
        )
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger(
                context.identity.tenant_id,
                request.run_id,
            )
            snapshot = await uow.shared_budget.get_tree_snapshot(
                context.identity.tenant_id,
                request.run_id,
            )
            if ledger is None or snapshot is None:
                raise RuntimeError("test shared budget snapshot is unavailable")
            delegation_id = delegation_relation_id(
                tenant_id=context.identity.tenant_id,
                parent_run_id=request.run_id,
                idempotency_key=delegation_request.idempotency_key,
            )
            await uow.delegations.claim_and_reserve(
                DelegationClaimCreate(
                    delegation_id=delegation_id,
                    tenant_id=context.identity.tenant_id,
                    parent_run_id=request.run_id,
                    source_agent_id=request.agent_id,
                    target_agent_id="examples.ticket_triage",
                    idempotency_key="approval-delegation-key",
                    request_hash=delegation_request_hash(
                        delegation_request,
                        identity=context.identity,
                    ),
                    budget_intent="inherit_parent",
                    child_input={"text": "approved"},
                    identity=context.identity.to_payload(),
                    trace_id=str(context.trace_id),
                    request_id=context.request_id,
                    parent_token_limit=100,
                    requested_token_reservation=50,
                    parent_cost_limit=None,
                    requested_cost_reservation=None,
                    budget_identity=self.shared_budget.delegation_identity(
                        tenant_id=context.identity.tenant_id,
                        canonical_request_bytes=delegation_request_bytes(
                            delegation_request,
                            identity=context.identity,
                        ),
                        parent_run_id=request.run_id,
                        source_agent_id=request.agent_id,
                        target_agent_id="examples.ticket_triage",
                        delegation_id=delegation_id,
                        idempotency_key=delegation_request.idempotency_key,
                        tree_snapshot_id=ledger.snapshot_id,
                        snapshot=snapshot,
                        trusted_token_bound=1024,
                        trusted_cost_bound=None,
                    ),
                )
            )
            await uow.commit()
        return AgentExecutionResult.completed({"delegated": True})


@pytest.mark.asyncio
async def test_approval_waits_for_delegation_then_closes_ordered_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WAITING 不能伪装 run completed；child 收口后 approval 自动公开。"""

    executor = _ApprovalThenDelegationExecutor()
    original_resolve = AgentRegistry.resolve_executor
    original_get = AgentRegistry.get

    def get_with_frozen_target(self: AgentRegistry, agent_id: str):
        descriptor = original_get(self, agent_id)
        if agent_id == "examples.basic":
            return descriptor.model_copy(update={"delegation_targets": ["examples.ticket_triage"]})
        return descriptor

    def resolve_executor(self: AgentRegistry, agent_id: str):
        if agent_id == "examples.basic":
            return executor
        return original_resolve(self, agent_id)

    monkeypatch.setattr(AgentRegistry, "resolve_executor", resolve_executor)
    # Phase 13.8A 要求 target 在 root 创建时进入 immutable tree snapshot；
    # 本合同原先绕过 registry service 直写 delegation，因此显式补齐该前置事实。
    monkeypatch.setattr(AgentRegistry, "get", get_with_frozen_target)
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'approval-delegation.db'}"
    events_path = tmp_path / "approval-delegation-events.jsonl"
    run_migrations(dsn)
    components = build_runtime_components(
        profile="local",
        profiles_dir=PROFILES,
        storage_dsn=dsn,
        events_path=events_path,
        artifact_root=tmp_path / "approval-delegation-artifacts",
    )
    executor.storage = components.storage
    executor.shared_budget = cast(
        SharedBudgetRuntime,
        components.executor_services["shared_budget"],
    )
    actor = IdentityContext.local_default()
    reviewer = IdentityContext(
        tenant_id=actor.tenant_id,
        user_id="delegation-reviewer",
        session_id="delegation-review-session",
        roles=["reviewer"],
        permissions=["*"],
        auth_method="test",
    )
    try:
        waiting = await components.orchestrator.start_run(
            agent_id="examples.basic",
            input={"text": "approval delegation"},
            identity=actor,
            request_id="request-approval-delegation",
        )
        async with components.storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]

        approved = await components.approval_service.approve(
            actor=reviewer,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
        )
        async with components.storage.uow() as uow:
            parent_while_waiting = await uow.runs.get(waiting.run_id)
            checkpoint = await uow.checkpoints.get_latest(waiting.run_id)
            resolution_state = await uow.approvals.get_resolution_state(approval.approval_id)
            rows = await uow.evidence_outbox.list_for_run(run_id=waiting.run_id)
            delegation_event_ids = [
                row.event_id for row in rows if row.operation_kind == "delegation"
            ]

        assert approved.run is not None and approved.run.status == RunStatus.WAITING
        assert approved.approval.status == "waiting"
        assert parent_while_waiting is not None and parent_while_waiting.status == "waiting"
        assert checkpoint is not None and checkpoint.state["kind"] == "delegation_terminal"
        assert resolution_state == "completed"

        # 复用 composition 已绑定的 local sink；direct sink write 仍会走同一容量 claim。
        bus = EventBus(sink=components.event_sink)
        phase_types = {
            "claimed": CanonicalEventType.DELEGATION_CLAIMED,
            "child": CanonicalEventType.DELEGATION_CHILD_CREATED,
            "final": CanonicalEventType.DELEGATION_COMPLETED,
        }
        for event_id in delegation_event_ids:
            phase = event_id.rsplit(":", maxsplit=1)[-1]
            await bus.publish(
                tenant_id=actor.tenant_id,
                run_id=waiting.run_id,
                agent_id="examples.basic",
                user_id=actor.user_id,
                event_type=phase_types[phase],
                payload={"status": phase},
                request_id="request-approval-delegation",
                trace_id=parent_while_waiting.trace_id,
                event_id=event_id,
            )
            async with components.storage.uow() as uow:
                await uow.evidence_outbox.mark_event_published(event_id=event_id)
                await uow.commit()

        # 本夹具只建立 relation 并手工发布 evidence，从未启动 child/queue；0016
        # terminal fence 要求先以这条 durable 证明释放 top-level reservation。
        async with components.storage.uow() as uow:
            delegation = (
                await uow.delegations.list_for_parent(
                    tenant_id=actor.tenant_id,
                    parent_run_id=waiting.run_id,
                )
            )[0]
            await uow.shared_budget.release_delegation(delegation_id=delegation.id)
            await uow.commit()

        original_recover = components.approval_service.recover_claimed
        recovery_attempts = 0

        async def fail_once(**kwargs: Any) -> Any:
            nonlocal recovery_attempts
            recovery_attempts += 1
            if recovery_attempts == 1:
                raise RuntimeError("injected approval recovery interruption")
            return await original_recover(**kwargs)

        monkeypatch.setattr(components.approval_service, "recover_claimed", fail_once)
        with pytest.raises(RuntimeError, match="injected approval recovery interruption"):
            await components.orchestrator.resume_run(
                checkpoint.resume_token,
                expected_run_id=waiting.run_id,
                identity=actor,
            )
        async with components.storage.uow() as uow:
            terminal_parent = await uow.runs.get(waiting.run_id)
            interrupted_approval = await uow.approvals.get(approval.approval_id)
        assert terminal_parent is not None and terminal_parent.status == "completed"
        assert interrupted_approval is not None and interrupted_approval.status == "waiting"

        resumed = await components.orchestrator.resume_run(
            checkpoint.resume_token,
            expected_run_id=waiting.run_id,
            identity=actor,
        )
        async with components.storage.uow() as uow:
            final_approval = await uow.approvals.get(approval.approval_id)
            group = await uow.evidence_outbox.ordered_group(
                group_id=f"approval:{approval.approval_id}:resolution"
            )
            group_states = {item.state for item in group}
        events = await components.event_sink.read(run_id=waiting.run_id)
    finally:
        await components.close()

    assert resumed.status == RunStatus.COMPLETED
    assert recovery_attempts == 2
    assert final_approval is not None and final_approval.status == "approved"
    assert group_states == {"published"}
    resolution_seq = next(
        event.seq for event in events if event.event_type == CanonicalEventType.APPROVAL_RESOLVED
    )
    terminal_seq = next(event.seq for event in events if event.terminal)
    assert resolution_seq < terminal_seq
    assert sum(event.terminal for event in events) == 1
