"""模型工具调用的durable claim协调与进程内一次性执行许可。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

from pydantic import ValidationError

from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.model_tool_loop_repositories import ModelToolLoopStorageConflict
from agent_harness.storage.tool_repositories import (
    ModelToolInvocationClaimCreate,
    ToolHandlerNotStartedProof,
    ToolInvocationRecord,
    ToolInvocationReplayConflict,
)
from agent_harness.tools.types import ResolvedToolIntent, ToolRuntimeContext

_MODEL_TOOL_EXECUTION_LEASE = timedelta(seconds=30)
ModelToolExecutionReviewReason = Literal[
    "claim_evidence_invalid",
    "commit_acknowledgement_unknown",
    "event_evidence_missing",
    "event_schema_unknown",
    "event_version_unknown",
    "executing_without_result",
    "handler_outcome_unknown",
    "result_evidence_missing",
]


class ModelToolExecutionClaimActive(RuntimeError):
    """相同identity仍由活跃claimed lease持有，当前owner不得接管。"""

    code = "tool.execution_claim_active"

    def __init__(self) -> None:
        super().__init__(self.code)


class ModelToolExecutionNeedsReview(RuntimeError):
    """执行权可能已交给handler或permit无效，禁止自动重放。"""

    code = "tool.execution_needs_review"

    def __init__(self) -> None:
        super().__init__(self.code)


class ToolExecutionPermit:
    """仅在executing提交确认后铸造、并在handler边界消费一次的本地许可。"""

    __slots__ = (
        "invocation_id",
        "tool_call_id",
        "execution_lease_digest",
        "execution_fence",
        "_nonce",
        "_consumed",
    )

    def __init__(
        self,
        *,
        invocation_id: str,
        tool_call_id: str,
        execution_lease_digest: str,
        execution_fence: int,
    ) -> None:
        self.invocation_id = invocation_id
        self.tool_call_id = tool_call_id
        self.execution_lease_digest = execution_lease_digest
        self.execution_fence = execution_fence
        self._nonce = uuid4().hex
        self._consumed = False

    @property
    def consumed(self) -> bool:
        """仅供协调器确认handler是否已经越过一次性边界。"""

        return self._consumed

    def consume_once(self) -> None:
        """由协调器在durable重验后调用；重复消费稳定失败。"""

        if self._consumed:
            raise ModelToolExecutionNeedsReview
        self._consumed = True


@asynccontextmanager
async def model_tool_execution_lock(
    storage: SQLAlchemyStorage,
    *,
    tenant_id: str,
    tool_call_id: str,
    approval_id: str | None,
):
    """串行化同一公共模型工具执行，避免并发请求观察半途executing状态。"""

    scopes = [f"model-tool-execution:{tenant_id}:{tool_call_id}"]
    if approval_id is not None:
        scopes.append(f"approved-tool-execution:{tenant_id}:{approval_id}")
    async with AsyncExitStack() as execution_locks:
        for scope in sorted(scopes):
            await execution_locks.enter_async_context(storage.idempotency_request_lock(scope))
        yield


class ModelToolExecutionClaimService:
    """在UoW提交确认、lease/fence重验与handler之间维持单一执行权。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        self._storage = storage

    async def acquire(
        self,
        data: ModelToolInvocationClaimCreate,
        *,
        now: datetime | None = None,
        prepare_new_owner_uow: (Callable[[SQLAlchemyUnitOfWork], Awaitable[None]] | None) = None,
    ) -> ToolExecutionPermit | ToolInvocationRecord:
        """创建或解析claim；仅首次owner可在同一提交准备关联证据预约。"""

        trusted_now = _as_utc(now or datetime.now(UTC))
        async with AsyncExitStack() as owner_locks:
            scopes = [f"model-tool-claim:{data.tenant_id}:{data.tool_call_id}"]
            if data.approval_id is not None:
                scopes.append(f"approved-tool-claim:{data.tenant_id}:{data.approval_id}")
            # 排序后逐个取得tool_call_id与approval_id锁，避免不同identity组合死锁。
            for scope in sorted(scopes):
                await owner_locks.enter_async_context(self._storage.idempotency_request_lock(scope))
            async with self._storage.uow() as uow:
                existing = await uow.tool_invocations.get_by_tool_call_id(data.tool_call_id)
                if existing is None and data.approval_id is not None:
                    existing = await uow.tool_invocations.get_by_approval_id(data.approval_id)
                if existing is None:
                    # owner锁已把两个唯一identity串行化；先写未提交claim确定赢家，
                    # 再仅由created分支准备证据，最后用一个commit公开全部事实。
                    (
                        existing,
                        created,
                    ) = await uow.tool_invocations.create_model_claim_for_locked_owner(data)
                    if created and prepare_new_owner_uow is not None:
                        await prepare_new_owner_uow(uow)
                else:
                    created = False
                    uow.tool_invocations.validate_model_claim_identity(
                        existing,
                        data,
                        include_lease=False,
                    )
                if created:
                    await uow.commit()

        if not _claim_recovery_evidence_is_valid(existing):
            await self._mark_needs_review(existing, reason="claim_evidence_invalid")
            raise ModelToolExecutionNeedsReview

        if existing.execution_state in {"completed", "failed"}:
            if existing.result_ref is None:
                await self._mark_needs_review(existing, reason="result_evidence_missing")
                raise ModelToolExecutionNeedsReview
            return existing
        if existing.execution_state == "executing":
            await self._mark_needs_review(existing, reason="executing_without_result")
            raise ModelToolExecutionNeedsReview
        if existing.execution_state == "needs_review":
            raise ModelToolExecutionNeedsReview
        if existing.execution_state != "claimed":
            raise ModelToolExecutionNeedsReview

        same_owner = (
            existing.execution_lease_digest == data.execution_lease_digest
            and existing.execution_fence == data.execution_fence
            and _as_utc_required(existing.execution_lease_expires_at)
            == _as_utc(data.execution_lease_expires_at)
        )
        if not same_owner:
            if _as_utc_required(existing.execution_lease_expires_at) > trusted_now:
                raise ModelToolExecutionClaimActive
            async with self._storage.uow() as uow:
                existing = await uow.tool_invocations.takeover_expired_model_claim(
                    existing=existing,
                    data=data,
                    now=trusted_now,
                )
                await uow.commit()
            if not _claim_recovery_evidence_is_valid(existing):
                await self._mark_needs_review(existing, reason="claim_evidence_invalid")
                raise ModelToolExecutionNeedsReview

        if _as_utc_required(existing.execution_lease_expires_at) <= trusted_now:
            raise ModelToolExecutionClaimActive
        try:
            async with self._storage.uow() as uow:
                executing = await uow.tool_invocations.begin_model_execution(
                    data=data,
                    now=trusted_now,
                )
                await uow.commit()
        except ToolInvocationReplayConflict:
            raise ModelToolExecutionNeedsReview from None
        return ToolExecutionPermit(
            invocation_id=executing.id,
            tool_call_id=data.tool_call_id,
            execution_lease_digest=data.execution_lease_digest,
            execution_fence=data.execution_fence,
        )

    async def mark_recovery_unknown(
        self,
        *,
        tool_call_id: str,
        reason: ModelToolExecutionReviewReason,
    ) -> ToolInvocationRecord:
        """供event/result恢复器把未知证据汇入同一耐久needs-review分支。"""

        async with self._storage.uow() as uow:
            existing = await uow.tool_invocations.get_by_tool_call_id(tool_call_id)
        if existing is None:
            raise ModelToolExecutionNeedsReview
        return await self._mark_needs_review(existing, reason=reason)

    async def mark_handler_outcome_unknown(
        self,
        permit: ToolExecutionPermit,
        *,
        reason: ModelToolExecutionReviewReason = "handler_outcome_unknown",
    ) -> ToolInvocationRecord:
        """handler越过permit但没有耐久结果时，原子围栏claim、loop与共享预算。"""

        if not permit.consumed:
            raise ModelToolExecutionNeedsReview
        async with self._storage.uow() as uow:
            existing = await uow.tool_invocations.get_by_tool_call_id(permit.tool_call_id)
        if (
            existing is None
            or existing.execution_state != "executing"
            or existing.result_ref is not None
            or existing.execution_lease_digest != permit.execution_lease_digest
            or existing.execution_fence != permit.execution_fence
            or existing.handler_started_at is None
        ):
            raise ModelToolExecutionNeedsReview
        if reason not in {"handler_outcome_unknown", "result_evidence_missing"}:
            raise ModelToolExecutionNeedsReview
        return await self._mark_needs_review(existing, reason=reason)

    async def _mark_needs_review(
        self,
        existing: ToolInvocationRecord,
        *,
        reason: ModelToolExecutionReviewReason,
    ) -> ToolInvocationRecord:
        """同一UoW关闭tool claim与协调loop，避免未知状态仍被terminal越过。"""

        if existing.loop_id is None or existing.run_id is None:
            raise ModelToolExecutionNeedsReview
        try:
            async with self._storage.uow() as uow:
                record = await uow.tool_invocations.mark_model_claim_needs_review(
                    tool_call_id=cast(str, existing.tool_call_id),
                    reason=reason,
                )
                loop = await uow.model_tool_loops.get(existing.tenant_id, existing.loop_id)
                if loop is None:
                    raise ToolInvocationReplayConflict
                review_value = record.metadata.get("model_tool_execution_review")
                if not isinstance(review_value, dict):
                    raise ToolInvocationReplayConflict
                review = cast(dict[str, object], review_value)
                evidence_digest = review.get("evidence_digest")
                if not isinstance(evidence_digest, str) or len(evidence_digest) != 64:
                    raise ToolInvocationReplayConflict
                if existing.approval_id is not None:
                    resolution = await uow.approvals.get_resolution(existing.approval_id)
                    if (
                        resolution is None
                        or resolution.approval.run_id != existing.run_id
                        or resolution.approval.tenant_id != existing.tenant_id
                    ):
                        raise ToolInvocationReplayConflict
                    await uow.approvals.mark_needs_review(
                        approval_id=existing.approval_id,
                        run_id=existing.run_id,
                        tenant_id=existing.tenant_id,
                        lease_id=resolution.lease_id,
                    )
                if loop.status in {"active", "waiting_approval"}:
                    await uow.model_tool_loops.fail(
                        tenant_id=loop.tenant_id,
                        loop_id=loop.loop_id,
                        expected_version=loop.version,
                        owner_lease_digest=loop.owner_lease_digest,
                        owner_fence=loop.owner_fence,
                        status="needs_review",
                        error_ref=f"model-tool-execution-review:{evidence_digest}",
                        expected_status=cast(Literal["active", "waiting_approval"], loop.status),
                    )
                elif loop.status != "needs_review":
                    raise ToolInvocationReplayConflict
                await uow.shared_budget.fence_needs_review_for_run_if_managed(
                    existing.tenant_id,
                    existing.run_id,
                )
                await uow.commit()
        except (ModelToolLoopStorageConflict, ToolInvocationReplayConflict):
            raise ModelToolExecutionNeedsReview from None
        return record

    async def require_handler_permit(
        self,
        permit: ToolExecutionPermit,
        *,
        now: datetime | None = None,
    ) -> None:
        """紧贴handler前重读durable row，并消费进程内permit恰好一次。"""

        if permit.consumed:
            raise ModelToolExecutionNeedsReview
        trusted_now = _as_utc(now or datetime.now(UTC))
        async with self._storage.uow() as uow:
            record = await uow.tool_invocations.get(permit.invocation_id)
        if (
            record is None
            or record.tool_call_id != permit.tool_call_id
            or record.execution_state != "executing"
            or record.result_ref is not None
            or record.execution_lease_digest != permit.execution_lease_digest
            or record.execution_fence != permit.execution_fence
            or record.handler_started_at is None
            or _as_utc_required(record.execution_lease_expires_at) <= trusted_now
        ):
            raise ModelToolExecutionNeedsReview
        permit.consume_once()

    async def complete(
        self,
        permit: ToolExecutionPermit,
        *,
        result_ref: str,
        execution_state: Literal["completed", "failed"],
        status: str,
    ) -> ToolInvocationRecord:
        """handler越过permit后，用同一lease/fence封存唯一确定结果。"""

        if not permit.consumed:
            raise ModelToolExecutionNeedsReview
        try:
            async with self._storage.uow() as uow:
                record = await uow.tool_invocations.finish_model_claim(
                    tool_call_id=permit.tool_call_id,
                    execution_lease_digest=permit.execution_lease_digest,
                    execution_fence=permit.execution_fence,
                    result_ref=result_ref,
                    execution_state=execution_state,
                    status=status,
                )
                await uow.commit()
        except Exception:
            # finish或commit抛错不能证明事务未落地。先重读相同lease/fence：精确终态
            # 直接作为ack-loss replay；其余仍由同一needs-review UoW关闭副作用窗口。
            return await self._recover_completion_unknown(
                permit,
                result_ref=result_ref,
                execution_state=execution_state,
                status=status,
            )
        except BaseException:
            # 取消不得被恢复读取吞掉；若终态尚未落地，先尽力围栏再保留原异常。
            try:
                await self._recover_completion_unknown(
                    permit,
                    result_ref=result_ref,
                    execution_state=execution_state,
                    status=status,
                )
            except ModelToolExecutionNeedsReview:
                pass
            raise
        return record

    async def _recover_completion_unknown(
        self,
        permit: ToolExecutionPermit,
        *,
        result_ref: str,
        execution_state: Literal["completed", "failed"],
        status: str,
    ) -> ToolInvocationRecord:
        """重读完成提交；只接受精确终态，否则关闭为待复核。"""

        async with self._storage.uow() as uow:
            existing = await uow.tool_invocations.get_by_tool_call_id(permit.tool_call_id)
        if existing is None:
            raise ModelToolExecutionNeedsReview
        same_owner = (
            existing.execution_lease_digest == permit.execution_lease_digest
            and existing.execution_fence == permit.execution_fence
            and existing.handler_started_at is not None
        )
        if (
            same_owner
            and existing.execution_state == execution_state
            and existing.status == status
            and existing.result_ref == result_ref
        ):
            return existing
        if existing.execution_state == "needs_review":
            raise ModelToolExecutionNeedsReview
        if same_owner and existing.execution_state == "executing" and existing.result_ref is None:
            await self._mark_needs_review(
                existing,
                reason="commit_acknowledgement_unknown",
            )
        raise ModelToolExecutionNeedsReview


def build_model_tool_invocation_claim(
    *,
    resolved: ResolvedToolIntent,
    context: ToolRuntimeContext,
    args_ref: str,
    approval_id: str | None,
    metadata: dict[str, object] | None = None,
    now: datetime | None = None,
) -> ModelToolInvocationClaimCreate:
    """从Registry重验结果和runtime身份构造普通/approved共用的claim。"""

    if context.run_id is None or context.trace_id is None:
        raise ToolInvocationReplayConflict
    binding: dict[str, object] = {
        "schema_version": "model-tool-call-binding-v1",
        "tenant_id": context.actor.tenant_id,
        "run_id": context.run_id,
        "agent_id": context.agent_id,
        "trace_id": context.trace_id,
        "request_id": context.request_id,
        "loop_id": resolved.loop_id,
        "turn_ordinal": resolved.turn_ordinal,
        "tool_call_id": resolved.tool_call_id,
        "tool_name": resolved.tool_name,
        "arguments_digest": resolved.arguments_digest,
        "tool_schema_ref": resolved.tool_schema_ref,
        "tool_schema_version": resolved.tool_schema_version,
        "tool_schema_digest": resolved.tool_schema_digest,
        "catalog_digest": resolved.catalog_digest,
        "action": resolved.action,
        "resource": resolved.resource,
        "approval_id": approval_id,
    }
    canonical = json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    binding["binding_digest"] = hashlib.sha256(canonical.encode()).hexdigest()
    trusted_now = _as_utc(now or datetime.now(UTC))
    lease_digest = hashlib.sha256(uuid4().bytes).hexdigest()
    return ModelToolInvocationClaimCreate(
        tenant_id=context.actor.tenant_id,
        agent_id=context.agent_id,
        run_id=context.run_id,
        tool_name=resolved.tool_name,
        args_ref=args_ref,
        approval_id=approval_id,
        arguments_hash=resolved.arguments_digest,
        trace_id=context.trace_id,
        request_id=context.request_id,
        loop_id=resolved.loop_id,
        turn_ordinal=resolved.turn_ordinal,
        tool_call_id=resolved.tool_call_id,
        binding=binding,
        execution_lease_digest=lease_digest,
        execution_fence=1,
        execution_lease_expires_at=trusted_now + _MODEL_TOOL_EXECUTION_LEASE,
        metadata=dict(metadata or {}),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_utc_required(value: datetime | None) -> datetime:
    if value is None:
        raise ModelToolExecutionNeedsReview
    return _as_utc(value)


def _claim_recovery_evidence_is_valid(record: ToolInvocationRecord) -> bool:
    """重算binding与换租proof；任何未知字段组合都由调用方关闭为review。"""

    binding = record.binding
    if not isinstance(binding, dict):
        return False
    binding_digest = binding.get("binding_digest")
    schema_version = binding.get("schema_version")
    if schema_version != "model-tool-call-binding-v1" or not isinstance(binding_digest, str):
        return False
    preimage = {key: value for key, value in binding.items() if key != "binding_digest"}
    canonical = json.dumps(
        preimage,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    if hashlib.sha256(canonical.encode()).hexdigest() != binding_digest:
        return False
    fence = record.execution_fence
    if isinstance(fence, bool) or not isinstance(fence, int) or fence < 1:
        return False
    proof_payload = record.not_started_proof
    if fence == 1:
        return proof_payload is None
    if proof_payload is None:
        return False
    try:
        proof = ToolHandlerNotStartedProof.model_validate(proof_payload)
    except ValidationError:
        return False
    return (
        proof.tool_call_id == record.tool_call_id
        and proof.binding_digest == binding_digest
        and proof.prior_fence == fence - 1
        and proof.next_fence == fence
    )


__all__ = [
    "ModelToolExecutionClaimActive",
    "ModelToolExecutionClaimService",
    "ModelToolExecutionNeedsReview",
    "ToolExecutionPermit",
    "build_model_tool_invocation_claim",
    "model_tool_execution_lock",
]
