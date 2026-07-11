"""Service profile 的 approval 排队、接管与 worker 收口。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from agent_harness.approvals._contracts import (
    ApprovalEnqueueUnavailable,
    ApprovalResolveResult,
    ApprovalStateConflict,
    as_utc,
)
from agent_harness.approvals.reconciliation import approval_evidence, publish_resolution_evidence
from agent_harness.audit import AuditService, build_audit_log
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.runtime.orchestrator import RunOrchestrator
from agent_harness.runtime.queue import RunQueue, build_resume_approval_message
from agent_harness.runtime.state import RunStatus
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.access_repositories import (
    ApprovalResolutionLease,
    ApprovalResolutionRepositoryConflict,
)


class ApprovalQueueResolutionMixin:
    """把 service queue 生命周期隔离在 ApprovalService 的内部协作者中。"""

    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _orchestrator: RunOrchestrator
    _audit: AuditService | None
    _recovery_lease_timeout: timedelta
    _queue: RunQueue | None

    async def _enqueue_approval(
        self,
        *,
        actor: IdentityContext,
        run_id: str,
        approval_id: str,
        request_id: str,
        comment: str | None,
    ) -> ApprovalResolveResult:
        """service approve 只取得 lease 并排队，不在 API 进程执行 continuation。"""

        assert self._queue is not None
        request_hash = hashlib.sha256(
            json.dumps(
                {"decision": "approve", "comment": comment},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        safe_comment = None if comment is None else str(redact_secrets(comment))
        try:
            async with self._storage.uow() as uow:
                state = await uow.approvals.claim_service_resolution(
                    approval_id=approval_id,
                    run_id=run_id,
                    tenant_id=actor.tenant_id,
                    reviewer_id=actor.user_id,
                    decision="approve",
                    request_hash=request_hash,
                    request_id=request_id,
                    comment=safe_comment,
                )
                record = await uow.approvals.get(approval_id)
                await uow.commit()
        except ApprovalResolutionRepositoryConflict as exc:
            async with self._storage.uow() as uow:
                state = await uow.approvals.get_resolution_queue_state(approval_id)
                has_claim = await uow.approvals.has_tool_claim(approval_id)
                fingerprint_matches = (
                    state is not None
                    and state.run_id == run_id
                    and state.tenant_id == actor.tenant_id
                    and state.reviewer_id == actor.user_id
                    and state.decision == "approve"
                    and state.request_hash == request_hash
                )
            is_active_service_lease = bool(
                state is not None
                and state.resolution_state in {"claimed", "execution_owned"}
                and state.enqueue_state in {"enqueue_pending", "queued"}
            )
            if is_active_service_lease and (not fingerprint_matches or has_claim):
                raise ApprovalStateConflict(str(exc), code=exc.code) from exc
            if not is_active_service_lease or state is None:
                # 已收口/evidence pending 状态进入通用补偿；active service lease
                # 不能被 raw local takeover 抢先并在 API 进程执行 continuation。
                await self._reconcile_conflicted_resolution(
                    actor=actor,
                    run_id=run_id,
                    approval_id=approval_id,
                    request_id=request_id,
                )
                raise ApprovalStateConflict(str(exc), code=exc.code) from exc
            async with self._storage.uow() as uow:
                state = await uow.approvals.get_resolution_queue_state(approval_id)
                if state is None:
                    raise ApprovalStateConflict(str(exc), code=exc.code) from exc
                if (
                    state.resolution_state == "execution_owned"
                    and state.claimed_at is not None
                    and as_utc(state.claimed_at)
                    <= datetime.now(tz=UTC) - self._recovery_lease_timeout
                ):
                    previous = state
                    state = await uow.approvals.takeover_service_resolution(
                        approval_id=approval_id,
                        run_id=run_id,
                        tenant_id=actor.tenant_id,
                        reviewer_id=actor.user_id,
                        decision="approve",
                        request_hash=request_hash,
                        request_id=request_id,
                        expired_before=datetime.now(tz=UTC) - self._recovery_lease_timeout,
                        comment=safe_comment,
                    )
                    if state is None:
                        raise ApprovalStateConflict(str(exc), code=exc.code) from exc
                    if self._audit is not None:
                        await uow.audit_logs.create(
                            build_audit_log(
                                actor=actor,
                                action="approval.resolution_taken_over",
                                resource=f"run:{run_id}:approval:{approval_id}",
                                payload={
                                    "approval_id": approval_id,
                                    "old_lease_id": previous.lease_id,
                                    "old_operation_id": previous.operation_id,
                                    "old_message_id": previous.message_id,
                                    "old_workflow_owner_id": previous.workflow_owner_id,
                                    "old_workflow_id": previous.workflow_id,
                                    "new_lease_id": state.lease_id,
                                    "new_operation_id": state.operation_id,
                                    "request_id": request_id,
                                },
                            )
                        )
                elif not (
                    state.resolution_state == "claimed"
                    and state.enqueue_state in {"enqueue_pending", "queued"}
                ):
                    raise ApprovalStateConflict(str(exc), code=exc.code) from exc
                if state.resolution_state != "claimed" or state.enqueue_state not in {
                    "enqueue_pending",
                    "queued",
                }:
                    raise ApprovalStateConflict(str(exc), code=exc.code) from exc
                record = await uow.approvals.get(approval_id)
                await uow.commit()
        assert record is not None
        if state.enqueue_state == "queued":
            run = await self._orchestrator.get_run(run_id, identity=actor)
            return ApprovalResolveResult(approval=record, run=run)
        message = build_resume_approval_message(
            request_id=state.request_id,
            tenant_id=state.tenant_id,
            run_id=state.run_id,
            approval_id=state.approval_id,
            resolution_lease_id=state.lease_id,
        )
        try:
            queued = await self._queue.enqueue(message)
            async with self._storage.uow() as uow:
                await uow.approvals.mark_resolution_queued(
                    approval_id=state.approval_id,
                    lease_id=state.lease_id,
                    operation_id=state.operation_id,
                    message_id=queued.message_id,
                )
                record = await uow.approvals.get(approval_id)
                await uow.commit()
        except Exception as exc:
            raise ApprovalEnqueueUnavailable("approval.enqueue_unavailable") from exc
        assert record is not None
        run = await self._orchestrator.get_run(run_id, identity=actor)
        return ApprovalResolveResult(approval=record, run=run)

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
            claimed_at=state.claimed_at,
        )
        return await self._continue_with_recovery_marker(
            actor=actor,
            lease=lease,
            request_id=state.request_id,
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
            lease = ApprovalResolutionLease(
                approval=record,
                lease_id=state.lease_id,
                state=state.resolution_state,
                claimed_at=state.claimed_at,
            )
            return await self._continue_with_recovery_marker(
                actor=actor,
                lease=lease,
                request_id=state.request_id,
                comment=state.comment,
            )
        if record.status == "waiting" and state.resolution_state == "execution_owned":
            run_result = await self._orchestrator.fail_queued_run(
                run_id=run_id,
                tenant_id=tenant_id,
                reason=error_code,
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
                if self._audit is not None:
                    await uow.audit_logs.create(
                        build_audit_log(
                            actor=actor,
                            action="approval.approved",
                            resource=record.resource,
                            payload={
                                **approval_evidence(record),
                                "request_id": state.request_id,
                                "runtime_error_code": error_code,
                            },
                        )
                    )
                await uow.commit()
        elif record.status != "approved" or state.resolution_state not in {
            "completed",
            "failed",
        }:
            raise ApprovalStateConflict(
                "approval deterministic failure state mismatch",
                code="approval.resolution_in_progress",
            )
        await publish_resolution_evidence(
            self._event_bus,
            actor=actor,
            record=record,
            request_id=state.request_id,
        )
        run = await self._orchestrator.get_run(record.run_id, identity=actor)
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
        request_id: str | None,
        comment: str | None,
    ) -> ApprovalResolveResult:
        raise NotImplementedError
