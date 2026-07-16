"""run terminal evidence 的确定性 event id 与幂等发布。"""

from __future__ import annotations

from typing import Any

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.runtime.executor import RunResult
from agent_harness.runtime.state import RunStatus
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.event_capacity_repositories import EvidenceOperationKind


async def publish_terminal_evidence(
    event_bus: EventBus,
    *,
    run_id: str,
    agent_id: str,
    status: RunStatus,
    identity: IdentityContext,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    correlation: dict[str, Any] | None = None,
) -> CanonicalEvent:
    """以 run 级稳定 key 发布 terminal event，允许 sink ack 丢失后重试。"""

    event_type = {
        RunStatus.COMPLETED: CanonicalEventType.RUN_COMPLETED,
        RunStatus.FAILED: CanonicalEventType.RUN_FAILED,
        RunStatus.CANCELLED: CanonicalEventType.RUN_CANCELLED,
    }[status]
    payload: dict[str, Any] = {"status": status.value}
    if output is not None:
        payload["output"] = output
    if error is not None and error.get("reason") is not None:
        payload["reason"] = error["reason"]
    if correlation:
        payload.update(correlation)
    return await event_bus.publish(
        tenant_id=identity.tenant_id,
        run_id=run_id,
        agent_id=agent_id,
        user_id=identity.user_id,
        event_type=event_type,
        payload=payload,
        request_id=request_id,
        trace_id=trace_id,
        terminal=True,
        visibility="public",
        event_id=f"run-terminal:{run_id}",
    )


async def persist_failed_execution(
    storage: SQLAlchemyStorage,
    event_bus: EventBus,
    *,
    run_id: str,
    agent_id: str,
    reason: str,
    identity: IdentityContext,
    request_id: str | None = None,
    trace_id: str | None = None,
    correlation: dict[str, Any] | None = None,
    publish_terminal: bool = True,
) -> RunResult:
    """先持久化确定性失败，再用稳定 event id 发布可补偿证据。"""

    safe_reason = str(redact_secrets(reason))
    error = {"reason": safe_reason}
    async with storage.uow() as uow:
        run = await uow.runs.get_for_update(run_id)
        if run is None or run.tenant_id != identity.tenant_id:
            raise LookupError(f"run not found: {run_id}")
        if publish_terminal:
            await uow.event_capacity.assert_terminal_publishable(run_id=run_id)
        elif await uow.evidence_outbox.has_pending_operation(
            run_id=run_id,
            operation_kind=EvidenceOperationKind.DELEGATION,
        ):
            raise RuntimeError("pending delegation evidence blocks terminal state")
        await uow.runs.set_status(run_id, RunStatus.FAILED.value, error=error)
        await uow.commit()
    terminal = None
    if publish_terminal:
        terminal = await publish_terminal_evidence(
            event_bus,
            run_id=run_id,
            agent_id=agent_id,
            status=RunStatus.FAILED,
            identity=identity,
            error=error,
            request_id=request_id,
            trace_id=trace_id,
            correlation=correlation,
        )
    return RunResult(
        run_id=run_id,
        status=RunStatus.FAILED,
        terminal_event=terminal.event_type.value if terminal is not None else None,
    )
