"""HITL approval 创建、审批、拒绝与 run resume。"""

from __future__ import annotations

from typing import Any

from agent_harness.audit import AuditService
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyDeniedError
from agent_harness.runtime import ResumeToken, RunOrchestrator, RunResult
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import ApprovalCreate, ApprovalRecord, SQLAlchemyStorage


class ApprovalStateConflict(RuntimeError):
    """approval 已 resolved 或状态非法时抛出。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "approval.invalid_transition"
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
    ) -> None:
        self._storage = storage
        self._event_bus = event_bus
        self._orchestrator = orchestrator
        self._audit = audit

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
            payload=_approval_evidence(record),
            request_id=request_id,
            trace_id=trace_id,
        )
        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                action="approval.required",
                resource=resource,
                payload=_approval_evidence(record),
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
                payload=_approval_evidence(row),
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
                payload=_approval_evidence(row),
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
        """批准 waiting approval；实际 run 推进由 `_resolve` 统一控制。"""

        return await self._resolve(
            actor=actor,
            run_id=run_id,
            approval_id=approval_id,
            status="approved",
            request_id=request_id,
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
        """拒绝 waiting approval；实际 run 失败写入由 `_resolve` 统一控制。"""

        return await self._resolve(
            actor=actor,
            run_id=run_id,
            approval_id=approval_id,
            status="denied",
            request_id=request_id,
            comment=comment,
        )

    async def _resolve(
        self,
        *,
        actor: IdentityContext,
        run_id: str,
        approval_id: str,
        status: str,
        request_id: str | None,
        comment: str | None,
    ) -> ApprovalResolveResult:
        """按 approve/deny 更新 approval，并只通过 runtime seam 推进 run。"""

        if not _can_resolve_approval(actor):
            raise PolicyDeniedError("approval resolve permission missing")
        async with self._storage.uow() as uow:
            existing = await uow.approvals.get(approval_id)
            if (
                existing is None
                or existing.run_id != run_id
                or existing.tenant_id != actor.tenant_id
            ):
                raise LookupError(f"approval not found: {approval_id}")
            if existing.status != "waiting":
                raise ApprovalStateConflict(f"approval is already {existing.status}: {approval_id}")
            resume_token = existing.resume_token
            agent_id = existing.agent_id
            resource = existing.resource
            trace_id = existing.trace_id

        # 先让 runtime seam 校验 resume token 和状态转换；失败时 approval 仍保持
        # waiting，避免出现“审批已通过但 run 没有推进”的不可恢复状态。
        run_result: RunResult | None = None
        if status == "approved":
            if resume_token is not None:
                run_result = await self._orchestrator.resume_run(
                    resume_token,
                    expected_run_id=run_id,
                    identity=actor,
                )
        else:
            run_result = await self._orchestrator.fail_run(
                run_id,
                reason="approval denied",
                identity=actor,
            )

        async with self._storage.uow() as uow:
            record = await uow.approvals.resolve(
                approval_id=approval_id,
                run_id=run_id,
                tenant_id=actor.tenant_id,
                status=status,
                resolved_by=actor.user_id,
                metadata={"comment": redact_secrets(comment)} if comment else None,
            )
            await uow.commit()

        await self._event_bus.publish(
            tenant_id=actor.tenant_id,
            run_id=run_id,
            agent_id=agent_id,
            user_id=actor.user_id,
            event_type=CanonicalEventType.APPROVAL_RESOLVED,
            payload=_approval_evidence(record),
            request_id=request_id,
            trace_id=trace_id,
        )
        if self._audit is not None:
            await self._audit.record(
                actor=actor,
                action=f"approval.{status}",
                resource=resource,
                payload={
                    **_approval_evidence(record),
                    "request_id": request_id,
                    "approval_request_id": record.request_id,
                },
            )
        return ApprovalResolveResult(approval=record, run=run_result)


def _resume_token_value(token: ResumeToken | str | None) -> str | None:
    if token is None:
        return None
    return token.value if isinstance(token, ResumeToken) else token


def _approval_evidence(record: ApprovalRecord) -> dict[str, Any]:
    return {
        "approval_id": record.approval_id,
        "tenant_id": record.tenant_id,
        "run_id": record.run_id,
        "agent_id": record.agent_id,
        "action": record.action,
        "resource": record.resource,
        "reason": record.reason,
        "status": record.status,
        "requested_by": record.requested_by,
        "resolved_by": record.resolved_by,
        "trace_id": record.trace_id,
        "request_id": record.request_id,
    }


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
