"""公开 RunOrchestrator 组合根，具体职责由内部模块协作完成。"""

from __future__ import annotations

from agent_harness.runtime._orchestrator_base import (
    OrchestratorState,
)
from agent_harness.runtime._orchestrator_base import (
    RunEnqueueUnavailable as RunEnqueueUnavailable,
)
from agent_harness.runtime._queued_run_orchestration import QueuedRunOrchestration
from agent_harness.runtime._run_continuation import RunContinuation
from agent_harness.runtime._run_lifecycle import RunLifecycle


class RunOrchestrator(
    QueuedRunOrchestration,
    RunContinuation,
    RunLifecycle,
    OrchestratorState,
):
    """协调持久化 run、checkpoint、executor 与 CanonicalEvent 边界。

    公开 API 保持在这个 provider-neutral 组合根；内部 mixin 仅按 queued service、
    continuation 和 lifecycle 职责拆分，不暴露新的外部 composition seam。
    """
