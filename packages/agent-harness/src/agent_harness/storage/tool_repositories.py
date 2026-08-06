"""工具执行相关 repository。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._tool_repository_contracts import (
    ModelToolInvocationClaimCreate,
    ToolHandlerNotStartedProof,
    ToolInvocationCreate,
    ToolInvocationRecord,
    ToolInvocationReplayConflict,
    WorkspaceCreate,
    WorkspaceRecord,
    _as_utc_required,
    _canonical_proof_digest,
)
from agent_harness.storage._tool_repository_support import (
    _MODEL_TOOL_EXECUTION_REVIEW_REASONS,
    _binding_digest,
    _tool_invocation_record,
    _workspace_record,
)
from agent_harness.storage.model_tool_loop_marker import mark_model_tool_loop_evidence_seen
from agent_harness.storage.model_tool_loop_repositories import require_model_tool_loop_active
from agent_harness.storage.models import ToolInvocationModel, WorkspaceModel
from agent_harness.storage.run_trace_gate import project_canonical_run_trace


class WorkspaceRepository:
    """workspace 表 repository，调用方不直接接触 ORM model。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前 UoW session；调用方负责事务提交或回滚。"""

        self._session = session

    async def create(self, data: WorkspaceCreate) -> WorkspaceRecord:
        """创建一个带策略引用的 workspace 记录，不解释或访问宿主文件路径。"""

        model = WorkspaceModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            run_id=data.run_id,
            root_path=data.root_path,
            policy_ref=data.policy_ref,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _workspace_record(model)

    async def get(self, workspace_id: str) -> WorkspaceRecord | None:
        """按主键读取 workspace 摘要，缺失时返回 ``None`` 供服务层映射。"""

        model = await self._session.get(WorkspaceModel, workspace_id)
        return None if model is None else _workspace_record(model)


class ToolInvocationRepository:
    """tool_invocations 表 repository，保存参数/result artifact 引用。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定工具调用持久化所使用的当前异步 session。"""

        self._session = session

    async def create(self, data: ToolInvocationCreate) -> ToolInvocationRecord:
        """持久化工具调用元数据，并在关联运行时投影可信 canonical trace。"""

        trace_id = data.trace_id
        if data.run_id is not None:
            # 调用方传入的 trace 只能作为一致性校验，不能覆盖已持久化的运行归属。
            trace_id = await project_canonical_run_trace(
                self._session,
                tenant_id=data.tenant_id,
                run_id=data.run_id,
                trace_id=data.trace_id,
            )
        if data.loop_id is not None:
            await require_model_tool_loop_active(
                self._session,
                tenant_id=data.tenant_id,
                loop_id=data.loop_id,
            )
            await mark_model_tool_loop_evidence_seen(self._session)
        model = ToolInvocationModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            run_id=data.run_id,
            tool_name=data.tool_name,
            args_ref=data.args_ref,
            result_ref=data.result_ref,
            approval_id=data.approval_id,
            arguments_hash=data.arguments_hash,
            execution_state=data.execution_state,
            status=data.status,
            duration_ms=data.duration_ms,
            trace_id=trace_id,
            request_id=data.request_id,
            metadata_json=data.metadata,
            loop_id=data.loop_id,
            turn_ordinal=data.turn_ordinal,
            tool_call_id=data.tool_call_id,
            binding_json=data.binding,
            execution_lease_digest=data.execution_lease_digest,
            execution_fence=data.execution_fence,
            execution_lease_expires_at=data.execution_lease_expires_at,
            handler_started_at=data.handler_started_at,
            not_started_proof_json=data.not_started_proof,
        )
        self._session.add(model)
        await self._session.flush()
        return _tool_invocation_record(model)

    async def get(self, invocation_id: str) -> ToolInvocationRecord | None:
        """按主键读取工具调用记录，不改变 approval 或执行状态。"""

        model = await self._session.get(ToolInvocationModel, invocation_id)
        return None if model is None else _tool_invocation_record(model)

    async def get_by_approval_id(self, approval_id: str) -> ToolInvocationRecord | None:
        """读取审批对应的唯一工具 claim，供续跑和审计路径复用。"""

        result = await self._session.scalars(
            select(ToolInvocationModel).where(ToolInvocationModel.approval_id == approval_id)
        )
        model = result.first()
        return None if model is None else _tool_invocation_record(model)

    async def get_by_tool_call_id(self, tool_call_id: str) -> ToolInvocationRecord | None:
        """读取模型驱动调用的唯一claim；legacy记录不会匹配。"""

        model = await self._session.scalar(
            select(ToolInvocationModel).where(ToolInvocationModel.tool_call_id == tool_call_id)
        )
        return None if model is None else _tool_invocation_record(model)

    async def list_by_model_loop(
        self,
        *,
        tenant_id: str,
        run_id: str,
        loop_id: str,
    ) -> list[ToolInvocationRecord]:
        """读取同一模型工具循环的全部claim，供终态联合前置校验使用。

        查询同时约束 tenant、run 与 loop，避免仅凭可预测的关联标识跨执行树读取；
        legacy NULL identity 不属于模型工具循环，也不会被纳入终态判断。
        """

        result = await self._session.scalars(
            select(ToolInvocationModel)
            .where(
                ToolInvocationModel.tenant_id == tenant_id,
                ToolInvocationModel.run_id == run_id,
                ToolInvocationModel.loop_id == loop_id,
            )
            .order_by(ToolInvocationModel.turn_ordinal.asc(), ToolInvocationModel.id.asc())
            .with_for_update()
        )
        return [_tool_invocation_record(model) for model in result.all()]

    async def create_model_claim(
        self,
        data: ModelToolInvocationClaimCreate,
    ) -> tuple[ToolInvocationRecord, bool]:
        """首次只写claimed；唯一键竞争后仅允许逐值相同的精确重放。"""

        existing = await self._model_claim_by_identities(data)
        if existing is not None:
            self.validate_model_claim_identity(existing, data, include_lease=False)
            return existing, False
        try:
            async with self._session.begin_nested():
                record = await self._insert_model_claim(data)
        except IntegrityError:
            existing = await self._model_claim_by_identities(data)
            if existing is None:
                raise ToolInvocationReplayConflict from None
            self.validate_model_claim_identity(existing, data, include_lease=False)
            return existing, False
        return record, True

    async def create_model_claim_for_locked_owner(
        self,
        data: ModelToolInvocationClaimCreate,
    ) -> tuple[ToolInvocationRecord, bool]:
        """在外部双identity owner锁内确定赢家，不用nested savepoint释放首次写入。

        Claim service只有在返回``created=True``后才准备容量/outbox；后续准备失败时，
        外层UoW rollback必须能连同首次claim一起撤销。未持有对应owner锁的调用方不得
        使用此入口，否则唯一键竞争会直接表现为``IntegrityError``。
        """

        existing = await self._model_claim_by_identities(data)
        if existing is not None:
            self.validate_model_claim_identity(existing, data, include_lease=False)
            return existing, False
        return await self._insert_model_claim(data), True

    async def _insert_model_claim(
        self,
        data: ModelToolInvocationClaimCreate,
    ) -> ToolInvocationRecord:
        """写入未提交的首次claim；调用方选择竞争与回滚策略。"""

        return await self.create(
            ToolInvocationCreate(
                tenant_id=data.tenant_id,
                agent_id=data.agent_id,
                run_id=data.run_id,
                tool_name=data.tool_name,
                args_ref=data.args_ref,
                approval_id=data.approval_id,
                arguments_hash=data.arguments_hash,
                execution_state="claimed",
                status="claimed",
                trace_id=data.trace_id,
                request_id=data.request_id,
                metadata=data.metadata,
                loop_id=data.loop_id,
                turn_ordinal=data.turn_ordinal,
                tool_call_id=data.tool_call_id,
                binding=data.binding,
                execution_lease_digest=data.execution_lease_digest,
                execution_fence=data.execution_fence,
                execution_lease_expires_at=data.execution_lease_expires_at,
            )
        )

    async def takeover_expired_model_claim(
        self,
        *,
        existing: ToolInvocationRecord,
        data: ModelToolInvocationClaimCreate,
        now: datetime,
    ) -> ToolInvocationRecord:
        """只对过期claimed做一次lease/fence CAS，并保存可复算未开始proof。"""

        self.validate_model_claim_identity(existing, data, include_lease=False)
        previous_expiry = _as_utc_required(existing.execution_lease_expires_at)
        previous_digest = existing.execution_lease_digest
        previous_fence = existing.execution_fence
        if (
            existing.execution_state != "claimed"
            or previous_digest is None
            or previous_fence is None
            or previous_expiry > _as_utc_required(now)
            or data.execution_fence != previous_fence + 1
        ):
            raise ToolInvocationReplayConflict
        proof = ToolHandlerNotStartedProof.build(
            tool_call_id=data.tool_call_id,
            binding_digest=_binding_digest(data.binding),
            prior_fence=previous_fence,
            next_fence=data.execution_fence,
            previous_lease_expires_at=previous_expiry,
        ).model_dump(mode="json")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ToolInvocationModel)
                .where(
                    ToolInvocationModel.id == existing.id,
                    ToolInvocationModel.execution_state == "claimed",
                    ToolInvocationModel.result_ref.is_(None),
                    ToolInvocationModel.execution_lease_digest == previous_digest,
                    ToolInvocationModel.execution_fence == previous_fence,
                    ToolInvocationModel.execution_lease_expires_at <= now,
                )
                .values(
                    execution_lease_digest=data.execution_lease_digest,
                    execution_fence=data.execution_fence,
                    execution_lease_expires_at=data.execution_lease_expires_at,
                    not_started_proof_json=proof,
                )
            ),
        )
        if result.rowcount != 1:
            raise ToolInvocationReplayConflict
        record = await self.get(existing.id)
        if record is None:  # pragma: no cover - guarded by conditional update
            raise ToolInvocationReplayConflict
        return record

    async def begin_model_execution(
        self,
        *,
        data: ModelToolInvocationClaimCreate,
        now: datetime,
    ) -> ToolInvocationRecord:
        """以active lease/fence把claimed提交为executing；调用方提交后才能铸造permit。"""

        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ToolInvocationModel)
                .where(
                    ToolInvocationModel.tool_call_id == data.tool_call_id,
                    ToolInvocationModel.execution_state == "claimed",
                    ToolInvocationModel.result_ref.is_(None),
                    ToolInvocationModel.execution_lease_digest == data.execution_lease_digest,
                    ToolInvocationModel.execution_fence == data.execution_fence,
                    ToolInvocationModel.execution_lease_expires_at > now,
                )
                .values(
                    execution_state="executing",
                    status="executing",
                    handler_started_at=now,
                )
            ),
        )
        if result.rowcount != 1:
            raise ToolInvocationReplayConflict
        record = await self.get_by_tool_call_id(data.tool_call_id)
        if record is None:  # pragma: no cover - guarded by conditional update
            raise ToolInvocationReplayConflict
        self.validate_model_claim_identity(record, data, include_lease=True)
        return record

    async def mark_model_claim_needs_review(
        self,
        *,
        tool_call_id: str,
        reason: str,
    ) -> ToolInvocationRecord:
        """把不确定claim单调关闭为needs-review，并保存去敏可复算摘要。"""

        if reason not in _MODEL_TOOL_EXECUTION_REVIEW_REASONS:
            raise ValueError("model tool execution review reason is invalid")
        model = await self._session.scalar(
            select(ToolInvocationModel)
            .where(ToolInvocationModel.tool_call_id == tool_call_id)
            .with_for_update()
        )
        if model is None:
            raise ToolInvocationReplayConflict
        if model.execution_state == "needs_review":
            return _tool_invocation_record(model)
        if model.execution_state not in {"claimed", "executing", "completed", "failed"}:
            raise ToolInvocationReplayConflict
        if model.execution_state in {"completed", "failed"} and model.result_ref is not None:
            raise ToolInvocationReplayConflict
        binding = model.binding_json
        if not isinstance(binding, dict):
            raise ToolInvocationReplayConflict
        binding_digest = _binding_digest(binding)
        review_preimage: dict[str, Any] = {
            "schema_version": "model-tool-execution-review-v1",
            "reason": reason,
            "tool_call_id": tool_call_id,
            "binding_digest": binding_digest,
            "observed_execution_state": model.execution_state,
            "execution_fence": model.execution_fence,
        }
        review = {
            **review_preimage,
            "evidence_digest": _canonical_proof_digest(review_preimage),
        }
        metadata = dict(model.metadata_json or {})
        metadata["model_tool_execution_review"] = review
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ToolInvocationModel)
                .where(
                    ToolInvocationModel.id == model.id,
                    ToolInvocationModel.execution_state == model.execution_state,
                    ToolInvocationModel.execution_lease_digest == model.execution_lease_digest,
                    ToolInvocationModel.execution_fence == model.execution_fence,
                    ToolInvocationModel.result_ref.is_(None),
                )
                .values(
                    execution_state="needs_review",
                    status="needs_review",
                    metadata_json=metadata,
                )
            ),
        )
        if result.rowcount != 1:
            raise ToolInvocationReplayConflict
        await self._session.refresh(model)
        return _tool_invocation_record(model)

    async def finish_model_claim(
        self,
        *,
        tool_call_id: str,
        execution_lease_digest: str,
        execution_fence: int,
        result_ref: str,
        execution_state: str,
        status: str,
    ) -> ToolInvocationRecord:
        """只有当前executing permit owner可封存确定性completed/failed结果。"""

        if execution_state not in {"completed", "failed"}:
            raise ValueError("model tool claim terminal must be completed or failed")
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ToolInvocationModel)
                .where(
                    ToolInvocationModel.tool_call_id == tool_call_id,
                    ToolInvocationModel.execution_state == "executing",
                    ToolInvocationModel.result_ref.is_(None),
                    ToolInvocationModel.execution_lease_digest == execution_lease_digest,
                    ToolInvocationModel.execution_fence == execution_fence,
                )
                .values(
                    result_ref=result_ref,
                    execution_state=execution_state,
                    status=status,
                )
            ),
        )
        if result.rowcount != 1:
            raise ToolInvocationReplayConflict
        record = await self.get_by_tool_call_id(tool_call_id)
        if record is None:  # pragma: no cover - guarded by conditional update
            raise ToolInvocationReplayConflict
        return record

    async def _model_claim_by_identities(
        self,
        data: ModelToolInvocationClaimCreate,
    ) -> ToolInvocationRecord | None:
        tool_claim = await self.get_by_tool_call_id(data.tool_call_id)
        approval_claim = (
            None if data.approval_id is None else await self.get_by_approval_id(data.approval_id)
        )
        if (
            tool_claim is not None
            and approval_claim is not None
            and tool_claim.id != approval_claim.id
        ):
            raise ToolInvocationReplayConflict
        return tool_claim or approval_claim

    @staticmethod
    def validate_model_claim_identity(
        existing: ToolInvocationRecord,
        data: ModelToolInvocationClaimCreate,
        *,
        include_lease: bool,
    ) -> None:
        """逐值比较受信binding；换租只排除三个owner lease字段。"""

        # needs-review证据是仓储在首次执行后追加的内部元数据，不属于调用方提交的
        # tool identity；重放仍逐值校验原始metadata，但不能要求调用方伪造恢复票据。
        existing_metadata = dict(existing.metadata)
        existing_metadata.pop("model_tool_execution_review", None)
        requested_metadata = dict(data.metadata)
        requested_metadata.pop("model_tool_execution_review", None)
        exact = (
            existing.tenant_id == data.tenant_id
            and existing.agent_id == data.agent_id
            and existing.run_id == data.run_id
            and existing.tool_name == data.tool_name
            and existing.args_ref == data.args_ref
            and existing.approval_id == data.approval_id
            and existing.arguments_hash == data.arguments_hash
            and existing.trace_id == data.trace_id
            and existing.request_id == data.request_id
            and existing.loop_id == data.loop_id
            and existing.turn_ordinal == data.turn_ordinal
            and existing.tool_call_id == data.tool_call_id
            and existing.binding == data.binding
            and existing_metadata == requested_metadata
        )
        if include_lease:
            exact = (
                exact
                and existing.execution_lease_digest == data.execution_lease_digest
                and existing.execution_fence == data.execution_fence
                and _as_utc_required(existing.execution_lease_expires_at)
                == _as_utc_required(data.execution_lease_expires_at)
            )
        if not exact:
            raise ToolInvocationReplayConflict

    async def finish_approved_claim(
        self,
        *,
        approval_id: str,
        result_ref: str,
        execution_state: str,
        status: str,
    ) -> ToolInvocationRecord:
        """用一个确定性 result artifact 封存唯一 approval claim。"""

        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ToolInvocationModel)
                .where(
                    ToolInvocationModel.approval_id == approval_id,
                    ToolInvocationModel.execution_state == "executing",
                    ToolInvocationModel.result_ref.is_(None),
                )
                .values(
                    result_ref=result_ref,
                    execution_state=execution_state,
                    status=status,
                )
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(f"approved tool claim cannot be finalized: {approval_id}")
        record = await self.get_by_approval_id(approval_id)
        if record is None:  # pragma: no cover - guarded by conditional update
            raise LookupError(f"approved tool claim not found: {approval_id}")
        return record
