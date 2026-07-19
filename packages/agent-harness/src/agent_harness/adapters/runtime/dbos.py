"""DBOS 2.26.0 durable workflow adapter 边界。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

import psycopg
from dbos import DBOS, SetWorkflowID

from agent_harness.contracts.dto import HarnessDTO

DBOSOperationKind = Literal["execute_run", "resume_approval"]
DBOSOperationHandler = Callable[["DBOSOperation"], Awaitable[dict[str, Any]]]


class DBOSOperation(HarnessDTO):
    """DBOS workflow 只保存稳定引用，不携带 repository 或 vendor 对象。

    工作流重放时必须从耐久边界重新解析真实状态；把连接、SDK 对象或请求正文放入
    payload 会破坏序列化、身份隔离与后续版本兼容性。
    """

    kind: DBOSOperationKind
    tenant_id: str
    run_id: str
    operation_id: str
    approval_id: str | None = None
    resolution_lease_id: str | None = None


class DBOSOperationOutcome(HarnessDTO):
    """屏蔽 DBOS 状态字符串的 provider-neutral 执行结果。"""

    status: Literal["succeeded", "deterministic_failed"]
    result: dict[str, Any] | None = None
    error_code: str | None = None


def workflow_id_for_operation(tenant_id: str, operation_id: str) -> str:
    """生成 tenant/operation 唯一且长度受控的 DBOS workflow id。

    使用空字节分隔后取摘要，避免原始租户或操作键出现在 DBOS 元数据中，也避免简单
    字符串拼接产生边界歧义；相同稳定操作重放时必定得到同一个 workflow id。
    """

    digest = hashlib.sha256(f"{tenant_id}\0{operation_id}".encode()).hexdigest()
    return f"agent-harness:{digest}"


_active_handlers: Mapping[str, DBOSOperationHandler] = {}


@DBOS.step(name="agent_harness_operation_step")
async def _dispatch_operation(payload: dict[str, Any]) -> dict[str, Any]:
    """将耐久 payload 还原为 DTO，并只分发给当前 worker 注册的操作处理器。"""

    operation = DBOSOperation.model_validate(payload)
    try:
        handler = _active_handlers[operation.kind]
    except KeyError as exc:
        raise RuntimeError(f"DBOS handler is not configured: {operation.kind}") from exc
    return await handler(operation)


@DBOS.workflow(name="agent_harness_operation_workflow")
async def _operation_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    """DBOS 工作流入口；业务副作用均收敛在可重放 step 内。"""

    return await _dispatch_operation(payload)


class DBOSRuntimeAdapter(Protocol):
    """运行时与持久工作流实现之间的最小适配协议。"""

    name: str

    async def start(self) -> None:
        """初始化 adapter 并恢复其负责的耐久工作；重复调用应保持安全。"""

        ...

    async def execute(self, operation: DBOSOperation) -> DBOSOperationOutcome:
        """按稳定操作身份执行或重放工作流，并映射为 provider-neutral 结果。"""

        ...

    async def close(self) -> None:
        """停止工作流运行时并释放该 adapter 占用的进程级资源。"""

        ...


class DBOSServiceRuntimeAdapter:
    """单 worker service profile 的 DBOS workflow envelope。

    当前服务形态以 PostgreSQL advisory lock 限制同一 executor id 只有一个活跃 worker；
    多 worker 部署需要更高层的协调器，不能绕过此锁并行启动相同 DBOS executor。
    """

    name = "dbos"

    def __init__(
        self,
        *,
        system_database_url: str,
        handlers: Mapping[str, DBOSOperationHandler],
        executor_id: str = "agent-harness-service-worker",
    ) -> None:
        """保存处理器和 executor 身份，并把 asyncpg DSN 转成 DBOS 所需的同步 DSN。"""

        self._database_url = system_database_url.replace("postgresql+asyncpg://", "postgresql://")
        self._handlers = dict(handlers)
        self._executor_id = executor_id
        self._dbos: DBOS | None = None
        self._lock_connection: psycopg.Connection[tuple[Any, ...]] | None = None

    async def start(self) -> None:
        """先取得 PostgreSQL 单实例 advisory lock，再启动 DBOS 恢复流程。

        锁连接的生命周期必须覆盖 DBOS 运行期；仅当锁取得成功后才发布 handler 映射，
        防止失败的第二个 worker 覆盖正在工作的进程内分发器。
        """

        global _active_handlers
        if self._dbos is not None:
            return
        # Advisory lock连接必须 autocommit；idle transaction会阻塞 DBOS 的
        # CREATE INDEX CONCURRENTLY 启动迁移，形成 worker readiness 死锁。
        connection = await asyncio.to_thread(psycopg.connect, self._database_url, autocommit=True)
        locked = await asyncio.to_thread(self._try_lock, connection)
        if not locked:
            await asyncio.to_thread(connection.close)
            raise RuntimeError(
                "DBOS executor_id is already active; parallel worker requires Conductor"
            )
        self._lock_connection = connection
        # 处理器只在锁持有者启动后生效，工作流重放不会调用未准备好的业务入口。
        _active_handlers = self._handlers
        self._dbos = DBOS(
            config={
                "name": "agent-harness-service-worker",
                "system_database_url": self._database_url,
                "executor_id": self._executor_id,
                "run_admin_server": False,
            }
        )
        await asyncio.to_thread(self._dbos.launch)

    async def execute(self, operation: DBOSOperation) -> DBOSOperationOutcome:
        """以稳定 workflow id 启动或重放操作，并仅映射已耐久的确定性失败状态。"""

        if self._dbos is None:
            raise RuntimeError("DBOS runtime adapter is not started")
        workflow_id = workflow_id_for_operation(operation.tenant_id, operation.operation_id)
        with SetWorkflowID(workflow_id):
            handle = DBOS.start_workflow(_operation_workflow, operation.to_payload())
        try:
            result = handle.get_result()
            resolved = await result if inspect.isawaitable(result) else result
        except Exception:
            # 只有 DBOS 已落库的失败状态可安全转为业务结果；未知异常仍向上抛出重试。
            workflow_status = await DBOS.get_workflow_status_async(workflow_id)
            if workflow_status is not None and workflow_status.status in {
                "ERROR",
                "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
            }:
                return DBOSOperationOutcome(
                    status="deterministic_failed",
                    error_code=f"dbos.{workflow_status.status.lower()}",
                )
            raise
        return DBOSOperationOutcome(status="succeeded", result=resolved)

    async def close(self) -> None:
        """按 DBOS 销毁、清空处理器、释放单实例锁的顺序关闭运行时。

        锁不能早于 ``destroy`` 释放，否则新 worker 可能在旧工作流仍清理资源时启动，
        导致相同 executor id 的恢复与处理器映射交叠。
        """

        global _active_handlers
        if self._dbos is not None:
            await asyncio.to_thread(self._dbos.destroy)
            self._dbos = None
        _active_handlers = {}
        if self._lock_connection is not None:
            connection = self._lock_connection
            # DBOS destroy可能已关闭当前loop的默认线程池；singleton lock必须
            # 保持到destroy之后，因此这里直接完成极短的unlock/close。
            self._unlock(connection)
            connection.close()
            self._lock_connection = None

    def _try_lock(self, connection: psycopg.Connection[tuple[Any, ...]]) -> bool:
        """尝试取得与 executor id 绑定的 PostgreSQL advisory lock，不等待竞争者释放。"""

        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_try_advisory_lock(hashtext(%s))",
                (f"agent-harness:{self._executor_id}",),
            )
            row = cursor.fetchone()
        return bool(row and row[0])

    def _unlock(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        """释放当前 adapter 自己持有的 advisory lock；调用方负责随后关闭连接。"""

        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_unlock(hashtext(%s))",
                (f"agent-harness:{self._executor_id}",),
            )


class NoopDBOSRuntimeAdapter:
    """local/test 占位；不提供 durable service evidence。

    它只满足统一运行时协议并返回最小结果；任何依赖真实工作流重放、崩溃恢复或单实例
    锁的结论，都必须使用 service DBOS adapter 的集成测试证明。
    """

    name = "dbos"

    async def start(self) -> None:
        """本地替身不连接外部系统，保留空启动以满足统一生命周期。"""

        return None

    async def execute(self, operation: DBOSOperation) -> DBOSOperationOutcome:
        """返回最小成功结果，仅供不验证 DBOS 耐久语义的本地调用路径使用。"""

        return DBOSOperationOutcome(status="succeeded", result={"run_id": operation.run_id})

    async def close(self) -> None:
        """本地替身没有待释放资源，保留空关闭以满足统一生命周期。"""

        return None


__all__ = [
    "DBOSOperation",
    "DBOSOperationOutcome",
    "DBOSRuntimeAdapter",
    "DBOSServiceRuntimeAdapter",
    "NoopDBOSRuntimeAdapter",
    "workflow_id_for_operation",
]
