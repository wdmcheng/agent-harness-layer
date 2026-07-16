"""Service profile 的 approval 排队、接管与 worker 收口。"""

from __future__ import annotations

from datetime import timedelta

from agent_harness.approvals._contracts import (
    ApprovalResolveResult,
    ApprovalStateConflict,
)
from agent_harness.approvals._queue_enqueue import ApprovalQueueEnqueueMixin
from agent_harness.approvals.reconciliation import (
    approval_evidence,
    complete_approval_evidence_group,
    publish_resolution_evidence,
    resolved_approval_record,
    stage_approval_evidence_group,
)
from agent_harness.audit import AuditService, build_audit_log
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.runtime.orchestrator import RunOrchestrator
from agent_harness.runtime.queue import RunQueue
from agent_harness.runtime.state import RunStatus
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.access_repositories import (
    ApprovalResolutionLease,
)


class ApprovalQueueResolutionMixin(ApprovalQueueEnqueueMixin):
    """把 service queue 生命周期隔离在 ApprovalService 的内部协作者中。"""

    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _orchestrator: RunOrchestrator
    _audit: AuditService | None
    _recovery_lease_timeout: timedelta
    _queue: RunQueue | None

    async def execute_queued_approval(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        run_id: str,
        operation_id: str,
        lease_id: str,
    ) -> ApprovalResolveResult:
        """worker 用当前 execution-owned lease 恢复原 continuation。"""

        state, record, actor = await self._load_queued_execution_actor(
            approval_id=approval_id,
            lease_id=lease_id,
            tenant_id=tenant_id,
            run_id=run_id,
            operation_id=operation_id,
        )
        lease = ApprovalResolutionLease(
            approval=record,
            lease_id=state.lease_id,
            state=state.resolution_state,
            resolution_request_id=state.request_id,
            claimed_at=state.claimed_at,
        )
        return await self._continue_with_recovery_marker(
            actor=actor,
            lease=lease,
            comment=state.comment,
        )

    async def finalize_queued_failure(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        run_id: str,
        operation_id: str,
        lease_id: str,
        error_code: str,
    ) -> ApprovalResolveResult:
        """收口 DBOS ERROR；已发生的 continuation 只补 evidence，不重放 handler。"""

        state, record, actor = await self._load_queued_execution_actor(
            approval_id=approval_id,
            lease_id=lease_id,
            tenant_id=tenant_id,
            run_id=run_id,
            operation_id=operation_id,
            allowed_states=frozenset(
                {"execution_owned", "recovery_pending", "completed", "failed"}
            ),
        )
        if record.status == "waiting" and state.resolution_state == "recovery_pending":
            async with self._storage.uow() as uow:
                run_record = await uow.runs.get(record.run_id)
            if run_record is not None and RunStatus(run_record.status) in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
            }:
                async with self._storage.uow() as uow:
                    await stage_approval_evidence_group(
                        uow,
                        record=record,
                        resolution_status="approved",
                        run_status=RunStatus(run_record.status),
                    )
                    await uow.commit()
                await publish_resolution_evidence(
                    self._event_bus,
                    actor=actor,
                    record=resolved_approval_record(record, status="approved"),
                    request_id=state.request_id,
                )
                run = await self._orchestrator.get_run(record.run_id, identity=actor)
                await complete_approval_evidence_group(
                    self._storage,
                    self._event_bus,
                    record=record,
                )
                async with self._storage.uow() as uow:
                    record = await uow.approvals.mark_approved_evidence_complete(
                        approval_id=record.approval_id,
                        run_id=record.run_id,
                        tenant_id=record.tenant_id,
                        lease_id=state.lease_id,
                    )
                    await uow.commit()
                return ApprovalResolveResult(approval=record, run=run)
            lease = ApprovalResolutionLease(
                approval=record,
                lease_id=state.lease_id,
                state=state.resolution_state,
                resolution_request_id=state.request_id,
                claimed_at=state.claimed_at,
            )
            return await self._continue_with_recovery_marker(
                actor=actor,
                lease=lease,
                comment=state.comment,
            )
        if record.status == "waiting" and state.resolution_state == "execution_owned":
            run_result = await self._orchestrator.fail_queued_run(
                run_id=run_id,
                tenant_id=tenant_id,
                reason=error_code,
                defer_terminal=True,
            )
            result_state = "failed" if run_result.status == RunStatus.FAILED else "completed"
            async with self._storage.uow() as uow:
                record = await uow.approvals.finalize_approved(
                    approval_id=approval_id,
                    run_id=record.run_id,
                    tenant_id=record.tenant_id,
                    lease_id=lease_id,
                    resolved_by=actor.user_id,
                    result_state=result_state,
                    metadata={"runtime_error_code": error_code},
                )
                evidence_record = resolved_approval_record(record, status="approved")
                await stage_approval_evidence_group(
                    uow,
                    record=record,
                    resolution_status="approved",
                    run_status=(
                        RunStatus.FAILED if result_state == "failed" else RunStatus.COMPLETED
                    ),
                )
                if self._audit is not None:
                    await uow.audit_logs.create(
                        build_audit_log(
                            actor=actor,
                            action="approval.approved",
                            resource=record.resource,
                            payload={
                                **approval_evidence(evidence_record),
                                "request_id": state.request_id,
                                "runtime_error_code": error_code,
                            },
                        )
                    )
                await uow.commit()
        elif record.status not in {"waiting", "approved"} or state.resolution_state not in {
            "completed",
            "failed",
        }:
            raise ApprovalStateConflict(
                "approval deterministic failure state mismatch",
                code="approval.resolution_in_progress",
            )
        async with self._storage.uow() as uow:
            persisted_run = await uow.runs.get(record.run_id)
            if persisted_run is None:
                raise LookupError(f"run not found: {record.run_id}")
            await stage_approval_evidence_group(
                uow,
                record=record,
                resolution_status="approved",
                run_status=RunStatus(persisted_run.status),
            )
            await uow.commit()
        await publish_resolution_evidence(
            self._event_bus,
            actor=actor,
            record=resolved_approval_record(record, status="approved"),
            request_id=state.request_id,
        )
        run = await self._orchestrator.get_run(record.run_id, identity=actor)
        await complete_approval_evidence_group(
            self._storage,
            self._event_bus,
            record=record,
        )
        if record.status == "waiting":
            async with self._storage.uow() as uow:
                record = await uow.approvals.mark_approved_evidence_complete(
                    approval_id=record.approval_id,
                    run_id=record.run_id,
                    tenant_id=record.tenant_id,
                    lease_id=lease_id,
                )
                await uow.commit()
        return ApprovalResolveResult(approval=record, run=run)

    async def _load_queued_execution_actor(
        self,
        *,
        approval_id: str,
        tenant_id: str,
        run_id: str,
        operation_id: str,
        lease_id: str,
        allowed_states: frozenset[str] = frozenset({"execution_owned"}),
    ):
        """校验 worker fencing，并重建只用于审批审计的 reviewer 身份。"""

        async with self._storage.uow() as uow:
            state = await uow.approvals.get_resolution_queue_state(approval_id)
            record = await uow.approvals.get(approval_id)
            run = None if record is None else await uow.runs.get(record.run_id)
            if (
                state is None
                or record is None
                or run is None
                or state.tenant_id != tenant_id
                or record.tenant_id != tenant_id
                or run.tenant_id != tenant_id
                or state.run_id != run_id
                or state.operation_id != operation_id
                or state.lease_id != lease_id
                or state.resolution_state not in allowed_states
            ):
                raise ApprovalStateConflict(
                    "approval execution lease mismatch",
                    code="approval.resolution_in_progress",
                )
            run_state = await uow.runs.get_execution(record.run_id)
        if run_state is None or run_state.tenant_id != tenant_id:
            raise LookupError(f"run execution state not found: {record.run_id}")
        run_identity_payload = run_state.execution_context.get("identity")
        if not isinstance(run_identity_payload, dict):
            raise ApprovalStateConflict("approval execution identity is missing")
        run_identity = IdentityContext.model_validate(run_identity_payload)
        if run_identity.tenant_id != tenant_id:
            raise ApprovalStateConflict("approval execution tenant mismatch")
        # 认证和授权已经由 APR-002 完成；worker 只重建审批人的审计身份，不能把
        # 原 run 提交者误记为 resolved_by。run 执行身份仍由 orchestrator 私有快照恢复。
        actor = IdentityContext(
            tenant_id=tenant_id,
            user_id=state.reviewer_id,
            session_id=run_identity.session_id,
            roles=[],
            permissions=[],
            auth_method="service-approval",
        )
        return state, record, actor

    async def _reconcile_conflicted_resolution(
        self,
        *,
        actor: IdentityContext,
        run_id: str,
        approval_id: str,
        request_id: str | None,
    ) -> None:
        raise NotImplementedError

    async def _continue_with_recovery_marker(
        self,
        *,
        actor: IdentityContext,
        lease: ApprovalResolutionLease,
        comment: str | None,
    ) -> ApprovalResolveResult:
        raise NotImplementedError
