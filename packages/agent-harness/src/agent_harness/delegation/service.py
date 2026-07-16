"""受控 `agent.delegate` application service 与 local/service 共享状态机。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, cast

from agent_harness.contracts import GuardrailDecisionStatus
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.delegation.models import (
    DelegationChildEvidence,
    DelegationRequest,
    DelegationSummary,
    aggregate_delegation_evidence,
    delegation_request_hash,
)
from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.events.sinks.base import EventSinkReplayConflict
from agent_harness.identity import IdentityContext
from agent_harness.models.usage import ModelUsageEvidence
from agent_harness.policy import PolicyCheck, PolicyEvaluation
from agent_harness.registry import AgentDescriptor, AgentRegistry, RegistryLoadError
from agent_harness.runtime import RunOrchestrator, RunStatus
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.delegation_repositories import (
    DelegatedChildRunRecord,
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationRecord,
    DelegationStorageConflict,
    DelegationUsageEvidenceRecord,
)
from agent_harness.storage.event_capacity_repositories import (
    EventCapacityExceeded,
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.storage.repositories import RunRecord, SessionRecord

DelegationMode = Literal["local", "service"]
_TERMINAL = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}


class DelegationOrchestrator(Protocol):
    async def start_run(self, **kwargs: Any) -> Any: ...

    async def submit_run(self, **kwargs: Any) -> Any: ...

    async def resume_run(self, resume_token: str, **kwargs: Any) -> Any: ...


class DelegationPolicy(Protocol):
    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation: ...


class DelegationError(RuntimeError):
    """只暴露合同允许的稳定错误码，不回显内部身份、余额或 provider evidence。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DelegationExecutionResult(HarnessDTO):
    delegation_id: str
    parent_run_id: str
    child_run_id: str
    status: str
    summary: DelegationSummary | None


class DelegationService:
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

    async def reconcile_child(self, child_run_id: str) -> DelegationExecutionResult:
        """worker/local 共用的可重入聚合；缺失或非法 usage 保持两类预约。"""

        async with self._storage.uow() as uow:
            child = await uow.runs.get(child_run_id)
            if child is None:
                raise DelegationError("delegation.execution_failed")
            delegation = await uow.delegations.get_by_child(child_run_id)
            if delegation is None and child.idempotency_key is not None:
                delegation_id = _delegation_id_from_child_key(child.idempotency_key)
                if delegation_id is not None:
                    delegation = await uow.delegations.attach_child(
                        delegation_id=delegation_id,
                        child_run_id=child_run_id,
                    )
                    await uow.commit()
            if delegation is None:
                raise DelegationError("delegation.execution_failed")
            if child.status not in _TERMINAL:
                return DelegationExecutionResult(
                    delegation_id=delegation.id,
                    parent_run_id=delegation.parent_run_id,
                    child_run_id=child.id,
                    status=delegation.status,
                    summary=await self.get_parent_summary(
                        tenant_id=delegation.tenant_id,
                        parent_run_id=delegation.parent_run_id,
                    ),
                )
            rows = await uow.delegations.usage_evidence_for_child(child.id)
            reservation = await uow.delegations.get_reservation(delegation.id)

        needs_review = False
        try:
            evidence = _child_evidence(child=child, rows=rows)
            summary = aggregate_delegation_evidence(
                parent_run_id=delegation.parent_run_id,
                children=[evidence],
            )
        except Exception:  # noqa: BLE001 - 非法 evidence 不得带 raw value 越过此边界
            needs_review = True
            evidence = _unknown_child_evidence(child)
            summary = aggregate_delegation_evidence(
                parent_run_id=delegation.parent_run_id,
                children=[evidence],
            )
        if summary.budget_status == "incomplete":
            needs_review = True
        if not needs_review and _budget_exceeded(summary, reservation):
            summary = summary.model_copy(update={"budget_status": "exceeded"})

        try:
            async with self._storage.uow() as uow:
                await uow.delegations.save_aggregation(
                    delegation_id=delegation.id,
                    # API Contract 5.30 把四个 unknown 数值定义为显式 null；
                    # storage 不能使用 HarnessDTO.to_payload() 的 exclude_none 语义。
                    summary=summary.model_dump(mode="json"),
                    evidence_refs=evidence.usage_evidence_refs + evidence.trace_refs,
                    needs_review=needs_review,
                )
                refreshed = await uow.delegations.get(delegation.id)
                await uow.commit()
        except DelegationStorageConflict as exc:
            raise DelegationError(exc.code) from exc
        if refreshed is None:
            raise DelegationError("delegation.execution_failed")
        if not needs_review:
            try:
                execution_identity = IdentityContext.model_validate(refreshed.identity)
            except ValueError as exc:
                raise DelegationError("delegation.execution_failed") from exc
            # service worker 可能在 parent submit 返回前完成 child；final 前由
            # worker 路径幂等补齐 child-created，不能依赖 parent 调用栈时序。
            await self._publish_child_created(
                delegation=refreshed,
                identity=execution_identity,
            )
            await self._publish_final(delegation=refreshed, summary=summary)
            await self._resume_parent_terminal_if_ready(refreshed)
        parent_summary = await self.get_parent_summary(
            tenant_id=delegation.tenant_id,
            parent_run_id=delegation.parent_run_id,
        )
        return DelegationExecutionResult(
            delegation_id=delegation.id,
            parent_run_id=delegation.parent_run_id,
            child_run_id=child.id,
            status=refreshed.status,
            summary=parent_summary,
        )

    async def reconcile_child_if_delegated(self, run_id: str) -> bool:
        """worker 可对任意 run 调用；只有受控 child 才进入聚合恢复。"""

        async with self._storage.uow() as uow:
            run = await uow.runs.get(run_id)
            delegation = await uow.delegations.get_by_child(run_id)
        if run is None or run.parent_run_id is None:
            return False
        if delegation is None and (
            run.idempotency_key is None
            or _delegation_id_from_child_key(run.idempotency_key) is None
        ):
            return False
        await self.reconcile_child(run_id)
        return True

    async def get_parent_summary(
        self,
        *,
        tenant_id: str,
        parent_run_id: str,
    ) -> DelegationSummary | None:
        async with self._storage.uow() as uow:
            projections = await uow.delegations.list_summary_projection_for_parent(
                tenant_id=tenant_id,
                parent_run_id=parent_run_id,
            )
            usage_by_child = await uow.delegations.usage_evidence_for_children(
                child_run_ids=[
                    projection.delegation.child_run_id
                    for projection in projections
                    if projection.delegation.child_run_id is not None
                    and projection.aggregate is not None
                ]
            )
            children: list[DelegationChildEvidence] = []
            exceeded = False
            for projection in projections:
                delegation = projection.delegation
                child_run_id = delegation.child_run_id
                if child_run_id is None:
                    if projection.aggregate is not None:
                        raise DelegationError("delegation.execution_failed")
                    continue
                child = projection.child
                reservation = projection.reservation
                aggregate = projection.aggregate
                if (
                    child is None
                    or reservation is None
                    or delegation.tenant_id != tenant_id
                    or delegation.parent_run_id != parent_run_id
                    or child.id != child_run_id
                    or child.tenant_id != tenant_id
                    or child.parent_run_id != parent_run_id
                    or child.agent_id != delegation.target_agent_id
                    or child.trace_id != delegation.trace_id
                    or child.idempotency_key != f"delegation:{delegation.id}"
                    or reservation.delegation_id != delegation.id
                    or reservation.tenant_id != tenant_id
                    or reservation.parent_run_id != parent_run_id
                ):
                    raise DelegationError("delegation.execution_failed")
                try:
                    RunStatus(child.status)
                except ValueError as exc:
                    raise DelegationError("delegation.execution_failed") from exc
                if aggregate is not None:
                    try:
                        summary = DelegationSummary.model_validate(aggregate.summary)
                    except ValueError as exc:
                        raise DelegationError("delegation.execution_failed") from exc
                    try:
                        durable_evidence = _child_evidence(
                            child=child,
                            rows=usage_by_child.get(child_run_id, []),
                        )
                    except ValueError as exc:
                        if (
                            aggregate.status != "needs_review"
                            or reservation.state != "needs_review"
                        ):
                            raise DelegationError("delegation.execution_failed") from exc
                        durable_evidence = _unknown_child_evidence(child)
                    durable_summary = aggregate_delegation_evidence(
                        parent_run_id=parent_run_id,
                        children=[durable_evidence],
                    )
                    if durable_summary.budget_status != "incomplete" and _budget_exceeded(
                        durable_summary, reservation
                    ):
                        durable_summary = durable_summary.model_copy(
                            update={"budget_status": "exceeded"}
                        )
                    # child status 的唯一真相源是 durable run；aggregate 可能保留较早状态，
                    # 其余公开字段则必须和当前 durable evidence 完全一致。
                    normalized_summary = summary.model_copy(
                        update={
                            "children": [
                                summary.children[0].model_copy(update={"status": child.status})
                            ]
                            if len(summary.children) == 1
                            else summary.children
                        }
                    )
                    if (
                        aggregate.delegation_id != delegation.id
                        or aggregate.tenant_id != tenant_id
                        or aggregate.parent_run_id != parent_run_id
                        or aggregate.child_run_id != child_run_id
                        or summary.parent_run_id != parent_run_id
                        or len(summary.children) != 1
                        or summary.children[0].run_id != child_run_id
                        or summary.children[0].agent_id != delegation.target_agent_id
                        or normalized_summary != durable_summary
                        or aggregate.evidence_refs
                        != durable_evidence.usage_evidence_refs + durable_evidence.trace_refs
                        or not _aggregate_reservation_consistent(
                            summary=durable_summary,
                            aggregate_status=aggregate.status,
                            reservation=reservation,
                        )
                    ):
                        raise DelegationError("delegation.execution_failed")
                    exceeded = exceeded or durable_summary.budget_status == "exceeded"
                    children.append(durable_evidence)
                    continue

                if reservation.state != "reserved":
                    raise DelegationError("delegation.execution_failed")
                # child relation 已 durable，但 terminal 聚合尚未结算时，RUN-002 必须
                # 暴露其身份与状态，同时把所有可继续增长的 usage 维度保持 unknown。
                children.append(_unknown_child_evidence(child))
        if not children:
            return None
        return aggregate_delegation_evidence(
            parent_run_id=parent_run_id,
            children=children,
            budget_exceeded=exceeded,
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


def _required_child_id(delegation: DelegationRecord) -> str:
    if delegation.child_run_id is None:
        raise DelegationError("delegation.execution_failed")
    return delegation.child_run_id


def _delegation_id_from_child_key(value: str) -> str | None:
    prefix = "delegation:"
    return value[len(prefix) :] if value.startswith(prefix) and len(value) > len(prefix) else None


def _published_child_payload(
    *,
    delegation: DelegationRecord,
    result: Mapping[str, object],
) -> dict[str, Any]:
    """从未再更新的 published outbox row 恢复 child-created 原始语义。"""

    child_run_id = result.get("child_run_id")
    status = result.get("status")
    if (
        result.get("delegation_id") != delegation.id
        or result.get("parent_run_id") != delegation.parent_run_id
        or child_run_id != delegation.child_run_id
        or result.get("source_agent_id") != delegation.source_agent_id
        or result.get("target_agent_id") != delegation.target_agent_id
        or result.get("trace_id") != delegation.trace_id
        or not isinstance(child_run_id, str)
        or status not in {"queued", "running", "completed", "failed"}
    ):
        raise DelegationError("delegation.execution_failed")
    return {"status": status, "child_run_id": child_run_id}


def _child_evidence(
    *,
    child: RunRecord | DelegatedChildRunRecord,
    rows: list[DelegationUsageEvidenceRecord],
) -> DelegationChildEvidence:
    evidence: list[ModelUsageEvidence] = []
    has_pending = False
    for row in rows:
        try:
            operation_kind = EvidenceOperationKind(row.operation_kind)
        except ValueError as exc:
            raise ValueError("delegation usage evidence operation mismatch") from exc
        if operation_kind not in {
            EvidenceOperationKind.MODEL_USAGE,
            EvidenceOperationKind.EMBEDDING_USAGE,
        } or row.reserved_event_count != operation_event_capacity(operation_kind):
            raise ValueError("delegation usage evidence reservation mismatch")
        if row.state != "published":
            has_pending = True
            continue
        result = row.result
        if not isinstance(result, Mapping) or "evidence" not in result:
            raise ValueError("published delegation usage evidence is incomplete")
        evidence.append(
            ModelUsageEvidence.model_validate(cast(Mapping[str, object], result)["evidence"])
        )
    if not evidence:
        return _unknown_child_evidence(child)
    if any(
        item.run_id != child.id
        or item.tenant_id != child.tenant_id
        or item.agent_id != child.agent_id
        or item.trace_id != child.trace_id
        for item in evidence
    ):
        raise ValueError("delegation usage evidence scope mismatch")
    input_values = [item.input_tokens for item in evidence]
    output_values = [item.output_tokens for item in evidence]
    input_complete = not has_pending and all(value is not None for value in input_values)
    output_complete = not has_pending and all(value is not None for value in output_values)
    all_cost = not has_pending and all(item.cost_status != "unavailable" for item in evidence)
    return DelegationChildEvidence(
        run_id=child.id,
        agent_id=child.agent_id,
        status=child.status,
        input_tokens=_known_sum(input_values),
        output_tokens=_known_sum(output_values),
        input_tokens_complete=input_complete,
        output_tokens_complete=output_complete,
        cost_usd=sum(item.cost_usd or 0 for item in evidence) if all_cost else None,
        cost_status=(
            "estimated"
            if all_cost and any(item.cost_status == "estimated" for item in evidence)
            else "reported"
            if all_cost
            else "unavailable"
        ),
        latency_ms=None if has_pending else sum(item.latency_ms for item in evidence),
        usage_evidence_refs=[row.event_id for row in rows],
        trace_refs=[child.trace_id],
    )


def _unknown_child_evidence(
    child: RunRecord | DelegatedChildRunRecord,
) -> DelegationChildEvidence:
    return DelegationChildEvidence(
        run_id=child.id,
        agent_id=child.agent_id,
        status=child.status,
        input_tokens=None,
        output_tokens=None,
        input_tokens_complete=False,
        output_tokens_complete=False,
        cost_usd=None,
        cost_status="unavailable",
        latency_ms=None,
        usage_evidence_refs=[],
        trace_refs=[child.trace_id],
    )


def _known_sum(values: list[int | None]) -> int | None:
    known = [value for value in values if value is not None]
    return sum(known) if known else None


def _budget_exceeded(summary: DelegationSummary, reservation: Any) -> bool:
    tokens = (summary.input_tokens or 0) + (summary.output_tokens or 0)
    if tokens > reservation.reserved_tokens:
        return True
    return bool(
        summary.cost_usd is not None
        and reservation.reserved_cost_usd is not None
        and summary.cost_usd > reservation.reserved_cost_usd
    )


def _aggregate_reservation_consistent(
    *,
    summary: DelegationSummary,
    aggregate_status: str,
    reservation: Any,
) -> bool:
    """聚合状态与预算结算必须来自同一次 durable 状态转换。"""

    if aggregate_status == "needs_review":
        return reservation.state == "needs_review" and summary.budget_status == "incomplete"
    if aggregate_status != "complete" or reservation.state != "settled":
        return False
    if summary.budget_status == "incomplete":
        return False
    return (
        reservation.settled_input_tokens == summary.input_tokens
        and reservation.settled_output_tokens == summary.output_tokens
        and reservation.settled_cost_usd == summary.cost_usd
    )


__all__ = [
    "DelegationError",
    "DelegationExecutionResult",
    "DelegationMode",
    "DelegationService",
]
