"""Provider-neutral fake runtime orchestrator for Phase 5."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.runtime.checkpoints import IdempotencyKey, ResumeToken
from agent_harness.runtime.state import TERMINAL_STATUSES, RunStatus
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.repositories import CheckpointCreate, RunCreate, SessionCreate


class InvalidRunTransition(RuntimeError):
    """Raised when a terminal or otherwise invalid run transition is requested."""


class RunResult(HarnessDTO):
    run_id: str
    status: RunStatus
    terminal_event: str | None = None
    resume_token: ResumeToken | None = None


class RunOrchestrator:
    """Coordinates run records, checkpoints and CanonicalEvent output."""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        identity: IdentityContext | None = None,
    ) -> None:
        self._storage = storage
        self._event_bus = event_bus
        self._identity = identity or IdentityContext.local_default()

    async def start_run(
        self,
        *,
        agent_id: str,
        input: dict[str, Any],
        idempotency_key: IdempotencyKey | str | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> RunResult:
        idempotency_value = _idempotency_value(idempotency_key)
        async with self._storage.uow() as uow:
            # identity 归属记录统一通过 repository 创建，API、CLI 和 worker
            # 路径都会穿过同一个 UoW 边界。
            tenant = await uow.tenants.ensure(self._identity.tenant_id)
            session = await uow.sessions.ensure(
                SessionCreate(
                    session_id=self._identity.session_id,
                    tenant_id=tenant.id,
                    user_id=self._identity.user_id,
                    agent_id=agent_id,
                )
            )
            # idempotency 在持久化存储里解析，不放进内存；service 重启后仍能
            # 安全响应重复提交。
            existing = None
            if idempotency_value is not None:
                existing = await uow.runs.get_by_idempotency_key(
                    tenant_id=tenant.id,
                    session_id=session.id,
                    agent_id=agent_id,
                    idempotency_key=idempotency_value,
                )
            if existing is not None:
                return RunResult(run_id=existing.id, status=RunStatus(existing.status))

            run = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant.id,
                    session_id=session.id,
                    agent_id=agent_id,
                    idempotency_key=idempotency_value,
                    input=input,
                )
            )
            await uow.runs.set_status(run.id, RunStatus.RUNNING.value)
            await uow.commit()

        await self._event_bus.publish(
            tenant_id=self._identity.tenant_id,
            run_id=run.id,
            agent_id=agent_id,
            user_id=self._identity.user_id,
            event_type=CanonicalEventType.RUN_STARTED,
            payload={"agent_id": agent_id},
        )
        # 当前 Phase 的可运行路径仍基于 fake provider。传入 checkpoint_state
        # 只证明 pause/resume 持久化，不把 DBOS 或 HITL approval 行为提前拉进 Phase 5。
        if checkpoint_state is not None:
            resume_token = await self._checkpoint(run.id, agent_id, checkpoint_state)
            return RunResult(run_id=run.id, status=RunStatus.WAITING, resume_token=resume_token)
        terminal = await self._complete(run.id, agent_id, output={"result": "fake-ok"})
        return RunResult(
            run_id=run.id,
            status=RunStatus.COMPLETED,
            terminal_event=terminal.event_type.value,
        )

    async def get_run(self, run_id: str) -> RunResult:
        """读取 run lifecycle 摘要，供 API/CLI 不碰 ORM 的 detail route 使用。"""

        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            status = RunStatus(run.status)
        terminal_event = None
        if status in TERMINAL_STATUSES:
            terminal_event = f"run.{status.value}"
        return RunResult(run_id=run_id, status=status, terminal_event=terminal_event)

    async def cancel_run(self, run_id: str) -> RunResult:
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            status = RunStatus(run.status)
            if status in TERMINAL_STATUSES:
                raise InvalidRunTransition(f"run is terminal: {run_id}")
            await uow.runs.set_status(run_id, RunStatus.CANCELLED.value)
            await uow.commit()
        terminal = await self._event_bus.publish(
            tenant_id=self._identity.tenant_id,
            run_id=run_id,
            agent_id=run.agent_id,
            user_id=self._identity.user_id,
            event_type=CanonicalEventType.RUN_CANCELLED,
            payload={"status": RunStatus.CANCELLED.value},
            terminal=True,
        )
        return RunResult(
            run_id=run_id,
            status=RunStatus.CANCELLED,
            terminal_event=terminal.event_type.value,
        )

    async def resume_run(
        self,
        resume_token: ResumeToken | str,
        *,
        expected_run_id: str | None = None,
    ) -> RunResult:
        token_value = _resume_token_value(resume_token)
        async with self._storage.uow() as uow:
            checkpoint = await uow.checkpoints.get_by_resume_token(token_value)
            if checkpoint is None:
                raise LookupError(f"checkpoint not found: {token_value}")
            # API route 的 path 里有 run_id。必须先校验 token 归属再完成 run，
            # 否则错误 URL 会推进 token 所属的另一个 run。
            if expected_run_id is not None and checkpoint.run_id != expected_run_id:
                raise LookupError("resume token does not belong to run")
            run = await uow.runs.get(checkpoint.run_id)
            if run is None:
                raise LookupError(f"run not found: {checkpoint.run_id}")
            if RunStatus(run.status) in TERMINAL_STATUSES:
                raise InvalidRunTransition(f"run is terminal: {run.id}")
        terminal = await self._complete(run.id, run.agent_id, output={"resumed": True})
        return RunResult(
            run_id=run.id,
            status=RunStatus.COMPLETED,
            terminal_event=terminal.event_type.value,
        )

    async def _checkpoint(self, run_id: str, agent_id: str, state: dict[str, Any]) -> ResumeToken:
        resume_token = ResumeToken(value=f"resume-{uuid4()}")
        async with self._storage.uow() as uow:
            await uow.checkpoints.create(
                CheckpointCreate(
                    tenant_id=self._identity.tenant_id,
                    run_id=run_id,
                    sequence=1,
                    resume_token=resume_token.value,
                    state=state,
                )
            )
            await uow.runs.set_status(run_id, RunStatus.WAITING.value)
            await uow.commit()
        await self._event_bus.publish(
            tenant_id=self._identity.tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            user_id=self._identity.user_id,
            event_type=CanonicalEventType.CHECKPOINT_CREATED,
            payload={"resume_token": resume_token.value, "state": state},
        )
        return resume_token

    async def _complete(self, run_id: str, agent_id: str, output: dict[str, Any]) -> CanonicalEvent:
        async with self._storage.uow() as uow:
            await uow.runs.set_status(run_id, RunStatus.COMPLETED.value, output=output)
            await uow.commit()
        return await self._event_bus.publish(
            tenant_id=self._identity.tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            user_id=self._identity.user_id,
            event_type=CanonicalEventType.RUN_COMPLETED,
            payload={"status": RunStatus.COMPLETED.value, "output": output},
            terminal=True,
        )


def _idempotency_value(key: IdempotencyKey | str | None) -> str | None:
    if key is None:
        return None
    return key.value if isinstance(key, IdempotencyKey) else key


def _resume_token_value(token: ResumeToken | str) -> str:
    return token.value if isinstance(token, ResumeToken) else token
