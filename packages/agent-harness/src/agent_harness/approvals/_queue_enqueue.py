"""Service approval resolution 的 lease claim 与 queue enqueue。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from agent_harness.approvals._contracts import (
    ApprovalEnqueueUnavailable,
    ApprovalResolveResult,
    ApprovalStateConflict,
    as_utc,
)
from agent_harness.approvals.reconciliation import (
    stage_approval_evidence_group,
)
from agent_harness.audit import AuditService, build_audit_log
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.runtime.orchestrator import RunOrchestrator
from agent_harness.runtime.queue import RunQueue, build_resume_approval_message
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.access_repositories import (
    ApprovalResolutionRepositoryConflict,
)


class ApprovalQueueEnqueueMixin:
    """只负责 API 侧原子 claim 与排队，不执行 continuation。"""

    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _orchestrator: RunOrchestrator
    _audit: AuditService | None
    _recovery_lease_timeout: timedelta
    _queue: RunQueue | None

    if TYPE_CHECKING:

        async def _reconcile_conflicted_resolution(
            self,
            *,
            actor: IdentityContext,
            run_id: str,
            approval_id: str,
            request_id: str,
        ) -> None:
            """声明组合服务必须提供的冲突恢复 seam，排队 mixin 不自行接管 continuation。"""

            ...

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
                if record is None:
                    raise LookupError(f"approval not found: {approval_id}")
                await stage_approval_evidence_group(
                    uow,
                    record=record,
                    resolution_status="approved",
                    run_status=None,
                )
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
                if record is None:
                    raise LookupError(f"approval not found: {approval_id}") from exc
                await stage_approval_evidence_group(
                    uow,
                    record=record,
                    resolution_status="approved",
                    run_status=None,
                )
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


__all__ = ["ApprovalQueueEnqueueMixin"]
