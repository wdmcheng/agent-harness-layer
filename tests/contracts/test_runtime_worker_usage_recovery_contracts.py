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
        async def recover_pending_usage_evidence(self, *, run_id: str | None = None) -> None:
            calls.append(f"recover:{run_id}")

    class ApprovalService:
        async def execute_queued_approval(self, **kwargs: object) -> SimpleNamespace:
            calls.append(f"approval:{kwargs['run_id']}")
            run = SimpleNamespace(
                status=RunStatus.COMPLETED,
                to_payload=lambda: {"status": "completed"},
            )
            return SimpleNamespace(run=run)

    class DelegationService:
        async def reconcile_child_if_delegated(self, run_id: str) -> bool:
            calls.append(f"delegation:{run_id}")
            return False

    class Components:
        queue = object()
        storage = SimpleNamespace(dsn="postgresql+asyncpg://unused")
        orchestrator = Orchestrator()
        approval_service = ApprovalService()
        delegation_service = DelegationService()

        async def close(self) -> None:
            calls.append("components:closed")

    class Adapter:
        def __init__(self, *, handlers: dict[str, Any], **_kwargs: object) -> None:
            self._handlers = handlers

        async def start(self) -> None:
            await self._handlers["resume_approval"](operation)
            raise WorkerStopped

        async def close(self) -> None:
            calls.append("adapter:closed")

    async def no_recovery(_components: object) -> None:
        return None

    def build_components(**_kwargs: object) -> Components:
        return Components()

    monkeypatch.setattr(runtime_worker, "build_runtime_components", build_components)
    monkeypatch.setattr(runtime_worker, "DBOSServiceRuntimeAdapter", Adapter)
    monkeypatch.setattr(runtime_worker, "_recover_pending_enqueue", no_recovery)
    monkeypatch.setattr(runtime_worker, "_recover_pending_usage", no_recovery)

    with pytest.raises(WorkerStopped):
        await runtime_worker.run_forever()

    assert calls[:3] == ["recover:run-a", "approval:run-a", "delegation:run-a"]
    assert calls[-2:] == ["adapter:closed", "components:closed"]
