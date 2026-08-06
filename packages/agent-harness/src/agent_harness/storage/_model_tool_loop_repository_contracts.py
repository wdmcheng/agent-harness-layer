"""模型工具循环 repository 的冻结 DTO 与稳定冲突信号。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO


class ModelToolLoopStorageConflict(RuntimeError):
    """loop identity、状态、version或lease不再匹配时的稳定失败。"""

    code = "model.tool_loop_replay_conflict"

    def __init__(self) -> None:
        super().__init__(self.code)


class ModelToolLoopFrozenBounds(HarnessDTO):
    """首次副作用前冻结且可逐值复算的loop上界。"""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["model-tool-loop-bounds-v1"] = "model-tool-loop-bounds-v1"
    max_turns: int = Field(ge=1, le=64, strict=True)
    max_total_tokens: int = Field(ge=1, strict=True)
    max_total_cost_usd: float | None = Field(ge=0)
    max_tool_output_bytes: int = Field(ge=1, le=1_048_576, strict=True)
    max_duration_seconds: int = Field(ge=1, le=3_600, strict=True)
    loop_started_at: datetime
    deadline_at: datetime

    @field_validator("max_total_cost_usd", mode="before")
    @classmethod
    def validate_cost_maximum(cls, value: object) -> object:
        """耐久冻结边界拒绝JSON bool与不可比较成本，避免恢复时静默改义。"""

        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("max_total_cost_usd must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_deadline(self) -> ModelToolLoopFrozenBounds:
        """deadline只能由冻结开始时间和duration推导，不能独立漂移。"""

        if any(
            value.tzinfo is None or value.utcoffset() is None
            for value in (
                self.loop_started_at,
                self.deadline_at,
            )
        ):
            raise ValueError("model tool loop bounds timestamps must be timezone-aware")
        if self.deadline_at != self.loop_started_at + timedelta(seconds=self.max_duration_seconds):
            raise ValueError("model tool loop bounds deadline mismatch")
        return self


class ModelToolLoopCumulativeUsage(HarnessDTO):
    """只投影已耐久settlement的连续模型回合累计值。"""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal["model-tool-loop-cumulative-usage-v1"] = (
        "model-tool-loop-cumulative-usage-v1"
    )
    turns_completed: int = Field(ge=0, strict=True)
    total_tokens_used: int = Field(ge=0, strict=True)
    total_cost_usd: float | None = Field(ge=0)

    @field_validator("total_cost_usd", mode="before")
    @classmethod
    def validate_total_cost(cls, value: object) -> object:
        """累计耐久成本只接受null或非bool有限非负number。"""

        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("total_cost_usd must be finite and non-negative")
        return value


class ModelToolLoopState(HarnessDTO):
    """当前可恢复step与owner refs的封闭耐久联合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["model-tool-loop-state-v1"] = "model-tool-loop-state-v1"
    next_step: Literal[
        "model_turn",
        "model_result",
        "approval_resume",
        "tool_execution",
        "terminal",
    ]
    model_usage_call_id: str | None
    tool_call_id: str | None
    approval_id: str | None
    checkpoint_ref: str | None
    context_ref: str | None
    next_request_digest: str | None

    @model_validator(mode="after")
    def validate_variant(self) -> ModelToolLoopState:
        """按step拒绝不完整current refs和已越过阶段的残留字段。"""

        usage = self.model_usage_call_id
        tool = self.tool_call_id
        approval = self.approval_id
        checkpoint = self.checkpoint_ref
        context = self.context_ref
        next_request = self.next_request_digest
        if self.next_step == "model_turn":
            initial = all(
                value is None
                for value in (usage, tool, approval, checkpoint, context, next_request)
            )
            continued = (
                usage is not None
                and tool is not None
                and context is not None
                and next_request is not None
                and ((approval is None) == (checkpoint is None))
            )
            if not (initial or continued):
                raise ValueError("model turn state refs are incomplete")
        elif self.next_step == "model_result":
            if usage is None or next_request is not None:
                raise ValueError("settled model result state refs are incomplete")
            if tool is None and any(value is not None for value in (approval, checkpoint, context)):
                raise ValueError("settled model result retained inconsistent prior refs")
            if approval is not None and checkpoint is None:
                raise ValueError("settled model result approval requires checkpoint ref")
            if context is not None and tool is None:
                raise ValueError("settled model result context requires tool ref")
        elif self.next_step == "approval_resume":
            if not (
                usage is not None
                and tool is not None
                and approval is None
                and checkpoint is not None
                and context is None
                and next_request is None
            ):
                raise ValueError("approval wait state refs are incomplete")
        elif self.next_step == "tool_execution":
            if not (
                usage is not None
                and tool is not None
                and approval is not None
                and checkpoint is not None
                and context is None
                and next_request is None
            ):
                raise ValueError("tool execution state refs are incomplete")
        else:
            if next_request is not None:
                raise ValueError("terminal state cannot retain a next request")
            if tool is not None and usage is None:
                raise ValueError("terminal tool ref requires model usage ref")
            if approval is not None and (tool is None or checkpoint is None):
                raise ValueError("terminal approval ref requires tool and checkpoint refs")
            if context is not None and tool is None:
                raise ValueError("terminal context ref requires tool ref")
        return self

    def terminal(self, *, model_usage_call_id: str | None = None) -> ModelToolLoopState:
        """保留已耐久current refs并封闭到terminal，不保留next request。"""

        return self.model_copy(
            update={
                "next_step": "terminal",
                "model_usage_call_id": model_usage_call_id or self.model_usage_call_id,
                "next_request_digest": None,
            }
        )


class ModelToolLoopCreate(HarnessDTO):
    """首次创建loop时冻结的identity、bound、usage摘要与owner lease。"""

    tenant_id: str
    run_id: str
    agent_id: str
    loop_id: str
    request_identity_digest: str
    operation_identity_digest: str
    catalog_digest: str
    frozen_bounds: ModelToolLoopFrozenBounds
    cumulative_usage: ModelToolLoopCumulativeUsage
    state: ModelToolLoopState
    owner_lease_digest: str
    owner_fence: int
    owner_lease_expires_at: datetime


class ModelToolLoopRecord(ModelToolLoopCreate):
    """调用方可持有的脱离ORM loop快照。"""

    id: str
    status: Literal[
        "active", "waiting_approval", "completed", "failed", "cancelled", "needs_review"
    ]
    next_turn_ordinal: int = Field(ge=1, strict=True)
    result_ref: str | None = None
    error_ref: str | None = None
    version: int
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def validate_record_state(self) -> ModelToolLoopRecord:
        """row status、ordinal、累计usage与终态refs必须描述同一位置。"""

        expected_turns = (
            self.next_turn_ordinal
            if self.state.next_step in {"model_result", "approval_resume", "tool_execution"}
            else self.next_turn_ordinal - 1
        )
        if self.cumulative_usage.turns_completed != expected_turns:
            raise ValueError("model tool loop ordinal and cumulative usage mismatch")
        if self.status == "active" and self.state.next_step not in {
            "model_turn",
            "model_result",
            "tool_execution",
        }:
            raise ValueError("active model tool loop state is invalid")
        if self.status == "waiting_approval" and self.state.next_step != "approval_resume":
            raise ValueError("waiting model tool loop state is invalid")
        if self.status in {"completed", "failed", "cancelled", "needs_review"}:
            if self.state.next_step != "terminal":
                raise ValueError("terminal model tool loop state is invalid")
        if self.status == "completed":
            if self.result_ref is None or self.error_ref is not None:
                raise ValueError("completed model tool loop references are invalid")
        elif self.status in {"failed", "cancelled", "needs_review"}:
            if self.result_ref is not None or self.error_ref is None:
                raise ValueError("failed model tool loop references are invalid")
        elif self.result_ref is not None or self.error_ref is not None:
            raise ValueError("non-terminal model tool loop cannot have terminal refs")
        return self


__all__ = [
    "ModelToolLoopStorageConflict",
    "ModelToolLoopFrozenBounds",
    "ModelToolLoopCumulativeUsage",
    "ModelToolLoopState",
    "ModelToolLoopCreate",
    "ModelToolLoopRecord",
]
