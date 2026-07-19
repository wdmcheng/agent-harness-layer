"""Frozen route、catalog 与 direct UoW 合同。"""

# 场景文件复用同一完整 frozen-root 夹具，避免复制不可变 snapshot 构造。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_invocation_contracts import *


class CountingEmbeddingProvider(LocalEmbeddingProvider):
    """记录 cache miss 写路径次数，证明拒绝发生在 provider 副作用前。"""

    def __init__(self, *, cache: StorageEmbeddingCache) -> None:
        super().__init__(cache=cache)
        self.calls = 0

    async def embed_cache_miss(self, request: EmbeddingRequest) -> Any:
        self.calls += 1
        return await super().embed_cache_miss(request)


def test_target_frozen_policy_overrides_global_model_route() -> None:
    """跨 agent child 的 plan 必须来自 target sub-snapshot，而非全局/source 默认值。"""

    config = TestIdentityRuntime().model_router_config(
        snapshot={
            "owner": {"agent_id": "agent-a", "cost_enabled": False},
            "agents": {
                "agent-b": {
                    "model_policy": {
                        "provider": "fake",
                        "default_model": "target-model",
                        "fallback_models": ["target-fallback"],
                    },
                    "routes": [
                        {
                            "usage_kind": "model",
                            "provider": "fake",
                            "model": "target-model",
                            "price_source_ref": "catalog:target",
                            "price_source_version": "target-v1",
                            "input_token_price_usd": "0",
                            "output_token_price_usd": "0",
                            "soft_max_tokens_per_call": 100,
                        },
                        {
                            "usage_kind": "model",
                            "provider": "fake",
                            "model": "target-fallback",
                            "price_source_ref": "catalog:target",
                            "price_source_version": "target-v1",
                            "input_token_price_usd": "0",
                            "output_token_price_usd": "0",
                            "soft_max_tokens_per_call": 100,
                        },
                    ],
                }
            },
        },
        agent_id="agent-b",
        base=ModelRouterConfig(default_model="global-source-model"),
    )
    plan = ModelRouter(config=config, providers={"fake": FakeModelProvider()}).plan(
        ModelRequest(prompt="target", estimated_input_tokens=1, max_output_tokens=1)
    )
    assert plan.model == "target-model"
    assert plan.decision.price_source_ref == "catalog:target"


@pytest.mark.asyncio
async def test_model_router_reload_cannot_change_existing_root_price_snapshot(
    tmp_path: Path,
) -> None:
    """Existing root 必须继续使用创建时冻结的价格与软阈值。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model-price-reload.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "model-price-reload-events.jsonl")
    router = ModelRouter(
        config=ModelRouterConfig(
            default_model="fake-basic",
            max_tokens_per_call=100,
            input_token_price_usd=Decimal("1"),
            output_token_price_usd=Decimal("2"),
            price_source_ref="catalog:fake",
            price_source_version="catalog-v1",
        ),
        providers={"fake": FakeModelProvider()},
    )
    try:
        run_id = await seed_managed_root(
            storage,
            cost_limit=Decimal("100"),
            model_input_price=Decimal("1"),
            model_output_price=Decimal("2"),
        )
        service = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
        )
        router.reload(
            ModelRouterConfig(
                default_model="fake-basic",
                max_tokens_per_call=1,
                input_token_price_usd=Decimal("0.01"),
                output_token_price_usd=Decimal("0.01"),
                price_source_ref="catalog:fake",
                price_source_version="catalog-v2",
            )
        )
        response = await service.complete(
            model_request(),
            context=context(run_id),
            usage_call_id="usage-model-price-reload",
        )
        async with storage.uow() as uow:
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-model-price-reload"
                )
            )
            claim_reserved_cost = None if claim is None else claim.reserved_cost
            claim_identity = None if claim is None else claim.identity_json
        assert response.decision.action == "call"
        assert claim is not None
        assert claim_reserved_cost == Decimal("7.00000000")
        assert claim_identity is not None
        assert claim_identity["price_source_version"] == "catalog-v1"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_service_reload_cannot_change_existing_root_price_snapshot(
    tmp_path: Path,
) -> None:
    """Embedding current service price 不是 existing tree 的价格真相源。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-price-reload.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-price-reload-events.jsonl")
    try:
        run_id = await seed_managed_root(
            storage,
            cost_limit=Decimal("100"),
            embedding_price=Decimal("2"),
        )
        service = EmbeddingInvocationService(
            provider=LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage)),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
            input_token_price_usd=Decimal("0.01"),
            price_source_ref="catalog:local:mock-small",
            price_source_version="catalog-v2",
        )
        await service.embed(
            EmbeddingRequest(input="abc", tenant_id="tenant-a"),
            context=context(run_id),
            usage_call_id="usage-embedding-price-reload",
        )
        async with storage.uow() as uow:
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-embedding-price-reload"
                )
            )
            claim_reserved_cost = None if claim is None else claim.reserved_cost
            claim_identity = None if claim is None else claim.identity_json
        assert claim is not None
        assert claim_reserved_cost == Decimal("6.00000000")
        assert claim_identity is not None
        assert claim_identity["price_source_version"] == "catalog-v1"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_active_tree_without_ledger_rejects_before_provider(tmp_path: Path) -> None:
    """0016 active writer 缺 immutable ledger 时不得回退到无共享预算路径。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'missing-ledger.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    provider = CountingFakeModelProvider()
    sink = LocalJsonlEventSink(tmp_path / "missing-ledger-events.jsonl")
    try:
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
        service = model_service(storage=storage, sink=sink, provider=provider)
        with pytest.raises(BudgetReservationRejected) as rejected:
            await service.complete(
                model_request(),
                context=context(run.id),
                usage_call_id="usage-missing-ledger",
            )
        assert rejected.value.reason == "snapshot_invalid"
        assert provider.calls == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_invalid_frozen_model_catalog_records_closed_budget_rejection(
    tmp_path: Path,
) -> None:
    """语义损坏但 hash 自洽的 catalog 也只能产生脱敏的统一硬拒绝。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'invalid-frozen-catalog.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "invalid-frozen-catalog-events.jsonl")
    provider = CountingFakeModelProvider()
    try:
        run_id = await seed_managed_root(storage)
        async with storage.uow() as uow:
            ledger = await uow.session.get(ParentBudgetLedgerModel, ("tenant-a", run_id))
            assert ledger is not None
            snapshot = dict(ledger.snapshot_json)
            agents = dict(snapshot["agents"])
            agent = dict(agents["agent-a"])
            agent["routes"] = []
            agents["agent-a"] = agent
            snapshot["agents"] = agents
            ledger.snapshot_json = snapshot
            ledger.snapshot_hash = hashlib.sha256(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            await uow.commit()
        service = model_service(storage=storage, sink=sink, provider=provider)
        with pytest.raises(BudgetReservationRejected) as rejected:
            await service.complete(
                model_request(),
                context=context(run_id),
                usage_call_id="usage-invalid-frozen-catalog",
            )
        async with storage.uow() as uow:
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id="usage-invalid-frozen-catalog",
            )
            outbox_result = outbox.result_json
    finally:
        await storage.dispose()

    assert rejected.value.reason == "snapshot_invalid"
    assert provider.calls == 0
    assert outbox_result is not None
    assert outbox_result["evidence"]["decision"] == {
        "action": "rejected",
        "budget_rejection_reason": "snapshot_invalid",
        "provider_called": False,
    }


@pytest.mark.asyncio
async def test_unconfigured_model_provider_records_closed_budget_rejection(
    tmp_path: Path,
) -> None:
    """Router 的未配置 provider 也必须收敛为 durable snapshot_invalid 拒绝。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'unconfigured-provider.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "unconfigured-provider-events.jsonl")
    provider = CountingFakeModelProvider()
    try:
        run_id = await seed_managed_root(storage)
        service = model_service(storage=storage, sink=sink, provider=provider)
        request = model_request().model_copy(update={"provider": "not-frozen"})
        with pytest.raises(BudgetReservationRejected) as rejected:
            await service.complete(
                request,
                context=context(run_id),
                usage_call_id="usage-unconfigured-provider",
            )
        async with storage.uow() as uow:
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id="usage-unconfigured-provider",
            )
            outbox_result = outbox.result_json
    finally:
        await storage.dispose()

    assert rejected.value.reason == "snapshot_invalid"
    assert provider.calls == 0
    assert outbox_result is not None
    assert outbox_result["evidence"]["decision"] == {
        "action": "rejected",
        "budget_rejection_reason": "snapshot_invalid",
        "provider_called": False,
    }
