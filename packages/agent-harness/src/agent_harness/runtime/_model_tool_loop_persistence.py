"""模型工具循环的Persistence职责。"""
# pyright: reportPrivateUsage=false, reportUnusedClass=false

from __future__ import annotations

from uuid import uuid4

from agent_harness.models.providers import ModelRequest
from agent_harness.models.route_chain_identity import model_route_operation_identity_digest
from agent_harness.models.structured import structured_digest
from agent_harness.models.tool_catalog import ToolCatalog
from agent_harness.runtime._model_tool_loop_contracts import (
    ModelToolLoopApprovalSnapshot,
    ModelToolLoopError,
    ModelToolLoopLimitState,
)
from agent_harness.runtime._model_tool_loop_mixin_base import _ModelToolLoopMixinBase
from agent_harness.runtime.executor import AgentApprovalRequest
from agent_harness.storage import (
    ModelToolLoopCreate,
    ModelToolLoopCumulativeUsage,
    ModelToolLoopFrozenBounds,
    ModelToolLoopRecord,
    ModelToolLoopState,
)
from agent_harness.storage.model_tool_loop_repositories import ModelToolLoopStorageConflict


class _ModelToolLoopPersistenceMixin(_ModelToolLoopMixinBase):
    async def _ensure_durable_loop(
        self,
        *,
        initial_request: ModelRequest,
        operation_key: str,
        catalog: ToolCatalog,
        loop_id: str,
        limit_state: ModelToolLoopLimitState,
    ) -> ModelToolLoopRecord | None:
        """在首个model/tool副作用前建立或逐值重验唯一loop协调row。"""

        if self._storage is None:
            return None
        try:
            async with self._storage.uow() as uow:
                existing = await uow.model_tool_loops.get(self._context.tenant_id, loop_id)
                requested_bounds = {
                    "max_turns": limit_state.max_turns,
                    "max_total_tokens": limit_state.max_total_tokens,
                    "max_total_cost_usd": limit_state.max_total_cost_usd,
                    "max_tool_output_bytes": limit_state.max_tool_output_bytes,
                    "max_duration_seconds": limit_state.max_duration_seconds,
                }
                if existing is not None:
                    if any(
                        getattr(existing.frozen_bounds, name) != value
                        for name, value in requested_bounds.items()
                    ):
                        raise ModelToolLoopStorageConflict
                owner_lease_digest = (
                    uuid4().hex + uuid4().hex if existing is None else existing.owner_lease_digest
                )
                owner_fence = 1 if existing is None else existing.owner_fence
                owner_expiry = (
                    limit_state.deadline_at if existing is None else existing.owner_lease_expires_at
                )
                record = await uow.model_tool_loops.create(
                    ModelToolLoopCreate(
                        tenant_id=self._context.tenant_id,
                        run_id=self._context.run_id,
                        agent_id=self._context.agent_id,
                        loop_id=loop_id,
                        request_identity_digest=structured_digest(initial_request.to_payload()),
                        operation_identity_digest=model_route_operation_identity_digest(
                            tenant_id=self._context.tenant_id,
                            run_id=self._context.run_id,
                            agent_id=self._context.agent_id,
                            request_id=self._context.request_id,
                            trace_id=self._context.trace_id,
                            operation_key=operation_key,
                        ),
                        catalog_digest=catalog.catalog_digest,
                        frozen_bounds=existing.frozen_bounds
                        if existing is not None
                        else ModelToolLoopFrozenBounds(
                            max_turns=limit_state.max_turns,
                            max_total_tokens=limit_state.max_total_tokens,
                            max_total_cost_usd=limit_state.max_total_cost_usd,
                            max_tool_output_bytes=limit_state.max_tool_output_bytes,
                            max_duration_seconds=limit_state.max_duration_seconds,
                            loop_started_at=limit_state.loop_started_at,
                            deadline_at=limit_state.deadline_at,
                        ),
                        cumulative_usage=ModelToolLoopCumulativeUsage(
                            turns_completed=0,
                            total_tokens_used=0,
                            total_cost_usd=0.0,
                        ),
                        state=ModelToolLoopState(
                            next_step="model_turn",
                            model_usage_call_id=None,
                            tool_call_id=None,
                            approval_id=None,
                            checkpoint_ref=None,
                            context_ref=None,
                            next_request_digest=None,
                        ),
                        owner_lease_digest=owner_lease_digest,
                        owner_fence=owner_fence,
                        owner_lease_expires_at=owner_expiry,
                    )
                )
                await uow.commit()
            return record
        except ModelToolLoopStorageConflict:
            raise ModelToolLoopError("model.tool_loop_replay_conflict") from None

    async def _wait_durable_loop(
        self,
        loop_id: str,
        *,
        approval: AgentApprovalRequest,
        snapshot: ModelToolLoopApprovalSnapshot,
    ) -> None:
        """审批artifact已冻结后，以当前version把active loop推进到waiting。"""

        if self._storage is None:
            return
        record = await self._durable_loop(loop_id)
        try:
            async with self._storage.uow() as uow:
                # approval row 由外层 orchestrator 在 loop 返回请求后创建；waiting
                # 快照只能绑定已冻结的 usage/tool/checkpoint，不能预造 grant identity。
                # resume_after_approval 会在任何工具 claim 前写入并核验真实 approval_id。
                await uow.model_tool_loops.wait_for_approval(
                    tenant_id=self._context.tenant_id,
                    loop_id=loop_id,
                    expected_version=record.version,
                    owner_lease_digest=record.owner_lease_digest,
                    owner_fence=record.owner_fence,
                    state={
                        "schema_version": "model-tool-loop-state-v1",
                        "next_step": "approval_resume",
                        "model_usage_call_id": snapshot.intent.model_usage_call_id,
                        "tool_call_id": snapshot.intent.tool_call_id,
                        "approval_id": None,
                        "checkpoint_ref": approval.arguments_ref,
                        "context_ref": None,
                        "next_request_digest": None,
                    },
                )
                await uow.commit()
        except ModelToolLoopStorageConflict:
            raise ModelToolLoopError("model.tool_loop_replay_conflict") from None

    async def _resume_durable_loop(
        self,
        loop_id: str,
        *,
        approval_id: str,
    ) -> None:
        """active grant逐值验证后，以同一owner恢复waiting loop。"""

        if self._storage is None:
            return
        record = await self._durable_loop(loop_id)
        if record.status == "active":
            return
        try:
            async with self._storage.uow() as uow:
                await uow.model_tool_loops.resume_after_approval(
                    tenant_id=self._context.tenant_id,
                    loop_id=loop_id,
                    expected_version=record.version,
                    owner_lease_digest=record.owner_lease_digest,
                    owner_fence=record.owner_fence,
                    state=record.state.model_copy(
                        update={
                            "next_step": "tool_execution",
                            "approval_id": approval_id,
                        }
                    ),
                )
                await uow.commit()
        except ModelToolLoopStorageConflict:
            raise ModelToolLoopError("model.tool_loop_replay_conflict") from None

    async def _commit_durable_turn(
        self,
        *,
        loop_id: str,
        turn_ordinal: int,
        limit_state: ModelToolLoopLimitState,
        next_request: ModelRequest,
        model_usage_call_id: str,
        tool_call_id: str,
        approval_id: str | None,
        checkpoint_ref: str | None,
        context_ref: str,
    ) -> None:
        """tool/context闭合后推进唯一next ordinal与累计model usage摘要。"""

        if self._storage is None:
            return
        record = await self._durable_loop(loop_id)
        if record.next_turn_ordinal != turn_ordinal:
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        try:
            async with self._storage.uow() as uow:
                await uow.model_tool_loops.commit_turn(
                    tenant_id=self._context.tenant_id,
                    loop_id=loop_id,
                    expected_version=record.version,
                    owner_lease_digest=record.owner_lease_digest,
                    owner_fence=record.owner_fence,
                    cumulative_usage={
                        "schema_version": "model-tool-loop-cumulative-usage-v1",
                        "turns_completed": turn_ordinal,
                        "total_tokens_used": limit_state.total_tokens_used,
                        "total_cost_usd": limit_state.total_cost_usd,
                    },
                    state={
                        "schema_version": "model-tool-loop-state-v1",
                        "next_step": "model_turn",
                        "model_usage_call_id": model_usage_call_id,
                        "tool_call_id": tool_call_id,
                        "approval_id": approval_id,
                        "checkpoint_ref": checkpoint_ref,
                        "context_ref": context_ref,
                        "next_request_digest": structured_digest(next_request.to_payload()),
                    },
                )
                await uow.commit()
        except ModelToolLoopStorageConflict:
            raise ModelToolLoopError("model.tool_loop_replay_conflict") from None

    async def _settle_durable_model_turn(
        self,
        *,
        loop_id: str,
        turn_ordinal: int,
        usage_call_id: str,
        limit_state: ModelToolLoopLimitState,
    ) -> None:
        """在解释model结果前先投影该turn已耐久的actual与current usage ref。"""

        if self._storage is None:
            return
        record = await self._durable_loop(loop_id)
        if record.next_turn_ordinal != turn_ordinal:
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        next_state = record.state.model_copy(
            update={
                "next_step": "model_result",
                "model_usage_call_id": usage_call_id,
                "next_request_digest": None,
            }
        )
        try:
            async with self._storage.uow() as uow:
                await uow.model_tool_loops.settle_model_turn(
                    tenant_id=self._context.tenant_id,
                    loop_id=loop_id,
                    expected_version=record.version,
                    owner_lease_digest=record.owner_lease_digest,
                    owner_fence=record.owner_fence,
                    cumulative_usage={
                        "schema_version": "model-tool-loop-cumulative-usage-v1",
                        "turns_completed": turn_ordinal,
                        "total_tokens_used": limit_state.total_tokens_used,
                        "total_cost_usd": limit_state.total_cost_usd,
                    },
                    state=next_state,
                )
                await uow.commit()
        except ModelToolLoopStorageConflict:
            raise ModelToolLoopError("model.tool_loop_replay_conflict") from None

    async def _expire_durable_model_turn(
        self,
        *,
        loop_id: str,
        turn_ordinal: int,
        usage_call_id: str,
        limit_state: ModelToolLoopLimitState,
    ) -> None:
        """provider已完成但deadline已到时，以过期CAS同时保存actual和failed终态。"""

        if self._storage is None:
            return
        record = await self._durable_loop(loop_id)
        if record.next_turn_ordinal != turn_ordinal:
            raise ModelToolLoopError("model.tool_loop_replay_conflict")
        try:
            async with self._storage.uow() as uow:
                await uow.model_tool_loops.expire_deadline(
                    tenant_id=self._context.tenant_id,
                    loop_id=loop_id,
                    expected_status="active",
                    expected_version=record.version,
                    owner_lease_digest=record.owner_lease_digest,
                    owner_fence=record.owner_fence,
                    expired_at=limit_state.deadline_at,
                    error_ref="error:model.tool_loop_limit_exceeded",
                    cumulative_usage={
                        "schema_version": "model-tool-loop-cumulative-usage-v1",
                        "turns_completed": turn_ordinal,
                        "total_tokens_used": limit_state.total_tokens_used,
                        "total_cost_usd": limit_state.total_cost_usd,
                    },
                    state=record.state.terminal(
                        model_usage_call_id=usage_call_id,
                    ),
                )
                await uow.commit()
        except ModelToolLoopStorageConflict:
            raise ModelToolLoopError("model.tool_loop_replay_conflict") from None
