"""Model provider adapter usage 归一化与失败封闭合同测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations


async def _usage_run(storage: SQLAlchemyStorage) -> str:
    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-a",
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id="session-a",
                agent_id="agent-a",
                trace_id="trace-a",
            )
        )
        await uow.commit()
        return run.id


@pytest.mark.asyncio
async def test_pydantic_ai_timeout_is_closed_as_provider_failure_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_harness.adapters.models import pydantic_ai
    from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider

    class UnusedAgent:
        def run_sync(self, prompt: str) -> Any:
            raise AssertionError(f"patched timeout seam should run first: {prompt}")

    def timeout_with_secret(*_: object, **__: object) -> Any:
        raise TimeoutError("Authorization=Bearer timeout-secret; raw prompt")

    def agent_factory(_: str) -> UnusedAgent:
        return UnusedAgent()

    monkeypatch.setattr(pydantic_ai, "_run_sync_with_timeout", timeout_with_secret)
    database = tmp_path / "timeout.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "timeout-events.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="openai:test"),
                providers={
                    "pydantic-ai": PydanticAIModelProvider(agent_factory=cast(Any, agent_factory))
                },
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )

        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await service.complete(
                ModelRequest(
                    provider="pydantic-ai",
                    prompt="private timeout prompt",
                    max_output_tokens=1,
                    timeout_seconds=1,
                ),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    trace_id="trace-a",
                ),
                usage_call_id="usage-timeout",
            )

        events = await sink.read(run_id=run_id)
        assert exc_info.value.code == "model.provider_failed"
        assert events[-1].payload is not None
        assert events[-1].payload["outcome"] == "failed"
        assert events[-1].payload["error_code"] == "model.provider_failed"
        assert events[-1].terminal is False
        serialized = (tmp_path / "timeout-events.jsonl").read_text(encoding="utf-8")
        assert "timeout-secret" not in serialized
        assert "private timeout prompt" not in serialized
        assert "raw prompt" not in serialized
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_pydantic_ai_success_is_normalized_before_usage_persistence(tmp_path: Path) -> None:
    from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider

    class Result:
        output = "provider raw output private"

        class Usage:
            input_tokens = 7
            output_tokens = 4

        def usage(self) -> Usage:
            return self.Usage()

    class Agent:
        def run_sync(self, prompt: str) -> Result:
            assert prompt == "private provider prompt"
            return Result()

    def agent_factory(_: str) -> Agent:
        return Agent()

    database = tmp_path / "pydantic-success.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "pydantic-success.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="openai:test"),
                providers={
                    "pydantic-ai": PydanticAIModelProvider(agent_factory=cast(Any, agent_factory))
                },
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )
        response = await service.complete(
            ModelRequest(
                provider="pydantic-ai",
                prompt="private provider prompt",
                max_output_tokens=5,
            ),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
            usage_call_id="usage-pydantic-success",
        )

        events = await sink.read(run_id=run_id)
        assert response.output_text == "provider raw output private"
        assert events[-1].payload is not None
        usage = cast(dict[str, Any], events[-1].payload["usage"])
        assert usage["provider"] == "pydantic-ai"
        assert usage["model"] == "openai:test"
        assert usage["input_tokens"] == 7
        assert usage["output_tokens"] == 4
        serialized = (tmp_path / "pydantic-success.jsonl").read_text(encoding="utf-8")
        assert "private provider prompt" not in serialized
        assert "provider raw output private" not in serialized
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_pydantic_ai_missing_usage_preserves_unknown_tokens_as_null(tmp_path: Path) -> None:
    from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider

    class Result:
        output = "two words"

    class Agent:
        def run_sync(self, prompt: str) -> Result:
            assert prompt == "private provider prompt"
            return Result()

    def agent_factory(_: str) -> Agent:
        return Agent()

    database = tmp_path / "pydantic-missing-usage.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "pydantic-missing-usage.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="openai:test"),
                providers={
                    "pydantic-ai": PydanticAIModelProvider(agent_factory=cast(Any, agent_factory))
                },
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )
        response = await service.complete(
            ModelRequest(
                provider="pydantic-ai",
                prompt="private provider prompt",
                estimated_input_tokens=7,
                max_output_tokens=5,
            ),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
            usage_call_id="usage-pydantic-missing",
        )

        events = await sink.read(run_id=run_id)
        assert response.token_usage == {}
        assert events[-1].payload is not None
        assert events[-1].payload["usage"]["input_tokens"] is None
        assert events[-1].payload["usage"]["output_tokens"] is None
    finally:
        await storage.dispose()
