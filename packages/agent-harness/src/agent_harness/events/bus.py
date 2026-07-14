"""为 run 事件分配序号，并守住 terminal event 只写一次的规则。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4
from weakref import WeakKeyDictionary

from agent_harness.artifacts import FileArtifactStore
from agent_harness.contracts.run_trace import TRACE_ID_PATTERN, RunTraceValidationError
from agent_harness.events.sinks.base import EventSink, EventSinkTerminalConflict
from agent_harness.events.types import CanonicalEvent, CanonicalEventType, EventRecordScope
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage.run_trace_gate import RunTraceResolver, RunTraceScopeConflict


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
    ) -> None:
        self._sink = sink
        self._artifact_store = artifact_store
        self._inline_payload_bytes = inline_payload_bytes
        inherited_resolver = getattr(sink, "run_trace_resolver", None)
        self._run_trace_resolver = run_trace_resolver or inherited_resolver
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

    def bind_run_trace_resolver(self, resolver: RunTraceResolver) -> None:
        """在首个 run-scoped publish 前绑定 resolver，并同步到底层 local sink。"""

        if self._run_trace_resolver is not None and self._run_trace_resolver != resolver:
            raise RuntimeError("EventBus run trace resolver is already configured")
        self._run_trace_resolver = resolver
        bind_resolver = getattr(self._sink, "bind_run_trace_resolver", None)
        if bind_resolver is not None:
            bind_resolver(resolver)

    def _lock_for_current_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._loop_locks_guard:
            lock = self._loop_locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._loop_locks[loop] = lock
            return lock

    async def terminal_event(self, run_id: str) -> CanonicalEvent | None:
        """返回已经持久化的 terminal evidence，缺失时由 runtime 决定是否补写。"""

        events = await self._sink.read(run_id=run_id)
        terminals = [event for event in events if event.terminal]
        return max(terminals, key=lambda event: event.seq, default=None)

    async def event_by_id(self, *, run_id: str, event_id: str) -> CanonicalEvent | None:
        """按已知 run 读取确定性 event-id，供状态已提交后的恢复路径复用。"""

        return next(
            (event for event in await self._sink.read(run_id=run_id) if event.event_id == event_id),
            None,
        )

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
    ) -> CanonicalEvent:
        """发布 CanonicalEvent；带 event_id 时重试返回已写 evidence。"""

        async with self._lock_for_current_loop():
            if self._artifact_store is not None:
                self._artifact_store.recover_pending()
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
            if terminal and visibility != "public":
                raise ValueError("terminal run events must be public")
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
                payload_bytes = json.dumps(event_payload).encode()
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
                    event_payload = {"artifact": {"size_bytes": len(payload_bytes)}}

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
            )
            try:

                def materialize_artifact() -> None:
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

                if pending_artifact_payload is None:
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
