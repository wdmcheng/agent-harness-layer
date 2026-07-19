"""Runtime worker 隔离 smoke 的故障注入与进程标记工具。"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, cast

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
    """返回 worker 就绪信号路径；未配置时不创建额外本地状态。"""

    value = os.environ.get("SERVICE_APP_READY_FILE", "").strip()
    return Path(value) if value else None


def write_recovery_marker(
    operation: DBOSOperation,
    phase: str,
    error: str | None = None,
) -> None:
    """为隔离 smoke 写入 worker 恢复位置和脱敏错误摘要。

    该文件只存在于显式环境变量指定的临时目录，不属于业务 evidence；它让
    外层脚本能定位 worker 退出前的恢复位置，同时避免读取数据库内部状态。
    """

    value = os.environ.get("SERVICE_APP_SMOKE_RECOVERY_MARKER", "").strip()
    if not value:
        return
    Path(value).write_text(
        json.dumps(
            {
                "run_id": operation.run_id,
                "operation_id": operation.operation_id,
                # 隔离 smoke 的既有 JSON 读写协议使用此键；其值描述 worker
                # 恢复位置，不表示开发过程阶段，不能在生产者单侧改名。
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


def install_shared_budget_failpoint(components: RuntimeComponents) -> None:
    """把隔离 smoke 故障点钉在 shared claim 的三个 durable 状态窗口。"""

    phase = os.environ.get("SERVICE_APP_SMOKE_SHARED_BUDGET_CRASH", "").strip()
    if phase not in {"not_started", "started", "result_committed"}:
        return
    model = components.executor_services.get("model_invocation")
    if model is None:
        raise RuntimeError("shared budget smoke requires model invocation service")
    marker_value = os.environ.get("SERVICE_APP_SMOKE_SHARED_BUDGET_MARKER", "").strip()
    if not marker_value:
        raise RuntimeError("shared budget failpoint requires an isolated marker path")
    marker = Path(marker_value)

    def crash(run_id: str, exit_code: int) -> None:
        """记录已命中的 durable 状态窗口后立即退出，不给清理逻辑执行机会。"""

        marker.write_text(
            json.dumps(
                # 与 service smoke 脚本共同消费的既有 marker schema 保持兼容；
                # 此处的值是副作用生命周期位置，不是开发阶段标签。
                {"phase": phase, "run_id": run_id},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os._exit(exit_code)

    original_mark = cast(Any, model)._mark_side_effect_started
    if phase == "not_started":

        async def before_started(**kwargs: object) -> None:
            """在标记外部副作用前退出，证明预约可由恢复安全继续或释放。"""

            context = cast(Any, kwargs["context"])
            crash(str(context.run_id), 25)

        cast(Any, model)._mark_side_effect_started = before_started
    elif phase == "started":

        async def after_started(**kwargs: object) -> None:
            """在副作用已 durable 标记、结果尚未提交时退出，验证需要人工复核。"""

            await original_mark(**kwargs)
            context = cast(Any, kwargs["context"])
            crash(str(context.run_id), 26)

        cast(Any, model)._mark_side_effect_started = after_started
    else:

        async def before_final_publish(**kwargs: object) -> None:
            """在结果已提交、最终 evidence 发布前退出，验证恢复只补投 outbox。"""

            evidence = cast(Any, kwargs["evidence"])
            crash(str(evidence.run_id), 27)

        cast(Any, model)._publish_final = before_final_publish


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
    "install_shared_budget_failpoint",
    "ready_file",
    "wait_for_smoke_reclaim_release",
    "write_receipt_marker",
    "write_recovery_marker",
]
