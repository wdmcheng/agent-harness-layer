"""模型工具循环耐久协调 repository 与 version/lease CAS。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage._model_tool_loop_repository_contracts import (
    ModelToolLoopCreate,
    ModelToolLoopCumulativeUsage,
    ModelToolLoopFrozenBounds,
    ModelToolLoopRecord,
    ModelToolLoopState,
    ModelToolLoopStorageConflict,
)
from agent_harness.storage._model_tool_loop_repository_support import (
    _record,
    require_model_tool_loop_active,
)
from agent_harness.storage.models import ModelToolLoopModel


class ModelToolLoopRepository:
    """只协调loop摘要；usage、tool、context与event仍由各自repository拥有。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ModelToolLoopCreate) -> ModelToolLoopRecord:
        """创建active loop；相同identity精确复用，不同preimage稳定冲突。"""

        existing = await self._get_model(data.tenant_id, data.loop_id)
        if existing is not None:
            self._validate_create_replay(existing, data)
            return _record(existing)
        model = ModelToolLoopModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            run_id=data.run_id,
            agent_id=data.agent_id,
            loop_id=data.loop_id,
            request_identity_digest=data.request_identity_digest,
            operation_identity_digest=data.operation_identity_digest,
            catalog_digest=data.catalog_digest,
            status="active",
            next_turn_ordinal=1,
            frozen_bounds_json=data.frozen_bounds.model_dump(mode="json"),
            cumulative_usage_json=data.cumulative_usage.model_dump(mode="json"),
            state_json=data.state.model_dump(mode="json"),
            result_ref=None,
            error_ref=None,
            version=1,
            owner_lease_digest=data.owner_lease_digest,
            owner_fence=data.owner_fence,
            owner_lease_expires_at=data.owner_lease_expires_at,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(model)
                await self._session.flush()
        except IntegrityError:
            existing = await self._get_model(data.tenant_id, data.loop_id)
            if existing is None:
                raise ModelToolLoopStorageConflict from None
            self._validate_create_replay(existing, data)
            return _record(existing)
        return _record(model)

    async def get(self, tenant_id: str, loop_id: str) -> ModelToolLoopRecord | None:
        """按稳定tenant-loop identity读取耐久快照。"""

        model = await self._get_model(tenant_id, loop_id)
        return None if model is None else _record(model)

    async def settle_model_turn(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        cumulative_usage: ModelToolLoopCumulativeUsage | dict[str, Any],
        state: ModelToolLoopState | dict[str, Any],
    ) -> ModelToolLoopRecord:
        """先耐久化当前model actual，再解释final或tool intent。"""

        current = await self._require_record(tenant_id, loop_id)
        usage = ModelToolLoopCumulativeUsage.model_validate(cumulative_usage)
        next_state = ModelToolLoopState.model_validate(state)
        if next_state.next_step != "model_result":
            raise ValueError("model settlement requires model_result state")
        if (
            current.status == "active"
            and current.next_turn_ordinal == usage.turns_completed
            and current.cumulative_usage == usage
            and current.state == next_state
        ):
            return current
        if (
            current.status != "active"
            or current.version != expected_version
            or current.owner_lease_digest != owner_lease_digest
            or current.owner_fence != owner_fence
        ):
            raise ModelToolLoopStorageConflict
        if current.state.next_step != "model_turn":
            raise ModelToolLoopStorageConflict
        if usage.turns_completed != current.next_turn_ordinal:
            raise ValueError("model settlement usage ordinal is not contiguous")
        return await self._transition(
            tenant_id=tenant_id,
            loop_id=loop_id,
            expected_status="active",
            next_status="active",
            expected_version=expected_version,
            owner_lease_digest=owner_lease_digest,
            owner_fence=owner_fence,
            values={
                "cumulative_usage_json": usage.model_dump(mode="json"),
                "state_json": next_state.model_dump(mode="json"),
            },
        )

    async def commit_turn(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        cumulative_usage: ModelToolLoopCumulativeUsage | dict[str, Any],
        state: ModelToolLoopState | dict[str, Any],
    ) -> ModelToolLoopRecord:
        """只允许active owner提交当前turn摘要并把next ordinal推进一位。"""

        current = await self._require_record(tenant_id, loop_id)
        if (
            current.status != "active"
            or current.version != expected_version
            or current.owner_lease_digest != owner_lease_digest
            or current.owner_fence != owner_fence
        ):
            raise ModelToolLoopStorageConflict
        usage = ModelToolLoopCumulativeUsage.model_validate(cumulative_usage)
        next_state = ModelToolLoopState.model_validate(state)
        if next_state.next_step != "model_turn":
            raise ValueError("committed turn must advance to model_turn")
        if usage.turns_completed != current.next_turn_ordinal:
            raise ValueError("committed turn usage ordinal is not contiguous")
        if current.state.next_step not in {"model_result", "tool_execution"}:
            raise ModelToolLoopStorageConflict
        return await self._transition(
            tenant_id=tenant_id,
            loop_id=loop_id,
            expected_status="active",
            next_status="active",
            expected_version=expected_version,
            owner_lease_digest=owner_lease_digest,
            owner_fence=owner_fence,
            values={
                "next_turn_ordinal": ModelToolLoopModel.next_turn_ordinal + 1,
                "cumulative_usage_json": usage.model_dump(mode="json"),
                "state_json": next_state.model_dump(mode="json"),
            },
        )

    async def wait_for_approval(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        state: ModelToolLoopState | dict[str, Any],
    ) -> ModelToolLoopRecord:
        """把active loop原子推进到waiting_approval。"""

        next_state = ModelToolLoopState.model_validate(state)
        if next_state.next_step != "approval_resume":
            raise ValueError("waiting loop requires approval_resume state")
        return await self._transition(
            tenant_id=tenant_id,
            loop_id=loop_id,
            expected_status="active",
            next_status="waiting_approval",
            expected_version=expected_version,
            owner_lease_digest=owner_lease_digest,
            owner_fence=owner_fence,
            values={"state_json": next_state.model_dump(mode="json")},
        )

    async def resume_after_approval(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        state: ModelToolLoopState | dict[str, Any],
    ) -> ModelToolLoopRecord:
        """只允许同一owner把waiting_approval恢复为active。"""

        next_state = ModelToolLoopState.model_validate(state)
        if next_state.next_step != "tool_execution":
            raise ValueError("resumed loop requires tool_execution state")
        return await self._transition(
            tenant_id=tenant_id,
            loop_id=loop_id,
            expected_status="waiting_approval",
            next_status="active",
            expected_version=expected_version,
            owner_lease_digest=owner_lease_digest,
            owner_fence=owner_fence,
            values={"state_json": next_state.model_dump(mode="json")},
        )

    async def terminate(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        status: Literal["completed"],
        result_ref: str,
        error_ref: None,
        cumulative_usage: ModelToolLoopCumulativeUsage | dict[str, Any] | None = None,
        state: ModelToolLoopState | dict[str, Any] | None = None,
    ) -> ModelToolLoopRecord:
        """把active loop提交为completed；联合terminal prerequisite由恢复层统一校验。"""

        current = await self._require_record(tenant_id, loop_id)
        usage = (
            current.cumulative_usage
            if cumulative_usage is None
            else ModelToolLoopCumulativeUsage.model_validate(cumulative_usage)
        )
        terminal_state = (
            current.state.terminal() if state is None else ModelToolLoopState.model_validate(state)
        )
        if terminal_state.next_step != "terminal":
            raise ValueError("completed loop requires terminal state")
        if usage.turns_completed < current.cumulative_usage.turns_completed:
            raise ValueError("completed loop cumulative usage cannot regress")
        ordinal_delta = usage.turns_completed - current.cumulative_usage.turns_completed
        if ordinal_delta not in {0, 1}:
            raise ValueError("completed loop usage must close at most one current turn")
        closes_settled_model = (
            current.state.next_step in {"model_result", "approval_resume", "tool_execution"}
            and usage.turns_completed == current.next_turn_ordinal
        )
        return await self._commit_terminal(
            tenant_id=tenant_id,
            loop_id=loop_id,
            expected_status="active",
            next_status=status,
            expected_version=expected_version,
            owner_lease_digest=owner_lease_digest,
            owner_fence=owner_fence,
            values={
                "next_turn_ordinal": current.next_turn_ordinal
                + max(ordinal_delta, int(closes_settled_model)),
                "cumulative_usage_json": usage.model_dump(mode="json"),
                "state_json": terminal_state.model_dump(mode="json"),
                "result_ref": result_ref,
                "error_ref": error_ref,
            },
        )

    async def fail(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        status: Literal["failed", "cancelled", "needs_review"],
        error_ref: str,
        expected_status: Literal["active", "waiting_approval"] = "active",
    ) -> ModelToolLoopRecord:
        """从active确定性提交失败、取消或needs-review终态。"""

        return await self._commit_terminal(
            tenant_id=tenant_id,
            loop_id=loop_id,
            expected_status=expected_status,
            next_status=status,
            expected_version=expected_version,
            owner_lease_digest=owner_lease_digest,
            owner_fence=owner_fence,
            values={"result_ref": None, "error_ref": error_ref},
        )

    async def cancel(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_status: Literal["active", "waiting_approval"],
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        error_ref: str,
    ) -> ModelToolLoopRecord:
        """从active或waiting审批提交唯一cancelled终态。"""

        return await self._commit_terminal(
            tenant_id=tenant_id,
            loop_id=loop_id,
            expected_status=expected_status,
            next_status="cancelled",
            expected_version=expected_version,
            owner_lease_digest=owner_lease_digest,
            owner_fence=owner_fence,
            values={"result_ref": None, "error_ref": error_ref},
        )

    async def expire_deadline(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_status: Literal["active", "waiting_approval"],
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        expired_at: datetime,
        error_ref: str,
        cumulative_usage: ModelToolLoopCumulativeUsage | dict[str, Any] | None = None,
        state: ModelToolLoopState | dict[str, Any] | None = None,
    ) -> ModelToolLoopRecord:
        """以冻结deadline为界终结过期owner，不把租约临时续活后再写终态。"""

        if expired_at.tzinfo is None or expired_at.utcoffset() is None:
            raise ValueError("model tool loop deadline must be timezone-aware")
        next_status = "failed" if expected_status == "active" else "cancelled"
        values: dict[str, object] = {"result_ref": None, "error_ref": error_ref}
        if cumulative_usage is not None or state is not None:
            if cumulative_usage is None or state is None:
                raise ValueError("deadline model settlement requires usage and state together")
            current = await self._require_record(tenant_id, loop_id)
            usage = ModelToolLoopCumulativeUsage.model_validate(cumulative_usage)
            terminal_state = ModelToolLoopState.model_validate(state)
            if (
                expected_status != "active"
                or terminal_state.next_step != "terminal"
                or usage.turns_completed != current.next_turn_ordinal
            ):
                raise ValueError("deadline model settlement is not contiguous")
            values.update(
                {
                    "next_turn_ordinal": current.next_turn_ordinal + 1,
                    "cumulative_usage_json": usage.model_dump(mode="json"),
                    "state_json": terminal_state.model_dump(mode="json"),
                }
            )
        return await self._commit_terminal(
            tenant_id=tenant_id,
            loop_id=loop_id,
            expected_status=expected_status,
            next_status=next_status,
            expected_version=expected_version,
            owner_lease_digest=owner_lease_digest,
            owner_fence=owner_fence,
            values=values,
            lease_expired_at=expired_at,
        )

    async def _commit_terminal(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_status: str,
        next_status: str,
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        values: dict[str, object],
        lease_expired_at: datetime | None = None,
    ) -> ModelToolLoopRecord:
        """所有terminal来源共用同一status/version/lease CAS提交点。"""

        if next_status not in {"completed", "failed", "cancelled", "needs_review"}:
            raise ValueError("model tool loop terminal status is invalid")
        result_ref = values.get("result_ref")
        error_ref = values.get("error_ref")
        if (next_status == "completed") != (isinstance(result_ref, str) and error_ref is None):
            raise ValueError("model tool loop terminal references are invalid")
        if next_status != "completed" and not (result_ref is None and isinstance(error_ref, str)):
            raise ValueError("model tool loop terminal references are invalid")
        if "state_json" not in values:
            current = await self._require_record(tenant_id, loop_id)
            values = {
                **values,
                "state_json": current.state.terminal().model_dump(mode="json"),
            }
            if current.state.next_step in {
                "model_result",
                "approval_resume",
                "tool_execution",
            }:
                values["next_turn_ordinal"] = current.next_turn_ordinal + 1
        return await self._transition(
            tenant_id=tenant_id,
            loop_id=loop_id,
            expected_status=expected_status,
            next_status=next_status,
            expected_version=expected_version,
            owner_lease_digest=owner_lease_digest,
            owner_fence=owner_fence,
            values=values,
            lease_expired_at=lease_expired_at,
        )

    async def _transition(
        self,
        *,
        tenant_id: str,
        loop_id: str,
        expected_status: str,
        next_status: str,
        expected_version: int,
        owner_lease_digest: str,
        owner_fence: int,
        values: dict[str, object],
        lease_expired_at: datetime | None = None,
    ) -> ModelToolLoopRecord:
        """以status/version/lease/fence联合CAS执行一次单调转换。"""

        lease_guard = (
            ModelToolLoopModel.owner_lease_expires_at > datetime.now(UTC)
            if lease_expired_at is None
            else ModelToolLoopModel.owner_lease_expires_at <= lease_expired_at
        )
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(ModelToolLoopModel)
                .where(
                    ModelToolLoopModel.tenant_id == tenant_id,
                    ModelToolLoopModel.loop_id == loop_id,
                    ModelToolLoopModel.status == expected_status,
                    ModelToolLoopModel.version == expected_version,
                    ModelToolLoopModel.owner_lease_digest == owner_lease_digest,
                    ModelToolLoopModel.owner_fence == owner_fence,
                    lease_guard,
                )
                .values(status=next_status, version=expected_version + 1, **values)
            ),
        )
        if result.rowcount != 1:
            raise ModelToolLoopStorageConflict
        model = await self._get_model(tenant_id, loop_id)
        if model is None:
            raise ModelToolLoopStorageConflict
        return _record(model)

    async def _get_model(self, tenant_id: str, loop_id: str) -> ModelToolLoopModel | None:
        return await self._session.scalar(
            select(ModelToolLoopModel).where(
                ModelToolLoopModel.tenant_id == tenant_id,
                ModelToolLoopModel.loop_id == loop_id,
            )
        )

    async def _require_record(self, tenant_id: str, loop_id: str) -> ModelToolLoopRecord:
        """在写入JSON字段前先取得并验证同一row的exact当前快照。"""

        model = await self._get_model(tenant_id, loop_id)
        if model is None:
            raise ModelToolLoopStorageConflict
        try:
            return _record(model)
        except ValueError:
            raise ModelToolLoopStorageConflict from None

    @staticmethod
    def _validate_create_replay(
        model: ModelToolLoopModel,
        data: ModelToolLoopCreate,
    ) -> None:
        exact = (
            model.run_id == data.run_id
            and model.agent_id == data.agent_id
            and model.request_identity_digest == data.request_identity_digest
            and model.operation_identity_digest == data.operation_identity_digest
            and model.catalog_digest == data.catalog_digest
            and model.frozen_bounds_json == data.frozen_bounds.model_dump(mode="json")
        )
        if not exact:
            raise ModelToolLoopStorageConflict


__all__ = [
    "ModelToolLoopCumulativeUsage",
    "ModelToolLoopCreate",
    "ModelToolLoopFrozenBounds",
    "ModelToolLoopRecord",
    "ModelToolLoopRepository",
    "ModelToolLoopState",
    "ModelToolLoopStorageConflict",
    "require_model_tool_loop_active",
]
