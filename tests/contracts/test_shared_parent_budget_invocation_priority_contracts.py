"""Model、embedding 的错误优先级与失败原子性合同。"""

# 场景复用 frozen-root 夹具；CountingEmbeddingProvider 来自 route 合同的共享探针。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_invocation_contracts import *
from tests.contracts.test_shared_parent_budget_invocation_routes_contracts import (
    CountingEmbeddingProvider,
)

from agent_harness.storage.models import EmbeddingCacheModel, RunEvidenceOutboxModel


@pytest.mark.asyncio
async def test_sequence_state_invalid_precedes_hard_budget(tmp_path: Path) -> None:
    """Sequence state 损坏与 hard budget 同时失败时必须先返回前者。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'sequence-priority.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    provider = CountingFakeModelProvider()
    sink = LocalJsonlEventSink(tmp_path / "sequence-priority-events.jsonl")
    try:
        run_id = await seed_managed_root(storage, token_limit=4)
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == run_id)
                .values(terminal_reservation=0)
            )
            await uow.commit()
        service = model_service(storage=storage, sink=sink, provider=provider)
        with pytest.raises(EventSequenceStateInvalid) as rejected:
            await service.complete(
                model_request(),
                context=context(run_id),
                usage_call_id="usage-sequence-invalid",
            )
        assert rejected.value.code == "event.sequence_state_invalid"
        assert provider.calls == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_sequence_state_invalid_precedes_budget_without_side_effects(
    tmp_path: Path,
) -> None:
    """Embedding 同时命中损坏 sequence 与超额时，先报 sequence 且整组零写入。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-sequence-priority.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-sequence-priority-events.jsonl")
    provider = CountingEmbeddingProvider(cache=StorageEmbeddingCache(storage))
    usage_call_id = "usage-embedding-sequence-invalid"
    try:
        run_id = await seed_managed_root(storage, token_limit=4)
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == run_id)
                .values(terminal_reservation=0)
            )
            await uow.commit()
        service = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
        )
        with pytest.raises(EventSequenceStateInvalid) as rejected:
            await service.embed(
                EmbeddingRequest(input="uncached-over-budget", tenant_id="tenant-a"),
                context=context(run_id),
                usage_call_id=usage_call_id,
            )
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == usage_call_id
                )
            )
            with pytest.raises(LookupError):
                await uow.evidence_outbox.get_usage(
                    tenant_id="tenant-a",
                    usage_call_id=usage_call_id,
                )
            capacity = await uow.session.get(RunEventCapacityModel, run_id)
            terminal_reservation = None if capacity is None else capacity.terminal_reservation
        assert rejected.value.code == "event.sequence_state_invalid"
        assert ledger is not None and ledger.token_impact == 0
        assert claim is None
        assert terminal_reservation == 0
        assert provider.calls == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_cache_hit_sequence_failure_keeps_cache_lookup_read_only(
    tmp_path: Path,
) -> None:
    """Cache hit 预查后若 sequence 拒绝，cache metadata 也不得单边提交。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-cache-hit-sequence.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-cache-hit-sequence-events.jsonl")
    provider = CountingEmbeddingProvider(cache=StorageEmbeddingCache(storage))
    request = EmbeddingRequest(input="cached-sequence-failure", tenant_id="tenant-a")
    usage_call_id = "usage-embedding-cache-hit-sequence"
    try:
        run_id = await seed_managed_root(storage, token_limit=4)
        cache_seed = await provider.embed_cache_miss(request)
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == run_id)
                .values(terminal_reservation=0)
            )
            await uow.commit()
        service = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
        )

        with pytest.raises(EventSequenceStateInvalid) as rejected:
            await service.embed(
                request,
                context=context(run_id),
                usage_call_id=usage_call_id,
            )

        async with storage.uow() as uow:
            cache_row = await uow.session.scalar(
                select(EmbeddingCacheModel).where(
                    EmbeddingCacheModel.tenant_id == "tenant-a",
                    EmbeddingCacheModel.provider == "local",
                    EmbeddingCacheModel.model == "mock-small",
                    EmbeddingCacheModel.input_hash == cache_seed.cache.input_hash,
                )
            )
            cache_status = None if cache_row is None else cache_row.metadata_json["cache_status"]
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            outbox = await uow.session.scalar(
                select(RunEvidenceOutboxModel).where(
                    RunEvidenceOutboxModel.usage_call_id == usage_call_id
                )
            )
        assert rejected.value.code == "event.sequence_state_invalid"
        assert cache_status == "miss"
        assert ledger is not None and ledger.token_impact == 0
        assert outbox is None
        assert provider.calls == 1  # 只有测试预置 miss，失败路径未调用 provider。
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_sequence_state_invalid_precedes_unbounded_cost_without_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 frozen 价格与坏 sequence 并存时，model 先报 sequence 且不建 evidence。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model-unbounded-sequence.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    provider = CountingFakeModelProvider()
    sink = LocalJsonlEventSink(tmp_path / "model-unbounded-sequence-events.jsonl")
    usage_call_id = "usage-model-unbounded-sequence"
    try:
        run_id = await seed_managed_root(
            storage,
            cost_limit=Decimal("10"),
        )
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == run_id)
                .values(terminal_reservation=0)
            )
            await uow.commit()
        service = model_service(storage=storage, sink=sink, provider=provider)
        unbounded_plan = (
            ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": provider},
            )
            .plan(model_request())
            .model_copy(update={"trusted_cost_bound": None})
        )

        async def plan_without_cost_bound(**_: object) -> Any:
            return unbounded_plan

        monkeypatch.setattr(service, "_plan", plan_without_cost_bound)

        with pytest.raises(EventSequenceStateInvalid) as rejected:
            await service.complete(
                model_request(),
                context=context(run_id),
                usage_call_id=usage_call_id,
            )

        async with storage.uow() as uow:
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == usage_call_id
                )
            )
            with pytest.raises(LookupError):
                await uow.evidence_outbox.get_usage(
                    tenant_id="tenant-a",
                    usage_call_id=usage_call_id,
                )
        assert rejected.value.code == "event.sequence_state_invalid"
        assert claim is None
        assert provider.calls == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_sequence_state_invalid_precedes_unbounded_cost_without_side_effects(
    tmp_path: Path,
) -> None:
    """缺 frozen 价格与坏 sequence 并存时，embedding 不得先写 budget evidence。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-unbounded-sequence.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    provider = CountingEmbeddingProvider(cache=StorageEmbeddingCache(storage))
    sink = LocalJsonlEventSink(tmp_path / "embedding-unbounded-sequence-events.jsonl")
    usage_call_id = "usage-embedding-unbounded-sequence"
    try:
        run_id = await seed_managed_root(
            storage,
            cost_limit=Decimal("10"),
            embedding_price=None,
        )
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == run_id)
                .values(terminal_reservation=0)
            )
            await uow.commit()
        service = EmbeddingInvocationService(
            provider=provider,
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
        )

        with pytest.raises(EventSequenceStateInvalid) as rejected:
            await service.embed(
                EmbeddingRequest(input="unbounded-sequence", tenant_id="tenant-a"),
                context=context(run_id),
                usage_call_id=usage_call_id,
            )

        async with storage.uow() as uow:
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == usage_call_id
                )
            )
            with pytest.raises(LookupError):
                await uow.evidence_outbox.get_usage(
                    tenant_id="tenant-a",
                    usage_call_id=usage_call_id,
                )
        assert rejected.value.code == "event.sequence_state_invalid"
        assert claim is None
        assert provider.calls == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token_limit", "expected_error"),
    [
        (4, BudgetReservationRejected),
        (100, EventCapacityExceeded),
    ],
    ids=["budget-precedes-capacity", "capacity-only"],
)
async def test_direct_budget_and_capacity_priority_matrix(
    tmp_path: Path,
    token_limit: int,
    expected_error: type[Exception],
) -> None:
    """有效 sequence 下，hard budget 先于 exhaustion，单独 exhaustion 保持原码。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / f'direct-priority-{token_limit}.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    provider = CountingFakeModelProvider()
    sink = LocalJsonlEventSink(tmp_path / f"direct-priority-{token_limit}-events.jsonl")
    try:
        run_id = await seed_managed_root(storage, token_limit=token_limit)
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == run_id)
                .values(highest_persisted_seq=MAX_EVENT_SEQ - 1)
            )
            await uow.commit()
        service = model_service(storage=storage, sink=sink, provider=provider)
        with pytest.raises(expected_error):
            await service.complete(
                model_request(),
                context=context(run_id),
                usage_call_id=f"usage-direct-priority-{token_limit}",
            )
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id
                    == f"usage-direct-priority-{token_limit}"
                )
            )
        assert ledger is not None and ledger.token_impact == 0
        assert claim is None
        assert provider.calls == 0
    finally:
        await storage.dispose()
