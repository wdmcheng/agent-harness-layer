"""Run 生命周期状态。"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    """运行状态机的持久化枚举；终态集合由同模块常量统一派生。"""

    CREATED = "created"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
