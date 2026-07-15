"""Model invocation usage 预约、路由、成本与脱敏合同测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.observability import TelemetryFacade, TelemetryRecord, TelemetryStatus
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations


class RecordingUsageTelemetryProvider:
    """记录 model/embedding started 与 final fan-out 的测试 provider。"""

    provider_name = "recording-usage"

    def __init__(self) -> None:
        self.records: list[TelemetryRecord] = []

    async def send(self, record: TelemetryRecord) -> TelemetryStatus:
        self.records.append(record)
        return TelemetryStatus(provider=self.provider_name, status="sent")


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
async def test_model_invocation_reserves_and_settles_durable_usage(tmp_path: Path) -> None:
    database = tmp_path / "usage.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    telemetry_provider = RecordingUsageTelemetryProvider()

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": FakeModelProvider()},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            telemetry=TelemetryFacade(local_sink=sink, providers=[telemetry_provider]),
        )
        response = await service.complete(
            ModelRequest(
                provider="fake",
                prompt="hello",
                estimated_input_tokens=2,
                max_output_tokens=3,
            ),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                request_id="request-a",
                trace_id="trace-a",
            ),
            usage_call_id="usage-a",
        )

        events = await sink.read(run_id=run_id)
        assert response.provider == "fake"
        assert [item.event_type.value for item in events] == [
            "model.request.started",
            "model.usage.updated",
        ]
        payloads: list[dict[str, Any]] = []
        for item in events:
            assert item.payload is not None
            payloads.append(item.payload)
        assert {item["correlation"]["usage_call_id"] for item in payloads} == {"usage-a"}
        assert payloads[1]["usage"]["input_tokens"] == 2
        assert events[1].terminal is False
        assert [record.name for record in telemetry_provider.records] == [
            "agent_harness.model.request.started",
            "agent_harness.model.usage.updated",
        ]
        async with storage.uow() as uow:
            assert await uow.evidence_outbox.pending(run_id=run_id) == []
            capacity = await uow.event_capacity.snapshot(run_id)
            assert capacity.highest_persisted_seq == 2
            assert capacity.outstanding_reserved_event_count == 0
            assert capacity.terminal_reservation == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_invocation_policy_rejection_has_zero_provider_side_effect(
    tmp_path: Path,
) -> None:
    class SpyProvider(FakeModelProvider):
        calls = 0

        def complete(self, request: ModelRequest, *, model: str):
            self.calls += 1
            return super().complete(request, model=model)

    database = tmp_path / "policy.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "policy-events.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    provider = SpyProvider()
    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_model="fake-basic",
                    max_tokens_per_call=1,
                ),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )
        response = await service.complete(
            ModelRequest(
                provider="fake",
                prompt="blocked",
                estimated_input_tokens=2,
                max_output_tokens=1,
            ),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
            usage_call_id="usage-policy",
        )

        events = await sink.read(run_id=run_id)
        assert events[-1].payload is not None
        assert provider.calls == 0
        assert response.decision.action == "policy_required"
        assert events[-1].payload["outcome"] == "rejected"
        assert events[-1].payload["error_code"] == "model.policy_required"
        assert events[-1].payload["usage"]["decision"]["provider_called"] is False
        assert response.token_usage == {}
        assert events[-1].payload["usage"]["input_tokens"] is None
        assert events[-1].payload["usage"]["output_tokens"] is None
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_fallback_calls_only_selected_model_and_records_actual_route(
    tmp_path: Path,
) -> None:
    class FallbackSpyProvider(FakeModelProvider):
        def __init__(self) -> None:
            self.called_models: list[str] = []

        def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
            self.called_models.append(model)
            return super().complete(request, model=model)

    database = tmp_path / "fallback.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "fallback-events.jsonl")
    provider = FallbackSpyProvider()

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_model="fake-large",
                    fallback_models=["fake-small"],
                    max_tokens_per_call=1,
                ),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )

        response = await service.complete(
            ModelRequest(
                provider="fake",
                prompt="fallback",
                estimated_input_tokens=2,
                max_output_tokens=1,
            ),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
            usage_call_id="usage-fallback",
        )

        events = await sink.read(run_id=run_id)
        assert provider.called_models == ["fake-small"]
        assert response.provider == "fake"
        assert response.model == "fake-small"
        assert events[-1].payload is not None
        usage = cast(dict[str, Any], events[-1].payload["usage"])
        assert usage["provider"] == "fake"
        assert usage["model"] == "fake-small"
        assert usage["decision"]["action"] == "fallback"
        assert usage["decision"]["fallback_model"] == "fake-small"
        assert usage["decision"]["provider_called"] is True
        assert events[-1].terminal is False
    finally:
        await storage.dispose()


@pytest.mark.parametrize(
    ("cost_status", "cost_usd", "price_source_ref", "price_source_version"),
    [
        ("reported", 0.0, None, None),
        ("estimated", 0.125, "pricing://test/model", "2026-07-14"),
    ],
)
@pytest.mark.asyncio
async def test_model_invocation_preserves_verified_reported_or_estimated_cost(
    tmp_path: Path,
    cost_status: str,
    cost_usd: float,
    price_source_ref: str | None,
    price_source_version: str | None,
) -> None:
    from agent_harness.models import ModelDecision

    class CostProvider(FakeModelProvider):
        def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
            return ModelResponse(
                provider="cost",
                model=model,
                output_text="cost output",
                decision=ModelDecision(
                    action="call",
                    estimated_tokens=request.estimated_input_tokens + request.max_output_tokens,
                    price_source_ref=price_source_ref,
                    price_source_version=price_source_version,
                ),
                token_usage={"input_tokens": 3, "output_tokens": 2},
                latency_ms=4,
                cost_usd=cost_usd,
                cost_status=cast(Any, cost_status),
            )

    database = tmp_path / f"cost-{cost_status}.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / f"cost-{cost_status}.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="cost-model"),
                providers={"cost": CostProvider()},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )
        await service.complete(
            ModelRequest(provider="cost", prompt="cost", max_output_tokens=2),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
            usage_call_id=f"usage-{cost_status}",
        )

        events = await sink.read(run_id=run_id)
        assert events[-1].payload is not None
        usage = cast(dict[str, Any], events[-1].payload["usage"])
        assert usage["cost_usd"] == cost_usd
        assert usage["cost_status"] == cost_status
        if cost_status == "estimated":
            assert usage["decision"]["price_source_ref"] == price_source_ref
            assert usage["decision"]["price_source_version"] == price_source_version
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_provider_decision_is_redacted_before_durable_outbox(tmp_path: Path) -> None:
    from agent_harness.models import ModelDecision

    class LeakingDecisionProvider(FakeModelProvider):
        def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
            return ModelResponse(
                provider="leaking",
                model=model,
                output_text="safe output",
                decision=ModelDecision(
                    action="call",
                    estimated_tokens=1,
                    reason=("Authorization=Bearer decision-secret; password=decision-password"),
                ),
                token_usage={"input_tokens": 1, "output_tokens": 1},
            )

    database = tmp_path / "decision-redaction.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "decision-redaction.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="safe-model"),
                providers={"leaking": LeakingDecisionProvider()},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )
        await service.complete(
            ModelRequest(provider="leaking", prompt="safe", max_output_tokens=1),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
            usage_call_id="usage-decision-redaction",
        )

        async with storage.uow() as uow:
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id="usage-decision-redaction",
            )
            serialized_outbox = json.dumps(outbox.result_json, ensure_ascii=False)
        serialized_event = (tmp_path / "decision-redaction.jsonl").read_text(encoding="utf-8")
        for secret in ("decision-secret", "decision-password"):
            assert secret not in serialized_outbox
            assert secret not in serialized_event
        assert "[REDACTED]" in serialized_outbox
    finally:
        await storage.dispose()
