"""Service worker 每种 durable operation 的 run-scoped usage 恢复合同。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent_harness.adapters.runtime import DBOSOperation
from agent_harness.runtime import RunStatus
from app.workers import runtime_worker


class WorkerStopped(RuntimeError):
    """让合同在 handler 执行一次后退出常驻 worker。"""


@pytest.mark.asyncio
async def test_resume_approval_recovers_run_usage_before_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """审批续跑前必须先补投该运行的用量证据，随后才执行续跑与委派对账。"""

    calls: list[str] = []
    operation = DBOSOperation(
        kind="resume_approval",
        tenant_id="tenant-a",
        run_id="run-a",
        operation_id="approval:run-a:resume",
        approval_id="approval-a",
        resolution_lease_id="lease-a",
    )

    class Orchestrator:
        """仅记录用量恢复调用的编排器替身。"""

        async def recover_pending_usage_evidence(self, *, run_id: str | None = None) -> None:
            """记录恢复目标运行，供调用顺序断言。"""

            calls.append(f"recover:{run_id}")

    class ApprovalService:
        """返回已完成运行的审批服务替身，不产生真实审批副作用。"""

        async def execute_queued_approval(self, **kwargs: object) -> SimpleNamespace:
            """记录审批续跑调用并返回最小完成结果。"""

            calls.append(f"approval:{kwargs['run_id']}")
            run = SimpleNamespace(
                status=RunStatus.COMPLETED,
                to_payload=lambda: {"status": "completed"},
            )
            return SimpleNamespace(run=run)

    class DelegationService:
        """记录委派对账调用的替身，模拟本运行不是子委派。"""

        async def reconcile_child_if_delegated(self, run_id: str) -> bool:
            """记录对账目标并返回未委派，避免进入额外恢复逻辑。"""

            calls.append(f"delegation:{run_id}")
            return False

    class Components:
        """为 worker 入口提供最小运行组件集合。"""

        queue = object()
        storage = SimpleNamespace(dsn="postgresql+asyncpg://unused")
        orchestrator = Orchestrator()
        approval_service = ApprovalService()
        delegation_service = DelegationService()

        async def close(self) -> None:
            """记录组件关闭，验证异常退出仍释放资源。"""

            calls.append("components:closed")

    class Adapter:
        """只分派一次指定操作后主动停止的 DBOS adapter 替身。"""

        def __init__(self, *, handlers: dict[str, Any], **_kwargs: object) -> None:
            """保存 worker 注册的 handler，其他构造参数在本合同中无关。"""

            self._handlers = handlers

        async def start(self) -> None:
            """调用一次审批恢复 handler 后停止常驻循环。"""

            await self._handlers["resume_approval"](operation)
            raise WorkerStopped

        async def close(self) -> None:
            """记录 adapter 关闭顺序。"""

            calls.append("adapter:closed")

    async def no_recovery(_components: object) -> None:
        """替换启动时全局恢复，隔离本测试关心的单个操作恢复。"""

        return None

    def build_components(**_kwargs: object) -> Components:
        """返回测试专用组件，避免 worker 读取真实 profile 或连接后端。"""

        return Components()

    monkeypatch.setattr(runtime_worker, "build_runtime_components", build_components)
    monkeypatch.setattr(runtime_worker, "DBOSServiceRuntimeAdapter", Adapter)
    monkeypatch.setattr(runtime_worker, "_recover_pending_enqueue", no_recovery)
    monkeypatch.setattr(runtime_worker, "_recover_pending_usage", no_recovery)

    # worker 先运行 operation 级恢复，再调用审批和委派 seam；关闭顺序也必须可预期。
    with pytest.raises(WorkerStopped):
        await runtime_worker.run_forever()

    assert calls[:3] == ["recover:run-a", "approval:run-a", "delegation:run-a"]
    assert calls[-2:] == ["adapter:closed", "components:closed"]
