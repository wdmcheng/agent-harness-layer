"""Delegated child 的所有应用终态入口都必须触发可重入聚合。"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from fastapi import Response
from starlette.requests import Request

from agent_harness.adapters.runtime import DBOSOperation, DBOSOperationOutcome
from agent_harness.delegation import DelegationRequest
from agent_harness.identity import IdentityContext
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import (
    AgentApprovalRequest,
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    InMemoryRunQueue,
    RunResult,
    RunStatus,
    build_resume_approval_message,
)
from agent_harness.storage import run_migrations
from agent_harness.storage.repositories import RunCreate, SessionCreate
from app import runtime as app_runtime
from app.api.routes import approvals as approval_routes
from app.api.routes.runs import RunResumeRequest, cancel_run, resume_run
from app.workers import runtime_worker
from app.workers.runtime_worker import consume_one
from app.workers.runtime_worker_operations import execute_approval_operation


def _request() -> Request:
    return Request({"type": "http", "headers": []})


def _local_profiles(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2] / "templates" / "service-app"
    target = tmp_path / "service-app"
    shutil.copytree(source, target)
    config = target / "agents" / "examples" / "basic" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "delegation_edges: []",
            "delegation_edges:\n  - examples.ticket_triage",
        ),
        encoding="utf-8",
    )
    return target / "configs" / "profiles"


class _DelegationRecorder:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def reconcile_child_if_delegated(self, run_id: str) -> bool:
        self.calls.append(f"reconcile:{run_id}")
        return True


@pytest.mark.asyncio
async def test_cancel_route_reconciles_child_after_terminal() -> None:
    calls: list[str] = []

    class Orchestrator:
        async def get_run(self, run_id: str, **_kwargs: object) -> RunResult:
            calls.append(f"get:{run_id}")
            return RunResult(run_id=run_id, status=RunStatus.RUNNING)

        async def cancel_run(self, run_id: str, **_kwargs: object) -> RunResult:
            calls.append(f"cancel:{run_id}")
            return RunResult(run_id=run_id, status=RunStatus.CANCELLED)

    result = await cancel_run(
        _request(),
        "child-cancel",
        cast(Any, Orchestrator()),
        IdentityContext.local_default(),
        cast(Any, _DelegationRecorder(calls)),
    )

    assert result.status == RunStatus.CANCELLED
    assert calls == [
        "get:child-cancel",
        "reconcile:child-cancel",
        "cancel:child-cancel",
        "reconcile:child-cancel",
    ]


@pytest.mark.asyncio
async def test_resume_route_reconciles_terminal_child_before_response() -> None:
    calls: list[str] = []

    class Orchestrator:
        async def get_run(self, run_id: str, **_kwargs: object) -> RunResult:
            calls.append(f"get:{run_id}")
            return RunResult(run_id=run_id, status=RunStatus.WAITING)

        async def resume_run(self, _token: str, **kwargs: object) -> RunResult:
            run_id = str(kwargs["expected_run_id"])
            calls.append(f"resume:{run_id}")
            return RunResult(run_id=run_id, status=RunStatus.COMPLETED)

    result = await resume_run(
        _request(),
        "child-resume",
        RunResumeRequest(resume_token="resume-child"),
        cast(Any, Orchestrator()),
        IdentityContext.local_default(),
        cast(Any, _DelegationRecorder(calls)),
    )

    assert result.status == RunStatus.COMPLETED
    assert calls == [
        "get:child-resume",
        "reconcile:child-resume",
        "resume:child-resume",
        "reconcile:child-resume",
    ]


@pytest.mark.asyncio
async def test_local_cancel_persists_delegated_child_aggregate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """local cancel 也走真实 orchestrator/storage/service，不只验证 mock 调用。"""

    class WaitingChildExecutor:
        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            del request, context
            return AgentExecutionResult.waiting(
                AgentApprovalRequest(
                    action="agent.execute",
                    resource="agent:examples.ticket_triage",
                    reason="cancel contract",
                    arguments_ref="artifact://cancel-contract",
                    arguments_hash="d" * 64,
                    continuation={"kind": "cancel_contract"},
                )
            )

    original_resolve = AgentRegistry.resolve_executor

    def resolve_executor(self: AgentRegistry, agent_id: str) -> Any:
        if agent_id == "examples.ticket_triage":
            return WaitingChildExecutor()
        return original_resolve(self, agent_id)

    monkeypatch.setattr(AgentRegistry, "resolve_executor", resolve_executor)
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'delegated-child-cancel.db'}"
    run_migrations(dsn)
    components = app_runtime.build_runtime_components(
        profile="local",
        profiles_dir=_local_profiles(tmp_path),
        storage_dsn=dsn,
        events_path=tmp_path / "delegated-child-cancel.jsonl",
        artifact_root=tmp_path / "artifacts",
    )
    actor = IdentityContext.local_default()
    try:
        async with components.storage.uow() as uow:
            await uow.tenants.ensure(actor.tenant_id)
            session = await uow.sessions.ensure(
                SessionCreate(
                    session_id=actor.session_id,
                    tenant_id=actor.tenant_id,
                    user_id=actor.user_id,
                    agent_id="examples.basic",
                )
            )
            parent = await uow.runs.create(
                RunCreate(
                    tenant_id=actor.tenant_id,
                    session_id=session.id,
                    agent_id="examples.basic",
                    trace_id="trace-delegated-child-cancel",
                )
            )
            budget_runtime = cast(Any, components.executor_services["shared_budget"])
            await uow.shared_budget.create_ledger(
                budget_runtime.ledger_create(
                    tenant_id=actor.tenant_id,
                    run_id=parent.id,
                    agent_id="examples.basic",
                )
            )
            await uow.commit()
        delegated = await components.delegation_service.delegate(
            DelegationRequest(
                parent_run_id=parent.id,
                source_agent_id="examples.basic",
                target_agent_id="examples.ticket_triage",
                child_input={"text": "cancel child"},
                idempotency_key="delegated-child-cancel",
            ),
            identity=actor,
        )
        response = await cancel_run(
            _request(),
            delegated.child_run_id,
            components.orchestrator,
            actor,
            components.delegation_service,
        )
        async with components.storage.uow() as uow:
            child = await uow.runs.get(delegated.child_run_id)
            aggregates = await uow.delegations.list_aggregates_for_parent(
                tenant_id=actor.tenant_id,
                parent_run_id=parent.id,
            )
    finally:
        await components.close()

    assert response.status == RunStatus.CANCELLED
    assert child is not None and child.status == RunStatus.CANCELLED.value
    assert len(aggregates) == 1
    assert aggregates[0].summary["children"][0]["status"] == RunStatus.CANCELLED.value


@pytest.mark.asyncio
async def test_approval_worker_success_reconciles_terminal_child() -> None:
    calls: list[str] = []

    class Orchestrator:
        async def recover_pending_usage_evidence(self, *, run_id: str) -> None:
            calls.append(f"usage:{run_id}")

    class ApprovalService:
        async def execute_queued_approval(self, **kwargs: object) -> SimpleNamespace:
            run_id = str(kwargs["run_id"])
            calls.append(f"approval:{run_id}")
            return SimpleNamespace(run=RunResult(run_id=run_id, status=RunStatus.COMPLETED))

    components = SimpleNamespace(
        orchestrator=Orchestrator(),
        approval_service=ApprovalService(),
        delegation_service=_DelegationRecorder(calls),
    )
    payload = await execute_approval_operation(
        cast(Any, components),
        DBOSOperation(
            kind="resume_approval",
            tenant_id="tenant-a",
            run_id="child-approved",
            operation_id="operation-a",
            approval_id="approval-a",
            resolution_lease_id="lease-a",
        ),
    )

    assert payload["status"] == RunStatus.COMPLETED.value
    assert calls == [
        "usage:child-approved",
        "approval:child-approved",
        "reconcile:child-approved",
    ]


@pytest.mark.parametrize(
    ("decision", "terminal_status"),
    [
        ("approved", RunStatus.COMPLETED),
        ("denied", RunStatus.FAILED),
    ],
)
@pytest.mark.asyncio
async def test_local_approval_route_reconciles_terminal_child(
    monkeypatch: pytest.MonkeyPatch,
    decision: Literal["approved", "denied"],
    terminal_status: RunStatus,
) -> None:
    calls: list[str] = []

    async def allow_resolution(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(approval_routes, "_check_resolve_permission", allow_resolution)

    approval = SimpleNamespace(
        approval_id="approval-local",
        tenant_id="local",
        run_id="child-local-approval",
        agent_id="agent-target",
        status=decision,
        action="agent.execute",
        resource="agent:agent-target",
        reason="contract",
        trace_id="trace-local-approval",
        request_id="request-local-approval",
        requested_by="local-user",
        resolved_by="reviewer",
        created_at=None,
    )

    class ApprovalService:
        uses_queue = False

        async def get_by_id(self, **_kwargs: object) -> SimpleNamespace:
            calls.append("get:approval-local")
            return approval

        async def approve(self, **kwargs: object) -> SimpleNamespace:
            calls.append(f"approve:{kwargs['run_id']}")
            return SimpleNamespace(
                approval=approval,
                run=RunResult(run_id=str(kwargs["run_id"]), status=terminal_status),
            )

        async def deny(self, **kwargs: object) -> SimpleNamespace:
            calls.append(f"deny:{kwargs['run_id']}")
            return SimpleNamespace(
                approval=approval,
                run=RunResult(run_id=str(kwargs["run_id"]), status=terminal_status),
            )

    result = await approval_routes.resolve_approval(
        _request(),
        Response(),
        "child-local-approval",
        "approval-local",
        approval_routes.ApprovalResolveRequest(decision=decision),
        IdentityContext.local_default(),
        cast(Any, ApprovalService()),
        None,
        cast(Any, _DelegationRecorder(calls)),
    )

    assert result.run is not None and result.run.status == terminal_status
    assert calls == [
        "get:approval-local",
        "reconcile:child-local-approval",
        f"{'approve' if decision == 'approved' else 'deny'}:child-local-approval",
        "reconcile:child-local-approval",
    ]


@pytest.mark.asyncio
async def test_approval_worker_failure_reconciles_before_queue_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    queue = InMemoryRunQueue()
    message = build_resume_approval_message(
        request_id="request-failed",
        tenant_id="tenant-a",
        run_id="child-failed",
        approval_id="approval-failed",
        resolution_lease_id="lease-failed",
    )
    await queue.enqueue(message)

    async def prepare(*_args: object, **_kwargs: object) -> None:
        calls.append("prepare")

    monkeypatch.setattr(runtime_worker, "_prepare_approval_owner", prepare)

    class ApprovalService:
        async def finalize_queued_failure(self, **kwargs: object) -> None:
            calls.append(f"finalize:{kwargs['run_id']}")

    class DeterministicDBOS:
        async def execute(self, _operation: object) -> DBOSOperationOutcome:
            return DBOSOperationOutcome(
                status="deterministic_failed",
                error_code="approval.resume_failed",
            )

    components = SimpleNamespace(
        queue=queue,
        approval_service=ApprovalService(),
        delegation_service=_DelegationRecorder(calls),
    )
    consumed = await consume_one(
        cast(Any, components),
        cast(Any, DeterministicDBOS()),
        consumer_id="approval-failure-worker",
    )

    assert consumed == "child-failed"
    assert calls == [
        "prepare",
        "finalize:child-failed",
        "reconcile:child-failed",
    ]
    assert await queue.pickup(consumer_id="after-ack") is None
