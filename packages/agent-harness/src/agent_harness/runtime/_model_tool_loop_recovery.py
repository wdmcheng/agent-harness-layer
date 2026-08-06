"""模型工具循环的Recovery职责。"""
# pyright: reportPrivateUsage=false, reportUnusedClass=false

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import ValidationError

from agent_harness.context import ContextAssemblyResult
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.structured import structured_digest
from agent_harness.models.usage import stable_usage_call_id
from agent_harness.runtime._model_tool_loop_contracts import (
    ModelToolLoopError,
    ModelToolLoopLimitState,
    _ModelToolLoopFinal,
    _ModelToolLoopRestore,
    _next_turn_request,
)
from agent_harness.runtime._model_tool_loop_mixin_base import _ModelToolLoopMixinBase
from agent_harness.storage import (
    ModelToolLoopRecord,
    ModelToolLoopState,
)
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.storage.model_tool_loop_repositories import ModelToolLoopStorageConflict


class _ModelToolLoopRecoveryMixin(_ModelToolLoopMixinBase):
    async def _complete_durable_loop(
        self,
        loop_id: str,
        *,
        operation_key: str,
        final: _ModelToolLoopFinal,
    ) -> None:
        """在同一UoW核清全部owner证据后提交唯一completed CAS。"""

        if self._storage is None:
            return
        record = await self._durable_loop(loop_id)
        if final.turn_ordinal != record.next_turn_ordinal:
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        try:
            result_ref = f"model-response-digest:{structured_digest(final.response.to_payload())}"
            if self._artifact_store is not None:
                result_ref = self._artifact_store.write_json(
                    {
                        "schema_version": "model-tool-loop-response-v1",
                        "loop_id": loop_id,
                        "response": final.response.to_payload(),
                    }
                ).ref
            async with self._storage.uow() as uow:
                await self._validate_terminal_prerequisites(
                    uow,
                    record=record,
                    operation_key=operation_key,
                )
                await uow.model_tool_loops.terminate(
                    tenant_id=self._context.tenant_id,
                    loop_id=loop_id,
                    expected_version=record.version,
                    owner_lease_digest=record.owner_lease_digest,
                    owner_fence=record.owner_fence,
                    status="completed",
                    result_ref=result_ref,
                    error_ref=None,
                    cumulative_usage={
                        "schema_version": "model-tool-loop-cumulative-usage-v1",
                        "turns_completed": final.turn_ordinal,
                        "total_tokens_used": final.limit_state.total_tokens_used,
                        "total_cost_usd": final.limit_state.total_cost_usd,
                    },
                    state=record.state.terminal(
                        model_usage_call_id=final.usage_call_id,
                    ),
                )
                await uow.commit()
        except ModelToolLoopStorageConflict:
            raise ModelToolLoopError("model.tool_loop_replay_conflict") from None
        except ModelToolLoopError:
            raise
        except (LookupError, RuntimeError, ValueError):
            raise ModelToolLoopError("model.tool_loop_needs_review") from None

    async def _validate_terminal_prerequisites(
        self,
        uow: Any,
        *,
        record: ModelToolLoopRecord,
        operation_key: str,
    ) -> None:
        """completed CAS额外要求当前最终model turn的usage也已完整发布。"""

        await self._validate_owner_prerequisites(
            uow,
            record=record,
            operation_key=operation_key,
            usage_turn_ordinals=range(1, record.next_turn_ordinal + 1),
            allow_terminal_approval=True,
            allow_pending_current_turn=False,
        )

    async def _validate_owner_prerequisites(
        self,
        uow: Any,
        *,
        record: ModelToolLoopRecord,
        operation_key: str,
        usage_turn_ordinals: range,
        allow_terminal_approval: bool,
        allow_pending_current_turn: bool,
    ) -> None:
        """在终态CAS事务内联合验证usage、tool、Context、approval、event与budget。

        这是一道只读栅栏：任何owner仍有未决、缺失或相互矛盾的证据时，调用方
        回滚整个UoW并保留active loop，以便先恢复原有证据而不是制造第二次副作用。
        """

        # SQLAlchemy UoW 的仓储集合是 runtime 的既有公共存储 seam；这里不读取 ORM。
        tool_invocations = await uow.tool_invocations.list_by_model_loop(
            tenant_id=self._context.tenant_id,
            run_id=self._context.run_id,
            loop_id=record.loop_id,
        )
        approvals = await uow.approvals.list_by_run(
            self._context.run_id,
            tenant_id=self._context.tenant_id,
            for_update=True,
        )
        approvals_by_id = {item.approval_id: item for item in approvals}

        expected_completed_turns = set(range(1, record.next_turn_ordinal))
        pending_turn = record.next_turn_ordinal if allow_pending_current_turn else None
        seen_turns: set[int] = set()
        in_flight_approval_ids: set[str] = set()
        for invocation in tool_invocations:
            if (
                invocation.execution_state != "completed"
                or invocation.status != "completed"
                or invocation.result_ref is None
                or invocation.handler_started_at is None
                or invocation.turn_ordinal is None
                or invocation.tool_call_id is None
                or invocation.turn_ordinal in seen_turns
                or (
                    invocation.turn_ordinal not in expected_completed_turns
                    and invocation.turn_ordinal != pending_turn
                )
            ):
                raise ModelToolLoopError("model.tool_loop_needs_review")

            seen_turns.add(invocation.turn_ordinal)
            if invocation.approval_id is not None:
                approval = approvals_by_id.get(invocation.approval_id)
                if approval is None:
                    raise ModelToolLoopError("model.tool_loop_needs_review")
                if approval.status != "approved":
                    if (
                        not allow_terminal_approval
                        or not await uow.approvals.model_loop_terminal_approval_ready(
                            approval_id=approval.approval_id,
                            run_id=self._context.run_id,
                            tenant_id=self._context.tenant_id,
                        )
                    ):
                        raise ModelToolLoopError("model.tool_loop_needs_review")
                    in_flight_approval_ids.add(approval.approval_id)
            assembly = await uow.context_assemblies.get_by_loop_turn(
                tenant_id=self._context.tenant_id,
                loop_id=record.loop_id,
                turn_ordinal=invocation.turn_ordinal,
                for_update=True,
            )
            if assembly is None and invocation.turn_ordinal == pending_turn:
                # 当前model_result的tool已exact完成、Context尚未首次创建是可信继续窗口；
                # 只有历史回合或terminal才要求Context必然存在。
                continue
            if (
                assembly is None
                or assembly.run_id != self._context.run_id
                or assembly.tool_call_id != invocation.tool_call_id
                or assembly.output_ref is None
                or assembly.input_identity_digest is None
                or assembly.output_digest is None
            ):
                raise ModelToolLoopError("model.tool_loop_needs_review")

        unexpected_turns = seen_turns - expected_completed_turns
        if not expected_completed_turns <= seen_turns or (
            unexpected_turns and (pending_turn is None or unexpected_turns != {pending_turn})
        ):
            raise ModelToolLoopError("model.tool_loop_needs_review")
        if any(
            item.status == "waiting" and item.approval_id not in in_flight_approval_ids
            for item in approvals
        ):
            raise ModelToolLoopError("model.tool_loop_needs_review")

        for turn_ordinal in usage_turn_ordinals:
            usage_call_id = stable_usage_call_id(
                context=self._context,
                operation_key=f"{operation_key}:model-turn:{turn_ordinal}",
            )
            usage = await uow.evidence_outbox.get_usage(
                tenant_id=self._context.tenant_id,
                usage_call_id=usage_call_id,
            )
            if (
                usage.run_id != self._context.run_id
                or usage.operation_kind != EvidenceOperationKind.MODEL_USAGE.value
                or usage.state != "published"
                or usage.result_json is None
                or usage.error_code is not None
            ):
                raise ModelToolLoopError("model.tool_loop_needs_review")

        if await uow.evidence_outbox.blocks_model_loop_terminal(
            run_id=self._context.run_id,
            in_flight_approval_ids=in_flight_approval_ids,
        ):
            raise ModelToolLoopError("model.tool_loop_needs_review")
        approval_capacity_allowance = len(in_flight_approval_ids) * operation_event_capacity(
            EvidenceOperationKind.APPROVAL_RESOLUTION
        )
        await uow.event_capacity.assert_model_loop_terminal_publishable(
            run_id=self._context.run_id,
            approval_capacity_allowance=approval_capacity_allowance,
        )
        if not await uow.shared_budget.terminal_allowed_for_run_if_managed(
            self._context.tenant_id,
            self._context.run_id,
        ):
            raise ModelToolLoopError("model.tool_loop_needs_review")

    def _replay_completed_response(
        self,
        record: ModelToolLoopRecord,
        *,
        initial_request: ModelRequest,
    ) -> ModelResponse:
        """从内容寻址artifact恢复已完成响应，禁止再次进入model或terminal CAS。"""

        if self._artifact_store is None or record.result_ref is None:
            raise ModelToolLoopError("model.tool_loop_needs_review")
        try:
            payload = self._artifact_store.read_json(record.result_ref)
            if (
                payload.get("schema_version") != "model-tool-loop-response-v1"
                or payload.get("loop_id") != record.loop_id
                or set(payload) != {"schema_version", "loop_id", "response"}
            ):
                raise ValueError("completed response artifact identity is invalid")
            response = ModelResponse.model_validate(payload["response"])
        except Exception:
            raise ModelToolLoopError("model.tool_loop_needs_review") from None
        if not self._response_matches_request(response, initial_request):
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        return response

    async def _restore_active_loop(
        self,
        record: ModelToolLoopRecord,
        *,
        initial_request: ModelRequest,
        operation_key: str,
    ) -> _ModelToolLoopRestore:
        """重算历史actual，并恢复next turn或已结算待解释的current result。"""

        bounds = record.frozen_bounds
        usage = record.cumulative_usage
        try:
            recorded_limit_state = ModelToolLoopLimitState.model_validate(
                {
                    "max_turns": bounds.max_turns,
                    "max_total_tokens": bounds.max_total_tokens,
                    "max_total_cost_usd": bounds.max_total_cost_usd,
                    "max_tool_output_bytes": bounds.max_tool_output_bytes,
                    "max_duration_seconds": bounds.max_duration_seconds,
                    "loop_started_at": bounds.loop_started_at,
                    "deadline_at": bounds.deadline_at,
                    "total_tokens_used": usage.total_tokens_used,
                    "total_cost_usd": usage.total_cost_usd,
                }
            )
        except (TypeError, ValidationError, ValueError):
            raise ModelToolLoopError("model.tool_loop_needs_review") from None
        turns_completed = usage.turns_completed
        model_result_pending = record.state.next_step == "model_result"
        expected_turns_completed = record.next_turn_ordinal - 1 + int(model_result_pending)
        if (
            turns_completed != expected_turns_completed
            or record.next_turn_ordinal < 1
            or record.next_turn_ordinal > recorded_limit_state.max_turns
            or recorded_limit_state.total_tokens_used > recorded_limit_state.max_total_tokens
            or (
                recorded_limit_state.max_total_cost_usd is not None
                and recorded_limit_state.total_cost_usd is not None
                and recorded_limit_state.total_cost_usd > recorded_limit_state.max_total_cost_usd
            )
        ):
            raise ModelToolLoopError("model.tool_loop_needs_review")
        try:
            recomputed = ModelToolLoopLimitState.model_validate(
                {
                    **recorded_limit_state.model_dump(mode="python"),
                    "total_tokens_used": 0,
                    "total_cost_usd": 0.0 if usage.total_cost_usd is not None else None,
                }
            )
        except ValidationError:
            raise ModelToolLoopError("model.tool_loop_needs_review") from None
        settled_turn_input_state: ModelToolLoopLimitState | None = None
        for turn_ordinal in range(1, turns_completed + 1):
            if model_result_pending and turn_ordinal == record.next_turn_ordinal:
                settled_turn_input_state = recomputed
            recomputed = await self._account_turn_usage(
                recomputed,
                usage_call_id=stable_usage_call_id(
                    context=self._context,
                    operation_key=f"{operation_key}:model-turn:{turn_ordinal}",
                ),
                loop_id=record.loop_id,
                turn_ordinal=turn_ordinal,
            )
        if (
            recomputed.total_tokens_used != recorded_limit_state.total_tokens_used
            or recomputed.total_cost_usd != recorded_limit_state.total_cost_usd
            or (
                model_result_pending
                and (
                    settled_turn_input_state is None
                    or record.state.model_usage_call_id
                    != stable_usage_call_id(
                        context=self._context,
                        operation_key=(f"{operation_key}:model-turn:{record.next_turn_ordinal}"),
                    )
                )
            )
        ):
            raise ModelToolLoopError("model.tool_loop_needs_review")
        limit_state = recomputed
        if self._storage is None:
            raise ModelToolLoopError("model.tool_loop_needs_review")
        try:
            async with self._storage.uow() as uow:
                await self._validate_owner_prerequisites(
                    uow,
                    record=record,
                    operation_key=operation_key,
                    usage_turn_ordinals=range(
                        1,
                        record.next_turn_ordinal
                        + (1 if record.state.next_step == "model_result" else 0),
                    ),
                    allow_terminal_approval=False,
                    allow_pending_current_turn=model_result_pending,
                )
        except ModelToolLoopError:
            raise
        except (LookupError, RuntimeError, ValueError):
            raise ModelToolLoopError("model.tool_loop_needs_review") from None
        if record.next_turn_ordinal == 1:
            initial_state = ModelToolLoopState(
                next_step="model_turn",
                model_usage_call_id=None,
                tool_call_id=None,
                approval_id=None,
                checkpoint_ref=None,
                context_ref=None,
                next_request_digest=None,
            )
            if record.state == initial_state or record.state.next_step == "model_result":
                return _ModelToolLoopRestore(
                    current_request=initial_request.model_copy(deep=True),
                    start_turn_ordinal=1,
                    limit_state=limit_state,
                    settled_turn_input_state=settled_turn_input_state,
                )
            else:
                raise ModelToolLoopError("model.tool_loop_needs_review")
        if record.state.next_step not in {"model_turn", "model_result"}:
            raise ModelToolLoopError("model.tool_loop_needs_review")
        replay_turn = getattr(self._context_assembly, "replay_loop_turn", None)
        if not callable(replay_turn):
            raise ModelToolLoopError("model.tool_loop_needs_review")
        current_request = initial_request.model_copy(deep=True)
        try:
            for turn_ordinal in range(1, record.next_turn_ordinal):
                assembly = await cast(Any, replay_turn)(
                    tenant_id=self._context.tenant_id,
                    run_id=self._context.run_id,
                    loop_id=record.loop_id,
                    turn_ordinal=turn_ordinal,
                )
                if type(assembly) is not ContextAssemblyResult:
                    raise ValueError("context replay returned an invalid result")
                current_request = _next_turn_request(current_request, assembly=assembly)
        except Exception:
            raise ModelToolLoopError("model.tool_loop_needs_review") from None
        if (
            record.state.next_step == "model_turn"
            and structured_digest(current_request.to_payload()) != record.state.next_request_digest
        ):
            raise ModelToolLoopError("model.tool_loop_needs_review")
        return _ModelToolLoopRestore(
            current_request=current_request,
            start_turn_ordinal=record.next_turn_ordinal,
            limit_state=limit_state,
            settled_turn_input_state=settled_turn_input_state,
        )

    async def _fail_durable_loop(
        self,
        loop_id: str,
        *,
        status: Literal["failed", "needs_review"],
        code: str,
    ) -> None:
        """确定失败与未知影响共享terminal CAS，但保留不同封闭状态。"""

        if self._storage is None:
            return
        record = await self._durable_loop(loop_id)
        if record.status not in {"active", "waiting_approval"}:
            return
        try:
            async with self._storage.uow() as uow:
                terminal_at = self._trusted_clock()
                deadline_expired = (
                    code == "model.tool_loop_limit_exceeded"
                    and record.owner_lease_expires_at <= terminal_at
                )
                if deadline_expired:
                    await uow.model_tool_loops.expire_deadline(
                        tenant_id=self._context.tenant_id,
                        loop_id=loop_id,
                        expected_status=cast(Literal["active", "waiting_approval"], record.status),
                        expected_version=record.version,
                        owner_lease_digest=record.owner_lease_digest,
                        owner_fence=record.owner_fence,
                        expired_at=terminal_at,
                        error_ref=f"error:{code}",
                    )
                elif status == "failed" and record.status == "waiting_approval":
                    await uow.model_tool_loops.cancel(
                        tenant_id=self._context.tenant_id,
                        loop_id=loop_id,
                        expected_status="waiting_approval",
                        expected_version=record.version,
                        owner_lease_digest=record.owner_lease_digest,
                        owner_fence=record.owner_fence,
                        error_ref=f"error:{code}",
                    )
                else:
                    await uow.model_tool_loops.fail(
                        tenant_id=self._context.tenant_id,
                        loop_id=loop_id,
                        expected_version=record.version,
                        owner_lease_digest=record.owner_lease_digest,
                        owner_fence=record.owner_fence,
                        status=status,
                        error_ref=f"error:{code}",
                        expected_status=cast(Literal["active", "waiting_approval"], record.status),
                    )
                if status == "needs_review":
                    await uow.shared_budget.fence_needs_review_for_run_if_managed(
                        self._context.tenant_id,
                        self._context.run_id,
                    )
                await uow.commit()
        except ModelToolLoopStorageConflict:
            raise ModelToolLoopError("model.tool_loop_replay_conflict") from None

    async def _durable_loop(self, loop_id: str) -> ModelToolLoopRecord:
        if self._storage is None:  # pragma: no cover - callers guard optional storage
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        async with self._storage.uow() as uow:
            record = await uow.model_tool_loops.get(self._context.tenant_id, loop_id)
        if record is None:
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        return record
