"""HITL approval 创建、审批、拒绝与 run resume。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from agent_harness.approvals.reconciliation import (
    approval_evidence,
    mark_recovery_pending,
    publish_resolution_evidence,
    reconcile_denied,
)
from agent_harness.audit import AuditService, build_audit_log
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyDeniedError
from agent_harness.runtime import (
    AgentExecutionLeaseLost,
    AgentExecutionUncertain,
    ApprovalGrant,
    ResumeToken,
    RunOrchestrator,
    RunResult,
    RunStatus,
)
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import ApprovalCreate, ApprovalRecord, SQLAlchemyStorage
from agent_harness.storage.access_repositories import (
    ApprovalResolutionLease,
    ApprovalResolutionRepositoryConflict,
)


class ApprovalStateConflict(RuntimeError):
    """approval 已 resolved 或状态非法时抛出。"""

    def __init__(self, message: str, *, code: str = "approval.invalid_transition") -> None:
        super().__init__(message)
        self.code = code
        self.status_code = 409


class ApprovalResolveResult(HarnessDTO):
    """审批 resolve 后返回审批记录和可能被推进的 run。"""

    approval: ApprovalRecord
    run: RunResult | None = None


class ApprovalService:
    """approval 状态机；API 和 CLI 都必须共用这条 seam。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        orchestrator: RunOrchestrator,
        audit: AuditService | None = None,
        recovery_lease_timeout_seconds: float = 300.0,
    ) -> None:
        self._storage = storage
        self._event_bus = event_bus
        self._orchestrator = orchestrator
        self._audit = audit
        self._recovery_lease_timeout = timedelta(seconds=max(0.0, recovery_lease_timeout_seconds))
        orchestrator.bind_approval_service(self)

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

        token_value = _resume_token_value(resume_token)
        async with self._storage.uow() as uow:
            await uow.tenants.ensure(actor.tenant_id)
            run = await uow.runs.get(run_id)
            if run is None or run.tenant_id != actor.tenant_id:
                raise LookupError(f"run not found: {run_id}")
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
                    trace_id=trace_id,
                    request_id=request_id,
                    metadata=redact_secrets(metadata or {}),
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
            trace_id=trace_id,
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

        if not _can_read_approval(actor):
            raise PolicyDeniedError("approval read permission missing")
        async with self._storage.uow() as uow:
            rows = await uow.approvals.list_by_run(run_id, tenant_id=actor.tenant_id)
        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                action="approval.list",
                resource=f"run:{run_id}",
                payload={"run_id": run_id, "count": len(rows)},
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

        if not (_can_read_approval(actor) or _can_resolve_approval(actor)):
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

        if not _can_read_approval(actor):
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

        if not _can_resolve_approval(actor):
            raise PolicyDeniedError("approval resolve permission missing")
        try:
            async with self._storage.uow() as uow:
                lease = await uow.approvals.claim_resolution(
                    approval_id=approval_id,
                    run_id=run_id,
                    tenant_id=actor.tenant_id,
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
            request_id=request_id,
            comment=comment,
        )

    async def _continue_with_recovery_marker(
        self,
        *,
        actor: IdentityContext,
        lease: ApprovalResolutionLease,
        request_id: str | None,
        comment: str | None,
    ) -> ApprovalResolveResult:
        try:
            return await self._continue_claimed_approval(
                actor=actor,
                lease=lease,
                request_id=request_id,
                comment=comment,
            )
        except ApprovalStateConflict:
            raise
        except Exception:
            await mark_recovery_pending(self._storage, lease)
            raise

    async def recover_claimed(
        self,
        *,
        actor: IdentityContext,
        run_id: str,
        approval_id: str,
        request_id: str | None = None,
        comment: str | None = None,
    ) -> ApprovalResolveResult:
        """恢复在唯一 tool claim 创建前中断的既有 lease。"""

        if not _can_resolve_approval(actor):
            raise PolicyDeniedError("approval resolve permission missing")
        async with self._storage.uow() as uow:
            lease = await uow.approvals.get_resolution(approval_id)
        if (
            lease is None
            or lease.approval.run_id != run_id
            or lease.approval.tenant_id != actor.tenant_id
        ):
            raise LookupError(f"approval resolution not found: {approval_id}")
        if lease.approval.status == "approved" and lease.state in {"completed", "failed"}:
            run_result = await self._orchestrator.get_run(run_id, identity=actor)
            await publish_resolution_evidence(
                self._event_bus,
                actor=actor,
                record=lease.approval,
                request_id=request_id,
            )
            return ApprovalResolveResult(approval=lease.approval, run=run_result)
        if lease.approval.status != "waiting" or lease.state not in {
            "claimed",
            "recovery_pending",
        }:
            raise ApprovalStateConflict(
                f"approval cannot be recovered from {lease.state}: {approval_id}",
                code="approval.resolution_in_progress",
            )
        return await self._continue_claimed_approval(
            actor=actor,
            lease=lease,
            request_id=request_id,
            comment=comment,
        )

    async def _continue_claimed_approval(
        self,
        *,
        actor: IdentityContext,
        lease: ApprovalResolutionLease,
        request_id: str | None,
        comment: str | None,
    ) -> ApprovalResolveResult:
        approval = lease.approval
        grant = ApprovalGrant(
            approval_id=approval.approval_id,
            lease_id=lease.lease_id,
            tenant_id=approval.tenant_id,
            identity_id=str(approval.metadata.get("identity_id") or actor.user_id),
            agent_id=approval.agent_id,
            run_id=approval.run_id,
            action=approval.action,
            resource=approval.resource,
            arguments_hash=str(approval.metadata.get("arguments_hash") or _empty_args_hash()),
        )
        try:
            run_result = None
            if approval.resume_token is not None:
                run_result = await self._orchestrator.resume_run(
                    approval.resume_token,
                    expected_run_id=approval.run_id,
                    identity=actor,
                    approval_grant=grant,
                )
        except AgentExecutionLeaseLost as exc:
            raise ApprovalStateConflict(
                str(exc),
                code="approval.resolution_in_progress",
            ) from exc
        except AgentExecutionUncertain as exc:
            await self._mark_needs_review(approval, lease.lease_id)
            raise ApprovalStateConflict(str(exc), code="approval.execution_needs_review") from exc

        result_state = (
            "failed"
            if run_result is not None and run_result.status == RunStatus.FAILED
            else "completed"
        )
        async with self._storage.uow() as uow:
            record = await uow.approvals.finalize_approved(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=actor.tenant_id,
                lease_id=lease.lease_id,
                resolved_by=actor.user_id,
                result_state=result_state,
                metadata={"comment": redact_secrets(comment)} if comment else None,
            )
            if self._audit is not None:
                await uow.audit_logs.create(
                    build_audit_log(
                        actor=actor,
                        action="approval.approved",
                        resource=record.resource,
                        payload={
                            **approval_evidence(record),
                            "request_id": request_id,
                            "approval_request_id": record.request_id,
                        },
                    )
                )
            await uow.commit()
        await publish_resolution_evidence(
            self._event_bus,
            actor=actor,
            record=record,
            request_id=request_id,
        )
        return ApprovalResolveResult(approval=record, run=run_result)

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

        if not _can_resolve_approval(actor):
            raise PolicyDeniedError("approval resolve permission missing")
        try:
            async with self._storage.uow() as uow:
                record = await uow.approvals.deny_waiting(
                    approval_id=approval_id,
                    run_id=run_id,
                    tenant_id=actor.tenant_id,
                    resolved_by=actor.user_id,
                    metadata={"comment": redact_secrets(comment)} if comment else None,
                )
                if self._audit is not None:
                    await uow.audit_logs.create(
                        build_audit_log(
                            actor=actor,
                            action="approval.denied",
                            resource=record.resource,
                            payload={
                                **approval_evidence(record),
                                "request_id": request_id,
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
            request_id=request_id,
        )
        return ApprovalResolveResult(approval=record, run=run_result)

    async def _reconcile_conflicted_resolution(
        self,
        *,
        actor: IdentityContext,
        run_id: str,
        approval_id: str,
        request_id: str | None,
    ) -> None:
        """公开重试仍返回 409，但先补偿已持久化为 pending 的 evidence。"""

        async with self._storage.uow() as uow:
            record = await uow.approvals.get(approval_id)
            state = await uow.approvals.get_resolution_state(approval_id)
            lease = await uow.approvals.get_resolution(approval_id)
        if record is None or record.run_id != run_id or record.tenant_id != actor.tenant_id:
            return
        if record.status == "denied" and state == "denied_pending":
            await reconcile_denied(
                self._storage,
                self._event_bus,
                self._orchestrator,
                actor=actor,
                record=record,
                request_id=request_id,
            )
            return
        if record.status == "approved" or (
            record.status == "waiting" and state == "recovery_pending"
        ):
            await self.recover_claimed(
                actor=actor,
                run_id=run_id,
                approval_id=approval_id,
                request_id=request_id,
            )
            return
        if (
            record.status != "waiting"
            or state != "claimed"
            or lease is None
            or lease.claimed_at is None
            or _as_utc(lease.claimed_at) > datetime.now(tz=UTC) - self._recovery_lease_timeout
        ):
            return
        expired_before = datetime.now(tz=UTC) - self._recovery_lease_timeout
        async with self._storage.uow() as uow:
            takeover = await uow.approvals.takeover_expired_resolution(
                approval_id=approval_id,
                run_id=run_id,
                tenant_id=actor.tenant_id,
                expired_before=expired_before,
            )
            if takeover is not None:
                await uow.commit()
        if takeover is not None:
            await self._continue_with_recovery_marker(
                actor=actor,
                lease=takeover,
                request_id=request_id,
                comment=None,
            )
            return
        async with self._storage.uow() as uow:
            current = await uow.approvals.get_resolution(approval_id)
        if (
            current is not None
            and current.lease_id == lease.lease_id
            and current.claimed_at is not None
            and _as_utc(current.claimed_at) <= expired_before
        ):
            await self.recover_claimed(
                actor=actor,
                run_id=run_id,
                approval_id=approval_id,
                request_id=request_id,
            )

    async def _mark_needs_review(self, approval: ApprovalRecord, lease_id: str) -> None:
        async with self._storage.uow() as uow:
            await uow.approvals.mark_needs_review(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                lease_id=lease_id,
            )
            await uow.commit()


def _resume_token_value(token: ResumeToken | str | None) -> str | None:
    if token is None:
        return None
    return token.value if isinstance(token, ResumeToken) else token


def _empty_args_hash() -> str:
    from agent_harness.tools import hash_tool_arguments

    return hash_tool_arguments({})


def _as_utc(value: datetime) -> datetime:
    """SQLite 可能返回 naive timestamp；lease 比较统一按 UTC 处理。"""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _can_resolve_approval(actor: IdentityContext) -> bool:
    permissions = set(actor.permissions)
    roles = set(actor.roles)
    return bool(
        {"*", "approval.resolve", "approval.approve", "approval.deny"} & permissions
        or "admin" in roles
    )


def _can_read_approval(actor: IdentityContext) -> bool:
    permissions = set(actor.permissions)
    roles = set(actor.roles)
    return bool({"*", "approval.read", "approval.resolve"} & permissions or "admin" in roles)
