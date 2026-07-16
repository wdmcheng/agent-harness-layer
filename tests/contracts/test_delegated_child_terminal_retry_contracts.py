"""终态已提交但聚合确认失败时，公开入口必须先补偿再返回冲突。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Response
from starlette.requests import Request

from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunResult, RunStatus
from app.api.routes import approvals as approval_routes
from app.api.routes.runs import cancel_run


def _request() -> Request:
    return Request({"type": "http", "headers": []})


class _FailOnceReconciler:
    """模拟 child terminal 已提交、第一次响应前聚合确认瞬时失败。"""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.terminal_committed = False
        self.failed = False

    async def reconcile_child_if_delegated(self, run_id: str) -> bool:
        self.calls.append(f"reconcile:{run_id}")
        if self.terminal_committed and not self.failed:
            self.failed = True
            raise OSError("aggregation unavailable")
        return True


@pytest.mark.asyncio
async def test_cancel_retry_recovers_aggregation_before_terminal_conflict() -> None:
    calls: list[str] = []
    reconciler = _FailOnceReconciler(calls)

    class Orchestrator:
        async def get_run(self, run_id: str, **_kwargs: object) -> RunResult:
            calls.append(f"get:{run_id}")
            status = RunStatus.CANCELLED if reconciler.terminal_committed else RunStatus.RUNNING
            return RunResult(run_id=run_id, status=status)

        async def cancel_run(self, run_id: str, **_kwargs: object) -> RunResult:
            calls.append(f"cancel:{run_id}")
            if reconciler.terminal_committed:
                raise RuntimeError("run already terminal")
            reconciler.terminal_committed = True
            return RunResult(run_id=run_id, status=RunStatus.CANCELLED)

    with pytest.raises(OSError, match="aggregation unavailable"):
        await cancel_run(
            _request(),
            "child-cancel-retry",
            cast(Any, Orchestrator()),
            IdentityContext.local_default(),
            cast(Any, reconciler),
        )

    with pytest.raises(RuntimeError, match="already terminal"):
        await cancel_run(
            _request(),
            "child-cancel-retry",
            cast(Any, Orchestrator()),
            IdentityContext.local_default(),
            cast(Any, reconciler),
        )

    assert calls == [
        "get:child-cancel-retry",
        "reconcile:child-cancel-retry",
        "cancel:child-cancel-retry",
        "reconcile:child-cancel-retry",
        "get:child-cancel-retry",
        "reconcile:child-cancel-retry",
        "cancel:child-cancel-retry",
    ]


@pytest.mark.asyncio
async def test_approval_retry_recovers_aggregation_before_resolution_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    reconciler = _FailOnceReconciler(calls)
    approval = SimpleNamespace(
        approval_id="approval-retry",
        tenant_id="local",
        run_id="child-approval-retry",
        agent_id="agent-target",
        status="waiting",
        action="agent.execute",
        resource="agent:agent-target",
        reason="retry contract",
        trace_id="trace-approval-retry",
        request_id="request-approval-retry",
        requested_by="local-user",
        resolved_by=None,
        created_at=None,
    )

    async def allow_resolution(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(approval_routes, "_check_resolve_permission", allow_resolution)

    class ApprovalService:
        uses_queue = False

        async def get_by_id(self, **_kwargs: object) -> SimpleNamespace:
            calls.append("get:approval-retry")
            return approval

        async def approve(self, **kwargs: object) -> SimpleNamespace:
            run_id = str(kwargs["run_id"])
            calls.append(f"approve:{run_id}")
            if reconciler.terminal_committed:
                raise RuntimeError("approval already resolved")
            reconciler.terminal_committed = True
            return SimpleNamespace(
                approval=approval,
                run=RunResult(run_id=run_id, status=RunStatus.COMPLETED),
            )

    async def resolve() -> object:
        return await approval_routes.resolve_approval(
            _request(),
            Response(),
            "child-approval-retry",
            "approval-retry",
            approval_routes.ApprovalResolveRequest(decision="approved"),
            IdentityContext.local_default(),
            cast(Any, ApprovalService()),
            None,
            cast(Any, reconciler),
        )

    with pytest.raises(OSError, match="aggregation unavailable"):
        await resolve()
    with pytest.raises(RuntimeError, match="already resolved"):
        await resolve()

    assert calls == [
        "get:approval-retry",
        "reconcile:child-approval-retry",
        "approve:child-approval-retry",
        "reconcile:child-approval-retry",
        "get:approval-retry",
        "reconcile:child-approval-retry",
        "approve:child-approval-retry",
    ]
