"""可信 bound façade 到 durable CanonicalEvent 的成功运行时合同。"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.model_usage_capacity_test_helpers import resolve_trace, seed_run

from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    FakeModelStreamScript,
    ModelInvocationService,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.stream_evidence_repositories import stream_group_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_text",
    [
        "OPENAI_API_KEY=abcdefgh suffix",
        "db_password=abcdefgh suffix",
        "client_secret=abcdefgh suffix",
        "access_token=abcdefgh suffix",
        "authorization=&secretvalue",
        "cookie:",
        "cookie:;session=abc",
        "set-cookie:",
        "set-cookie:;Path=/",
        "authorization: Bearer ;a",
        "authorization: Basic ,a",
        "cookie:\na",
        "set-cookie:\ra",
    ],
)
async def test_bound_stream_never_persists_or_publishes_embedded_secret_values(
    tmp_path: Path,
    provider_text: str,
) -> None:
    """逐字符 provider fragment 也只能形成与既有规则同义的 durable/public 文本。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-secret.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "stream-secret.jsonl", run_trace_resolver=resolve_trace)
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
            providers={
                "fake": FakeModelProvider(
                    stream_script=FakeModelStreamScript(fragments=tuple(provider_text))
                )
            },
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
    )
    try:
        run_id = await seed_run(storage, request_id="request-secret")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-secret",
            trace_id="trace-a",
        )
        await bound.stream(
            ModelRequest(capability="text_stream", prompt="x", max_output_tokens=8),
            operation_key="secret-stream",
        )

        expected = redact_secrets(provider_text)
        assert isinstance(expected, str)
        events = await sink.read(run_id=run_id)
        delta_events = [
            event for event in events if event.event_type == CanonicalEventType.MODEL_OUTPUT_DELTA
        ]
        public_text = "".join(cast(dict[str, str], event.payload)["text"] for event in delta_events)
        assert public_text == expected
        assert "abcdefgh" not in public_text

        first_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], first_payload["correlation"])["usage_call_id"]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            persisted_text = "".join(
                cast(dict[str, Any], cast(dict[str, Any], item.result_json)["event"])["payload"][
                    "text"
                ]
                for item in group
                if item.state == "published" and item.sequence_in_group != 65
            )
        assert persisted_text == expected
        assert "abcdefgh" not in persisted_text
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_bound_stream_persists_public_deltas_before_usage_and_returns_final(
    tmp_path: Path,
) -> None:
    """业务只拿最终 DTO；增量只经 committed event，且双预留最终精确清零。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'stream-runtime.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "stream-runtime.jsonl", run_trace_resolver=resolve_trace)
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
            providers={"fake": FakeModelProvider()},
        ),
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        response = await bound.stream(
            ModelRequest(
                capability="text_stream",
                prompt="你好",
                max_output_tokens=8,
            ),
            operation_key="primary-stream",
        )

        events = await sink.read(run_id=run_id)
        assert response.output_text == "fake:你好"
        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_OUTPUT_DELTA,
            CanonicalEventType.MODEL_OUTPUT_COMPLETED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        assert [event.visibility for event in events] == [
            "internal",
            "public",
            "public",
            "internal",
        ]
        first_payload = cast(dict[str, Any], events[0].payload)
        usage_call_id = cast(dict[str, str], first_payload["correlation"])["usage_call_id"]
        assert events[1].payload == {  # type: ignore[union-attr]
            "correlation": {"usage_call_id": usage_call_id},
            "attempt": 1,
            "chunk_ordinal": 1,
            "text": "fake:你好",
        }
        assert events[2].payload["chunk_count"] == 1  # type: ignore[index]
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id=usage_call_id
            )
            capacity = await uow.event_capacity.snapshot(run_id)
            group_states = [item.state for item in group]
            usage_state = usage.state
        assert group_states[0] == group_states[64] == "published"
        assert all(state == "cancelled" for state in group_states[1:64])
        assert usage_state == "published"
        assert capacity.highest_persisted_seq == 4
        assert capacity.outstanding_reserved_event_count == 0
    finally:
        await service.aclose()
        await storage.dispose()
