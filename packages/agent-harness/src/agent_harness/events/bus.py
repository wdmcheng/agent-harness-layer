"""为 run 事件分配序号，并守住 terminal event 只写一次的规则。"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from weakref import WeakKeyDictionary

from agent_harness.artifacts import FileArtifactStore
from agent_harness.contracts.run_trace import TRACE_ID_PATTERN, RunTraceValidationError
from agent_harness.events.capacity import usage_capacity_projection
from agent_harness.events.local_capacity import LocalEventCapacityClaim
from agent_harness.events.serialization import canonical_event_bytes, canonical_json_bytes
from agent_harness.events.sinks.base import EventSink, EventSinkTerminalConflict
from agent_harness.events.types import (
    CanonicalEvent,
    CanonicalEventType,
    EventRecordScope,
    validate_terminal_semantics,
)
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage.run_trace_gate import RunTraceResolver, RunTraceScopeConflict

if TYPE_CHECKING:
    from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage


class TerminalEventError(RuntimeError):
    """run 已经有 terminal event 时抛出。"""


class EventBus:
    """runtime、API 和 eval seam 共用的不绑定 provider 的事件发布器。"""

    def __init__(
        self,
        *,
        sink: EventSink,
        artifact_store: FileArtifactStore | None = None,
        inline_payload_bytes: int = 8192,
        run_trace_resolver: RunTraceResolver | None = None,
        capacity_storage: SQLAlchemyStorage | None = None,
    ) -> None:
        """装配事件 sink、可选 artifact store 与 run-scoped 一致性协作者。

        本地 sink 需要 EventBus 注入容量 claim 和 trace resolver；service sink 会在
        自己的数据库事务中处理这些职责。按事件循环维护锁只解决本进程协程竞争，
        不能替代 sink 的跨进程原子写入保证。
        """

        self._sink = sink
        self._artifact_store = artifact_store
        self._inline_payload_bytes = inline_payload_bytes
        inherited_resolver = getattr(sink, "run_trace_resolver", None)
        self._run_trace_resolver = run_trace_resolver or inherited_resolver
        self._capacity_storage = capacity_storage
        self._local_capacity_claim = (
            LocalEventCapacityClaim(capacity_storage) if capacity_storage is not None else None
        )
        bind_capacity_claim = getattr(sink, "bind_capacity_claim", None)
        if self._local_capacity_claim is not None and bind_capacity_claim is not None:
            bind_capacity_claim(self._local_capacity_claim.claim)
        bind_resolver = getattr(sink, "bind_run_trace_resolver", None)
        if run_trace_resolver is not None and bind_resolver is not None:
            bind_resolver(run_trace_resolver)
        # DBOS recovery handler 与 worker consumer 可能位于同进程的不同 event loop。
        # asyncio.Lock 只能绑定一个 loop；这里按 loop 隔离进程内协调，跨 loop/
        # 跨进程的 seq、event_id 与 terminal 原子性继续由 PostgreSQL sink 保证。
        self._loop_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
            WeakKeyDictionary()
        )
        self._loop_locks_guard = threading.Lock()

    @property
    def run_trace_resolver_configured(self) -> bool:
        """供 composition root 判定是否已显式提供持久化 resolver。"""

        return self._run_trace_resolver is not None

    @property
    def sink_manages_capacity(self) -> bool:
        """报告 sink 是否在 event insert 事务内消费数据库预约。"""

        return bool(getattr(self._sink, "manages_event_capacity", False))

    @property
    def capacity_managed(self) -> bool:
        """报告 sink 或 local EventBus 是否维护持久化容量账本。"""

        return self.sink_manages_capacity or self._capacity_storage is not None

    def bind_run_trace_resolver(self, resolver: RunTraceResolver) -> None:
        """在首个 run-scoped publish 前绑定 resolver，并同步到底层 local sink。"""

        if self._run_trace_resolver is not None and self._run_trace_resolver != resolver:
            raise RuntimeError("EventBus run trace resolver is already configured")
        self._run_trace_resolver = resolver
        bind_resolver = getattr(self._sink, "bind_run_trace_resolver", None)
        if bind_resolver is not None:
            bind_resolver(resolver)

    def _lock_for_current_loop(self) -> asyncio.Lock:
        """取得当前 asyncio loop 专属锁，避免跨 loop 复用绑定式 ``asyncio.Lock``。"""

        loop = asyncio.get_running_loop()
        with self._loop_locks_guard:
            lock = self._loop_locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._loop_locks[loop] = lock
            return lock

    async def terminal_event(self, run_id: str) -> CanonicalEvent | None:
        """返回已经持久化的 terminal evidence，缺失时由 runtime 决定是否补写。"""

        terminal_reader = getattr(self._sink, "terminal_event", None)
        if terminal_reader is not None:
            return await terminal_reader(run_id=run_id, include_internal=True)
        # 保留对已有第三方/测试 sink 的兼容；内置 sink 均走上面的受限查询。
        events = await self._sink.read(run_id=run_id)
        terminals = [event for event in events if event.terminal]
        return max(terminals, key=lambda event: event.seq, default=None)

    async def event_by_id(self, *, run_id: str, event_id: str) -> CanonicalEvent | None:
        """按已知 run 读取确定性 event-id，供状态已提交后的恢复路径复用。"""

        return next(
            (event for event in await self._sink.read(run_id=run_id) if event.event_id == event_id),
            None,
        )

    async def reconcile_local_capacity(self, *, run_id: str) -> None:
        """新预约前接管既有 local JSONL 前缀；service sink 无需额外对账。

        先由 sink 在跨进程文件锁内报告最高已落盘序号，再在同一进程 loop 锁内更新
        数据库容量账本，避免阻塞式 ``flock`` 与并发协程互相等待造成死锁。
        """

        capacity_storage = self._capacity_storage
        if capacity_storage is None:
            return
        reconcile_capacity = getattr(self._sink, "reconcile_capacity", None)
        if reconcile_capacity is None:
            raise RuntimeError("local event sink does not support capacity reconciliation")

        async def reconcile(highest_persisted_seq: int) -> None:
            """在独立 UoW 中把已落盘 JSONL 前缀记入容量账本，避免重复占用序号。"""

            async with capacity_storage.uow() as uow:
                await uow.event_capacity.reconcile_local_prefix(
                    run_id=run_id,
                    highest_persisted_seq=highest_persisted_seq,
                )
                await uow.commit()

        # ``flock`` 是阻塞式跨进程锁；同一 event loop 内必须先用 async lock
        # 串行化，否则第二个协程会阻塞 loop，令持锁协程无法完成数据库提交。
        async with self._lock_for_current_loop():
            await reconcile_capacity(run_id, reconcile=reconcile)

    async def publish(
        self,
        *,
        tenant_id: str,
        run_id: str,
        event_type: CanonicalEventType,
        agent_id: str | None = None,
        user_id: str | None = None,
        parent_run_id: str | None = None,
        payload: dict[str, Any] | None = None,
        terminal: bool = False,
        visibility: str = "internal",
        request_id: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
        raw_event_ref: str | None = None,
        event_id: str | None = None,
        record_scope: EventRecordScope = "run",
        timestamp: datetime | None = None,
    ) -> CanonicalEvent:
        """发布 CanonicalEvent；带 event_id 时重试返回已写 evidence。"""

        # 必须早于 trace/sink 查询、seq、容量和 artifact 操作；否则非法
        # delegation final 可侵占 run terminal 槽或提前关闭 reader。
        validate_terminal_semantics(
            event_type=event_type,
            terminal=terminal,
            visibility=visibility,
        )
        async with self._lock_for_current_loop():
            if record_scope == "run":
                if trace_id is None or TRACE_ID_PATTERN.fullmatch(trace_id) is None:
                    raise RunTraceValidationError
                if self._run_trace_resolver is None:
                    raise RuntimeError("run-scoped EventBus requires a trace resolver")
                canonical_trace = await self._run_trace_resolver(
                    tenant_id=tenant_id,
                    run_id=run_id,
                )
                if trace_id != canonical_trace:
                    raise RunTraceScopeConflict
            elif record_scope != "non_run":
                raise ValueError("record_scope must be run or non_run")
            if (
                terminal
                and record_scope == "run"
                and not self.sink_manages_capacity
                and self._capacity_storage is not None
                and (
                    event_id is None
                    or await self.event_by_id(run_id=run_id, event_id=event_id) is None
                )
            ):
                async with self._capacity_storage.uow() as uow:
                    await uow.event_capacity.assert_terminal_publishable(run_id=run_id)
            if await self._sink.has_terminal(run_id):
                if (
                    event_id is None
                    or await self.event_by_id(run_id=run_id, event_id=event_id) is None
                ):
                    raise TerminalEventError(f"run already has terminal event: {run_id}")
            if event_type == CanonicalEventType.MODEL_USAGE_UPDATED:
                if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
                    raise ValueError("model usage final event requires a complete usage payload")
                # 局部 import 避免 models -> events 的生命周期依赖形成模块环。
                from agent_harness.models.usage import ModelUsageEvidence

                evidence = ModelUsageEvidence.model_validate(payload["usage"])
                if (
                    evidence.tenant_id != tenant_id
                    or evidence.run_id != run_id
                    or evidence.agent_id != agent_id
                    or evidence.request_id != request_id
                    or evidence.trace_id != trace_id
                ):
                    raise ValueError("model usage payload does not match canonical event scope")
                payload = {**payload, "usage": evidence.to_payload()}
            if self._artifact_store is not None:
                self._artifact_store.recover_pending()
            # Terminal event 会关闭 run stream。先检查再分配 seq，避免被拒绝的
            # 重复 terminal 写入消耗序号，进而干扰 SSE resume 调用方。
            if terminal and event_id is None and await self._sink.has_terminal(run_id):
                raise TerminalEventError(f"run already has terminal event: {run_id}")
            seq = await self._sink.latest_seq(run_id) + 1
            event_payload = None if payload is None else redact_secrets(payload)
            payload_ref: str | None = None
            payload_checksum: str | None = None
            pending_artifact_payload: dict[str, Any] | None = None
            if event_payload is not None:
                payload_bytes = canonical_json_bytes(event_payload)
                # 大 payload 进入 artifact store；事件本身只保留摘要、checksum
                # 和 ref，避免 local JSONL、SSE、trace adapter 被大内容撑爆。
                if (
                    self._artifact_store is not None
                    and len(payload_bytes) > self._inline_payload_bytes
                ):
                    # 先计算内容寻址元数据，真正写 artifact 必须等 sink 原子 claim
                    # event-id 成功；边界碰撞不能在磁盘留下碰撞方 payload。
                    payload_checksum = hashlib.sha256(payload_bytes).hexdigest()
                    payload_ref = f"artifact://{payload_checksum}"
                    pending_artifact_payload = event_payload
                    event_payload = (
                        usage_capacity_projection(event_payload, size_bytes=len(payload_bytes))
                        if event_type
                        in {
                            CanonicalEventType.MODEL_REQUEST_STARTED,
                            CanonicalEventType.MODEL_USAGE_UPDATED,
                        }
                        else {"artifact": {"size_bytes": len(payload_bytes)}}
                    )

            event = CanonicalEvent(
                event_id=event_id or str(uuid4()),
                tenant_id=tenant_id,
                run_id=run_id,
                user_id=user_id,
                agent_id=agent_id,
                parent_run_id=parent_run_id,
                event_type=event_type,
                seq=seq,
                payload=event_payload,
                payload_ref=payload_ref,
                payload_checksum=payload_checksum,
                raw_event_ref=raw_event_ref,
                terminal=terminal,
                visibility=visibility,
                request_id=request_id,
                trace_id=trace_id,
                record_scope=record_scope,
                span_id=span_id,
                timestamp=timestamp if timestamp is not None else datetime.now(UTC),
            )
            canonical_event_bytes(event)
            try:

                def materialize_artifact() -> None:
                    """在 sink 已取得 event-id 后写入大载荷，防止失败重试留下孤儿文件。"""

                    if pending_artifact_payload is None:
                        return
                    artifact_store = self._artifact_store
                    if artifact_store is None:
                        raise RuntimeError("artifact store is not configured")
                    artifact = artifact_store.write_json(pending_artifact_payload)
                    if artifact.ref != payload_ref or artifact.checksum_sha256 != payload_checksum:
                        raise RuntimeError(
                            "artifact materialization does not match event reference"
                        )

                @contextmanager
                def materialize_artifact_claim(
                    event_path: Path,
                    event_id_for_claim: str,
                    event_size_before: int,
                ) -> Generator[None, None, None]:
                    """把 artifact 文件声明与事件追加绑定为可回滚的本地存储 claim。"""

                    if pending_artifact_payload is None:
                        yield
                        return
                    artifact_store = self._artifact_store
                    if artifact_store is None:
                        raise RuntimeError("artifact store is not configured")
                    with artifact_store.claim_json(
                        pending_artifact_payload,
                        event_path=event_path,
                        event_id=event_id_for_claim,
                        event_size_before=event_size_before,
                    ) as artifact:
                        if (
                            artifact.ref != payload_ref
                            or artifact.checksum_sha256 != payload_checksum
                        ):
                            raise RuntimeError(
                                "artifact materialization does not match event reference"
                            )
                        yield

                if (
                    event.record_scope == "run"
                    and not self.sink_manages_capacity
                    and self._local_capacity_claim is not None
                ):
                    write_with_capacity_claim = getattr(
                        self._sink,
                        "write_with_capacity_claim",
                        None,
                    )
                    if write_with_capacity_claim is None:
                        raise RuntimeError(
                            "local event sink does not support atomic capacity claim"
                        )
                    persisted = await write_with_capacity_claim(
                        event,
                        capacity_claim=self._local_capacity_claim.claim,
                        artifact_claim=(
                            materialize_artifact_claim
                            if pending_artifact_payload is not None
                            else None
                        ),
                    )
                elif pending_artifact_payload is None:
                    persisted = await self._sink.write(event)
                else:
                    write_with_artifact_claim = getattr(
                        self._sink,
                        "write_with_artifact_claim",
                        None,
                    )
                    if write_with_artifact_claim is not None:
                        persisted = await write_with_artifact_claim(
                            event,
                            artifact_claim=materialize_artifact_claim,
                        )
                    else:
                        write_after_claim = getattr(self._sink, "write_after_claim", None)
                        if write_after_claim is None:
                            raise RuntimeError("event sink does not support artifact claim")
                        persisted = await write_after_claim(
                            event,
                            after_claim=materialize_artifact,
                        )
            except EventSinkTerminalConflict as exc:
                raise TerminalEventError(f"run already has terminal event: {run_id}") from exc
            return persisted
