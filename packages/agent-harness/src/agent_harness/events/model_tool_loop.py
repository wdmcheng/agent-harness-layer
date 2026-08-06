"""模型工具循环复用CanonicalEvent目录的有序producer。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from agent_harness.events._model_tool_loop_event_recovery import (
    ModelToolLoopEventPublishPending,
    ModelToolLoopEventRecoveryError,
    _DurableModelToolLoopEventIntent,
    recover_group,
    recover_pending_for_run,
)
from agent_harness.events.bus import EventBus
from agent_harness.events.types import CanonicalEventType
from agent_harness.models.structured import structured_digest
from agent_harness.models.tool_intent import ToolIntent
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage, SQLAlchemyUnitOfWork
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    operation_event_capacity,
)

if TYPE_CHECKING:
    from agent_harness.context import ContextAssemblyResult, ContextFragment
    from agent_harness.tools import (
        ResolvedToolIntent,
        ToolCallResult,
        ToolRuntimeContext,
    )


@dataclass(frozen=True)
class ModelToolLoopEventStep:
    """producer私有的冻结group/event/correlation句柄。"""

    kind: Literal["tool", "context"]
    group_id: str
    started_event_id: str
    final_event_id: str
    context: UsageEvidenceContext
    identity_id: str
    correlation: dict[str, object]
    unused_reservation: int


class ModelToolLoopEventProducer:
    """在对应副作用前预约容量，并只发布去敏refs/digests。"""

    def __init__(self, *, storage: SQLAlchemyStorage, event_bus: EventBus) -> None:
        """复用既有UoW、event capacity/outbox与EventBus。"""

        self._storage = storage
        self._event_bus = event_bus

    async def begin_tool(
        self,
        *,
        context: ToolRuntimeContext,
        intent: ToolIntent,
        resolved: ResolvedToolIntent,
        capacity_pre_reserved: bool = False,
    ) -> ModelToolLoopEventStep:
        """Policy/claim通过后、handler前耐久预约并发布tool started。"""

        step = await self.reserve_tool(
            context=context,
            intent=intent,
            capacity_pre_reserved=capacity_pre_reserved,
        )
        await self.start_tool(step=step, resolved=resolved)
        return step

    async def reserve_tool(
        self,
        *,
        context: ToolRuntimeContext,
        intent: ToolIntent,
        capacity_pre_reserved: bool = False,
    ) -> ModelToolLoopEventStep:
        """在execution claim前只预约容量与identity，不发布started事实。"""

        step = self._tool_step(context=context, intent=intent)
        await self._reserve_group(
            step,
            operation_kind=EvidenceOperationKind.TOOL_INVOCATION,
            capacity_pre_reserved=capacity_pre_reserved,
        )
        return step

    async def prepare_tool_claim(
        self,
        *,
        context: ToolRuntimeContext,
        intent: ToolIntent,
    ) -> ModelToolLoopEventStep:
        """构造稳定事件identity，并在owner UoW开始前完成本地容量前缀对账。

        该入口不写预约；Registry 必须把返回的 step 交给
        ``reserve_tool_in_owner_uow``，与 durable tool claim 在同一事务提交。
        """

        step = self._tool_step(context=context, intent=intent)
        await self._event_bus.reconcile_local_capacity(run_id=step.context.run_id)
        return step

    async def reserve_tool_in_owner_uow(
        self,
        *,
        step: ModelToolLoopEventStep,
        uow: SQLAlchemyUnitOfWork,
    ) -> None:
        """在 claim owner 的同一 UoW 预约容量与两项稳定 event identity。"""

        if type(step) is not ModelToolLoopEventStep or step.kind != "tool":
            raise ValueError("tool event step is invalid")
        await self._stage_reserved_group(
            uow,
            step,
            operation_kind=EvidenceOperationKind.TOOL_INVOCATION,
            capacity_pre_reserved=False,
        )

    async def start_tool(
        self,
        *,
        step: ModelToolLoopEventStep,
        resolved: ResolvedToolIntent,
    ) -> None:
        """claim取得执行权或exact终态后，才发布唯一tool started事实。"""

        from agent_harness.tools import ResolvedToolIntent

        if type(step) is not ModelToolLoopEventStep or step.kind != "tool":
            raise ValueError("tool event step is invalid")
        if type(resolved) is not ResolvedToolIntent:
            raise ValueError("tool started event requires an exact resolved intent")
        if (
            step.correlation.get("loop_id") != resolved.loop_id
            or step.correlation.get("turn_ordinal") != resolved.turn_ordinal
            or step.correlation.get("tool_call_id") != resolved.tool_call_id
            or step.correlation.get("model_usage_call_id") != resolved.model_usage_call_id
            or step.correlation.get("catalog_digest") != resolved.catalog_digest
        ):
            raise ValueError("tool event identity does not match resolved intent")
        payload: dict[str, object] = {
            "schema_version": "model-tool-loop-event-v1",
            "correlation": step.correlation,
            "tool": {
                "name": resolved.tool_name,
                "action": resolved.action,
                "resource": resolved.resource,
            },
        }
        await self._publish(
            step,
            event_id=step.started_event_id,
            event_type=CanonicalEventType.TOOL_CALL_STARTED,
            payload=payload,
        )

    async def finish_tool(
        self,
        *,
        step: ModelToolLoopEventStep,
        result: ToolCallResult,
    ) -> None:
        """工具结果耐久后发布唯一completed/failed并释放未使用最大槽位。"""

        if type(step) is not ModelToolLoopEventStep or step.kind != "tool":
            raise ValueError("tool event step is invalid")
        from agent_harness.tools import ToolCallResult

        if type(result) is not ToolCallResult:
            raise ValueError("tool final event requires an exact result")
        event_type = (
            CanonicalEventType.TOOL_CALL_COMPLETED
            if result.status == "completed"
            else CanonicalEventType.TOOL_CALL_FAILED
        )
        payload: dict[str, object] = {
            "schema_version": "model-tool-loop-event-v1",
            "correlation": step.correlation,
            "tool": {
                "name": result.tool_name,
                "status": result.status,
                "source_ref": result.source_ref,
                "artifact_ref": result.artifact_ref,
                "truncated": bool(result.truncation.get("truncated", False)),
                "error_code": result.error.code.value if result.error is not None else None,
            },
        }
        await self._publish(
            step,
            event_id=step.final_event_id,
            event_type=event_type,
            payload=payload,
            release_after=step.unused_reservation,
        )

    async def begin_context(
        self,
        *,
        context: UsageEvidenceContext,
        identity_id: str,
        intent: ToolIntent,
        fragment: ContextFragment,
    ) -> ModelToolLoopEventStep:
        """ContextAssembler前冻结两项事件和唯一untrusted输入ref。"""

        correlation = self._correlation(intent)
        step = self._step(
            kind="context",
            context=context,
            identity_id=identity_id,
            correlation=correlation,
            unused_reservation=0,
        )
        await self._reserve_group(
            step,
            operation_kind=EvidenceOperationKind.CONTEXT_ASSEMBLY,
            capacity_pre_reserved=False,
        )
        await self._publish(
            step,
            event_id=step.started_event_id,
            event_type=CanonicalEventType.CONTEXT_ASSEMBLY_STARTED,
            payload={
                "schema_version": "model-tool-loop-event-v1",
                "correlation": correlation,
                "context": {
                    "input_refs": [fragment.source_ref],
                    "artifact_ref": fragment.artifact_ref,
                    "trust_level": fragment.trust_level,
                },
            },
        )
        return step

    async def finish_context(
        self,
        *,
        step: ModelToolLoopEventStep,
        result: ContextAssemblyResult,
    ) -> None:
        """只发布assembly refs、摘要和文本digest，不发布assembled text。"""

        if type(step) is not ModelToolLoopEventStep or step.kind != "context":
            raise ValueError("context event step is invalid")
        await self._publish(
            step,
            event_id=step.final_event_id,
            event_type=CanonicalEventType.CONTEXT_ASSEMBLY_COMPLETED,
            payload={
                "schema_version": "model-tool-loop-event-v1",
                "correlation": step.correlation,
                "context": {
                    "assembly_id": result.id,
                    "output_ref": result.output_ref,
                    "input_refs": result.input_refs,
                    "assembled_text_digest": structured_digest(
                        {"assembled_text": result.assembled_text}
                    ),
                    "truncation": result.truncation_summary,
                },
            },
        )

    async def recover_group(self, *, group_id: str) -> int:
        """只委派耐久 event 恢复，不据此重放任何业务副作用。"""

        return await recover_group(
            storage=self._storage,
            event_bus=self._event_bus,
            group_id=group_id,
        )

    async def recover_pending_for_run(self, *, run_id: str, loop_id: str) -> int:
        """供bound runtime/startup重放消费当前run的exact pending event。"""

        return await recover_pending_for_run(
            storage=self._storage,
            event_bus=self._event_bus,
            run_id=run_id,
            loop_id=loop_id,
        )

    @staticmethod
    def _tool_step(
        *,
        context: ToolRuntimeContext,
        intent: ToolIntent,
    ) -> ModelToolLoopEventStep:
        """从公共运行身份构造可在 owner UoW 中原子预约的工具事件句柄。"""

        if type(intent) is not ToolIntent:
            raise ValueError("tool event requires an exact intent")
        if context.run_id is None or context.trace_id is None:
            raise ValueError("tool events require bound run and trace identity")
        return ModelToolLoopEventProducer._step(
            kind="tool",
            context=UsageEvidenceContext(
                tenant_id=context.actor.tenant_id,
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            ),
            identity_id=context.actor.user_id,
            correlation=ModelToolLoopEventProducer._correlation(intent),
            unused_reservation=1,
        )

    @staticmethod
    def _correlation(intent: ToolIntent) -> dict[str, object]:
        """事件统一使用冻结loop/turn/tool/usage/catalog坐标。"""

        return {
            "loop_id": intent.loop_id,
            "turn_ordinal": intent.turn_ordinal,
            "tool_call_id": intent.tool_call_id,
            "model_usage_call_id": intent.model_usage_call_id,
            "catalog_digest": intent.catalog_digest,
        }

    @staticmethod
    def _step(
        *,
        kind: Literal["tool", "context"],
        context: UsageEvidenceContext,
        identity_id: str,
        correlation: dict[str, object],
        unused_reservation: int,
    ) -> ModelToolLoopEventStep:
        digest = structured_digest(
            {
                "schema_version": "model-tool-loop-event-group-v1",
                "kind": kind,
                "context": context.to_payload(),
                "identity_id": identity_id,
                "correlation": correlation,
            }
        )
        return ModelToolLoopEventStep(
            kind=kind,
            group_id=f"model-tool-loop:{digest}",
            started_event_id=f"model-tool-loop:{digest}:started",
            final_event_id=f"model-tool-loop:{digest}:final",
            context=context.model_copy(deep=True),
            identity_id=identity_id,
            correlation=dict(correlation),
            unused_reservation=unused_reservation,
        )

    async def _reserve_group(
        self,
        step: ModelToolLoopEventStep,
        *,
        operation_kind: EvidenceOperationKind,
        capacity_pre_reserved: bool,
    ) -> None:
        """同一UoW冻结容量与两个不可覆盖event identity。"""

        await self._event_bus.reconcile_local_capacity(run_id=step.context.run_id)
        async with self._storage.uow() as uow:
            await self._stage_reserved_group(
                uow,
                step,
                operation_kind=operation_kind,
                capacity_pre_reserved=capacity_pre_reserved,
            )
            await uow.commit()

    @staticmethod
    async def _stage_reserved_group(
        uow: SQLAlchemyUnitOfWork,
        step: ModelToolLoopEventStep,
        *,
        operation_kind: EvidenceOperationKind,
        capacity_pre_reserved: bool,
    ) -> None:
        """只操作调用方 UoW；commit/rollback 完全由同一业务 owner 决定。"""

        existing = await uow.evidence_outbox.ordered_group(group_id=step.group_id)
        if not existing and not capacity_pre_reserved:
            reserved = await uow.event_capacity.reserve(
                run_id=step.context.run_id,
                operation_kind=operation_kind,
            )
            if reserved != operation_event_capacity(operation_kind):
                raise RuntimeError("event capacity registry returned an invalid reservation")
        await uow.evidence_outbox.stage_reserved_group(
            tenant_id=step.context.tenant_id,
            run_id=step.context.run_id,
            group_id=step.group_id,
            items=(
                {
                    "event_id": step.started_event_id,
                    "operation_kind": operation_kind.value,
                    "sequence_in_group": 1,
                    "reserved_event_count": 1,
                },
                {
                    "event_id": step.final_event_id,
                    "operation_kind": operation_kind.value,
                    "sequence_in_group": 2,
                    "reserved_event_count": 1,
                },
            ),
        )

    async def _publish(
        self,
        step: ModelToolLoopEventStep,
        *,
        event_id: str,
        event_type: CanonicalEventType,
        payload: dict[str, object],
        release_after: int = 0,
    ) -> None:
        """先耐久exact intent，再发布并结算对应预约。"""

        intent = _DurableModelToolLoopEventIntent(
            tenant_id=step.context.tenant_id,
            run_id=step.context.run_id,
            agent_id=step.context.agent_id,
            identity_id=step.identity_id,
            request_id=step.context.request_id,
            trace_id=step.context.trace_id,
            event_type=event_type,
            payload=payload,
            release_after=release_after,
        ).model_dump(mode="json")
        async with self._storage.uow() as uow:
            existing = await uow.evidence_outbox.get_by_event_id(event_id=event_id)
            already_published = existing is not None and existing.state == "published"
            await uow.evidence_outbox.persist_reserved_event(
                event_id=event_id,
                result=intent,
            )
            await uow.commit()
        try:
            await self._event_bus.publish(
                tenant_id=step.context.tenant_id,
                run_id=step.context.run_id,
                agent_id=step.context.agent_id,
                user_id=step.identity_id,
                event_type=event_type,
                payload=payload,
                request_id=step.context.request_id,
                trace_id=step.context.trace_id,
                event_id=event_id,
            )
            async with self._storage.uow() as uow:
                await uow.evidence_outbox.mark_event_published(event_id=event_id)
                if release_after and not already_published:
                    await uow.event_capacity.release(
                        run_id=step.context.run_id,
                        reserved_event_count=release_after,
                    )
                await uow.commit()
        except Exception as exc:
            # intent已先行提交；这里的唯一安全动作是保留active owner，让下一次
            # runtime重放相同event id/envelope，而不是把可证明事实永久降为unknown。
            raise ModelToolLoopEventPublishPending(
                group_id=step.group_id,
                message=str(exc),
            ) from exc


__all__ = [
    "ModelToolLoopEventPublishPending",
    "ModelToolLoopEventProducer",
    "ModelToolLoopEventRecoveryError",
    "ModelToolLoopEventStep",
]
