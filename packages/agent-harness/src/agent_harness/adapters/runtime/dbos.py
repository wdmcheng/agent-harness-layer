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
    """DBOS workflow只保存稳定 refs，不携带 repository/vendor对象。"""

    kind: DBOSOperationKind
    tenant_id: str
    run_id: str
    operation_id: str
    approval_id: str | None = None
    resolution_lease_id: str | None = None


class DBOSOperationOutcome(HarnessDTO):
    """屏蔽DBOS状态字符串的provider-neutral执行结果。"""

    status: Literal["succeeded", "deterministic_failed"]
    result: dict[str, Any] | None = None
    error_code: str | None = None


def workflow_id_for_operation(tenant_id: str, operation_id: str) -> str:
    """生成 tenant/operation 唯一且长度受控的 DBOS workflow id。"""

    digest = hashlib.sha256(f"{tenant_id}\0{operation_id}".encode()).hexdigest()
    return f"agent-harness:{digest}"


_active_handlers: Mapping[str, DBOSOperationHandler] = {}


@DBOS.step(name="agent_harness_operation_step")
async def _dispatch_operation(payload: dict[str, Any]) -> dict[str, Any]:
    operation = DBOSOperation.model_validate(payload)
    try:
        handler = _active_handlers[operation.kind]
    except KeyError as exc:
        raise RuntimeError(f"DBOS handler is not configured: {operation.kind}") from exc
    return await handler(operation)


@DBOS.workflow(name="agent_harness_operation_workflow")
async def _operation_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    return await _dispatch_operation(payload)


class DBOSRuntimeAdapter(Protocol):
    name: str

    async def start(self) -> None: ...

    async def execute(self, operation: DBOSOperation) -> DBOSOperationOutcome: ...

    async def close(self) -> None: ...


class DBOSServiceRuntimeAdapter:
    """单 worker service profile 的 DBOS workflow envelope。"""

    name = "dbos"

    def __init__(
        self,
        *,
        system_database_url: str,
        handlers: Mapping[str, DBOSOperationHandler],
        executor_id: str = "agent-harness-service-worker",
    ) -> None:
        self._database_url = system_database_url.replace("postgresql+asyncpg://", "postgresql://")
        self._handlers = dict(handlers)
        self._executor_id = executor_id
        self._dbos: DBOS | None = None
        self._lock_connection: psycopg.Connection[tuple[Any, ...]] | None = None

    async def start(self) -> None:
        """先取得 PostgreSQL singleton advisory lock，再启动 DBOS recovery。"""

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
        if self._dbos is None:
            raise RuntimeError("DBOS runtime adapter is not started")
        workflow_id = workflow_id_for_operation(operation.tenant_id, operation.operation_id)
        with SetWorkflowID(workflow_id):
            handle = DBOS.start_workflow(_operation_workflow, operation.to_payload())
        try:
            result = handle.get_result()
            resolved = await result if inspect.isawaitable(result) else result
        except Exception:
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
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_try_advisory_lock(hashtext(%s))",
                (f"agent-harness:{self._executor_id}",),
            )
            row = cursor.fetchone()
        return bool(row and row[0])

    def _unlock(self, connection: psycopg.Connection[tuple[Any, ...]]) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "select pg_advisory_unlock(hashtext(%s))",
                (f"agent-harness:{self._executor_id}",),
            )


class NoopDBOSRuntimeAdapter:
    """local/test 占位；不提供 durable service evidence。"""

    name = "dbos"

    async def start(self) -> None:
        return None

    async def execute(self, operation: DBOSOperation) -> DBOSOperationOutcome:
        return DBOSOperationOutcome(status="succeeded", result={"run_id": operation.run_id})

    async def close(self) -> None:
        return None


__all__ = [
    "DBOSOperation",
    "DBOSOperationOutcome",
    "DBOSRuntimeAdapter",
    "DBOSServiceRuntimeAdapter",
    "NoopDBOSRuntimeAdapter",
    "workflow_id_for_operation",
]
