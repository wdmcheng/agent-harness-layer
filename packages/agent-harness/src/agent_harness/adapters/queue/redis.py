"""基于 Redis Streams consumer group 的 durable run queue adapter。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agent_harness.runtime.queue import (
    QueueConflictError,
    QueueDelivery,
    QueueEnqueueResult,
    QueueReceipt,
    RunQueueMessage,
    StaleQueueReceiptError,
    UnsupportedQueueMessageError,
)

# 同一 Lua 脚本内完成去重索引读取、冲突检查、XADD 与索引写入；拆成多个 Redis 调用会
# 让并发投递在消息存在但保护摘要未登记，或反向情况中留下无法安全解释的状态。
_ENQUEUE_SCRIPT = """
local existing_hash = redis.call('HGET', KEYS[2], 'protected_hash')
if existing_hash then
  if existing_hash ~= ARGV[1] then
    return redis.error_reply('QUEUE_OPERATION_CONFLICT')
  end
  return {
    redis.call('HGET', KEYS[2], 'message_id'),
    redis.call('HGET', KEYS[2], 'payload')
  }
end
local message_id = redis.call('XADD', KEYS[1], '*', 'payload', ARGV[2])
redis.call(
  'HSET', KEYS[2],
  'protected_hash', ARGV[1],
  'message_id', message_id,
  'payload', ARGV[2]
)
return {message_id, ARGV[2]}
"""

# 确认前从 PEL 读取当前 owner 和 delivery count，确保被 reclaim 的旧 worker 不能
# 只凭 message id 确认新 owner 的投递。
_ACK_SCRIPT = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #pending ~= 1 then
  return 0
end
if pending[1][2] ~= ARGV[3] or tonumber(pending[1][4]) ~= tonumber(ARGV[4]) then
  return 0
end
return redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
"""

# XAUTOCLAIM 返回后再次核验 PEL owner；这一步将 Redis 的重领结果转换为本项目 receipt
# fencing 语义，避免竞争中的所有权变化被静默接受。
_RECLAIM_SCRIPT = """
local claimed = redis.call(
  'XAUTOCLAIM', KEYS[1], ARGV[1], ARGV[2], ARGV[3], '0-0', 'COUNT', 1
)
if #claimed[2] == 0 then
  return {}
end
local message_id = claimed[2][1][1]
local fields = claimed[2][1][2]
local payload = nil
for index = 1, #fields, 2 do
  if fields[index] == 'payload' then
    payload = fields[index + 1]
    break
  end
end
if not payload then
  return redis.error_reply('QUEUE_PAYLOAD_MISSING')
end
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], message_id, message_id, 1)
if #pending ~= 1 or pending[1][2] ~= ARGV[2] then
  return redis.error_reply('QUEUE_RECLAIM_FENCE_LOST')
end
return {message_id, payload, pending[1][4]}
"""


class RedisRunQueue:
    """Redis Streams 适配器；应用结果持久化后才允许调用 ``ack``。

    stream 只交接稳定操作引用，耐久业务事实仍在数据库。去重索引、PEL 所有权和 delivery
    count 共同实现与内存替身一致的幂等、重领和确认栅栏语义。
    """

    def __init__(
        self,
        client: Redis,
        *,
        namespace: str = "agent-harness:service:runs",
        group: str = "agent-harness-workers",
    ) -> None:
        """绑定 Redis 客户端、命名空间与消费组，并为组初始化维护进程内双检锁。"""

        self._client = client
        self._namespace = namespace.rstrip(":")
        self._stream = f"{self._namespace}:stream"
        self._group = group
        self._group_ready = False
        self._group_lock = asyncio.Lock()

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        namespace: str = "agent-harness:service:runs",
        group: str = "agent-harness-workers",
    ) -> RedisRunQueue:
        """从连接字符串创建 decode-responses 客户端，保持消息字段在适配层内统一为文本。"""

        # redis-py 的 from_url kwargs 在当前 typing 中标为 Unknown；返回边界仍是 Redis。
        client = Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
            dsn, decode_responses=True
        )
        return cls(client, namespace=namespace, group=group)

    @property
    def stream_name(self) -> str:
        """返回当前 adapter 使用的完整 stream 名称，供受控运维和集成测试定位。"""

        return self._stream

    async def enqueue(self, message: RunQueueMessage) -> QueueEnqueueResult:
        """原子写入或复用同一租户、操作键的消息；保护字段漂移时拒绝覆盖。"""

        await self._ensure_group()
        dedupe_key = self._dedupe_key(message)
        try:
            raw = await self._client.eval(
                _ENQUEUE_SCRIPT,
                2,
                self._stream,
                dedupe_key,
                message.protected_hash(),
                message.canonical_json(),
            )
        except ResponseError as exc:
            if "QUEUE_OPERATION_CONFLICT" in str(exc):
                raise QueueConflictError("queue operation protected fields conflict") from exc
            raise
        result = cast(list[object], raw)
        # 始终重新解析 Redis 保存的 payload，保证复用结果与首次入队走相同版本校验。
        message_id = self._as_text(result[0])
        persisted = self.decode_message(self._as_text(result[1]))
        return QueueEnqueueResult(message_id=message_id, message=persisted)

    async def pickup(
        self, *, consumer_id: str, block_milliseconds: int = 0
    ) -> QueueDelivery | None:
        """从消费组领取从未投递的消息，并生成首次 delivery count 为一的 receipt 坐标。"""

        await self._ensure_group()
        raw = await self._client.xreadgroup(
            self._group,
            consumer_id,
            {self._stream: ">"},
            count=1,
            block=block_milliseconds or None,
        )
        rows = cast(list[tuple[object, list[tuple[object, dict[object, object]]]]], raw)
        if not rows or not rows[0][1]:
            return None
        message_id_raw, fields = rows[0][1][0]
        message_id = self._as_text(message_id_raw)
        message = self.decode_message(self._field(fields, "payload"))
        # `>` 只返回从未投递过的新 entry，因此本 consumer 的 delivery count 固定为 1。
        # 不再查询当前 PEL owner；查询会把 reclaim 后的新 owner receipt 泄漏给旧 worker。
        return QueueDelivery(
            message=message,
            stream=self._stream,
            group=self._group,
            message_id=message_id,
            consumer_id=consumer_id,
            delivery_count=1,
        )

    async def reclaim(self, *, consumer_id: str, min_idle_seconds: float) -> QueueDelivery | None:
        """领取超过空闲阈值的 pending 消息，并返回 Redis 已增加的 delivery count。"""

        await self._ensure_group()
        raw = await self._client.eval(
            _RECLAIM_SCRIPT,
            1,
            self._stream,
            self._group,
            consumer_id,
            max(0, int(min_idle_seconds * 1000)),
        )
        result = cast(list[object], raw)
        if not result:
            return None
        message_id = self._as_text(result[0])
        message = self.decode_message(self._as_text(result[1]))
        return QueueDelivery(
            message=message,
            stream=self._stream,
            group=self._group,
            message_id=message_id,
            consumer_id=consumer_id,
            delivery_count=int(cast(int | str, result[2])),
        )

    async def ack(self, receipt: QueueReceipt) -> None:
        """只确认仍与当前 stream、group、消费者和 delivery count 完全匹配的 receipt。"""

        if receipt.stream != self._stream or receipt.group != self._group:
            raise StaleQueueReceiptError("queue receipt stream/group mismatch")
        raw = await self._client.eval(
            _ACK_SCRIPT,
            1,
            self._stream,
            self._group,
            receipt.message_id,
            receipt.consumer_id,
            receipt.delivery_count,
        )
        if int(cast(int | str, raw)) != 1:
            raise StaleQueueReceiptError("queue receipt ownership is stale")

    def decode_message(self, payload: str | bytes) -> RunQueueMessage:
        """未知版本保持 pending，由兼容 worker 后续 reclaim。

        先检查 schema 版本而不是直接宽松构造 DTO，使当前 worker 不会误确认未来版本的
        消息；该消息仍留在消费组 pending list 中等待兼容实现领取。
        """

        import json

        decoded: object = json.loads(payload)
        if not isinstance(decoded, dict):
            raise UnsupportedQueueMessageError("unsupported schema_version")
        raw = cast(dict[str, object], decoded)
        if raw.get("schema_version") != 1:
            raise UnsupportedQueueMessageError("unsupported schema_version")
        return RunQueueMessage.model_validate(raw)

    async def close(self) -> None:
        """关闭 Redis 客户端连接；持久消息和消费组状态仍保留在服务端。"""

        await self._client.aclose()

    async def cleanup_namespace(self) -> None:
        """删除本 adapter namespace；仅供隔离测试和显式运维清理。"""

        keys: list[str] = []
        iterator = cast(
            AsyncIterator[object],
            self._client.scan_iter(  # pyright: ignore[reportUnknownMemberType]
                match=f"{self._namespace}:*"
            ),
        )
        async for key in iterator:
            keys.append(self._as_text(key))
        if keys:
            await self._client.delete(*keys)
        self._group_ready = False

    async def _ensure_group(self) -> None:
        """惰性创建消费组，并用进程内锁避免同一 adapter 的并发重复初始化。"""

        if self._group_ready:
            return
        async with self._group_lock:
            if self._group_ready:
                return
            try:
                await self._client.xgroup_create(
                    self._stream,
                    self._group,
                    id="0-0",
                    mkstream=True,
                )
            except ResponseError as exc:
                # 多进程同时初始化时已有组是正常结果，其余 Redis 错误必须向上暴露。
                if "BUSYGROUP" not in str(exc):
                    raise
            self._group_ready = True

    def _dedupe_key(self, message: RunQueueMessage) -> str:
        """由租户和稳定操作键生成不泄露原始标识的命名空间去重索引键。"""

        identity = f"{message.tenant_id}\0{message.operation_id}".encode()
        digest = hashlib.sha256(identity).hexdigest()
        return f"{self._namespace}:dedupe:{digest}"

    @classmethod
    def _field(cls, fields: dict[object, object], name: str) -> str:
        """从 redis-py 可能返回文本或 bytes 键的字段映射读取必填字段并规范化为文本。"""

        value = fields.get(name)
        if value is None:
            value = fields.get(name.encode())
        if value is None:
            raise UnsupportedQueueMessageError(f"missing queue field: {name}")
        return cls._as_text(value)

    @staticmethod
    def _as_text(value: object) -> str:
        """把 redis-py 的 bytes 或其他标量统一转换为文本，保持 DTO 解码入口单一。"""

        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)


__all__ = ["RedisRunQueue"]
