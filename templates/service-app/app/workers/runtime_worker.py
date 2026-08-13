"""Service profile 的耐久队列 worker：负责恢复、栅栏执行与确认消息。"""

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
    install_shared_budget_failpoint as _install_shared_budget_failpoint,
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
    """在开始消费前补投已落库但尚未入队的运行和审批恢复操作。

    入队与业务状态持久化不能依赖单一外部事务。此处按各记录的稳定标识重建
    队列消息，并在入队成功后才写回关联 message ID，使 worker 重启不会漏掉
    已提交的待执行工作，也不会把同一审批恢复错误地标记为新的操作。
    """
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
    """为审批恢复消息抢占带栅栏的执行所有权，拒绝过期或被篡改的投递。

    若抢占已由本 worker 或恢复流程完成，必须逐字段核对消息、租约和 workflow
    身份仍指向同一审批决定；不匹配时立即失败，不能让旧 receipt 继续执行。
    """
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


async def _shared_budget_requires_manual_review(
    components: RuntimeComponents,
    message: RunQueueMessage,
) -> bool:
    """started-unknown 已 durable 封锁后消费 queue，但不伪造 terminal 结果。"""

    if message.kind != "execute_run":
        return False
    storage = getattr(components, "storage", None)
    if storage is None:
        return False
    async with storage.uow() as uow:
        ownership = await uow.shared_budget.resolve_operation_ownership(
            tenant_id=message.tenant_id,
            run_id=message.run_id,
        )
        ledger = await uow.shared_budget.get_ledger(
            message.tenant_id,
            ownership.budget_owner_run_id,
        )
    return ledger is not None and ledger.state == "needs_review"


async def consume_one(
    components: RuntimeComponents,
    dbos: DBOSServiceRuntimeAdapter,
    *,
    consumer_id: str,
) -> str | None:
    """认领或拉取一条队列消息，完成应用执行后才确认对应 receipt。

    先尝试 reclaim 是为了接管故障 worker 遗留的 pending 消息；每个分支都在
    durable 状态已确定后调用 ack。人为介入的共享预算状态是唯一的提前确认路径，
    它明确保留非终态运行，避免同一无自动恢复价值的消息被无限重领。
    """
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
        if await _shared_budget_requires_manual_review(components, message):
            # provider 已 started 而 result 未知时，recovery 已把 ledger/claim
            # 提升为 needs_review。该 queue delivery 不再有自动执行价值；确认它
            # 可避免无限 reclaim，同时 run 保持非 terminal 等待人工处置。
            await components.queue.ack(delivery.receipt)
            return message.run_id
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
                recovery_request_id=message.request_id,
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
    env_file: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
    artifact_root: Path | None = None,
    workspace_root: Path | None = None,
    idempotency_key: str | None = None,
) -> str | None:
    """构造运行时与 DBOS 适配器，并按一次性或常驻模式消费耐久队列。

    本地 profile 没有队列时保留旧的最小执行行为；service profile 则先恢复
    enqueue 与 usage 证据，再启动 DBOS 并持续处理消息。所有退出路径都要
    删除就绪标记、关闭 DBOS 和存储连接，避免 smoke 误判遗留 worker 仍可用。
    """

    components = build_runtime_components(
        profile=profile,
        profiles_dir=profiles_dir,
        env_file=env_file,
        storage_dsn=storage_dsn,
        events_path=events_path,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
    )
    _install_shared_budget_failpoint(components)
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
        """在 DBOS workflow 内执行 run，并写入仅供 crash smoke 读取的恢复标记。

        marker 只记录稳定的进入、完成或异常类别，不能泄露模型输入或 Provider
        返回值。委派子运行的终态协调放在真实执行成功后，确保父子事件顺序由
        已持久化的 operation 结果决定。
        """
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
            error_label = type(exc).__name__
            if str(exc).endswith(" occurred before pending evidence settled"):
                # 该消息只含异常类型，由 runtime terminal fence 显式构造；其他
                # executor 异常仍只记录类型，避免把输入或 provider 内容写入 marker。
                error_label = str(exc)
            _write_recovery_marker(operation, "error", error_label)
            raise
        _write_recovery_marker(operation, "completed")
        return result.to_payload()

    async def approval_handler(operation: DBOSOperation) -> dict[str, object]:
        """将审批恢复操作委派给专用服务，保持与普通运行相同的 DBOS 边界。"""
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
    env_file: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
    artifact_root: Path | None = None,
    workspace_root: Path | None = None,
    idempotency_key: str | None = None,
) -> str:
    """运行一次 worker 迭代，并返回实际处理的运行 ID。

    本地模式用于兼容既有 smoke；service 模式只在成功确认一条耐久操作后返回，
    因而测试可以把返回值与队列、执行记录及最终事件逐一关联。
    """

    run_id = await _run_worker(
        once=True,
        profile=profile,
        profiles_dir=profiles_dir,
        env_file=env_file,
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
    env_file: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
    artifact_root: Path | None = None,
    workspace_root: Path | None = None,
) -> None:
    """常驻消费耐久操作，取消或异常时由底层 finally 回收所有运行时资源。"""

    await _run_worker(
        once=False,
        profile=profile,
        profiles_dir=profiles_dir,
        env_file=env_file,
        storage_dsn=storage_dsn,
        events_path=events_path,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
        idempotency_key=None,
    )


def parse_args() -> argparse.Namespace:
    """解析 worker 启动参数；路径参数保持 Path 类型以便组合层做边界校验。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Consume one durable task and exit.")
    parser.add_argument("--profile", default="local")
    parser.add_argument("--profiles-dir", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--storage-dsn")
    parser.add_argument("--events-path", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--idempotency-key")
    return parser.parse_args()


def main() -> None:
    """作为命令行入口运行一次或常驻 worker，并把配置错误转为稳定退出码。"""
    args = parse_args()
    try:
        if args.once:
            run_id = asyncio.run(
                run_once(
                    profile=args.profile,
                    profiles_dir=args.profiles_dir,
                    env_file=getattr(args, "env_file", None),
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
                    env_file=getattr(args, "env_file", None),
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
