"""HITL approval 创建、审批、拒绝与 run resume。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from agent_harness.approvals._continuation import ApprovalContinuationMixin
from agent_harness.approvals._contracts import (
    ApprovalEnqueueUnavailable,
    ApprovalResolveResult,
    ApprovalStateConflict,
    can_read_approval,
    can_resolve_approval,
    resume_token_value,
)
from agent_harness.approvals._queue_resolution import ApprovalQueueResolutionMixin
from agent_harness.approvals.reconciliation import (
    approval_evidence,
    reconcile_denied,
    resolved_approval_record,
    stage_approval_evidence_group,
)
from agent_harness.audit import AuditService, build_audit_log
from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyDeniedError
from agent_harness.runtime.checkpoints import ResumeToken
from agent_harness.runtime.orchestrator import RunOrchestrator
from agent_harness.runtime.queue import RunQueue
from agent_harness.runtime.state import RunStatus
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import ApprovalCreate, ApprovalRecord, SQLAlchemyStorage
from agent_harness.storage.access_repositories import ApprovalResolutionRepositoryConflict

# 这些类型仍以原公开模块为身份，避免拆分改变文档、序列化或诊断输出。
ApprovalEnqueueUnavailable.__module__ = __name__
ApprovalResolveResult.__module__ = __name__
ApprovalStateConflict.__module__ = __name__

__all__ = [
    "ApprovalEnqueueUnavailable",
    "ApprovalResolveResult",
    "ApprovalService",
    "ApprovalStateConflict",
]


class ApprovalService(ApprovalContinuationMixin, ApprovalQueueResolutionMixin):
    """approval 状态机；API 和 CLI 都必须共用这条 seam。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        orchestrator: RunOrchestrator,
        audit: AuditService | None = None,
        recovery_lease_timeout_seconds: float = 300.0,
        queue: RunQueue | None = None,
    ) -> None:
        """装配审批状态机及其外部协作者，并把服务回绑定给 orchestrator。

        ``queue`` 缺失时使用本地同步续接；存在时只由 worker 执行 continuation。
        租约超时被限制为非负值，避免错误配置将失效 lease 变成永久占用。
        """

        self._storage = storage
        self._event_bus = event_bus
        self._orchestrator = orchestrator
        self._audit = audit
        self._recovery_lease_timeout = timedelta(seconds=max(0.0, recovery_lease_timeout_seconds))
        self._queue = queue
        orchestrator.bind_approval_service(self)

    @property
    def uses_queue(self) -> bool:
        """说明审批恢复是否由 service worker 异步执行。"""

        return self._queue is not None

    async def require_approval(
        self,
        *,
        actor: IdentityContext,
        run_id: str,
        agent_id: str,
        action: str,
        resource: str,
        reason: str,
        resume_token: ResumeToken | str | None = None,
        trace_id: str | None = None,
        request_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalRecord:
        """创建 waiting approval，并同步发布 event/audit 证据。"""

        await self._event_bus.reconcile_local_capacity(run_id=run_id)
        token_value = resume_token_value(resume_token)
        async with self._storage.uow() as uow:
            await uow.tenants.ensure(actor.tenant_id)
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != actor.tenant_id:
                raise LookupError(f"run not found: {run_id}")
            canonical_trace = run.trace_id
            # 审批能力必须绑定到发起暂停的原执行身份。调用方可补充 continuation
            # 元数据，但不能覆盖 identity/session；reviewer 只授权，不接管 run。
            bound_metadata = {
                **(metadata or {}),
                "identity_id": actor.user_id,
                "session_id": actor.session_id,
            }
            record = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id=actor.tenant_id,
                    run_id=run_id,
                    agent_id=agent_id,
                    action=action,
                    resource=resource,
                    reason=redact_secrets(reason),
                    resume_token=token_value,
                    requested_by=actor.user_id,
                    trace_id=canonical_trace,
                    request_id=request_id,
                    metadata=redact_secrets(bound_metadata),
                )
            )
            await uow.commit()
        await self._event_bus.publish(
            tenant_id=actor.tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            user_id=actor.user_id,
            event_type=CanonicalEventType.APPROVAL_REQUIRED,
            payload=approval_evidence(record),
            request_id=request_id,
            trace_id=canonical_trace,
        )
        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                action="approval.required",
                resource=resource,
                payload=approval_evidence(record),
            )
        return record

    async def list_for_run(self, *, actor: IdentityContext, run_id: str) -> list[ApprovalRecord]:
        """列出调用方租户可见的 run approvals，并记录 read audit evidence。"""

        if not can_read_approval(actor):
            raise PolicyDeniedError("approval read permission missing")
        async with self._storage.uow() as uow:
            rows = await uow.approvals.list_by_run(run_id, tenant_id=actor.tenant_id)
        if self._audit is not None:
            audit_payload: dict[str, Any] = {
                "requested_run_id": run_id,
                "count": len(rows),
            }
            if rows:
                audit_payload.update(
                    {
                        "run_id": rows[0].run_id,
                        "trace_id": rows[0].trace_id,
                    }
                )
            await self._audit.record(
                actor=actor,
                action="approval.list",
                resource=f"run:{run_id}",
                payload=audit_payload,
            )
        return rows

    async def get_by_id(
        self,
        *,
        actor: IdentityContext,
        approval_id: str,
        audit_read: bool = True,
    ) -> ApprovalRecord:
        """按 approval id 读取记录，供 CLI 和内部 resolve 前置检查复用。"""

        if not (can_read_approval(actor) or can_resolve_approval(actor)):
            raise PolicyDeniedError("approval permission missing")
        async with self._storage.uow() as uow:
            row = await uow.approvals.get(approval_id)
        if row is None or row.tenant_id != actor.tenant_id:
            raise LookupError(f"approval not found: {approval_id}")
        if audit_read and self._audit is not None:
            await self._audit.record(
                actor=actor,
                action="approval.read",
                resource=f"run:{row.run_id}:approval:{approval_id}",
                payload=approval_evidence(row),
            )
        return row

    async def get(
        self,
        *,
        actor: IdentityContext,
        run_id: str,
        approval_id: str,
    ) -> ApprovalRecord:
        """按 run + approval 双重定位公开 API detail，避免跨 run 泄漏。"""

        if not can_read_approval(actor):
            raise PolicyDeniedError("approval read permission missing")
        async with self._storage.uow() as uow:
            row = await uow.approvals.get(approval_id)
        if row is None or row.run_id != run_id or row.tenant_id != actor.tenant_id:
            raise LookupError(f"approval not found: {approval_id}")
        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                action="approval.read",
                resource=f"run:{run_id}:approval:{approval_id}",
                payload=approval_evidence(row),
            )
        return row

    async def approve(
        self,
        *,
        actor: IdentityContext,
        run_id: str,
        approval_id: str,
        request_id: str | None = None,
        comment: str | None = None,
    ) -> ApprovalResolveResult:
        """取得私有 lease，单次恢复真实 continuation，再公开 approved。"""

        if not can_resolve_approval(actor):
            raise PolicyDeniedError("approval resolve permission missing")
        await self._event_bus.reconcile_local_capacity(run_id=run_id)
        resolution_request_id = request_id or str(uuid4())
        if self._queue is not None:
            return await self._enqueue_approval(
                actor=actor,
                run_id=run_id,
                approval_id=approval_id,
                request_id=resolution_request_id,
                comment=comment,
            )
        try:
            async with self._storage.uow() as uow:
                lease = await uow.approvals.claim_resolution(
                    approval_id=approval_id,
                    run_id=run_id,
                    tenant_id=actor.tenant_id,
                    request_id=resolution_request_id,
                )
                await stage_approval_evidence_group(
                    uow,
                    record=lease.approval,
                    resolution_status="approved",
                    run_status=None,
                )
                await uow.commit()
        except ApprovalResolutionRepositoryConflict as exc:
            await self._reconcile_conflicted_resolution(
                actor=actor,
                run_id=run_id,
                approval_id=approval_id,
                request_id=request_id,
            )
            raise ApprovalStateConflict(str(exc), code=exc.code) from exc

        return await self._continue_with_recovery_marker(
            actor=actor,
            lease=lease,
            comment=comment,
        )

    async def deny(
        self,
        *,
        actor: IdentityContext,
        run_id: str,
        approval_id: str,
        request_id: str | None = None,
        comment: str | None = None,
    ) -> ApprovalResolveResult:
        """仅在 approve lease 尚未胜出时原子 deny。"""

        if not can_resolve_approval(actor):
            raise PolicyDeniedError("approval resolve permission missing")
        await self._event_bus.reconcile_local_capacity(run_id=run_id)
        resolution_request_id = request_id or str(uuid4())
        try:
            async with self._storage.uow() as uow:
                record = await uow.approvals.deny_waiting(
                    approval_id=approval_id,
                    run_id=run_id,
                    tenant_id=actor.tenant_id,
                    resolved_by=actor.user_id,
                    request_id=resolution_request_id,
                    metadata={"comment": redact_secrets(comment)} if comment else None,
                )
                evidence_record = resolved_approval_record(record, status="denied")
                await stage_approval_evidence_group(
                    uow,
                    record=record,
                    resolution_status="denied",
                    run_status=RunStatus.FAILED,
                )
                if self._audit is not None:
                    await uow.audit_logs.create(
                        build_audit_log(
                            actor=actor,
                            action="approval.denied",
                            resource=record.resource,
                            payload={
                                **approval_evidence(evidence_record),
                                "request_id": resolution_request_id,
                                "approval_request_id": record.request_id,
                            },
                        )
                    )
                await uow.commit()
        except ApprovalResolutionRepositoryConflict as exc:
            await self._reconcile_conflicted_resolution(
                actor=actor,
                run_id=run_id,
                approval_id=approval_id,
                request_id=request_id,
            )
            raise ApprovalStateConflict(str(exc), code=exc.code) from exc
        run_result = await reconcile_denied(
            self._storage,
            self._event_bus,
            self._orchestrator,
            actor=actor,
            record=record,
            resolution_request_id=resolution_request_id,
        )
        async with self._storage.uow() as uow:
            completed = await uow.approvals.get(approval_id)
        if completed is None:
            raise LookupError(f"approval not found: {approval_id}")
        return ApprovalResolveResult(approval=completed, run=run_result)
