"""PostgreSQL 模型用量容量、claim 与租户并发合同测试。"""

from __future__ import annotations

from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    MAX_EVENT_SEQ as MAX_EVENT_SEQ,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    CanonicalEvent as CanonicalEvent,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    EventCapacityExceeded as EventCapacityExceeded,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    EvidenceOperationKind as EvidenceOperationKind,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    FakeModelProvider as FakeModelProvider,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    ModelInvocationService as ModelInvocationService,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    ModelRequest as ModelRequest,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    ModelResponse as ModelResponse,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    ModelRouter as ModelRouter,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    ModelRouterConfig as ModelRouterConfig,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    PostgreSQLEventSink as PostgreSQLEventSink,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    RunEventCapacityModel as RunEventCapacityModel,
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
    _seed_run as _seed_run,
)
from tests.contracts.test_model_usage_postgresql_capacity_contracts import (
    _started_evidence as _started_evidence,
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
    update as update,
)


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
