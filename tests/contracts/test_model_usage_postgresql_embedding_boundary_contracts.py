"""PostgreSQL embedding 重放与超大证据边界合同测试。"""

from __future__ import annotations

from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    CanonicalEvent as CanonicalEvent,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    CanonicalEventEnvelopeStateInvalid as CanonicalEventEnvelopeStateInvalid,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    EmbeddingCacheInfo as EmbeddingCacheInfo,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    EmbeddingInvocationService as EmbeddingInvocationService,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    EmbeddingRequest as EmbeddingRequest,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    EmbeddingResponse as EmbeddingResponse,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    PostgreSQLEventSink as PostgreSQLEventSink,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    StorageRunTraceResolver as StorageRunTraceResolver,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    UsageEvidenceContext as UsageEvidenceContext,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    _json as _json,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    _seed_run as _seed_run,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    asyncio as asyncio,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    isolated_database as isolated_database,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    pytest as pytest,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    pytestmark as pytestmark,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    text as text,
)


@pytest.mark.asyncio
async def test_postgresql_embedding_stable_call_id_does_not_replay_cache_or_provider() -> None:
    """真实 PostgreSQL 下相同 embedding stable call ID 并发时，缓存和 provider 都只能执行一次。"""

    class CountingProvider:
        """可阻塞的 embedding provider 替身，用显式开始/释放信号构造稳定并发竞争窗口。"""

        provider = "counting-embedding"
        model = "embedding-model"

        def __init__(self) -> None:
            """初始化调用计数与两个协作事件，使测试能在 provider 执行中注入第二个调用。"""

            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            """记录首次 provider 调用并等待释放，返回固定向量以隔离并发控制而非模型质量。"""

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
