"""Durable run queue 的 provider-neutral DTO 与 fake 合同。"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from agent_harness.runtime.queue import (
    InMemoryRunQueue,
    QueueConflictError,
    RunQueueMessage,
    StaleQueueReceiptError,
    UnsupportedQueueMessageError,
    build_execute_message,
    build_resume_approval_message,
)


def _clock() -> tuple[Callable[[], float], Callable[[float], None]]:
    current = 0.0

    def now() -> float:
        return current

    def advance(seconds: float) -> None:
        nonlocal current
        current += seconds

    return now, advance


def test_execute_message_has_stable_identity_and_canonical_hash() -> None:
    message = build_execute_message(
        request_id="req-1",
        tenant_id="tenant-1",
        run_id="run-1",
    )
    with_client_key = build_execute_message(
        request_id="req-2",
        tenant_id="tenant-1",
        run_id="run-2",
        idempotency_key="client-key",
    )

    assert message.operation_id == "run:run-1:execute"
    assert message.idempotency_key == message.operation_id
    assert with_client_key.idempotency_key == "client-key"
    assert message.schema_version == 1
    assert message.kind == "execute_run"
    assert (
        message.canonical_json()
        == message.model_validate_json(message.canonical_json()).canonical_json()
    )
    assert len(message.protected_hash()) == 64


def test_approval_message_uses_lease_scoped_operation_and_only_refs() -> None:
    message = build_resume_approval_message(
        request_id="req-approval",
        tenant_id="tenant-1",
        run_id="run-1",
        approval_id="approval-1",
        resolution_lease_id="lease-1",
    )

    expected = "run:run-1:approval:approval-1:lease:lease-1"
    assert message.operation_id == expected
    assert message.idempotency_key == expected
    assert message.approval_id == "approval-1"
    assert message.resolution_lease_id == "lease-1"
    assert set(message.to_payload()) == {
        "schema_version",
        "kind",
        "request_id",
        "operation_id",
        "idempotency_key",
        "tenant_id",
        "run_id",
        "approval_id",
        "resolution_lease_id",
    }


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ({"request_id": ""}, "request_id"),
        ({"idempotency_key": ""}, "idempotency_key"),
        ({"schema_version": 2}, "schema_version"),
        ({"kind": "resume_approval"}, "approval_id"),
    ],
)
def test_invalid_message_fails_closed_with_field_path(
    payload: dict[str, object], path: str
) -> None:
    base: dict[str, object] = {
        "schema_version": 1,
        "kind": "execute_run",
        "request_id": "req-1",
        "operation_id": "run:run-1:execute",
        "idempotency_key": "run:run-1:execute",
        "tenant_id": "tenant-1",
        "run_id": "run-1",
    }
    base.update(payload)

    with pytest.raises(ValidationError) as exc_info:
        RunQueueMessage.model_validate(base)

    assert path in str(exc_info.value)


def test_unknown_serialized_version_has_stable_error() -> None:
    queue = InMemoryRunQueue()

    with pytest.raises(UnsupportedQueueMessageError, match="schema_version"):
        queue.decode_message(
            '{"schema_version":2,"kind":"execute_run","request_id":"req",'
            '"operation_id":"run:r:execute","idempotency_key":"run:r:execute",'
            '"tenant_id":"t","run_id":"r"}'
        )


@pytest.mark.asyncio
async def test_fake_reuses_first_message_and_rejects_protected_conflict() -> None:
    queue = InMemoryRunQueue()
    first = build_execute_message(
        request_id="req-first",
        tenant_id="tenant-1",
        run_id="run-1",
        idempotency_key="client-key",
    )
    retry = first.model_copy(update={"request_id": "req-attempt-2"})

    first_result = await queue.enqueue(first)
    retry_result = await queue.enqueue(retry)

    assert retry_result.message_id == first_result.message_id
    assert retry_result.message.request_id == "req-first"
    assert queue.message_count == 1

    conflict = first.model_copy(update={"idempotency_key": "different"})
    with pytest.raises(QueueConflictError):
        await queue.enqueue(conflict)


@pytest.mark.asyncio
async def test_fake_reclaim_fences_stale_ack_and_current_owner_can_ack() -> None:
    now, advance = _clock()
    queue = InMemoryRunQueue(clock=now)
    message = build_execute_message(
        request_id="req-1",
        tenant_id="tenant-1",
        run_id="run-1",
    )
    await queue.enqueue(message)

    delivery_a = await queue.pickup(consumer_id="worker-a")
    assert delivery_a is not None
    assert delivery_a.delivery_count == 1

    advance(31)
    delivery_b = await queue.reclaim(consumer_id="worker-b", min_idle_seconds=30)
    assert delivery_b is not None
    assert delivery_b.message_id == delivery_a.message_id
    assert delivery_b.delivery_count == 2

    with pytest.raises(StaleQueueReceiptError):
        await queue.ack(delivery_a.receipt)

    await queue.ack(delivery_b.receipt)
    assert await queue.reclaim(consumer_id="worker-c", min_idle_seconds=0) is None
    assert await queue.pickup(consumer_id="worker-c") is None


@pytest.mark.asyncio
async def test_execute_and_multiple_approval_operations_are_independent() -> None:
    queue = InMemoryRunQueue()
    messages = [
        build_execute_message(request_id="req-1", tenant_id="t", run_id="r"),
        build_resume_approval_message(
            request_id="req-2",
            tenant_id="t",
            run_id="r",
            approval_id="a-1",
            resolution_lease_id="lease-1",
        ),
        build_resume_approval_message(
            request_id="req-3",
            tenant_id="t",
            run_id="r",
            approval_id="a-1",
            resolution_lease_id="lease-2",
        ),
    ]

    results = [await queue.enqueue(message) for message in messages]

    assert len({result.message_id for result in results}) == 3
    assert queue.message_count == 3
