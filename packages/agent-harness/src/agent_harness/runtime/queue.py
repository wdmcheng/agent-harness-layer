"""跨进程 run queue 的稳定 DTO、协议与内存替身。"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Literal, Protocol, cast

from pydantic import Field, ValidationInfo, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO

QueueMessageKind = Literal["execute_run", "resume_approval"]


class QueueError(RuntimeError):
    """Queue seam 的稳定基础异常。"""


class QueueConflictError(QueueError):
    """同 tenant/operation 的受保护字段发生冲突。"""


class StaleQueueReceiptError(QueueError):
    """delivery ownership 已变化，旧 receipt 不得确认。"""


class UnsupportedQueueMessageError(QueueError):
    """当前 worker 不支持 message schema version。"""


class RunQueueMessage(HarnessDTO):
    """Redis 只承载稳定 refs，不复制执行输入或身份真相源。"""

    schema_version: Literal[1] = 1
    kind: QueueMessageKind
    request_id: str
    operation_id: str
    idempotency_key: str
    tenant_id: str
    run_id: str
    approval_id: str | None = Field(default=None, validate_default=True)
    resolution_lease_id: str | None = Field(default=None, validate_default=True)

    @field_validator(
        "request_id",
        "operation_id",
        "idempotency_key",
        "tenant_id",
        "run_id",
        "approval_id",
        "resolution_lease_id",
    )
    @classmethod
    def _non_empty_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be empty")
        return normalized

    @field_validator("approval_id")
    @classmethod
    def _approval_ref_required(cls, value: str | None, info: ValidationInfo) -> str | None:
        if info.data.get("kind") == "resume_approval" and value is None:
            raise ValueError("approval_id is required for resume_approval")
        return value

    @field_validator("resolution_lease_id")
    @classmethod
    def _lease_ref_required(cls, value: str | None, info: ValidationInfo) -> str | None:
        if info.data.get("kind") == "resume_approval" and value is None:
            raise ValueError("resolution_lease_id is required for resume_approval")
        return value

    @model_validator(mode="after")
    def _validate_operation_shape(self) -> RunQueueMessage:
        if self.kind == "execute_run":
            if self.approval_id is not None or self.resolution_lease_id is not None:
                raise ValueError("execute_run must not contain approval refs")
            expected_operation = f"run:{self.run_id}:execute"
        else:
            # 字段 validator 已保证 resume_approval 的 refs 存在；此处只组装身份公式。
            assert self.approval_id is not None
            assert self.resolution_lease_id is not None
            expected_operation = (
                f"run:{self.run_id}:approval:{self.approval_id}:lease:{self.resolution_lease_id}"
            )
            if self.idempotency_key != expected_operation:
                raise ValueError("resume_approval idempotency_key must equal operation_id")
        if self.operation_id != expected_operation:
            raise ValueError(f"operation_id must equal {expected_operation}")
        return self

    def canonical_json(self) -> str:
        """生成字段顺序无关、跨进程稳定的 JSON。"""

        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def protected_hash(self) -> str:
        """计算 dedupe 保护字段 hash；attempt request id 不参与冲突判断。"""

        payload = self.to_payload()
        payload.pop("request_id")
        canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_execute_message(
    *,
    request_id: str,
    tenant_id: str,
    run_id: str,
    idempotency_key: str | None = None,
) -> RunQueueMessage:
    """构造 initial run operation；无客户端 key 时回退到 operation id。"""

    operation_id = f"run:{run_id}:execute"
    return RunQueueMessage(
        kind="execute_run",
        request_id=request_id,
        operation_id=operation_id,
        idempotency_key=operation_id if idempotency_key is None else idempotency_key,
        tenant_id=tenant_id,
        run_id=run_id,
    )


def build_resume_approval_message(
    *,
    request_id: str,
    tenant_id: str,
    run_id: str,
    approval_id: str,
    resolution_lease_id: str,
) -> RunQueueMessage:
    """构造与 resolution lease 绑定的 approval continuation operation。"""

    operation_id = f"run:{run_id}:approval:{approval_id}:lease:{resolution_lease_id}"
    return RunQueueMessage(
        kind="resume_approval",
        request_id=request_id,
        operation_id=operation_id,
        idempotency_key=operation_id,
        tenant_id=tenant_id,
        run_id=run_id,
        approval_id=approval_id,
        resolution_lease_id=resolution_lease_id,
    )


class QueueReceipt(HarnessDTO):
    """绑定当前 consumer 与 delivery count 的确认凭证。"""

    stream: str
    group: str
    message_id: str
    consumer_id: str
    delivery_count: int


class QueueDelivery(HarnessDTO):
    """一次可被 fencing 的 queue delivery。"""

    message: RunQueueMessage
    stream: str
    group: str
    message_id: str
    consumer_id: str
    delivery_count: int

    @property
    def receipt(self) -> QueueReceipt:
        return QueueReceipt(
            stream=self.stream,
            group=self.group,
            message_id=self.message_id,
            consumer_id=self.consumer_id,
            delivery_count=self.delivery_count,
        )


class QueueEnqueueResult(HarnessDTO):
    """enqueue 成功或幂等复用的稳定结果。"""

    message_id: str
    message: RunQueueMessage


class RunQueue(Protocol):
    """运行时与 broker adapter 之间的 provider-neutral 协议。"""

    async def enqueue(self, message: RunQueueMessage) -> QueueEnqueueResult: ...

    async def pickup(
        self, *, consumer_id: str, block_milliseconds: int = 0
    ) -> QueueDelivery | None: ...

    async def reclaim(
        self, *, consumer_id: str, min_idle_seconds: float
    ) -> QueueDelivery | None: ...

    async def ack(self, receipt: QueueReceipt) -> None: ...

    async def close(self) -> None: ...


@dataclass
class _PendingDelivery:
    consumer_id: str
    delivery_count: int
    delivered_at: float


class InMemoryRunQueue:
    """保持 queue 协议语义的确定性 fake；不能替代真实 Redis 证据。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        stream: str = "agent-harness:runs",
        group: str = "agent-harness-workers",
    ) -> None:
        self._clock = clock
        self._stream = stream
        self._group = group
        self._sequence = 0
        self._messages: dict[str, RunQueueMessage] = {}
        self._operation_index: dict[tuple[str, str], tuple[str, str]] = {}
        self._available: deque[str] = deque()
        self._pending: dict[str, _PendingDelivery] = {}
        self._acked: set[str] = set()

    @property
    def message_count(self) -> int:
        return len(self._messages)

    def decode_message(self, payload: str | bytes) -> RunQueueMessage:
        """先检查版本，使未知版本可被后续新版 worker reclaim。"""

        decoded: object = json.loads(payload)
        if not isinstance(decoded, dict):
            raise UnsupportedQueueMessageError("unsupported schema_version")
        raw = cast(dict[str, object], decoded)
        if raw.get("schema_version") != 1:
            raise UnsupportedQueueMessageError("unsupported schema_version")
        return RunQueueMessage.model_validate(raw)

    async def enqueue(self, message: RunQueueMessage) -> QueueEnqueueResult:
        key = (message.tenant_id, message.operation_id)
        protected_hash = message.protected_hash()
        existing = self._operation_index.get(key)
        if existing is not None:
            message_id, existing_hash = existing
            if existing_hash != protected_hash:
                raise QueueConflictError("queue operation protected fields conflict")
            return QueueEnqueueResult(
                message_id=message_id,
                message=self._messages[message_id],
            )

        self._sequence += 1
        message_id = f"{self._sequence}-0"
        self._messages[message_id] = message
        self._operation_index[key] = (message_id, protected_hash)
        self._available.append(message_id)
        return QueueEnqueueResult(message_id=message_id, message=message)

    async def pickup(
        self, *, consumer_id: str, block_milliseconds: int = 0
    ) -> QueueDelivery | None:
        del block_milliseconds  # fake 不阻塞；调用方用真实 adapter 验证 blocking 行为。
        while self._available:
            message_id = self._available.popleft()
            if message_id in self._acked:
                continue
            pending = _PendingDelivery(
                consumer_id=consumer_id,
                delivery_count=1,
                delivered_at=self._clock(),
            )
            self._pending[message_id] = pending
            return self._delivery(message_id, pending)
        return None

    async def reclaim(self, *, consumer_id: str, min_idle_seconds: float) -> QueueDelivery | None:
        now = self._clock()
        for message_id, pending in self._pending.items():
            if message_id in self._acked:
                continue
            if now - pending.delivered_at < min_idle_seconds:
                continue
            reclaimed = _PendingDelivery(
                consumer_id=consumer_id,
                delivery_count=pending.delivery_count + 1,
                delivered_at=now,
            )
            self._pending[message_id] = reclaimed
            return self._delivery(message_id, reclaimed)
        return None

    async def ack(self, receipt: QueueReceipt) -> None:
        pending = self._pending.get(receipt.message_id)
        if (
            receipt.stream != self._stream
            or receipt.group != self._group
            or pending is None
            or pending.consumer_id != receipt.consumer_id
            or pending.delivery_count != receipt.delivery_count
        ):
            raise StaleQueueReceiptError("queue receipt ownership is stale")
        self._acked.add(receipt.message_id)
        del self._pending[receipt.message_id]

    async def close(self) -> None:
        return None

    def _delivery(self, message_id: str, pending: _PendingDelivery) -> QueueDelivery:
        return QueueDelivery(
            message=self._messages[message_id],
            stream=self._stream,
            group=self._group,
            message_id=message_id,
            consumer_id=pending.consumer_id,
            delivery_count=pending.delivery_count,
        )


__all__ = [
    "InMemoryRunQueue",
    "QueueConflictError",
    "QueueDelivery",
    "QueueEnqueueResult",
    "QueueError",
    "QueueReceipt",
    "RunQueue",
    "RunQueueMessage",
    "StaleQueueReceiptError",
    "UnsupportedQueueMessageError",
    "build_execute_message",
    "build_resume_approval_message",
]
