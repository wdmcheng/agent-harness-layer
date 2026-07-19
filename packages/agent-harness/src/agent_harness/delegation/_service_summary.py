"""Delegation child 聚合与 parent relation-first summary。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_harness.delegation._service_evidence import (
    aggregate_reservation_consistent as _aggregate_reservation_consistent,
)
from agent_harness.delegation._service_evidence import (
    budget_exceeded as _budget_exceeded,
)
from agent_harness.delegation._service_evidence import (
    child_evidence as _child_evidence,
)
from agent_harness.delegation._service_evidence import (
    delegation_id_from_child_key as _delegation_id_from_child_key,
)
from agent_harness.delegation._service_evidence import (
    unknown_child_evidence as _unknown_child_evidence,
)
from agent_harness.delegation._service_types import (
    TERMINAL_RUN_STATUSES as _TERMINAL,
)
from agent_harness.delegation._service_types import (
    DelegationError,
    DelegationExecutionResult,
)
from agent_harness.delegation.models import (
    DelegationChildEvidence,
    DelegationSummary,
    aggregate_delegation_evidence,
)
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunStatus
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.delegation_repositories import (
    DelegationRecord,
    DelegationStorageConflict,
)


class DelegationSummaryMixin:
    """只使用同一 service 的 storage/publish seam，不另建状态真相源。"""

    _storage: SQLAlchemyStorage

    if TYPE_CHECKING:

        async def _publish_child_created(
            self, *, delegation: DelegationRecord, identity: IdentityContext
        ) -> None:
            """确保 child 绑定事件已按有序 outbox 规则发布。"""

            ...

        async def _publish_final(
            self, *, delegation: DelegationRecord, summary: DelegationSummary
        ) -> None:
            """确保聚合终态事件已在 child 事件之后发布。"""

            ...

        async def _resume_parent_terminal_if_ready(self, delegation: DelegationRecord) -> None:
            """在 delegation 终态证据齐备后恢复父 run 的终态推进。"""

            ...

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
            shared_ledger = await uow.shared_budget.get_ledger(
                delegation.tenant_id, delegation.parent_run_id
            )
            cost_enabled = shared_ledger is None or shared_ledger.cost_limit is not None

        needs_review = False
        try:
            evidence = _child_evidence(child=child, rows=rows)
            summary = aggregate_delegation_evidence(
                parent_run_id=delegation.parent_run_id,
                children=[evidence],
                cost_enabled=cost_enabled,
            )
        except Exception:  # noqa: BLE001 - 非法 evidence 不得带 raw value 越过此边界
            needs_review = True
            evidence = _unknown_child_evidence(child)
            summary = aggregate_delegation_evidence(
                parent_run_id=delegation.parent_run_id,
                children=[evidence],
                cost_enabled=cost_enabled,
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
        """按 durable delegation relation 汇总 parent 的所有 child，不依赖内存缓存。

        已结算 child 必须同时满足聚合、预约和 outbox 一致性；仅关联但尚未结算的
        terminal child 以未知用量保留。发现矛盾记录即失败，防止 API 以不完整摘要
        掩盖账本或证据损坏。
        """

        async with self._storage.uow() as uow:
            shared_ledger = await uow.shared_budget.get_ledger(tenant_id, parent_run_id)
            cost_enabled = shared_ledger is None or shared_ledger.cost_limit is not None
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
                        cost_enabled=cost_enabled,
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
                            cost_enabled=cost_enabled,
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
            cost_enabled=cost_enabled,
        )


__all__ = ["DelegationSummaryMixin"]
