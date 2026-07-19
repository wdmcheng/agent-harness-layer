"""Approval continuation、evidence 补偿与过期 lease 恢复。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_harness.approvals._contracts import (
    ApprovalResolveResult,
    ApprovalStateConflict,
    as_utc,
    can_resolve_approval,
    empty_args_hash,
)
from agent_harness.approvals.reconciliation import (
    approval_evidence,
    complete_approval_evidence_group,
    mark_recovery_pending,
    publish_resolution_evidence,
    reconcile_denied,
    resolved_approval_record,
    stage_approval_evidence_group,
)
from agent_harness.audit import AuditService, build_audit_log
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyDeniedError
from agent_harness.runtime.executor import (
    AgentExecutionLeaseLost,
    AgentExecutionUncertain,
    ApprovalGrant,
)
from agent_harness.runtime.orchestrator import RunOrchestrator
from agent_harness.runtime.state import RunStatus
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import ApprovalRecord, SQLAlchemyStorage
from agent_harness.storage.access_repositories import ApprovalResolutionLease
from agent_harness.storage.event_capacity_repositories import EvidenceOperationKind


class ApprovalContinuationMixin:
    """承载 local/worker 共用的 continuation 与恢复语义。"""

    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _orchestrator: RunOrchestrator
    _audit: AuditService | None
    _recovery_lease_timeout: timedelta

    async def _continue_with_recovery_marker(
        self,
        *,
        actor: IdentityContext,
        lease: ApprovalResolutionLease,
        comment: str | None,
    ) -> ApprovalResolveResult:
        """尝试续跑已 claim 的审批；未知异常先标为 recovery_pending 再向上抛出。

        状态冲突保持原样，让调用方返回稳定 409；其他异常可能发生在外部 executor
        之后，必须保留 durable 恢复标记，不能把 lease 静默当作未执行。
        """

        try:
            return await self._continue_claimed_approval(
                actor=actor,
                lease=lease,
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

        # service worker 的 reviewer 身份来自已通过 APR-002 的 durable queue
        # fingerprint，故意不携带可复用权限；它只能恢复同 tenant/run/approval 的
        # 既有 lease，不能借此创建新的 resolution。
        if not can_resolve_approval(actor) and actor.auth_method != "service-approval":
            raise PolicyDeniedError("approval resolve permission missing")
        async with self._storage.uow() as uow:
            lease = await uow.approvals.get_resolution(approval_id)
        if (
            lease is None
            or lease.approval.run_id != run_id
            or lease.approval.tenant_id != actor.tenant_id
            or (
                lease.approval.resolved_by is not None
                and lease.approval.resolved_by != actor.user_id
            )
        ):
            raise LookupError(f"approval resolution not found: {approval_id}")
        if lease.approval.status in {"waiting", "approved"} and lease.state in {
            "completed",
            "failed",
        }:
            evidence_record = resolved_approval_record(lease.approval, status="approved")
            async with self._storage.uow() as uow:
                persisted_run = await uow.runs.get(run_id)
                if persisted_run is None:
                    raise LookupError(f"run not found: {run_id}")
                await stage_approval_evidence_group(
                    uow,
                    record=lease.approval,
                    resolution_status="approved",
                    run_status=RunStatus(persisted_run.status),
                )
                await uow.commit()
            await publish_resolution_evidence(
                self._event_bus,
                actor=actor,
                record=evidence_record,
                request_id=lease.resolution_request_id,
            )
            run_result = await self._orchestrator.get_run(run_id, identity=actor)
            await complete_approval_evidence_group(
                self._storage,
                self._event_bus,
                record=lease.approval,
            )
            record = lease.approval
            if record.status == "waiting":
                async with self._storage.uow() as uow:
                    record = await uow.approvals.mark_approved_evidence_complete(
                        approval_id=record.approval_id,
                        run_id=record.run_id,
                        tenant_id=record.tenant_id,
                        lease_id=lease.lease_id,
                    )
                    await uow.commit()
            return ApprovalResolveResult(approval=record, run=run_result)
        if lease.approval.status == "waiting" and lease.state == "recovery_pending":
            async with self._storage.uow() as uow:
                run = await uow.runs.get(run_id)
            if run is not None and RunStatus(run.status) in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
            }:
                evidence_record = resolved_approval_record(
                    lease.approval,
                    status="approved",
                )
                async with self._storage.uow() as uow:
                    await stage_approval_evidence_group(
                        uow,
                        record=lease.approval,
                        resolution_status="approved",
                        run_status=RunStatus(run.status),
                    )
                    await uow.commit()
                if self._audit is not None:
                    async with self._storage.uow() as uow:
                        audits = await uow.audit_logs.list_for_tenant(actor.tenant_id)
                        if not any(
                            item.action == "approval.approved"
                            and isinstance(item.payload.get("evidence"), dict)
                            and item.payload["evidence"].get("approval_id")
                            == lease.approval.approval_id
                            for item in audits
                        ):
                            await uow.audit_logs.create(
                                build_audit_log(
                                    actor=actor,
                                    action="approval.approved",
                                    resource=lease.approval.resource,
                                    payload={
                                        **approval_evidence(evidence_record),
                                        "request_id": lease.resolution_request_id,
                                        "approval_request_id": lease.approval.request_id,
                                    },
                                )
                            )
                            await uow.commit()
                await publish_resolution_evidence(
                    self._event_bus,
                    actor=actor,
                    record=evidence_record,
                    request_id=lease.resolution_request_id,
                )
                run_result = await self._orchestrator.get_run(run_id, identity=actor)
                await complete_approval_evidence_group(
                    self._storage,
                    self._event_bus,
                    record=lease.approval,
                )
                async with self._storage.uow() as uow:
                    record = await uow.approvals.mark_approved_evidence_complete(
                        approval_id=lease.approval.approval_id,
                        run_id=run_id,
                        tenant_id=actor.tenant_id,
                        lease_id=lease.lease_id,
                    )
                    await uow.commit()
                return ApprovalResolveResult(approval=record, run=run_result)
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
            comment=comment,
        )

    async def _continue_claimed_approval(
        self,
        *,
        actor: IdentityContext,
        lease: ApprovalResolutionLease,
        comment: str | None,
    ) -> ApprovalResolveResult:
        """以持久化 lease 构造 grant，续跑 run 并按有序 evidence 完成审批决议。

        grant 全部字段取自审批记录，业务调用方不能自选。若 run 因 delegation 等待，
        则先发布 resolution 再检查 child 收口边沿，避免事件流被提前关闭或遗漏恢复。
        """

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
            arguments_hash=str(approval.metadata.get("arguments_hash") or empty_args_hash()),
        )
        try:
            run_result = None
            if approval.resume_token is not None:
                run_result = await self._orchestrator.resume_run(
                    approval.resume_token,
                    expected_run_id=approval.run_id,
                    identity=actor,
                    approval_grant=grant,
                    defer_terminal=True,
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
        run_status = (
            None
            if run_result is not None and run_result.status == RunStatus.WAITING
            else RunStatus.FAILED
            if result_state == "failed"
            else RunStatus.COMPLETED
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
            evidence_record = resolved_approval_record(record, status="approved")
            await stage_approval_evidence_group(
                uow,
                record=record,
                resolution_status="approved",
                # approval 已确定执行，但 delegation child 尚未收口时只冻结
                # resolution；terminal 状态由私有 checkpoint 恢复后再补齐。
                run_status=run_status,
            )
            if self._audit is not None:
                await uow.audit_logs.create(
                    build_audit_log(
                        actor=actor,
                        action="approval.approved",
                        resource=record.resource,
                        payload={
                            **approval_evidence(evidence_record),
                            "request_id": lease.resolution_request_id,
                            "approval_request_id": record.request_id,
                            "comment": redact_secrets(comment) if comment else None,
                        },
                    )
                )
            await uow.commit()
        await publish_resolution_evidence(
            self._event_bus,
            actor=actor,
            record=evidence_record,
            request_id=lease.resolution_request_id,
        )
        if (
            run_result is not None
            and run_result.status == RunStatus.WAITING
            and run_result.resume_token is not None
        ):
            async with self._storage.uow() as uow:
                delegation_pending = await uow.evidence_outbox.has_pending_operation(
                    run_id=approval.run_id,
                    operation_kind=EvidenceOperationKind.DELEGATION,
                )
            if not delegation_pending:
                # child final 可能在 approval resolution 发布前已经到达。此时 child
                # 路径会刻意不关闭 stream；resolution 发布方负责补上恢复边沿。
                run_result = await self._orchestrator.resume_run(
                    run_result.resume_token,
                    expected_run_id=approval.run_id,
                    identity=actor,
                )
                async with self._storage.uow() as uow:
                    recovered_record = await uow.approvals.get(approval.approval_id)
                if recovered_record is None:
                    raise LookupError(f"approval not found: {approval.approval_id}")
                if recovered_record.status == "approved":
                    return ApprovalResolveResult(
                        approval=recovered_record,
                        run=run_result,
                    )
        if run_result is not None and run_result.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            run_result = await self._orchestrator.get_run(
                approval.run_id,
                identity=actor,
            )
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
                    lease_id=lease.lease_id,
                )
                await uow.commit()
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
            resolution_request_id = await uow.approvals.get_resolution_request_id(approval_id)
            lease = await uow.approvals.get_resolution(approval_id)
        if record is None or record.run_id != run_id or record.tenant_id != actor.tenant_id:
            return
        if record.status == "waiting" and state == "denied_pending":
            if not resolution_request_id:
                raise RuntimeError(f"approval resolution request id missing: {approval_id}")
            await reconcile_denied(
                self._storage,
                self._event_bus,
                self._orchestrator,
                actor=actor,
                record=record,
                resolution_request_id=resolution_request_id,
            )
            return
        if record.status == "approved" or (
            record.status == "waiting" and state in {"recovery_pending", "completed", "failed"}
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
            or as_utc(lease.claimed_at) > datetime.now(tz=UTC) - self._recovery_lease_timeout
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
                comment=None,
            )
            return
        async with self._storage.uow() as uow:
            current = await uow.approvals.get_resolution(approval_id)
        if (
            current is not None
            and current.lease_id == lease.lease_id
            and current.claimed_at is not None
            and as_utc(current.claimed_at) <= expired_before
        ):
            await self.recover_claimed(
                actor=actor,
                run_id=run_id,
                approval_id=approval_id,
                request_id=request_id,
            )

    async def _mark_needs_review(self, approval: ApprovalRecord, lease_id: str) -> None:
        """将无法证明结果的审批 lease 升级为人工复核，禁止自动再次执行工具。"""

        async with self._storage.uow() as uow:
            await uow.approvals.mark_needs_review(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                lease_id=lease_id,
            )
            await uow.commit()
