"""Run 终态、checkpoint 与公开 evidence 的持久化边界。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from agent_harness.events import CanonicalEvent, CanonicalEventType
from agent_harness.identity import IdentityContext
from agent_harness.runtime._orchestrator_base import OrchestratorState
from agent_harness.runtime._orchestrator_support import run_correlation
from agent_harness.runtime.checkpoints import ResumeToken
from agent_harness.runtime.continuation import InvalidRunTransition
from agent_harness.runtime.evidence import persist_failed_execution, publish_terminal_evidence
from agent_harness.runtime.executor import RunResult
from agent_harness.runtime.state import TERMINAL_STATUSES, RunStatus
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage.repositories import CheckpointCreate


class RunLifecyclePersistence(OrchestratorState):
    """集中维护 run 状态变更与对应 durable evidence 的一致性。"""

    async def fail_run(
        self,
        run_id: str,
        *,
        reason: str,
        identity: IdentityContext | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
        input: dict[str, Any] | None = None,
        defer_terminal: bool = False,
    ) -> RunResult:
        """把非 terminal run 转为 failed，并公开脱敏失败 evidence。"""

        active_identity = identity or self._identity
        safe_reason = str(redact_secrets(reason))
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != active_identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            if RunStatus(run.status) in TERMINAL_STATUSES:
                raise InvalidRunTransition(f"run is terminal: {run.id}")
            await uow.runs.set_status(
                run_id,
                RunStatus.FAILED.value,
                error={"reason": safe_reason},
            )
            await uow.commit()
        terminal = None
        if not defer_terminal:
            terminal = await publish_terminal_evidence(
                self._event_bus,
                run_id=run_id,
                agent_id=run.agent_id,
                status=RunStatus.FAILED,
                identity=active_identity,
                error={"reason": safe_reason},
                request_id=request_id,
                trace_id=run.trace_id,
                correlation=run_correlation(input or run.input),
            )
        return RunResult(
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_event=terminal.event_type.value if terminal is not None else None,
        )

    async def _fail_execution(
        self,
        run_id: str,
        agent_id: str,
        reason: str,
        *,
        identity: IdentityContext,
        request_id: str | None = None,
        trace_id: str | None = None,
        input: dict[str, Any] | None = None,
        defer_terminal: bool = False,
    ) -> RunResult:
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
        if run is None or run.tenant_id != identity.tenant_id:
            raise LookupError(f"run not found: {run_id}")
        return await persist_failed_execution(
            self._storage,
            self._event_bus,
            run_id=run_id,
            agent_id=agent_id,
            reason=reason,
            identity=identity,
            request_id=request_id,
            trace_id=run.trace_id,
            correlation=run_correlation(input or {}),
            publish_terminal=not defer_terminal,
        )

    async def _checkpoint(
        self,
        run_id: str,
        agent_id: str,
        state: dict[str, Any],
        *,
        identity: IdentityContext,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> ResumeToken:
        resume_token = ResumeToken(value=f"resume-{uuid4()}")
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            state = {**state, "trace_id": run.trace_id}
            await uow.checkpoints.create(
                CheckpointCreate(
                    tenant_id=identity.tenant_id,
                    run_id=run_id,
                    sequence=1,
                    resume_token=resume_token.value,
                    state=state,
                )
            )
            await uow.runs.set_status(run_id, RunStatus.WAITING.value)
            await uow.commit()
        await self._event_bus.publish(
            tenant_id=identity.tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            user_id=identity.user_id,
            event_type=CanonicalEventType.CHECKPOINT_CREATED,
            payload={"state": state, **run_correlation(run.input)},
            request_id=request_id,
            trace_id=run.trace_id,
        )
        return resume_token

    async def record_guardrail_check(
        self,
        *,
        run_id: str,
        agent_id: str,
        identity: IdentityContext,
        payload: dict[str, Any],
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> CanonicalEvent:
        """把 run 创建前的输入 guardrail 决策挂回 run event stream。"""

        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
        if run is None or run.tenant_id != identity.tenant_id:
            raise LookupError(f"run not found: {run_id}")
        return await self._event_bus.publish(
            tenant_id=identity.tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            user_id=identity.user_id,
            event_type=CanonicalEventType.INPUT_GUARDRAIL_CHECKED,
            payload={**payload, **run_correlation(run.input)},
            request_id=request_id,
            trace_id=run.trace_id,
        )

    async def _complete(
        self,
        run_id: str,
        agent_id: str,
        output: dict[str, Any],
        *,
        identity: IdentityContext,
        request_id: str | None = None,
        trace_id: str | None = None,
        input: dict[str, Any] | None = None,
        defer_terminal: bool = False,
    ) -> CanonicalEvent | None:
        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != identity.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            await uow.runs.set_status(run_id, RunStatus.COMPLETED.value, output=output)
            await uow.commit()
        if defer_terminal:
            return None
        return await publish_terminal_evidence(
            self._event_bus,
            run_id=run_id,
            agent_id=agent_id,
            status=RunStatus.COMPLETED,
            identity=identity,
            output=output,
            request_id=request_id,
            trace_id=run.trace_id,
            correlation=run_correlation(input or {}),
        )


__all__ = ["RunLifecyclePersistence"]
