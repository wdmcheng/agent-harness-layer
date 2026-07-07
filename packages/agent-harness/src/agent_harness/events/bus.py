"""为 run 事件分配序号，并守住 terminal event 只写一次的规则。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events.sinks.base import EventSink
from agent_harness.events.types import CanonicalEvent, CanonicalEventType
from agent_harness.security.redaction import redact_secrets


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
    ) -> None:
        self._sink = sink
        self._artifact_store = artifact_store
        self._inline_payload_bytes = inline_payload_bytes
        self._lock = asyncio.Lock()

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
    ) -> CanonicalEvent:
        async with self._lock:
            # Terminal event 会关闭 run stream。先检查再分配 seq，避免被拒绝的
            # 重复 terminal 写入消耗序号，进而干扰 SSE resume 调用方。
            if terminal and await self._sink.has_terminal(run_id):
                raise TerminalEventError(f"run already has terminal event: {run_id}")
            seq = await self._sink.latest_seq(run_id) + 1
            event_payload = None if payload is None else redact_secrets(payload)
            payload_ref: str | None = None
            payload_checksum: str | None = None
            if event_payload is not None:
                payload_bytes = json.dumps(event_payload).encode()
                # 大 payload 进入 artifact store；事件本身只保留摘要、checksum
                # 和 ref，避免 local JSONL、SSE、trace adapter 被大内容撑爆。
                if (
                    self._artifact_store is not None
                    and len(payload_bytes) > self._inline_payload_bytes
                ):
                    artifact = self._artifact_store.write_json(event_payload)
                    payload_ref = artifact.ref
                    payload_checksum = artifact.checksum_sha256
                    event_payload = {"artifact": {"size_bytes": artifact.size_bytes}}

            event = CanonicalEvent(
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
                span_id=span_id,
            )
            await self._sink.write(event)
            return event
