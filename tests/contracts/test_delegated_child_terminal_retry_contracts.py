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
    """构造最小 ASGI 请求，隔离路由合同与真实网络服务器。"""

    return Request({"type": "http", "headers": []})


class _FailOnceReconciler:
    """模拟 child terminal 已提交、第一次响应前聚合确认瞬时失败。"""

    def __init__(self, calls: list[str]) -> None:
        """绑定可观察调用序列，并将 terminal/首次失败状态初始化为未发生。"""

        self.calls = calls
        self.terminal_committed = False
        self.failed = False

    async def reconcile_child_if_delegated(self, run_id: str) -> bool:
        """在 terminal 已提交的第一次补偿时失败，之后允许重试成功。"""

        self.calls.append(f"reconcile:{run_id}")
        if self.terminal_committed and not self.failed:
            self.failed = True
            raise OSError("aggregation unavailable")
        return True


@pytest.mark.asyncio
async def test_cancel_retry_recovers_aggregation_before_terminal_conflict() -> None:
    """验证重试 cancel 会先补偿 child 聚合，再报告既有 terminal 冲突。"""

    calls: list[str] = []
    reconciler = _FailOnceReconciler(calls)

    class Orchestrator:
        """按桩状态呈现运行中或已取消，并记录 route 对 orchestrator 的调用顺序。"""

        async def get_run(self, run_id: str, **_kwargs: object) -> RunResult:
            """读取当前 terminal 标志，模拟取消前后的 run 查询结果。"""

            calls.append(f"get:{run_id}")
            status = RunStatus.CANCELLED if reconciler.terminal_committed else RunStatus.RUNNING
            return RunResult(run_id=run_id, status=status)

        async def cancel_run(self, run_id: str, **_kwargs: object) -> RunResult:
            """首次取消提交 terminal，重复取消抛出冲突以验证补偿先行。"""

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
    """验证 approval 重试同样先恢复 child 聚合，再暴露已决议冲突。"""

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
        """替换路由权限检查，使测试仅覆盖重试与补偿顺序。"""

        return None

    monkeypatch.setattr(approval_routes, "_check_resolve_permission", allow_resolution)

    class ApprovalService:
        """在首次 approve 后标记 terminal 的最小审批服务替身。"""

        uses_queue = False

        async def get_by_id(self, **_kwargs: object) -> SimpleNamespace:
            """返回固定 waiting approval，并记录 resolve 路由的读取动作。"""

            calls.append("get:approval-retry")
            return approval

        async def approve(self, **kwargs: object) -> SimpleNamespace:
            """首次批准返回已完成 run，重试时模拟 approval 已被其他路径收口。"""

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
        """通过真实 route 函数触发一次批准请求，保留所有公开 seam 参数。"""

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
