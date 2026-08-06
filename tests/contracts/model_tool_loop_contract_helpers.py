"""模型工具loop仓储合同共用的exact初始JSON夹具。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict

from agent_harness.storage import (
    ModelToolLoopCumulativeUsage,
    ModelToolLoopFrozenBounds,
    ModelToolLoopState,
)


class ModelToolLoopInitialSnapshot(TypedDict):
    """可直接展开进ModelToolLoopCreate的强类型初始字段。"""

    frozen_bounds: ModelToolLoopFrozenBounds
    cumulative_usage: ModelToolLoopCumulativeUsage
    state: ModelToolLoopState


def initial_model_tool_loop_snapshot(
    *,
    started_at: datetime | None = None,
    duration_seconds: int = 60,
) -> ModelToolLoopInitialSnapshot:
    """返回首次副作用前必须完整持久化的bounds、usage与current refs。"""

    started = started_at or datetime(2030, 1, 1, tzinfo=UTC)
    return {
        "frozen_bounds": ModelToolLoopFrozenBounds(
            max_turns=4,
            max_total_tokens=4096,
            max_total_cost_usd=1.0,
            max_tool_output_bytes=8192,
            max_duration_seconds=duration_seconds,
            loop_started_at=started,
            deadline_at=started + timedelta(seconds=duration_seconds),
        ),
        "cumulative_usage": ModelToolLoopCumulativeUsage(
            turns_completed=0,
            total_tokens_used=0,
            total_cost_usd=0.0,
        ),
        "state": ModelToolLoopState(
            next_step="model_turn",
            model_usage_call_id=None,
            tool_call_id=None,
            approval_id=None,
            checkpoint_ref=None,
            context_ref=None,
            next_request_digest=None,
        ),
    }
