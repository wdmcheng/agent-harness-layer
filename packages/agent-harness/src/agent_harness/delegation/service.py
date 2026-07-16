"""受控 `agent.delegate` application service 与 local/service 共享状态机。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from agent_harness.contracts import GuardrailDecisionStatus
from agent_harness.delegation._service_evidence import (
    published_child_payload as _published_child_payload,
)
from agent_harness.delegation._service_evidence import (
    required_child_id as _required_child_id,
)
from agent_harness.delegation._service_recovery import DelegationRecoveryMixin
from agent_harness.delegation._service_summary import DelegationSummaryMixin
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
    DelegationRequest,
    DelegationSummary,
    delegation_request_hash,
)
from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.events.sinks.base import EventSinkReplayConflict
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck
from agent_harness.registry import AgentDescriptor, AgentRegistry, RegistryLoadError
from agent_harness.runtime import RunOrchestrator, RunStatus
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.delegation_repositories import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationRecord,
    DelegationStorageConflict,
)
from agent_harness.storage.event_capacity_repositories import (
    EventCapacityExceeded,
    EvidenceOperationKind,
)
from agent_harness.storage.repositories import RunRecord

# 公开类型仍以 application service facade 为身份，避免拆分泄漏私有模块名。
DelegationError.__module__ = __name__
DelegationExecutionResult.__module__ = __name__


class DelegationService(DelegationSummaryMixin, DelegationRecoveryMixin):
    """授权后原子 claim，再恢复唯一 child 并从 durable evidence 聚合。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        registry: AgentRegistry,
        policy: DelegationPolicy,
        event_bus: EventBus,
        orchestrator: RunOrchestrator | DelegationOrchestrator,
        mode: DelegationMode,
    ) -> None:
        self._storage = storage
        self._registry = registry
        self._policy = policy
        self._event_bus = event_bus
        self._orchestrator = orchestrator
        self._mode = mode

    async def delegate(
        self,
        request: DelegationRequest,
        *,
        identity: IdentityContext,
    ) -> DelegationExecutionResult:
        """执行或恢复一个单层 delegation；拒绝路径不创建业务状态。"""

        parent, source, target = await self._authorize(request=request, identity=identity)
        await self._event_bus.reconcile_local_capacity(run_id=parent.id)
        request_hash = delegation_request_hash(request, identity=identity)
        scope = f"delegation-parent:{identity.tenant_id}:{parent.id}"
        try:
            async with self._storage.idempotency_request_lock(scope):
                async with self._storage.uow() as uow:
                    claim = await uow.delegations.claim_and_reserve(
                        DelegationClaimCreate(
                            tenant_id=identity.tenant_id,
                            parent_run_id=parent.id,
                            source_agent_id=source.agent_id,
                            target_agent_id=target.agent_id,
                            idempotency_key=request.idempotency_key,
                            request_hash=request_hash,
                            budget_intent=request.budget_intent,
                            child_input=request.child_input,
                            identity=identity.to_payload(),
                            trace_id=parent.trace_id,
                            request_id=request.request_id,
                            parent_token_limit=source.budget.max_tokens_per_run,
                            requested_token_reservation=target.budget.max_tokens_per_run,
                            parent_cost_limit=source.budget.max_cost_usd_per_run,
                            requested_cost_reservation=target.budget.max_cost_usd_per_run,
                        )
                    )
                    await uow.commit()
        except EventCapacityExceeded as exc:
            raise DelegationError("event.sequence_exhausted") from exc
        except DelegationBudgetExceeded as exc:
            raise DelegationError("delegation.budget_exceeded") from exc
        except DelegationStorageConflict as exc:
            raise DelegationError(exc.code) from exc

        delegation = claim.delegation
        await self._publish_claimed(delegation=delegation, identity=identity)
        if delegation.status == "failed" and delegation.child_run_id is None:
            await self._publish_pre_child_failed(delegation=delegation, identity=identity)
            raise DelegationError("delegation.execution_failed")
        delegation = await self._recover_or_launch_child(
            delegation=delegation,
            request=request,
            identity=identity,
        )
        await self._publish_child_created(delegation=delegation, identity=identity)
        if self._mode == "local":
            return await self.reconcile_child(delegation.child_run_id or "")
        return DelegationExecutionResult(
            delegation_id=delegation.id,
            parent_run_id=delegation.parent_run_id,
            child_run_id=_required_child_id(delegation),
            status=delegation.status,
            summary=await self.get_parent_summary(
                tenant_id=delegation.tenant_id,
                parent_run_id=delegation.parent_run_id,
            ),
        )

    async def _authorize(
        self,
        *,
        request: DelegationRequest,
        identity: IdentityContext,
    ) -> tuple[RunRecord, AgentDescriptor, AgentDescriptor]:
        async with self._storage.uow() as uow:
            parent = await uow.runs.get(request.parent_run_id)
            parent_session = None if parent is None else await uow.sessions.get(parent.session_id)
        if (
            parent is None
            or parent_session is None
            or parent.tenant_id != identity.tenant_id
            or parent.agent_id != request.source_agent_id
            or parent.session_id != identity.session_id
            or parent_session.id != parent.session_id
            or parent_session.tenant_id != identity.tenant_id
            or parent_session.user_id != identity.user_id
        ):
            raise DelegationError("delegation.policy_denied")
        if parent.status in _TERMINAL:
            raise DelegationError("delegation.execution_failed")
        if request.source_agent_id == request.target_agent_id:
            raise DelegationError("delegation.cycle_detected")
        if parent.parent_run_id is not None:
            raise DelegationError("delegation.depth_exceeded")
        try:
            source = self._registry.get(request.source_agent_id)
            target = self._registry.get(request.target_agent_id)
        except RegistryLoadError as exc:
            raise DelegationError("delegation.target_not_found") from exc
        if not self._registry.check_delegation(source.agent_id, target.agent_id).allowed:
            raise DelegationError("delegation.edge_denied")
        decision = await self._policy.evaluate(
            PolicyCheck(
                actor=identity,
                action="agent.delegate",
                resource=f"agent:{target.agent_id}",
                context={
                    "parent_run_id": parent.id,
                    "source_agent_id": source.agent_id,
                    "target_agent_id": target.agent_id,
                    "request_id": request.request_id,
                },
            )
        )
        if decision.decision != GuardrailDecisionStatus.ALLOW.value:
            raise DelegationError("delegation.policy_denied")
        return parent, source, target

    async def _recover_or_launch_child(
        self,
        *,
        delegation: DelegationRecord,
        request: DelegationRequest,
        identity: IdentityContext,
    ) -> DelegationRecord:
        child_id = delegation.child_run_id
        if child_id is None:
            async with self._storage.uow() as uow:
                existing = await uow.runs.get_by_idempotency_key(
                    tenant_id=delegation.tenant_id,
                    session_id=identity.session_id,
                    agent_id=delegation.target_agent_id,
                    idempotency_key=f"delegation:{delegation.id}",
                )
            if existing is not None:
                child_id = existing.id
        if child_id is None:
            launcher = (
                self._orchestrator.start_run
                if self._mode == "local"
                else self._orchestrator.submit_run
            )
            try:
                result = await launcher(
                    agent_id=delegation.target_agent_id,
                    input=request.child_input,
                    idempotency_key=f"delegation:{delegation.id}",
                    identity=identity,
                    request_id=delegation.request_id,
                    trace_id=delegation.trace_id,
                    parent_run_id=delegation.parent_run_id,
                )
            except Exception as exc:
                async with self._storage.uow() as uow:
                    existing = await uow.runs.get_by_idempotency_key(
                        tenant_id=delegation.tenant_id,
                        session_id=identity.session_id,
                        agent_id=delegation.target_agent_id,
                        idempotency_key=f"delegation:{delegation.id}",
                    )
                    if existing is None:
                        delegation = await uow.delegations.release_pre_child_failure(
                            delegation_id=delegation.id,
                        )
                        await uow.commit()
                if existing is None:
                    await self._publish_pre_child_failed(
                        delegation=delegation,
                        identity=identity,
                    )
                    raise DelegationError("delegation.execution_failed") from exc
                child_id = existing.id
            else:
                child_id = result.run_id
        async with self._storage.uow() as uow:
            attached = await uow.delegations.attach_child(
                delegation_id=delegation.id,
                child_run_id=child_id,
            )
            await uow.commit()
        return attached

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


__all__ = [
    "DelegationError",
    "DelegationExecutionResult",
    "DelegationMode",
    "DelegationService",
]
