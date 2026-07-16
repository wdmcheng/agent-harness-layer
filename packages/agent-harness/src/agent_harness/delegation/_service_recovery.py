"""Delegation 已提交 claim 的可重入恢复路径。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_harness.delegation._service_evidence import required_child_id as _required_child_id
from agent_harness.delegation._service_types import (
    TERMINAL_RUN_STATUSES as _TERMINAL,
)
from agent_harness.delegation._service_types import (
    DelegationError,
    DelegationExecutionResult,
)
from agent_harness.delegation.models import (
    DelegationRequest,
    delegation_request_hash,
)
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.delegation_repositories import (
    DelegationRecord,
    DelegationStorageConflict,
)
from agent_harness.storage.repositories import RunRecord, SessionRecord


class DelegationRecoveryMixin:
    """只推进 durable claim，不重新授权或重复预算预约。"""

    _storage: SQLAlchemyStorage
    _event_bus: EventBus

    if TYPE_CHECKING:

        async def _publish_claimed(
            self, *, delegation: DelegationRecord, identity: IdentityContext
        ) -> None: ...
        async def _publish_pre_child_failed(
            self, *, delegation: DelegationRecord, identity: IdentityContext
        ) -> None: ...
        async def _publish_child_created(
            self, *, delegation: DelegationRecord, identity: IdentityContext
        ) -> None: ...
        async def _resume_parent_terminal_if_ready(self, delegation: DelegationRecord) -> None: ...
        async def _recover_or_launch_child(
            self,
            *,
            delegation: DelegationRecord,
            request: DelegationRequest,
            identity: IdentityContext,
        ) -> DelegationRecord: ...
        async def reconcile_child(self, child_run_id: str) -> DelegationExecutionResult: ...

    async def recover_pending_for_parent(self, *, parent_run_id: str) -> int:
        """不重跑授权/预算 claim，推进已提交 delegation 的 pending evidence。"""

        try:
            async with self._storage.uow() as uow:
                parent = await uow.runs.get(parent_run_id)
                if parent is None:
                    raise DelegationError("delegation.execution_failed")
                parent_session = await uow.sessions.get(parent.session_id)
                if parent_session is None:
                    raise DelegationError("delegation.execution_failed")
                candidates = await uow.delegations.list_recovery_candidates_for_parent(
                    tenant_id=parent.tenant_id,
                    parent_run_id=parent_run_id,
                )
        except DelegationStorageConflict as exc:
            raise DelegationError(exc.code) from exc
        for candidate in candidates:
            await self._recover_committed_operation(
                parent=parent,
                parent_session=parent_session,
                delegation=candidate.delegation,
            )
        return len(candidates)

    async def _recover_committed_operation(
        self,
        *,
        parent: RunRecord,
        parent_session: SessionRecord,
        delegation: DelegationRecord,
    ) -> None:
        """durable claim 是恢复授权；只重放确定性 event/child/aggregation 步骤。"""

        if delegation.budget_intent != "inherit_parent":
            raise DelegationError("delegation.execution_failed")
        try:
            identity = IdentityContext.model_validate(delegation.identity)
            request = DelegationRequest(
                parent_run_id=delegation.parent_run_id,
                source_agent_id=delegation.source_agent_id,
                target_agent_id=delegation.target_agent_id,
                child_input=delegation.child_input,
                idempotency_key=delegation.idempotency_key,
                budget_intent="inherit_parent",
                request_id=delegation.request_id,
            )
        except ValueError as exc:
            raise DelegationError("delegation.execution_failed") from exc
        if (
            identity.tenant_id != parent.tenant_id
            or identity.session_id != parent.session_id
            or identity.user_id != parent_session.user_id
            or parent_session.id != parent.session_id
            or parent_session.tenant_id != parent.tenant_id
            or delegation.parent_run_id != parent.id
            or delegation.source_agent_id != parent.agent_id
            or delegation.trace_id != parent.trace_id
            or delegation.request_hash != delegation_request_hash(request, identity=identity)
            or parent.status in _TERMINAL
        ):
            raise DelegationError("delegation.execution_failed")

        await self._event_bus.reconcile_local_capacity(run_id=parent.id)
        await self._publish_claimed(delegation=delegation, identity=identity)
        if delegation.status == "failed" and delegation.child_run_id is None:
            await self._publish_pre_child_failed(delegation=delegation, identity=identity)
            await self._resume_parent_terminal_if_ready(delegation)
            return
        try:
            recovered = await self._recover_or_launch_child(
                delegation=delegation,
                request=request,
                identity=identity,
            )
        except DelegationError:
            async with self._storage.uow() as uow:
                failed = await uow.delegations.get(delegation.id)
            if failed is None or failed.status != "failed" or failed.child_run_id is not None:
                raise
            await self._resume_parent_terminal_if_ready(failed)
            return
        await self._publish_child_created(delegation=recovered, identity=identity)
        await self.reconcile_child(_required_child_id(recovered))


__all__ = ["DelegationRecoveryMixin"]
