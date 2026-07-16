"""Runtime worker 隔离 smoke 的故障注入与进程标记工具。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from agent_harness.adapters.runtime import (
    DBOSOperation,
    workflow_id_for_operation,
)
from agent_harness.runtime import (
    QueueDelivery,
    RunQueueMessage,
)
from app.runtime import RuntimeComponents


def ready_file() -> Path | None:
    value = os.environ.get("SERVICE_APP_READY_FILE", "").strip()
    return Path(value) if value else None


def write_recovery_marker(operation: DBOSOperation, phase: str, error: str | None = None) -> None:
    value = os.environ.get("SERVICE_APP_SMOKE_RECOVERY_MARKER", "").strip()
    if not value:
        return
    Path(value).write_text(
        json.dumps(
            {
                "run_id": operation.run_id,
                "operation_id": operation.operation_id,
                "phase": phase,
                "error": error,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def write_receipt_marker(delivery: QueueDelivery) -> None:
    """为隔离 smoke 保存真实 reclaim receipt，不进入默认 worker evidence。"""

    value = os.environ.get("SERVICE_APP_SMOKE_RECEIPT_MARKER", "").strip()
    if not value:
        return
    Path(value).write_text(
        json.dumps(delivery.receipt.to_payload(), sort_keys=True),
        encoding="utf-8",
    )


def crash_before_queue_ack(message: RunQueueMessage) -> None:
    """仅供隔离 smoke 在应用结果/evidence durable 后、Redis ack 前硬退出。"""

    selector = os.environ.get("SERVICE_APP_SMOKE_CRASH_BEFORE_ACK", "").strip()
    if selector not in {"1", message.kind, message.run_id}:
        return
    marker_value = os.environ.get("SERVICE_APP_SMOKE_ACK_CRASH_MARKER", "").strip()
    if not marker_value:
        raise RuntimeError("ack crash failpoint requires an isolated marker path")
    Path(marker_value).write_text(
        json.dumps(
            {
                "kind": message.kind,
                "operation_id": message.operation_id,
                "run_id": message.run_id,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os._exit(24)


async def wait_for_smoke_reclaim_release(delivery: QueueDelivery) -> None:
    """让隔离 smoke 在 reclaimed receipt ack 前验证旧 owner fencing。"""

    value = os.environ.get("SERVICE_APP_SMOKE_RECLAIM_RELEASE", "").strip()
    if not value or delivery.delivery_count < 2:
        return
    release = Path(value)
    deadline = asyncio.get_running_loop().time() + 60
    while not release.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("smoke reclaim receipt release timed out")
        await asyncio.sleep(0.05)


async def crash_after_application_owner(
    components: RuntimeComponents,
    operation: DBOSOperation,
    *,
    executor_id: str,
) -> None:
    """仅供隔离 smoke 在 DBOS durable handler 内制造真实进程硬退出。"""

    selector = os.environ.get("SERVICE_APP_SMOKE_CRASH_AFTER_OWNER", "").strip()
    if selector not in {"1", operation.run_id}:
        return
    workflow_id = workflow_id_for_operation(operation.tenant_id, operation.operation_id)
    async with components.storage.uow() as uow:
        claimed = await uow.runs.claim_execution(
            run_id=operation.run_id,
            operation_id=operation.operation_id,
            owner_id=workflow_id,
            workflow_id=workflow_id,
        )
        await uow.commit()
    if not claimed:
        raise RuntimeError("smoke failpoint could not persist application owner")
    marker = Path(os.environ.get("SERVICE_APP_SMOKE_CRASH_MARKER", "/smoke/crash-owner.json"))
    marker.write_text(
        json.dumps(
            {
                "run_id": operation.run_id,
                "tenant_id": operation.tenant_id,
                "operation_id": operation.operation_id,
                "owner_id": workflow_id,
                "workflow_id": workflow_id,
                "executor_id": executor_id,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os._exit(23)


__all__ = [
    "crash_after_application_owner",
    "crash_before_queue_ack",
    "ready_file",
    "wait_for_smoke_reclaim_release",
    "write_receipt_marker",
    "write_recovery_marker",
]
