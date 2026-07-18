"""Delegation ordered evidence publication 与 parent terminal resume seam。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from agent_harness.delegation._service_evidence import (
    published_child_payload as _published_child_payload,
)
from agent_harness.delegation._service_evidence import (
    required_child_id as _required_child_id,
)
from agent_harness.delegation._service_types import (
    TERMINAL_RUN_STATUSES as _TERMINAL,
)
from agent_harness.delegation._service_types import (
    DelegationError as DelegationError,
)
from agent_harness.delegation._service_types import (
    DelegationExecutionResult as DelegationExecutionResult,
)
from agent_harness.delegation._service_types import (
    DelegationMode as DelegationMode,
)
from agent_harness.delegation._service_types import (
    DelegationOrchestrator as DelegationOrchestrator,
)
from agent_harness.delegation._service_types import (
    DelegationPolicy as DelegationPolicy,
)
from agent_harness.delegation.models import (
    DelegationSummary,
)
from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.events.sinks.base import EventSinkReplayConflict
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunOrchestrator, RunStatus
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.delegation_repositories import (
    DelegationRecord,
)
from agent_harness.storage.event_capacity_repositories import (
    EvidenceOperationKind,
)


class _DelegationPublicationMixin:
    """发布 claim/child/final evidence，并在持久化后推进 parent terminal。"""

    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _orchestrator: RunOrchestrator | DelegationOrchestrator

    async def _publish_claimed(
        self,
        *,
        delegation: DelegationRecord,
        identity: IdentityContext,
    ) -> None:
        await self._publish(
            delegation=delegation,
            identity=identity,
            phase="claimed",
            event_type=CanonicalEventType.DELEGATION_CLAIMED,
            payload={"status": "claimed"},
        )

    async def _publish_child_created(
        self,
        *,
        delegation: DelegationRecord,
        identity: IdentityContext,
    ) -> None:
        await self._publish(
            delegation=delegation,
            identity=identity,
            phase="child",
            event_type=CanonicalEventType.DELEGATION_CHILD_CREATED,
            payload={"status": delegation.status, "child_run_id": _required_child_id(delegation)},
        )

    async def _publish_final(
        self,
        *,
        delegation: DelegationRecord,
        summary: DelegationSummary,
    ) -> None:
        identity = IdentityContext.model_validate(delegation.identity)
        event_type = (
            CanonicalEventType.DELEGATION_COMPLETED
            if delegation.status == "completed"
            else CanonicalEventType.DELEGATION_FAILED
        )
        await self._publish(
            delegation=delegation,
            identity=identity,
            phase="final",
            event_type=event_type,
            payload={
                "status": delegation.status,
                "summary": summary.to_payload(),
                **(
                    {"error_code": "delegation.execution_failed"}
                    if delegation.status == "failed"
                    else {}
                ),
            },
        )

    async def _publish_pre_child_failed(
        self,
        *,
        delegation: DelegationRecord,
        identity: IdentityContext,
    ) -> None:
        await self._publish(
            delegation=delegation,
            identity=identity,
            phase="final",
            event_type=CanonicalEventType.DELEGATION_FAILED,
            payload={
                "status": "failed",
                "error_code": "delegation.execution_failed",
            },
        )

    async def _resume_parent_terminal_if_ready(self, delegation: DelegationRecord) -> None:
        """最后一个 child evidence 发布后，恢复 parent 冻结的 terminal intent。"""

        async with self._storage.uow() as uow:
            pending = await uow.evidence_outbox.has_pending_operation(
                run_id=delegation.parent_run_id,
                operation_kind=EvidenceOperationKind.DELEGATION,
            )
            parent = await uow.runs.get(delegation.parent_run_id)
            checkpoint = await uow.checkpoints.get_latest(delegation.parent_run_id)
        if (
            pending
            or parent is None
            or checkpoint is None
            or checkpoint.state.get("kind") != "delegation_terminal"
        ):
            return
        approval_recovery = checkpoint.state.get("approval_recovery")
        if approval_recovery is not None:
            if not isinstance(approval_recovery, Mapping):
                raise DelegationError("delegation.execution_failed")
            approval_id = cast(Mapping[str, object], approval_recovery).get("approval_id")
            if not isinstance(approval_id, str) or not approval_id:
                raise DelegationError("delegation.execution_failed")
            resolution = await self._event_bus.event_by_id(
                run_id=delegation.parent_run_id,
                event_id=f"approval-resolution:{approval_id}",
            )
            if resolution is None:
                # approval continuation 会在 resolution 发布后复查 delegation
                # pending；两条路径至少一条负责恢复，且 terminal 永不越过 resolution。
                return
        parent_status = RunStatus(parent.status)
        if parent_status != RunStatus.WAITING and not (
            parent_status.value in _TERMINAL
            and checkpoint.state.get("approval_recovery") is not None
        ):
            return
        identity_payload = checkpoint.state.get("identity")
        if not isinstance(identity_payload, dict):
            raise DelegationError("delegation.execution_failed")
        execution_identity = IdentityContext.model_validate(identity_payload)
        await self._orchestrator.resume_run(
            checkpoint.resume_token,
            expected_run_id=delegation.parent_run_id,
            identity=execution_identity,
        )

    async def _publish(
        self,
        *,
        delegation: DelegationRecord,
        identity: IdentityContext,
        phase: str,
        event_type: CanonicalEventType,
        payload: dict[str, Any],
    ) -> None:
        event_id = f"delegation:{delegation.id}:{phase}"
        published_result: dict[str, object] | None = None
        try:
            async with self._storage.uow() as uow:
                existing = await uow.evidence_outbox.get_by_event_id(event_id=event_id)
                if existing is not None and existing.state == "published":
                    if not isinstance(existing.result_json, Mapping):
                        raise DelegationError("delegation.execution_failed")
                    published_result = dict(existing.result_json)
                await uow.evidence_outbox.ensure_event_publishable(event_id=event_id)
        except LookupError as exc:
            raise DelegationError("delegation.execution_failed") from exc
        if phase == "child" and published_result is not None:
            payload = _published_child_payload(
                delegation=delegation,
                result=published_result,
            )
        try:
            # 即使 outbox 已标记 published，也必须让 sink 复核同 event_id 的稳定
            # envelope；evidence 缺失时同一路径受控重建，语义冲突则封闭失败。
            await self._event_bus.publish(
                tenant_id=delegation.tenant_id,
                run_id=delegation.parent_run_id,
                agent_id=delegation.source_agent_id,
                user_id=identity.user_id,
                event_type=event_type,
                payload={
                    "delegation_id": delegation.id,
                    "source_agent_id": delegation.source_agent_id,
                    "target_agent_id": delegation.target_agent_id,
                    **payload,
                },
                request_id=delegation.request_id,
                trace_id=delegation.trace_id,
                event_id=event_id,
            )
        except EventSinkReplayConflict as exc:
            raise DelegationError("delegation.execution_failed") from exc
        try:
            async with self._storage.uow() as uow:
                if phase == "final":
                    parent = await uow.runs.get_for_update(delegation.parent_run_id)
                    if parent is None or parent.tenant_id != delegation.tenant_id:
                        raise DelegationError("delegation.execution_failed")
                await uow.evidence_outbox.mark_event_published(event_id=event_id)
                await uow.commit()
        except LookupError as exc:
            raise DelegationError("delegation.execution_failed") from exc


__all__ = ["_DelegationPublicationMixin"]
