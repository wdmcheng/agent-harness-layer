"""模型工具循环的Limit职责。"""
# pyright: reportPrivateUsage=false, reportUnusedClass=false

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from agent_harness.models.tool_intent import (
    tool_loop_identity_digest,
)
from agent_harness.models.usage import ModelUsageEvidence
from agent_harness.runtime._model_tool_loop_contracts import (
    ModelToolLoopError,
    ModelToolLoopLimitOverrides,
    ModelToolLoopLimitState,
)
from agent_harness.runtime._model_tool_loop_mixin_base import _ModelToolLoopMixinBase
from agent_harness.tools.types import ToolRuntimeContext


class _ModelToolLoopLimitMixin(_ModelToolLoopMixinBase):
    def _trusted_monotonic_now(self) -> float:
        """读取单进程递增时钟并拒绝bool、NaN与无穷值。"""

        try:
            value = self._monotonic_clock()
            normalized = float(value)
            if not math.isfinite(normalized):
                raise ValueError("monotonic clock returned a non-finite value")
            return normalized
        except Exception:
            raise ModelToolLoopError("model.tool_loop_limit_invalid") from None

    def _monotonic_deadline_key(
        self,
        state: ModelToolLoopLimitState,
    ) -> tuple[str, str, str, datetime, datetime]:
        """把进程内guard绑定到可信run和耐久UTC起止时间。"""

        return (
            self._context.tenant_id,
            self._context.run_id,
            self._context.agent_id,
            state.loop_started_at.astimezone(UTC),
            state.deadline_at.astimezone(UTC),
        )

    def _register_monotonic_deadline(
        self,
        state: ModelToolLoopLimitState,
        *,
        wall_now: datetime,
    ) -> float:
        """首次绑定时按持久化剩余时长建立单进程guard，绝不向后延长。"""

        key = self._monotonic_deadline_key(state)
        existing = self._monotonic_deadlines.get(key)
        if existing is not None:
            return existing
        normalized_wall = wall_now.astimezone(UTC)
        started_at = state.loop_started_at.astimezone(UTC)
        if normalized_wall < started_at:
            # 新进程无法证明回拨前已消耗的时间；关闭失败比重新授予完整窗口安全。
            raise ModelToolLoopError("model.tool_loop_limit_invalid")
        remaining = max(
            0.0,
            min(
                float(state.max_duration_seconds),
                (state.deadline_at.astimezone(UTC) - normalized_wall).total_seconds(),
            ),
        )
        deadline = self._trusted_monotonic_now() + remaining
        self._monotonic_deadlines[key] = deadline
        return deadline

    def _freeze_limits(
        self,
        overrides: ModelToolLoopLimitOverrides | None,
    ) -> ModelToolLoopLimitState:
        """在任何catalog/model/tool副作用前冻结Agent maxima与可选缩权。"""

        maxima = self._loop_limits
        if maxima is None:
            raise ModelToolLoopError("model.tool_intent_invalid")
        if overrides is not None and type(overrides) is not ModelToolLoopLimitOverrides:
            raise ModelToolLoopError("model.tool_loop_limit_invalid")
        override = overrides

        def narrowed_int(name: str, maximum: int) -> int:
            value = None if override is None else getattr(override, name)
            if value is None:
                return maximum
            if type(value) is not int or value > maximum:
                raise ModelToolLoopError("model.tool_loop_limit_invalid")
            return value

        max_cost = maxima.max_total_cost_usd
        override_cost = None if override is None else override.max_total_cost_usd
        if override_cost is not None and max_cost is not None and override_cost > max_cost:
            raise ModelToolLoopError("model.tool_loop_limit_invalid")
        effective_cost = max_cost if override_cost is None else override_cost
        try:
            now = self._trusted_clock()
            if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("trusted clock returned an invalid datetime")
            started_at = now.astimezone(UTC)
            duration = narrowed_int("max_duration_seconds", maxima.max_duration_seconds)
            state = ModelToolLoopLimitState(
                max_turns=narrowed_int("max_turns", maxima.max_turns),
                max_total_tokens=narrowed_int("max_total_tokens", maxima.max_total_tokens),
                max_total_cost_usd=effective_cost,
                max_tool_output_bytes=narrowed_int(
                    "max_tool_output_bytes", maxima.max_tool_output_bytes
                ),
                max_duration_seconds=duration,
                loop_started_at=started_at,
                deadline_at=started_at + timedelta(seconds=duration),
                total_tokens_used=0,
                total_cost_usd=0.0,
            )
            self._register_monotonic_deadline(state, wall_now=started_at)
            return state
        except ModelToolLoopError:
            raise
        except Exception:
            raise ModelToolLoopError("model.tool_loop_limit_invalid") from None

    def _check_deadline(self, state: ModelToolLoopLimitState) -> None:
        """每个模型、工具与Context副作用边界前后重验同一绝对deadline。"""

        try:
            now = self._trusted_clock()
            if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("trusted clock returned an invalid datetime")
        except Exception:
            raise ModelToolLoopError("model.tool_loop_limit_invalid") from None
        monotonic_deadline = self._register_monotonic_deadline(state, wall_now=now)
        if (
            now.astimezone(UTC) >= state.deadline_at.astimezone(UTC)
            or self._trusted_monotonic_now() >= monotonic_deadline
        ):
            raise ModelToolLoopError("model.tool_loop_limit_exceeded")

    @staticmethod
    def _check_model_budget_remaining(state: ModelToolLoopLimitState) -> None:
        """没有正余额时禁止启动下一次model reservation/client/provider。"""

        if state.total_tokens_used >= state.max_total_tokens or (
            state.max_total_cost_usd is not None
            and state.total_cost_usd is not None
            and state.total_cost_usd >= state.max_total_cost_usd
        ):
            raise ModelToolLoopError("model.tool_loop_limit_exceeded")

    def _check_tool_can_continue(
        self,
        state: ModelToolLoopLimitState,
        *,
        turn_ordinal: int,
    ) -> None:
        """工具之后必有下一模型轮，因此在Registry/handler前验证可继续性。"""

        self._check_deadline(state)
        self._check_model_budget_remaining(state)
        if turn_ordinal >= state.max_turns:
            raise ModelToolLoopError("model.tool_loop_limit_exceeded")

    async def _account_turn_usage(
        self,
        state: ModelToolLoopLimitState,
        *,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
    ) -> ModelToolLoopLimitState:
        """只从既有durable model settlement读取actual token/cost并单调累计。"""

        try:
            raw = await self._model_turns.read_tool_loop_turn_usage(
                context=self._context,
                usage_call_id=usage_call_id,
                loop_id=loop_id,
                turn_ordinal=turn_ordinal,
            )
            if type(raw) is not ModelUsageEvidence:
                raise ValueError("model usage reader returned an invalid DTO")
            usage = ModelUsageEvidence.model_validate(
                ModelUsageEvidence.model_dump(raw, mode="python")
            ).model_copy(deep=True)
        except Exception:
            raise ModelToolLoopError("model.tool_loop_needs_review") from None
        if (
            usage.usage_kind != "model"
            or usage.tenant_id != self._context.tenant_id
            or usage.run_id != self._context.run_id
            or usage.agent_id != self._context.agent_id
            or usage.request_id != self._context.request_id
            or usage.trace_id != self._context.trace_id
            or usage.input_tokens is None
            or usage.output_tokens is None
        ):
            raise ModelToolLoopError("model.tool_loop_needs_review")
        total_tokens = state.total_tokens_used + usage.input_tokens + usage.output_tokens
        if total_tokens > state.max_total_tokens:
            raise ModelToolLoopError("model.tool_loop_limit_exceeded")
        if usage.cost_usd is None:
            if state.max_total_cost_usd is not None:
                raise ModelToolLoopError("model.tool_loop_needs_review")
            total_cost = None
        else:
            total_cost = (state.total_cost_usd or 0.0) + usage.cost_usd
            if state.max_total_cost_usd is not None and total_cost > state.max_total_cost_usd:
                raise ModelToolLoopError("model.tool_loop_limit_exceeded")
        try:
            return ModelToolLoopLimitState.model_validate(
                {
                    **state.model_dump(mode="python"),
                    "total_tokens_used": total_tokens,
                    "total_cost_usd": total_cost,
                }
            )
        except ValidationError:
            raise ModelToolLoopError("model.tool_loop_needs_review") from None

    def _loop_id(self, operation_key: str) -> str:
        """只从绑定运行身份与语义operation派生唯一loop id。"""

        return tool_loop_identity_digest(
            tenant_id=self._context.tenant_id,
            run_id=self._context.run_id,
            agent_id=self._context.agent_id,
            request_id=self._context.request_id,
            trace_id=self._context.trace_id,
            operation_key=operation_key,
        )

    def _tool_context(self) -> ToolRuntimeContext:
        """从绑定身份生成唯一工具运行上下文，不接受调用方覆盖。"""

        return ToolRuntimeContext(
            actor=self._identity,
            agent_id=self._context.agent_id,
            run_id=self._context.run_id,
            request_id=self._context.request_id,
            trace_id=self._context.trace_id,
        )
