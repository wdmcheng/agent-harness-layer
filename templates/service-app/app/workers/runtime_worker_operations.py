"""Service runtime worker 的 durable operation handlers。"""

from __future__ import annotations

from agent_harness.adapters.runtime import DBOSOperation
from agent_harness.runtime import RunStatus
from app.runtime import RuntimeComponents


async def execute_approval_operation(
    components: RuntimeComponents,
    operation: DBOSOperation,
) -> dict[str, object]:
    """先恢复当前 run 的 usage，再继续已批准副作用与终态收口。"""

    assert operation.approval_id is not None
    assert operation.resolution_lease_id is not None
    await components.orchestrator.recover_pending_usage_evidence(run_id=operation.run_id)
    result = await components.approval_service.execute_queued_approval(
        approval_id=operation.approval_id,
        tenant_id=operation.tenant_id,
        run_id=operation.run_id,
        operation_id=operation.operation_id,
        lease_id=operation.resolution_lease_id,
    )
    if result.run is None:
        raise RuntimeError("approval continuation did not return run result")
    if result.run.status in {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }:
        # approval continuation 绕过普通 execute handler；终态 child 仍须在
        # DBOS result durable 前完成同一个可重入 delegation 聚合。
        await components.delegation_service.reconcile_child_if_delegated(operation.run_id)
    return result.run.to_payload()


__all__ = ["execute_approval_operation"]
