"""service profile 的 durable queue runtime worker。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

from agent_harness.adapters.runtime import (
    DBOSOperation,
    DBOSServiceRuntimeAdapter,
    workflow_id_for_operation,
)
from agent_harness.config import SettingsLoadError, settings_error_lines
from agent_harness.runtime import (
    RunQueueMessage,
    RunStatus,
    build_execute_message,
    build_resume_approval_message,
)
from app.runtime import RuntimeComponents, build_runtime_components
from app.workers.runtime_worker_operations import execute_approval_operation
from app.workers.runtime_worker_smoke import (
    crash_after_application_owner as _crash_after_application_owner,
)
from app.workers.runtime_worker_smoke import (
    crash_before_queue_ack as _crash_before_queue_ack,
)
from app.workers.runtime_worker_smoke import (
    ready_file as _ready_file,
)
from app.workers.runtime_worker_smoke import (
    wait_for_smoke_reclaim_release as _wait_for_smoke_reclaim_release,
)
from app.workers.runtime_worker_smoke import (
    write_receipt_marker as _write_receipt_marker,
)
from app.workers.runtime_worker_smoke import (
    write_recovery_marker as _write_recovery_marker,
)

EXECUTOR_ID = os.environ.get("SERVICE_APP_EXECUTOR_ID", "agent-harness-service-worker")
RECLAIM_IDLE_SECONDS = float(os.environ.get("SERVICE_APP_RECLAIM_IDLE_SECONDS", "30"))


async def _recover_pending_enqueue(components: RuntimeComponents) -> None:
    if components.queue is None:
        return
    async with components.storage.uow() as uow:
        pending_runs = await uow.runs.list_pending_enqueue()
        pending_approvals = await uow.approvals.list_pending_resolution_enqueue()
    for state in pending_runs:
        message = build_execute_message(
            request_id=state.request_id,
            tenant_id=state.tenant_id,
            run_id=state.run_id,
            idempotency_key=state.effective_idempotency_key,
        )
        queued = await components.queue.enqueue(message)
        await components.orchestrator.reconcile_queued_run(
            message=message, message_id=queued.message_id
        )
    for state in pending_approvals:
        queued = await components.queue.enqueue(
            build_resume_approval_message(
                request_id=state.request_id,
                tenant_id=state.tenant_id,
                run_id=state.run_id,
                approval_id=state.approval_id,
                resolution_lease_id=state.lease_id,
            )
        )
        async with components.storage.uow() as uow:
            await uow.approvals.mark_resolution_queued(
                approval_id=state.approval_id,
                lease_id=state.lease_id,
                operation_id=state.operation_id,
                message_id=queued.message_id,
            )
            await uow.commit()


async def _recover_pending_usage(components: RuntimeComponents) -> None:
    """DBOS 接管新消息前补投确定性 usage，未知结果继续阻止 terminal。"""

    await components.orchestrator.recover_pending_usage_evidence()


async def _prepare_approval_owner(
    components: RuntimeComponents,
    message: RunQueueMessage,
    *,
    message_id: str,
) -> None:
    assert message.approval_id is not None
    assert message.resolution_lease_id is not None
    workflow_id = workflow_id_for_operation(message.tenant_id, message.operation_id)
    async with components.storage.uow() as uow:
        claimed = await uow.approvals.claim_resolution_execution(
            approval_id=message.approval_id,
            tenant_id=message.tenant_id,
            run_id=message.run_id,
            lease_id=message.resolution_lease_id,
            operation_id=message.operation_id,
            request_id=message.request_id,
            message_id=message_id,
            workflow_owner_id=EXECUTOR_ID,
            workflow_id=workflow_id,
        )
        if not claimed:
            state = await uow.approvals.get_resolution_queue_state(message.approval_id)
            if (
                state is None
                or state.tenant_id != message.tenant_id
                or state.run_id != message.run_id
                or state.lease_id != message.resolution_lease_id
                or state.operation_id != message.operation_id
                or state.request_id != message.request_id
                or state.message_id != message_id
                or not state.reviewer_id
                or state.decision != "approve"
                or not state.request_hash
                or state.resolution_state
                not in {"execution_owned", "recovery_pending", "completed", "failed"}
                or state.workflow_owner_id != EXECUTOR_ID
                or state.workflow_id != workflow_id
            ):
                raise RuntimeError("approval queue message lost resolution fencing")
        await uow.commit()


async def consume_one(
    components: RuntimeComponents,
    dbos: DBOSServiceRuntimeAdapter,
    *,
    consumer_id: str,
) -> str | None:
    assert components.queue is not None
    delivery = await components.queue.reclaim(
        consumer_id=consumer_id,
        min_idle_seconds=RECLAIM_IDLE_SECONDS,
    )
    if delivery is None:
        delivery = await components.queue.pickup(consumer_id=consumer_id, block_milliseconds=1000)
    if delivery is None:
        return None
    _write_receipt_marker(delivery)
    await _wait_for_smoke_reclaim_release(delivery)
    message = delivery.message
    if message.kind == "execute_run":
        await components.orchestrator.reconcile_queued_run(
            message=message,
            message_id=delivery.receipt.message_id,
        )
    if message.kind == "resume_approval":
        await _prepare_approval_owner(
            components,
            message,
            message_id=delivery.receipt.message_id,
        )
    outcome = await dbos.execute(
        DBOSOperation(
            kind=message.kind,
            tenant_id=message.tenant_id,
            run_id=message.run_id,
            operation_id=message.operation_id,
            approval_id=message.approval_id,
            resolution_lease_id=message.resolution_lease_id,
        )
    )
    if outcome.status == "deterministic_failed":
        error_code = outcome.error_code or "dbos.deterministic_failure"
        if message.kind == "resume_approval":
            assert message.approval_id is not None
            assert message.resolution_lease_id is not None
            await components.approval_service.finalize_queued_failure(
                approval_id=message.approval_id,
                tenant_id=message.tenant_id,
                run_id=message.run_id,
                operation_id=message.operation_id,
                lease_id=message.resolution_lease_id,
                error_code=error_code,
            )
            await components.delegation_service.reconcile_child_if_delegated(message.run_id)
        else:
            await components.orchestrator.fail_queued_run(
                run_id=message.run_id,
                tenant_id=message.tenant_id,
                reason=error_code,
            )
            await components.delegation_service.reconcile_child_if_delegated(message.run_id)
        _crash_before_queue_ack(message)
        await components.queue.ack(delivery.receipt)
        return message.run_id
    result = outcome.result
    if result is None:
        raise RuntimeError("DBOS operation succeeded without a result")
    status = result.get("status")
    if status not in {
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.WAITING.value,
    }:
        raise RuntimeError("DBOS operation did not persist a deterministic result")
    _crash_before_queue_ack(message)
    await components.queue.ack(delivery.receipt)
    return message.run_id


async def _run_worker(
    *,
    once: bool,
    profile: str = "local",
    profiles_dir: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
    artifact_root: Path | None = None,
    workspace_root: Path | None = None,
    idempotency_key: str | None = None,
) -> str | None:
    """启动一次runtime/DBOS生命周期并按模式消费queue。"""

    components = build_runtime_components(
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        events_path=events_path,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
    )
    if components.queue is None:
        try:
            if not once:
                raise RuntimeError("local runtime worker requires --once")
            result = await components.orchestrator.start_run(
                agent_id="examples.basic",
                input={"source": "worker"},
                idempotency_key=idempotency_key or f"worker-{uuid4()}",
            )
            return result.run_id
        finally:
            await components.close()

    async def execute_handler(operation: DBOSOperation) -> dict[str, object]:
        _write_recovery_marker(operation, "entered")
        try:
            await _crash_after_application_owner(
                components,
                operation,
                executor_id=EXECUTOR_ID,
            )
            result = await components.orchestrator.execute_run(
                run_id=operation.run_id,
                tenant_id=operation.tenant_id,
                operation_id=operation.operation_id,
                owner_id=workflow_id_for_operation(operation.tenant_id, operation.operation_id),
                workflow_id=workflow_id_for_operation(operation.tenant_id, operation.operation_id),
            )
            await components.delegation_service.reconcile_child_if_delegated(operation.run_id)
        except Exception as exc:
            _write_recovery_marker(operation, "error", type(exc).__name__)
            raise
        _write_recovery_marker(operation, "completed")
        return result.to_payload()

    async def approval_handler(operation: DBOSOperation) -> dict[str, object]:
        return await execute_approval_operation(components, operation)

    dbos = DBOSServiceRuntimeAdapter(
        system_database_url=components.storage.dsn,
        handlers={
            "execute_run": execute_handler,
            "resume_approval": approval_handler,
        },
        executor_id=EXECUTOR_ID,
    )
    try:
        await _recover_pending_enqueue(components)
        await _recover_pending_usage(components)
        await dbos.start()
        if not once:
            print("runtime-worker: ready", flush=True)
        ready_file = _ready_file()
        if ready_file is not None:
            ready_file.write_text(EXECUTOR_ID, encoding="utf-8")
        consumer_id = f"{EXECUTOR_ID}:{uuid4()}"
        while True:
            run_id = await consume_one(
                components,
                dbos,
                consumer_id=consumer_id,
            )
            if run_id is not None and once:
                return run_id
            if run_id is None and once:
                raise RuntimeError("runtime worker found no queue message")
    finally:
        ready_file = _ready_file()
        if ready_file is not None:
            ready_file.unlink(missing_ok=True)
        await dbos.close()
        await components.close()


async def run_once(
    *,
    profile: str = "local",
    profiles_dir: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
    artifact_root: Path | None = None,
    workspace_root: Path | None = None,
    idempotency_key: str | None = None,
) -> str:
    """local保持旧smoke；service消费并确认一条durable queue operation。"""

    run_id = await _run_worker(
        once=True,
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        events_path=events_path,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
        idempotency_key=idempotency_key,
    )
    assert run_id is not None
    return run_id


async def run_forever(
    *,
    profile: str = "service",
    profiles_dir: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
    artifact_root: Path | None = None,
    workspace_root: Path | None = None,
) -> None:
    """持续等待 durable operation；取消时由finally关闭DBOS与连接。"""

    await _run_worker(
        once=False,
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        events_path=events_path,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
        idempotency_key=None,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Consume one durable task and exit.")
    parser.add_argument("--profile", default="local")
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--storage-dsn")
    parser.add_argument("--events-path", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--idempotency-key")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        if args.once:
            run_id = asyncio.run(
                run_once(
                    profile=args.profile,
                    profiles_dir=args.profiles_dir,
                    storage_dsn=args.storage_dsn,
                    events_path=args.events_path,
                    artifact_root=args.artifact_root,
                    workspace_root=args.workspace_root,
                    idempotency_key=args.idempotency_key,
                )
            )
            print(f"runtime-worker: run_id={run_id}")
        else:
            asyncio.run(
                run_forever(
                    profile=args.profile,
                    profiles_dir=args.profiles_dir,
                    storage_dsn=args.storage_dsn,
                    events_path=args.events_path,
                    artifact_root=args.artifact_root,
                    workspace_root=args.workspace_root,
                )
            )
    except SettingsLoadError as exc:
        for line in settings_error_lines(exc):
            print(line, file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
