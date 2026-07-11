"""Approval evidence 故障注入合同的共享 helper。"""

from collections.abc import Awaitable, Callable

from agent_harness.events import CanonicalEvent, CanonicalEventType


def fail_once_on_event(
    *,
    event_type: CanonicalEventType,
    mode: str,
    original_write: Callable[[CanonicalEvent], Awaitable[CanonicalEvent]],
) -> Callable[[CanonicalEvent], Awaitable[CanonicalEvent]]:
    """在目标 event 写入前或写入后只失败一次。"""

    failed = False

    async def write(event: CanonicalEvent) -> CanonicalEvent:
        nonlocal failed
        should_fail = not failed and event.event_type == event_type
        if should_fail and mode == "before":
            failed = True
            raise OSError(f"{event_type.value} sink unavailable")
        persisted = await original_write(event)
        if should_fail and mode == "after":
            failed = True
            raise OSError(f"{event_type.value} sink acknowledgement lost")
        return persisted

    return write
