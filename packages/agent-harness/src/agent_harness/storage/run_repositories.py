"""Run lifecycle、queue execution 与 canonical trace repository。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import (
    AgentRunModel,
    RunEventCapacityModel,
    RunTraceBindingModel,
)


class RunCreate(HarnessDTO):
    """创建 run 时写入 repository 的稳定输入。"""

    tenant_id: str
    session_id: str
    agent_id: str
    idempotency_key: str | None = None
    parent_run_id: str | None = None
    trace_id: str
    input: dict[str, Any] = Field(default_factory=dict)


class RunRecord(RunCreate):
    """repository 对 runtime 返回的 run 摘要。"""

    id: str
    status: str
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class RunExecutionRecord(HarnessDTO):
    """service runtime 使用的私有 queue/execution 状态。"""

    run_id: str
    tenant_id: str
    status: str
    execution_context: dict[str, Any] = Field(default_factory=dict)
    operation_id: str
    request_id: str
    effective_idempotency_key: str
    enqueue_state: str
    message_id: str | None = None
    owner_id: str | None = None
    workflow_id: str | None = None


class RunTraceRepositoryConflict(RuntimeError):
    """Repository 在任何持久化副作用前拒绝 trace claim 冲突。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _run_record(model: AgentRunModel) -> RunRecord:
    return RunRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        session_id=model.session_id,
        agent_id=model.agent_id,
        idempotency_key=model.idempotency_key,
        parent_run_id=model.parent_run_id,
        trace_id=model.trace_id,
        input=model.input_json,
        status=model.status,
        output=model.output_json,
        error=model.error_json,
    )


def _run_execution_record(model: AgentRunModel) -> RunExecutionRecord:
    required = {
        "execution_context": model.execution_context_json,
        "operation_id": model.queue_operation_id,
        "request_id": model.queue_request_id,
        "effective_idempotency_key": model.queue_effective_idempotency_key,
        "enqueue_state": model.queue_enqueue_state,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise RuntimeError(f"run execution state incomplete: {', '.join(missing)}")
    return RunExecutionRecord(
        run_id=model.id,
        tenant_id=model.tenant_id,
        status=model.status,
        execution_context=cast(dict[str, Any], model.execution_context_json),
        operation_id=cast(str, model.queue_operation_id),
        request_id=cast(str, model.queue_request_id),
        effective_idempotency_key=cast(str, model.queue_effective_idempotency_key),
        enqueue_state=cast(str, model.queue_enqueue_state),
        message_id=model.queue_message_id,
        owner_id=model.execution_owner_id,
        workflow_id=model.execution_workflow_id,
    )


class RunRepository:
    """run lifecycle repository，集中处理幂等、queue state 与 canonical trace。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        data: RunCreate,
        *,
        execution_context: dict[str, Any] | None = None,
        caller_trace_id: str | None = None,
    ) -> RunRecord:
        """创建 run 记录，并在幂等键命中时返回既有记录。"""

        if data.idempotency_key is not None:
            # 创建前先处理 idempotency，重复 API/CLI 提交会收敛到同一条持久化
            # AgentRun 记录。
            existing = await self.get_by_idempotency_key(
                tenant_id=data.tenant_id,
                session_id=data.session_id,
                agent_id=data.agent_id,
                idempotency_key=data.idempotency_key,
            )
            if existing is not None:
                if caller_trace_id is not None and existing.trace_id != caller_trace_id:
                    raise RunTraceRepositoryConflict("trace.idempotency_conflict")
                return existing

        await self._validate_trace_claim(data)

        model = AgentRunModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            session_id=data.session_id,
            agent_id=data.agent_id,
            idempotency_key=data.idempotency_key,
            parent_run_id=data.parent_run_id,
            trace_id=data.trace_id,
            input_json=data.input,
            execution_context_json=execution_context,
            status="created",
        )
        self._session.add(model)
        self._session.add(
            RunEventCapacityModel(
                run_id=model.id,
                tenant_id=data.tenant_id,
                highest_persisted_seq=0,
                outstanding_reserved_event_count=0,
                terminal_reservation=1,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if data.idempotency_key is not None:
                raise RunTraceRepositoryConflict("trace.idempotency_race") from exc
            raise
        if data.parent_run_id is None:
            await self._insert_root_trace_binding(data=data, root_run_id=model.id)
        return _run_record(model)

    async def create_queued(
        self,
        data: RunCreate,
        *,
        execution_context: dict[str, Any],
        operation_id: str,
        request_id: str,
        effective_idempotency_key: str | None,
        caller_trace_id: str | None = None,
    ) -> RunRecord:
        """同一 repository 写入 run 与 enqueue_pending 私有状态。"""

        if data.idempotency_key is not None:
            existing = await self.get_by_idempotency_key(
                tenant_id=data.tenant_id,
                session_id=data.session_id,
                agent_id=data.agent_id,
                idempotency_key=data.idempotency_key,
            )
            if existing is not None:
                if caller_trace_id is not None and existing.trace_id != caller_trace_id:
                    raise RunTraceRepositoryConflict("trace.idempotency_conflict")
                return existing
        await self._validate_trace_claim(data)
        run_id = str(uuid4())
        canonical_operation = f"run:{run_id}:execute"
        if operation_id and not operation_id.endswith(":execute"):
            raise ValueError("execute operation_id must end with :execute")
        model = AgentRunModel(
            id=run_id,
            tenant_id=data.tenant_id,
            session_id=data.session_id,
            agent_id=data.agent_id,
            idempotency_key=data.idempotency_key,
            parent_run_id=data.parent_run_id,
            trace_id=data.trace_id,
            input_json=data.input,
            status="created",
            execution_context_json=execution_context,
            queue_operation_id=canonical_operation,
            queue_request_id=request_id,
            queue_effective_idempotency_key=(effective_idempotency_key or canonical_operation),
            queue_enqueue_state="enqueue_pending",
        )
        self._session.add(model)
        self._session.add(
            RunEventCapacityModel(
                run_id=model.id,
                tenant_id=data.tenant_id,
                highest_persisted_seq=0,
                outstanding_reserved_event_count=0,
                terminal_reservation=1,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if data.idempotency_key is not None:
                raise RunTraceRepositoryConflict("trace.idempotency_race") from exc
            raise
        if data.parent_run_id is None:
            await self._insert_root_trace_binding(data=data, root_run_id=model.id)
        return _run_record(model)

    async def _insert_root_trace_binding(self, *, data: RunCreate, root_run_id: str) -> None:
        """把并发唯一键竞争映射成稳定 conflict，并由外层 UoW 回滚整笔 run。"""

        self._session.add(
            RunTraceBindingModel(
                trace_id=data.trace_id,
                tenant_id=data.tenant_id,
                root_run_id=root_run_id,
            )
        )
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise RunTraceRepositoryConflict("trace.conflict") from exc

    async def get_execution(self, run_id: str) -> RunExecutionRecord | None:
        model = await self._session.get(AgentRunModel, run_id)
        if model is None or model.queue_operation_id is None:
            return None
        return _run_execution_record(model)

    async def list_pending_enqueue(self) -> list[RunExecutionRecord]:
        result = await self._session.scalars(
            select(AgentRunModel).where(
                AgentRunModel.status == "created",
                AgentRunModel.queue_enqueue_state == "enqueue_pending",
            )
        )
        return [_run_execution_record(model) for model in result.all()]

    async def mark_queued(
        self, *, run_id: str, operation_id: str, message_id: str
    ) -> RunExecutionRecord:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AgentRunModel)
                .where(
                    AgentRunModel.id == run_id,
                    AgentRunModel.queue_operation_id == operation_id,
                    AgentRunModel.queue_enqueue_state.in_(["enqueue_pending", "queued"]),
                )
                .values(queue_enqueue_state="queued", queue_message_id=message_id)
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"run queue state conflict: {run_id}")
        model = await self._session.get(AgentRunModel, run_id)
        assert model is not None
        return _run_execution_record(model)

    async def claim_execution(
        self,
        *,
        run_id: str,
        operation_id: str,
        owner_id: str,
        workflow_id: str,
    ) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AgentRunModel)
                .where(
                    AgentRunModel.id == run_id,
                    AgentRunModel.status == "created",
                    AgentRunModel.queue_enqueue_state == "queued",
                    AgentRunModel.queue_operation_id == operation_id,
                    AgentRunModel.execution_owner_id.is_(None),
                )
                .values(
                    status="running",
                    execution_owner_id=owner_id,
                    execution_workflow_id=workflow_id,
                    started_at=datetime.now(tz=UTC),
                )
            ),
        )
        if result.rowcount == 1:
            return True
        model = await self._session.get(AgentRunModel, run_id)
        return bool(
            model is not None
            and model.queue_operation_id == operation_id
            and model.execution_owner_id == owner_id
            and model.execution_workflow_id == workflow_id
        )

    async def get(self, run_id: str) -> RunRecord | None:
        model = await self._session.get(AgentRunModel, run_id)
        return None if model is None else _run_record(model)

    async def get_for_update(self, run_id: str) -> RunRecord | None:
        """锁定 run 终态竞争范围，和 delegation claim 使用相同的加锁顺序。"""

        model = await self._session.scalar(
            select(AgentRunModel).where(AgentRunModel.id == run_id).with_for_update()
        )
        return None if model is None else _run_record(model)

    async def get_trace(self, run_id: str) -> str | None:
        """读取持久化 canonical trace，不从 event 或 caller 参数回推。"""

        model = await self._session.get(AgentRunModel, run_id)
        return None if model is None else model.trace_id

    async def get_trace_binding_root(self, *, tenant_id: str, trace_id: str) -> str | None:
        """按已认证 tenant 读取 claim，不向调用方暴露其他 tenant 的归属。"""

        model = await self._session.scalar(
            select(RunTraceBindingModel).where(
                RunTraceBindingModel.trace_id == trace_id,
                RunTraceBindingModel.tenant_id == tenant_id,
            )
        )
        return None if model is None else model.root_run_id

    async def trace_binding_exists(self, *, trace_id: str) -> bool:
        """只返回全局 claim 是否存在，供副作用前门禁使用且不泄露归属。"""

        binding_id = await self._session.scalar(
            select(RunTraceBindingModel.trace_id).where(RunTraceBindingModel.trace_id == trace_id)
        )
        return binding_id is not None

    async def list_for_tenant(self, tenant_id: str) -> list[RunRecord]:
        """按创建顺序返回 tenant runs，供合同验证副作用计数。"""

        result = await self._session.scalars(
            select(AgentRunModel)
            .where(AgentRunModel.tenant_id == tenant_id)
            .order_by(AgentRunModel.created_at, AgentRunModel.id)
        )
        return [_run_record(model) for model in result.all()]

    async def _validate_trace_claim(self, data: RunCreate) -> None:
        """在 run flush 前验证全局 root claim 与 parent tenant/trace 一致。"""

        if data.parent_run_id is not None:
            parent = await self._session.get(AgentRunModel, data.parent_run_id)
            if parent is None or parent.tenant_id != data.tenant_id:
                raise RunTraceRepositoryConflict("trace.parent_tenant_conflict")
            if parent.trace_id != data.trace_id:
                raise RunTraceRepositoryConflict("trace.parent_trace_conflict")
            return
        if await self.trace_binding_exists(trace_id=data.trace_id):
            raise RunTraceRepositoryConflict("trace.conflict")

    async def get_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        session_id: str,
        agent_id: str,
        idempotency_key: str,
    ) -> RunRecord | None:
        """按 tenant/session/agent/idempotency_key 查找重复提交。"""

        result = await self._session.scalars(
            select(AgentRunModel).where(
                AgentRunModel.tenant_id == tenant_id,
                AgentRunModel.session_id == session_id,
                AgentRunModel.agent_id == agent_id,
                AgentRunModel.idempotency_key == idempotency_key,
            )
        )
        model = result.first()
        return None if model is None else _run_record(model)

    async def set_status(
        self,
        run_id: str,
        status: str,
        *,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> RunRecord:
        model = await self._session.get(AgentRunModel, run_id)
        if model is None:
            raise LookupError(f"run not found: {run_id}")
        # 状态更新是唯一写 terminal output/error 的 repository 路径。集中在这里，
        # 防止 runtime adapter 直接改 JSON column。
        model.status = status
        model.output_json = output
        model.error_json = error
        await self._session.flush()
        return _run_record(model)
