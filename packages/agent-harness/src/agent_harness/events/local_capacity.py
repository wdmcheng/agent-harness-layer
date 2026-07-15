"""Local JSONL 写入与 SQLite event capacity 账本的协调边界。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from agent_harness.events.capacity import (
    LocalCapacityCommitUncertain,
    UsageCapacitySettlement,
    usage_capacity_binding,
    validate_usage_capacity_outbox,
)
from agent_harness.events.types import CanonicalEvent
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    operation_event_capacity,
)


class LocalEventCapacityClaim:
    """在 JSONL append 前校验账本，并在 append 成功后提交同一 UoW。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        self._storage = storage

    @asynccontextmanager
    async def claim(self, event: CanonicalEvent) -> AsyncGenerator[None]:
        async with self._storage.uow() as uow:
            if event.record_scope == "non_run" and not await uow.event_capacity.exists(
                event.run_id
            ):
                yield
                return
            usage_binding = usage_capacity_binding(event)
            if usage_binding is not None:
                try:
                    outbox = await uow.evidence_outbox.get_usage(
                        tenant_id=event.tenant_id,
                        usage_call_id=usage_binding.usage_call_id,
                    )
                except LookupError:
                    outbox = None
                settlement = (
                    UsageCapacitySettlement(
                        tenant_id=outbox.tenant_id,
                        run_id=outbox.run_id,
                        usage_call_id=outbox.usage_call_id,
                        event_id=outbox.event_id,
                        operation_kind=outbox.operation_kind,
                        state=outbox.state,
                        reserved_event_count=outbox.reserved_event_count,
                        result_json=outbox.result_json,
                        error_code=outbox.error_code,
                    )
                    if outbox is not None
                    else None
                )
                reserved_event_count = validate_usage_capacity_outbox(
                    event=event,
                    binding=usage_binding,
                    outbox=settlement,
                    expected_reserved_event_count=operation_event_capacity(
                        EvidenceOperationKind(usage_binding.operation_kind)
                    ),
                )
            else:
                outbox = await uow.evidence_outbox.get_by_event_id(event_id=event.event_id)
                reserved_event_count = outbox.reserved_event_count if outbox is not None else 0
            await uow.event_capacity.record_local_event(
                run_id=event.run_id,
                seq=event.seq,
                reserved_event_count=reserved_event_count,
                terminal=event.terminal,
            )
            yield
            try:
                await uow.commit()
            except BaseException as commit_error:
                try:
                    async with self._storage.uow() as verifier:
                        snapshot = await verifier.event_capacity.snapshot(event.run_id)
                except BaseException as verification_error:
                    raise LocalCapacityCommitUncertain(
                        "local capacity commit outcome is uncertain"
                    ) from verification_error
                if snapshot.highest_persisted_seq >= event.seq:
                    return
                raise commit_error


__all__ = ["LocalEventCapacityClaim"]
