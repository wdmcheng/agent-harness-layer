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
    """Redis 只承载稳定 refs，不复制执行输入或身份真相源。

    worker 收到消息后必须回到耐久存储读取 run 与 approval；队列中的字段仅用于定位、
    去重和交接，不能成为可被重放或篡改的执行事实来源。
    """

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
        """去除标识符首尾空白，并拒绝空值以保持去重坐标可比较。"""

        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be empty")
        return normalized

    @field_validator("approval_id")
    @classmethod
    def _approval_ref_required(cls, value: str | None, info: ValidationInfo) -> str | None:
        """恢复审批时要求消息携带 approval 引用，避免 worker 猜测待恢复对象。"""

        if info.data.get("kind") == "resume_approval" and value is None:
            raise ValueError("approval_id is required for resume_approval")
        return value

    @field_validator("resolution_lease_id")
    @classmethod
    def _lease_ref_required(cls, value: str | None, info: ValidationInfo) -> str | None:
        """恢复审批时绑定 resolution lease，确保旧消息不能确认新的决议所有权。"""

        if info.data.get("kind") == "resume_approval" and value is None:
            raise ValueError("resolution_lease_id is required for resume_approval")
        return value

    @model_validator(mode="after")
    def _validate_operation_shape(self) -> RunQueueMessage:
        """将消息类型、稳定操作键和幂等键收敛为不可混用的身份公式。"""

        if self.kind == "execute_run":
            # 普通执行不应带审批引用；带入后可能让 worker 错把新任务当作恢复任务。
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
        """生成字段顺序无关、跨进程稳定的 JSON，用于 adapter 的持久化与比较。"""

        return json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def protected_hash(self) -> str:
        """计算去重保护字段摘要；单次 attempt request id 不参与冲突判断。

        同一操作可以由不同请求再次投递，但任何会改变执行归属或恢复对象的字段都必须
        与首次投递一致，避免静默覆盖已占用的队列操作键。
        """

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
    """构造普通执行消息；无客户端幂等键时稳定回退到操作键。"""

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
    """构造与 resolution lease 绑定的审批恢复消息。

    lease 被纳入操作键和幂等键，使同一 approval 的旧恢复消息不能与新决议竞争。
    """

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
    """绑定当前 consumer 与 delivery count 的确认凭证。

    这是 fencing token，不是仅凭 message id 即可确认的收据；重领后的旧 worker 必须
    因 delivery count 或 consumer 不匹配而失败。
    """

    stream: str
    group: str
    message_id: str
    consumer_id: str
    delivery_count: int


class QueueDelivery(HarnessDTO):
    """一次可被 fencing 的队列投递，携带消息与当前所有权坐标。"""

    message: RunQueueMessage
    stream: str
    group: str
    message_id: str
    consumer_id: str
    delivery_count: int

    @property
    def receipt(self) -> QueueReceipt:
        """从当前投递快照生成确认凭证，不读取任何可变队列状态。"""

        return QueueReceipt(
            stream=self.stream,
            group=self.group,
            message_id=self.message_id,
            consumer_id=self.consumer_id,
            delivery_count=self.delivery_count,
        )


class QueueEnqueueResult(HarnessDTO):
    """投递成功或幂等复用后的稳定结果，始终返回耐久消息身份。"""

    message_id: str
    message: RunQueueMessage


class RunQueue(Protocol):
    """运行时与 broker adapter 之间的 provider-neutral 协议。"""

    async def enqueue(self, message: RunQueueMessage) -> QueueEnqueueResult:
        """原子写入或复用同一租户、同一操作键的消息；冲突时 fail closed。"""

        ...

    async def pickup(
        self, *, consumer_id: str, block_milliseconds: int = 0
    ) -> QueueDelivery | None:
        """取得下一条可用消息并转为当前消费者所有的投递；没有消息时返回 ``None``。"""

        ...

    async def reclaim(self, *, consumer_id: str, min_idle_seconds: float) -> QueueDelivery | None:
        """领取空闲超时的未确认消息，并提高 delivery count 使旧收据失效。"""

        ...

    async def ack(self, receipt: QueueReceipt) -> None:
        """只确认仍归当前收据所有的投递；所有权变化必须抛出 fencing 错误。"""

        ...

    async def close(self) -> None:
        """释放 adapter 持有的连接或本地资源；调用应当可安全地收尾。"""

        ...


@dataclass
class _PendingDelivery:
    """内存替身中记录未确认投递的最小所有权和空闲时间状态。"""

    consumer_id: str
    delivery_count: int
    delivered_at: float


class InMemoryRunQueue:
    """保持 queue 协议语义的确定性 fake；不能替代真实 Redis 证据。

    该替身只模拟幂等、pending、重领和 fencing 规则，便于合同测试精确控制时钟；它
    不模拟 broker 的阻塞、崩溃持久化或跨进程竞争，因此这些行为必须由集成测试证明。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        stream: str = "agent-harness:runs",
        group: str = "agent-harness-workers",
    ) -> None:
        """初始化独立的消息、操作索引和 pending 状态，并注入可控时钟供测试使用。"""

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
        """返回已写入消息数；包含待确认和已确认消息，便于断言去重是否复用。"""

        return len(self._messages)

    def decode_message(self, payload: str | bytes) -> RunQueueMessage:
        """先检查版本，使未知版本可被后续新版 worker reclaim。

        不把未知 schema 交给 Pydantic 的宽泛错误处理，是为了让 worker 明确拒绝确认，
        留给兼容该版本的消费者后续重领。
        """

        decoded: object = json.loads(payload)
        if not isinstance(decoded, dict):
            raise UnsupportedQueueMessageError("unsupported schema_version")
        raw = cast(dict[str, object], decoded)
        if raw.get("schema_version") != 1:
            raise UnsupportedQueueMessageError("unsupported schema_version")
        return RunQueueMessage.model_validate(raw)

    async def enqueue(self, message: RunQueueMessage) -> QueueEnqueueResult:
        """写入新消息或复用完全相同操作的首次消息，保护字段不同时拒绝覆盖。"""

        key = (message.tenant_id, message.operation_id)
        protected_hash = message.protected_hash()
        existing = self._operation_index.get(key)
        if existing is not None:
            message_id, existing_hash = existing
            # 仅 request id 不同的重试可以复用；其他字段漂移意味着同一键承载了不同意图。
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
        """从可用队列领取首条未确认消息，并建立首次 delivery ownership。"""

        del block_milliseconds  # fake 不阻塞；调用方用真实 adapter 验证 blocking 行为。
        while self._available:
            message_id = self._available.popleft()
            # 已确认消息可能残留在本地队列中，跳过它以保持 ack 的最终性。
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
        """领取空闲超时的 pending 消息，并用新 delivery count 使旧收据失效。"""

        now = self._clock()
        for message_id, pending in self._pending.items():
            if message_id in self._acked:
                continue
            if now - pending.delivered_at < min_idle_seconds:
                continue
            # 先替换 pending 再返回投递，确保新的 receipt 立即成为唯一可确认凭证。
            reclaimed = _PendingDelivery(
                consumer_id=consumer_id,
                delivery_count=pending.delivery_count + 1,
                delivered_at=now,
            )
            self._pending[message_id] = reclaimed
            return self._delivery(message_id, reclaimed)
        return None

    async def ack(self, receipt: QueueReceipt) -> None:
        """按 stream、group、消费者和 delivery count 四元组确认当前所有权。"""

        pending = self._pending.get(receipt.message_id)
        if (
            receipt.stream != self._stream
            or receipt.group != self._group
            or pending is None
            or pending.consumer_id != receipt.consumer_id
            or pending.delivery_count != receipt.delivery_count
        ):
            # 任何字段不匹配都视为过期收据，不能让旧 worker 确认被重领的消息。
            raise StaleQueueReceiptError("queue receipt ownership is stale")
        self._acked.add(receipt.message_id)
        del self._pending[receipt.message_id]

    async def close(self) -> None:
        """内存替身没有外部资源；保留该空操作以满足统一队列协议。"""

        return None

    def _delivery(self, message_id: str, pending: _PendingDelivery) -> QueueDelivery:
        """把内部 pending 状态投影为不可变 delivery DTO，避免调用方修改队列真相。"""

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
