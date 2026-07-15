"""Model usage event capacity 的真实 PostgreSQL 合同测试。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from sqlalchemy import text, update
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.embeddings import (
    EmbeddingCacheInfo,
    EmbeddingInvocationService,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventEnvelopeStateInvalid,
    CanonicalEventType,
    EventBus,
    PostgreSQLEventSink,
)
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EventCapacityExceeded,
    EvidenceOperationKind,
)
from agent_harness.storage.models import RunEventCapacityModel
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL capacity 合同需要 AGENT_HARNESS_TEST_POSTGRES_DSN。",
)


async def _seed_run(storage: SQLAlchemyStorage, *, suffix: str) -> tuple[str, str, str]:
    tenant_id = f"tenant-{suffix}"
    trace_id = f"trace-{suffix}"
    async with storage.uow() as uow:
        await uow.tenants.ensure(tenant_id)
        session = await uow.sessions.create(
            SessionCreate(tenant_id=tenant_id, user_id="user-a", agent_id="agent-a")
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id=tenant_id,
                session_id=session.id,
                agent_id="agent-a",
                trace_id=trace_id,
            )
        )
        await uow.commit()
    return tenant_id, run.id, trace_id


def _started_evidence(
    *,
    tenant_id: str,
    run_id: str,
    trace_id: str,
) -> dict[str, object]:
    """构造并发 claim 需要逐值一致的 durable started 身份。"""

    return {
        "usage_kind": "model",
        "tenant_id": tenant_id,
        "provider": "fake",
        "model": "fake-basic",
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "cost_status": "unavailable",
        "latency_ms": 0,
        "decision": {"provider_called": False},
        "run_id": run_id,
        "agent_id": "agent-a",
        "request_id": None,
        "trace_id": trace_id,
    }


@pytest.mark.asyncio
async def test_postgresql_capacity_cas_allows_only_one_concurrent_reservation() -> None:
    """两个独立事务争夺最后两格，必须只有一个能在副作用前成功。"""

    async with isolated_database("usage_capacity_cas") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            _tenant_id, run_id, _trace_id = await _seed_run(storage, suffix="capacity")
            async with storage.uow() as uow:
                await uow.session.execute(
                    update(RunEventCapacityModel)
                    .where(RunEventCapacityModel.run_id == run_id)
                    .values(highest_persisted_seq=MAX_EVENT_SEQ - 3)
                )
                await uow.commit()

            async def reserve_once() -> int | Exception:
                try:
                    async with storage.uow() as uow:
                        reserved = await uow.event_capacity.reserve(
                            run_id=run_id,
                            operation_kind=EvidenceOperationKind.MODEL_USAGE,
                        )
                        await uow.commit()
                        return reserved
                except Exception as exc:
                    return exc

            results = await asyncio.gather(reserve_once(), reserve_once())
            assert sum(result == 2 for result in results) == 1
            failures = [result for result in results if isinstance(result, Exception)]
            assert len(failures) == 1
            assert isinstance(failures[0], EventCapacityExceeded)
            async with storage.uow() as uow:
                snapshot = await uow.event_capacity.snapshot(run_id)
            assert snapshot.highest_persisted_seq == MAX_EVENT_SEQ - 3
            assert snapshot.outstanding_reserved_event_count == 2
            assert snapshot.terminal_reservation == 1
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_non_run_event_advances_matching_stream_capacity() -> None:
    """non-run 不获得 run ownership，但同名 stream 仍必须推进物理 seq high-water。"""

    async with isolated_database("usage_non_run_stream_capacity") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            tenant_id, run_id, trace_id = await _seed_run(storage, suffix="non-run-stream")
            sink = PostgreSQLEventSink(storage)
            non_run = await sink.write(
                CanonicalEvent(
                    event_id="non-run-same-stream",
                    tenant_id=tenant_id,
                    run_id=run_id,
                    event_type=CanonicalEventType.RUN_STARTED,
                    seq=0,
                    record_scope="non_run",
                )
            )
            run_event = await sink.write(
                CanonicalEvent(
                    event_id="run-after-non-run",
                    tenant_id=tenant_id,
                    run_id=run_id,
                    event_type=CanonicalEventType.RUN_STARTED,
                    seq=0,
                    trace_id=trace_id,
                )
            )

            async with storage.uow() as uow:
                snapshot = await uow.event_capacity.snapshot(run_id)
            assert non_run.seq == 1
            assert run_event.seq == 2
            assert snapshot.highest_persisted_seq == 2
            assert snapshot.outstanding_reserved_event_count == 0
            assert snapshot.terminal_reservation == 1
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_usage_claim_allows_only_one_concurrent_winner() -> None:
    """唯一约束竞争必须在预约前选出唯一胜者，loser 只读取既有 settlement。"""

    async with isolated_database("usage_claim_cas") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            tenant_id, run_id, trace_id = await _seed_run(storage, suffix="claim")

            async def claim_once() -> bool:
                async with storage.uow() as uow:
                    claim = await uow.evidence_outbox.claim_usage(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        usage_call_id="postgres-claim",
                        event_id=f"usage:{tenant_id}:postgres-claim:final",
                        operation_kind=EvidenceOperationKind.MODEL_USAGE,
                        started_evidence=_started_evidence(
                            tenant_id=tenant_id,
                            run_id=run_id,
                            trace_id=trace_id,
                        ),
                    )
                    await uow.commit()
                    return claim.created

            assert sorted(await asyncio.gather(claim_once(), claim_once())) == [False, True]
            async with storage.uow() as uow:
                snapshot = await uow.event_capacity.snapshot(run_id)
                outbox = await uow.evidence_outbox.list_for_run(run_id=run_id)
                states = [item.state for item in outbox]
            assert states == ["started"]
            assert snapshot.outstanding_reserved_event_count == 2
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_usage_claim_rejects_cross_tenant_run_before_reservation() -> None:
    async with isolated_database("usage_claim_tenant") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            _tenant_id, run_id, _trace_id = await _seed_run(storage, suffix="owner")
            async with storage.uow() as uow:
                await uow.tenants.ensure("tenant-attacker")
                await uow.commit()

            with pytest.raises(ValueError, match="usage tenant does not own run"):
                async with storage.uow() as uow:
                    await uow.evidence_outbox.claim_usage(
                        tenant_id="tenant-attacker",
                        run_id=run_id,
                        usage_call_id="cross-tenant",
                        event_id="usage:tenant-attacker:cross-tenant:final",
                        operation_kind=EvidenceOperationKind.MODEL_USAGE,
                        started_evidence=_started_evidence(
                            tenant_id="tenant-attacker",
                            run_id=run_id,
                            trace_id="trace-owner",
                        ),
                    )
                    await uow.commit()

            async with storage.uow() as uow:
                snapshot = await uow.event_capacity.snapshot(run_id)
                outbox = await uow.evidence_outbox.list_for_run(run_id=run_id)
            assert snapshot.outstanding_reserved_event_count == 0
            assert outbox == []
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_model_stable_call_id_does_not_replay_provider_or_leak_capacity() -> None:
    class CountingProvider(FakeModelProvider):
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
            self.calls += 1
            return super().complete(request, model=model)

    async with isolated_database("usage_model_retry") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        provider = CountingProvider()
        try:
            tenant_id, run_id, trace_id = await _seed_run(storage, suffix="model-retry")
            service = ModelInvocationService(
                router=ModelRouter(
                    config=ModelRouterConfig(default_model="fake-basic"),
                    providers={"fake": provider},
                ),
                storage=storage,
                event_bus=EventBus(
                    sink=PostgreSQLEventSink(storage),
                    run_trace_resolver=StorageRunTraceResolver(storage),
                ),
            )
            context = UsageEvidenceContext(
                tenant_id=tenant_id,
                run_id=run_id,
                agent_id="agent-a",
                trace_id=trace_id,
            )
            request = ModelRequest(provider="fake", prompt="hello", max_output_tokens=1)
            results = await asyncio.gather(
                service.complete(request, context=context, usage_call_id="postgres-model-retry"),
                service.complete(request, context=context, usage_call_id="postgres-model-retry"),
                return_exceptions=True,
            )

            assert sum(isinstance(result, ModelResponse) for result in results) == 1
            failures = [result for result in results if isinstance(result, Exception)]
            assert len(failures) == 1
            assert "durable settlement" in str(failures[0])
            assert provider.calls == 1
            events = await PostgreSQLEventSink(storage).read(run_id=run_id)
            assert [event.event_type for event in events] == [
                CanonicalEventType.MODEL_REQUEST_STARTED,
                CanonicalEventType.MODEL_USAGE_UPDATED,
            ]
            async with storage.uow() as uow:
                snapshot = await uow.event_capacity.snapshot(run_id)
                outbox = await uow.evidence_outbox.list_for_run(run_id=run_id)
                states = [item.state for item in outbox]
            assert states == ["published"]
            assert snapshot.outstanding_reserved_event_count == 0
            assert snapshot.highest_persisted_seq == 2
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_embedding_stable_call_id_does_not_replay_cache_or_provider() -> None:
    class CountingProvider:
        provider = "counting-embedding"
        model = "embedding-model"

        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return EmbeddingResponse(
                provider=self.provider,
                model=self.model,
                vector_ref="embedding://postgres/result",
                vector=[0.5],
                cache=EmbeddingCacheInfo(
                    hit=False,
                    input_hash="hash-a",
                    vector_ref="embedding://postgres/result",
                ),
                latency_ms=1,
            )

    async with isolated_database("usage_embedding_retry") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        provider = CountingProvider()
        try:
            tenant_id, run_id, trace_id = await _seed_run(storage, suffix="embedding-retry")
            service = EmbeddingInvocationService(
                provider=provider,
                storage=storage,
                event_bus=EventBus(
                    sink=PostgreSQLEventSink(storage),
                    run_trace_resolver=StorageRunTraceResolver(storage),
                ),
            )
            context = UsageEvidenceContext(
                tenant_id=tenant_id,
                run_id=run_id,
                agent_id="agent-a",
                trace_id=trace_id,
            )
            request = EmbeddingRequest(input="private embedding", tenant_id=tenant_id)
            winner = asyncio.create_task(
                service.embed(
                    request,
                    context=context,
                    usage_call_id="postgres-embedding-retry",
                )
            )
            await provider.started.wait()
            with pytest.raises(RuntimeError, match="durable settlement"):
                await service.embed(
                    request,
                    context=context,
                    usage_call_id="postgres-embedding-retry",
                )
            provider.release.set()
            await winner

            assert provider.calls == 1
            events = await PostgreSQLEventSink(storage).read(run_id=run_id)
            assert [event.event_type for event in events] == [
                CanonicalEventType.MODEL_REQUEST_STARTED,
                CanonicalEventType.MODEL_USAGE_UPDATED,
            ]
            async with storage.uow() as uow:
                snapshot = await uow.event_capacity.snapshot(run_id)
                outbox = await uow.evidence_outbox.list_for_run(run_id=run_id)
                states = [item.state for item in outbox]
            assert states == ["published"]
            assert snapshot.outstanding_reserved_event_count == 0
            assert snapshot.highest_persisted_seq == 2
        finally:
            provider.release.set()
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_direct_oversized_envelope_fails_closed_on_read() -> None:
    """绕过正常 writer 的历史超限 row 不能被截断或伪造成空页。"""

    async with isolated_database("usage_oversized_row") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            tenant_id, run_id, trace_id = await _seed_run(storage, suffix="oversized")
            oversized = CanonicalEvent(
                event_id="legacy-oversized",
                tenant_id=tenant_id,
                run_id=run_id,
                agent_id="agent-a",
                event_type=CanonicalEventType.RUN_STARTED,
                seq=1,
                payload={"legacy": "中" * 30_000},
                trace_id=trace_id,
            )
            async with storage.engine.begin() as connection:
                await connection.execute(
                    text(
                        "insert into canonical_events("
                        "id, tenant_id, run_id, stream_id, agent_id, event_type, seq, terminal, "
                        "visibility, payload_json, trace_id, record_scope, envelope_json) values ("
                        ":event_id, :tenant_id, :run_id, :run_id, :agent_id, :event_type, "
                        "1, false, "
                        "'internal', cast(:payload_json as json), :trace_id, 'run', "
                        "cast(:envelope_json as json))"
                    ),
                    {
                        "event_id": oversized.event_id,
                        "tenant_id": tenant_id,
                        "run_id": run_id,
                        "agent_id": "agent-a",
                        "event_type": oversized.event_type.value,
                        "payload_json": _json(oversized.payload),
                        "trace_id": trace_id,
                        "envelope_json": _json(oversized.to_payload()),
                    },
                )
            with pytest.raises(CanonicalEventEnvelopeStateInvalid) as exc_info:
                await PostgreSQLEventSink(storage).read(run_id=run_id)
            assert exc_info.value.code == "event.envelope_state_invalid"
        finally:
            await storage.dispose()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)
