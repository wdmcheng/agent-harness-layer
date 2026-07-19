"""真实 Redis Streams 的 durable queue 条件合同。"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from agent_harness.adapters.queue.redis import RedisRunQueue
from agent_harness.runtime.queue import (
    QueueConflictError,
    StaleQueueReceiptError,
    UnsupportedQueueMessageError,
    build_execute_message,
    build_resume_approval_message,
)


def _redis_dsn() -> str:
    """读取真实 Redis 测试连接；未配置时跳过，避免 fake 替代存储语义证据。"""

    dsn = os.getenv("REDIS_TEST_DSN")
    if not dsn:
        pytest.skip("REDIS_TEST_DSN 未配置；fake 结果不能替代真实 Redis 证据")
    return dsn


@pytest.mark.asyncio
async def test_redis_dedupe_reclaim_fenced_ack_and_unknown_version() -> None:
    """验证真实 Streams 的去重、回收 fencing、确认权与未知版本隔离语义。"""

    dsn = _redis_dsn()
    namespace = f"agent-harness:test:{uuid4().hex}"
    queue = RedisRunQueue.from_dsn(dsn, namespace=namespace, group="workers")
    inspector = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        dsn, decode_responses=True
    )
    try:
        first = build_execute_message(
            request_id="req-first",
            tenant_id="tenant-1",
            run_id="run-1",
            idempotency_key="client-key",
        )
        retry = first.model_copy(update={"request_id": "req-second-attempt"})
        first_result = await queue.enqueue(first)
        retry_result = await queue.enqueue(retry)

        assert retry_result.message_id == first_result.message_id
        assert retry_result.message.request_id == "req-first"
        assert await inspector.xlen(queue.stream_name) == 1

        delivery_a = await queue.pickup(consumer_id="worker-a", block_milliseconds=10)
        assert delivery_a is not None
        delivery_b = await queue.reclaim(consumer_id="worker-b", min_idle_seconds=0)
        assert delivery_b is not None
        assert delivery_b.message_id == delivery_a.message_id
        assert delivery_b.delivery_count > delivery_a.delivery_count

        with pytest.raises(StaleQueueReceiptError):
            await queue.ack(delivery_a.receipt)
        await queue.ack(delivery_b.receipt)
        assert await queue.reclaim(consumer_id="worker-c", min_idle_seconds=0) is None

        await inspector.xadd(
            queue.stream_name,
            {
                "payload": (
                    '{"schema_version":2,"kind":"execute_run","request_id":"req",'
                    '"operation_id":"run:r:execute","idempotency_key":"run:r:execute",'
                    '"tenant_id":"t","run_id":"r"}'
                )
            },
        )
        with pytest.raises(UnsupportedQueueMessageError):
            await queue.pickup(consumer_id="worker-version", block_milliseconds=10)
        pending = await inspector.xpending(queue.stream_name, "workers")
        assert pending["pending"] == 1
    finally:
        await queue.cleanup_namespace()
        await queue.close()
        await inspector.aclose()


@pytest.mark.asyncio
async def test_redis_atomic_dedupe_conflict_and_distinct_approval_operations() -> None:
    """验证并发同操作共享首条消息，而不同审批 lease 维持独立队列操作。"""

    dsn = _redis_dsn()
    namespace = f"agent-harness:test:{uuid4().hex}"
    queue = RedisRunQueue.from_dsn(dsn, namespace=namespace, group="workers")
    inspector = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        dsn, decode_responses=True
    )
    try:
        execute = build_execute_message(
            request_id="req-first", tenant_id="tenant-1", run_id="run-1"
        )
        attempts = [
            execute.model_copy(update={"request_id": f"req-attempt-{index}"}) for index in range(8)
        ]
        results = await asyncio.gather(*(queue.enqueue(message) for message in attempts))

        assert len({result.message_id for result in results}) == 1
        first_request_ids = {result.message.request_id for result in results}
        assert len(first_request_ids) == 1
        assert first_request_ids <= {message.request_id for message in attempts}

        with pytest.raises(QueueConflictError):
            await queue.enqueue(execute.model_copy(update={"idempotency_key": "changed"}))

        approval_results = [
            await queue.enqueue(
                build_resume_approval_message(
                    request_id=f"req-approval-{lease}",
                    tenant_id="tenant-1",
                    run_id="run-1",
                    approval_id="approval-1",
                    resolution_lease_id=lease,
                )
            )
            for lease in ("lease-1", "lease-2")
        ]
        assert len({result.message_id for result in approval_results}) == 2
        assert await inspector.xlen(queue.stream_name) == 3
    finally:
        await queue.cleanup_namespace()
        await queue.close()
        await inspector.aclose()


@pytest.mark.asyncio
async def test_pickup_never_returns_receipt_owned_by_concurrent_reclaimer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """复现 XREADGROUP 后立刻被 reclaim 的窗口，旧 worker只能拿自己的 stale receipt。"""

    dsn = _redis_dsn()
    namespace = f"agent-harness:test:{uuid4().hex}"
    client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        dsn, decode_responses=True
    )
    queue = RedisRunQueue(client, namespace=namespace, group="workers")
    try:
        await queue.enqueue(
            build_execute_message(request_id="req", tenant_id="tenant", run_id="run")
        )
        original_xreadgroup = client.xreadgroup

        async def xread_then_reclaim(*args: Any, **kwargs: Any) -> object:
            """在读取后模拟竞争 worker 回收消息，暴露 receipt 所有权竞态。"""

            rows = await original_xreadgroup(*args, **kwargs)
            await client.xautoclaim(
                queue.stream_name,
                "workers",
                "worker-b",
                0,
                start_id="0-0",
                count=1,
            )
            return rows

        monkeypatch.setattr(client, "xreadgroup", xread_then_reclaim)
        delivery_a = await queue.pickup(consumer_id="worker-a", block_milliseconds=10)

        assert delivery_a is not None
        assert delivery_a.consumer_id == "worker-a"
        assert delivery_a.delivery_count == 1
        with pytest.raises(StaleQueueReceiptError):
            await queue.ack(delivery_a.receipt)
    finally:
        await queue.cleanup_namespace()
        await queue.close()
