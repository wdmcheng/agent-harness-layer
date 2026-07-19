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
    """构造无需认证头的最小 HTTP 请求对象，供路由函数直接调用。"""

    return Request({"type": "http", "headers": []})


def _local_profiles(tmp_path: Path) -> Path:
    """复制 service-app 模板并仅为本测试打开 basic agent 的 ticket 委派边。"""

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
    """记录子 run 聚合调用的委派服务替身，用于断言终结前后的对账顺序。"""

    def __init__(self, calls: list[str]) -> None:
        """复用外层调用记录，使路由和 worker 操作可按时间顺序比较。"""

        self.calls = calls

    async def reconcile_child_if_delegated(self, run_id: str) -> bool:
        """记录针对 child run 的可重入聚合触发，并模拟确有委派关联。"""

        self.calls.append(f"reconcile:{run_id}")
        return True


@pytest.mark.asyncio
async def test_cancel_route_reconciles_child_after_terminal() -> None:
    """验证取消路由在读取和写入终结状态前后都触发 child 聚合，覆盖可重入恢复。"""

    calls: list[str] = []

    class Orchestrator:
        """记录取消路由所需 get/cancel 操作的最小编排器替身。"""

        async def get_run(self, run_id: str, **_kwargs: object) -> RunResult:
            """记录预读取并返回运行中的 child，驱动取消分支。"""

            calls.append(f"get:{run_id}")
            return RunResult(run_id=run_id, status=RunStatus.RUNNING)

        async def cancel_run(self, run_id: str, **_kwargs: object) -> RunResult:
            """记录取消写入并返回已终结的 child 状态。"""

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
    """验证恢复路由在 child 变为终结状态后完成聚合，再把结果返回给 HTTP 调用方。"""

    calls: list[str] = []

    class Orchestrator:
        """记录恢复路由所需 get/resume 操作的最小编排器替身。"""

        async def get_run(self, run_id: str, **_kwargs: object) -> RunResult:
            """记录预读取并返回等待恢复的 child。"""

            calls.append(f"get:{run_id}")
            return RunResult(run_id=run_id, status=RunStatus.WAITING)

        async def resume_run(self, _token: str, **kwargs: object) -> RunResult:
            """从路由传入的 expected run id 构造完成结果，验证恢复坐标未丢失。"""

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
        """让 ticket child 停在审批等待状态的执行器，用于随后真实取消路径。"""

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """忽略普通输入并返回带稳定审批上下文的 waiting 结果。"""

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
        """仅替换目标 child 的 executor，其他 agent 保持真实 registry 解析。"""

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
    """验证审批 worker 成功恢复 child 后，先补 usage 再执行审批并聚合子状态。"""

    calls: list[str] = []

    class Orchestrator:
        """记录审批恢复前 usage 证据恢复调用的最小编排器替身。"""

        async def recover_pending_usage_evidence(self, *, run_id: str) -> None:
            """记录针对 child 的 usage 恢复，证明其先于审批业务执行。"""

            calls.append(f"usage:{run_id}")

    class ApprovalService:
        """返回已完成 child 的审批服务替身，记录 worker 提交的 run 坐标。"""

        async def execute_queued_approval(self, **kwargs: object) -> SimpleNamespace:
            """记录审批执行并返回 completed run，驱动委派聚合路径。"""

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
    """验证本地审批路由无队列时，批准或拒绝的终结 child 都会被前后两次聚合。"""

    calls: list[str] = []

    async def allow_resolution(**_kwargs: object) -> None:
        """替换权限检查为允许，隔离本测试对路由终结和聚合顺序的验证。"""

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
        """无队列审批服务替身，根据路由决策返回指定的 child 终结状态。"""

        uses_queue = False

        async def get_by_id(self, **_kwargs: object) -> SimpleNamespace:
            """记录审批读取并返回预设耐久审批对象。"""

            calls.append("get:approval-local")
            return approval

        async def approve(self, **kwargs: object) -> SimpleNamespace:
            """记录批准操作并返回与参数一致的 child run 结果。"""

            calls.append(f"approve:{kwargs['run_id']}")
            return SimpleNamespace(
                approval=approval,
                run=RunResult(run_id=str(kwargs["run_id"]), status=terminal_status),
            )

        async def deny(self, **kwargs: object) -> SimpleNamespace:
            """记录拒绝操作并返回与参数一致的 child run 结果。"""

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
    """验证审批 worker 的确定性失败先收敛 child 聚合，再确认恢复消息。"""

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
        """替换 approval owner 准备逻辑并记录它先于失败收敛执行。"""

        calls.append("prepare")

    monkeypatch.setattr(runtime_worker, "_prepare_approval_owner", prepare)

    class ApprovalService:
        """记录确定性 DBOS 失败后的审批失败收敛调用的服务替身。"""

        async def finalize_queued_failure(self, **kwargs: object) -> None:
            """记录按 run id 失败收敛，供断言其发生在队列确认之前。"""

            calls.append(f"finalize:{kwargs['run_id']}")

    class DeterministicDBOS:
        """始终返回审批恢复确定性失败的 DBOS 替身。"""

        async def execute(self, _operation: object) -> DBOSOperationOutcome:
            """返回可安全映射的失败状态，不触发未知异常重试路径。"""

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
