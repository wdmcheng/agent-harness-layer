"""Embedding cache、allocation 与 crash window 合同。"""

# 场景文件复用同一完整 frozen-root 夹具，避免复制不可变 snapshot 构造。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_invocation_contracts import *


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", [False, True])
async def test_fallback_reclaims_only_a_route_in_frozen_sub_snapshot(
    tmp_path: Path,
    frozen: bool,
) -> None:
    """Fallback 形成最终 route identity 后重做 snapshot/hard 检查，且列表有限。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / f'fallback-{frozen}.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / f"fallback-{frozen}-events.jsonl")
    provider = CountingFakeModelProvider()
    try:
        run_id = await seed_managed_root(
            storage,
            include_fallback=frozen,
            soft_token_limit=4,
            fallback_soft_token_limit=100,
        )
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_model="fake-basic",
                    fallback_models=["fake-fallback"],
                    max_tokens_per_call=4,
                    route_max_tokens_per_call={"fake-fallback": 100},
                    input_token_price_usd=Decimal("0"),
                    output_token_price_usd=Decimal("0"),
                    price_source_ref="catalog:fake",
                    price_source_version="catalog-v1",
                ),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
        )
        if not frozen:
            response = await service.complete(
                model_request(), context=context(run_id), usage_call_id="usage-fallback"
            )
            assert response.decision.action == "policy_required"
            assert provider.calls == 0
            return
        response = await service.complete(
            model_request(), context=context(run_id), usage_call_id="usage-fallback"
        )
        assert response.model == "fake-fallback"
        assert response.decision.action == "fallback"
        assert provider.calls == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_cache_hit_commits_zero_claim_and_usage_together(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-events.jsonl")
    try:
        run_id = await seed_managed_root(storage)
        input_hash = hashlib.sha256(b"cached input").hexdigest()
        async with storage.uow() as uow:
            await uow.embedding_cache.put(
                EmbeddingCacheCreate(
                    tenant_id="tenant-a",
                    provider="local",
                    model="mock-small",
                    input_hash=input_hash,
                    vector_ref="embedding://cached",
                    metadata={
                        "cache_status": "miss",
                        "provider_latency_status": "recorded",
                        "provider_latency_ms": 1,
                        "dimensions": 4,
                        "vector_ref": "embedding://cached",
                    },
                )
            )
            await uow.commit()
        service = EmbeddingInvocationService(
            provider=LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage)),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
            input_token_price_usd=Decimal("0"),
            price_source_ref="catalog:local:mock-small",
            price_source_version="catalog-v1",
        )
        response = await service.embed(
            EmbeddingRequest(input="cached input", tenant_id="tenant-a"),
            context=context(run_id),
            usage_call_id="usage-embedding-hit",
        )
        assert response.cache.hit is True
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id="usage-embedding-hit"
            )
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-embedding-hit"
                )
            )
            terminal_allowed = await uow.shared_budget.terminal_allowed("tenant-a", run_id)
            outbox_state = outbox.state
            claim_state = None if claim is None else claim.state
            claim_token_impact = None if claim is None else claim.token_impact
        assert ledger is not None and ledger.token_impact == 0
        assert outbox_state == "published"
        assert claim_state == "settled"
        assert claim_token_impact == 0
        assert terminal_allowed is True
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_embedding_cache_miss_replay_uses_durable_started_identity(
    tmp_path: Path,
) -> None:
    """首次 miss 写入缓存后，同 usage 重放不得漂移成新的 hit identity。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-replay.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-replay-events.jsonl")
    try:
        run_id = await seed_managed_root(storage)
        service = EmbeddingInvocationService(
            provider=LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage)),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
            input_token_price_usd=Decimal("0"),
            price_source_ref="catalog:local:mock-small",
            price_source_version="catalog-v1",
        )
        request = EmbeddingRequest(input="cache warms after miss", tenant_id="tenant-a")
        first = await service.embed(
            request,
            context=context(run_id),
            usage_call_id="usage-embedding-miss-replay",
        )
        replayed = await service.embed(
            request,
            context=context(run_id),
            usage_call_id="usage-embedding-miss-replay",
        )

        assert first.cache.hit is False
        assert replayed == first
        async with storage.uow() as uow:
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id="usage-embedding-miss-replay",
            )
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-embedding-miss-replay"
                )
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            started = dict(outbox.result_json or {}).get("started")
            outbox_state = outbox.state
            claim_state = None if claim is None else claim.state
            ledger_impact = None if ledger is None else ledger.token_impact
        assert outbox_state == "published"
        assert claim_state == "settled"
        assert ledger_impact == len(request.input.encode("utf-8"))
        assert isinstance(started, dict)
        assert started["decision"]["cache_status"] == "lookup"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_exact_embedding_replay_precedes_cache_and_snapshot_integrity(
    tmp_path: Path,
) -> None:
    """Embedding durable miss 先于后写 cache 与当前 snapshot 完整性。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-corrupt-snapshot.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-corrupt-snapshot-events.jsonl")
    service = EmbeddingInvocationService(
        provider=LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage)),
        storage=storage,
        event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        shared_budget=TestIdentityRuntime(),
        input_token_price_usd=Decimal("0"),
        price_source_ref="catalog:local:mock-small",
        price_source_version="catalog-v1",
    )
    run_id = await seed_managed_root(storage)
    request = EmbeddingRequest(input="durable embedding miss", tenant_id="tenant-a")
    try:
        first = await service.embed(
            request,
            context=context(run_id),
            usage_call_id="usage-embedding-corrupt-snapshot",
        )
        async with storage.uow() as uow:
            ledger = await uow.session.get(
                ParentBudgetLedgerModel,
                ("tenant-a", run_id),
            )
            assert ledger is not None
            snapshot = dict(ledger.snapshot_json)
            snapshot["catalog_version"] = "catalog-corrupted-after-embedding"
            ledger.snapshot_json = snapshot
            await uow.commit()

        replayed = await service.embed(
            request,
            context=context(run_id),
            usage_call_id="usage-embedding-corrupt-snapshot",
        )
        with pytest.raises(BudgetOperationConflict):
            await service.embed(
                request.model_copy(update={"input": "changed embedding input"}),
                context=context(run_id),
                usage_call_id="usage-embedding-corrupt-snapshot",
            )

        assert first.cache.hit is False
        assert replayed == first
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_delegated_embedding_cache_hit_creates_zero_allocation_only(
    tmp_path: Path,
) -> None:
    """Delegated child hit 只能落 relation-scoped allocation，不能双计 direct。"""

    storage, delegation_service, _runtime, parent_run_id, sink = await build_delegation_service(
        tmp_path,
        mode="service",
        child_status=RunStatus.RUNNING,
    )
    try:
        delegated = await delegation_service.delegate(
            DelegationRequest(
                parent_run_id=parent_run_id,
                source_agent_id="agent-source",
                target_agent_id="agent-target",
                child_input={"prompt": "cache child"},
                idempotency_key="delegated-cache-hit",
                request_id="request-a",
            ),
            identity=delegation_identity(),
        )
        input_hash = hashlib.sha256(b"delegated cached input").hexdigest()
        async with storage.uow() as uow:
            await uow.embedding_cache.put(
                EmbeddingCacheCreate(
                    tenant_id="tenant-a",
                    provider="local",
                    model="mock-small",
                    input_hash=input_hash,
                    vector_ref="embedding://delegated-cached",
                    metadata={
                        "cache_status": "miss",
                        "provider_latency_status": "recorded",
                        "provider_latency_ms": 1,
                        "dimensions": 4,
                        "vector_ref": "embedding://delegated-cached",
                    },
                )
            )
            await uow.commit()
        service = EmbeddingInvocationService(
            provider=LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage)),
            storage=storage,
            event_bus=EventBus(sink=sink),
            shared_budget=TestIdentityRuntime(),
        )
        response = await service.embed(
            EmbeddingRequest(input="delegated cached input", tenant_id="tenant-a"),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=delegated.child_run_id,
                agent_id="agent-target",
                request_id="request-a",
                trace_id="trace-parent",
            ),
            usage_call_id="usage-delegated-embedding-hit",
        )
        async with storage.uow() as uow:
            await uow.runs.set_status(delegated.child_run_id, RunStatus.COMPLETED.value)
            await uow.commit()
        reconciled = await delegation_service.reconcile_child(delegated.child_run_id)
        async with storage.uow() as uow:
            direct = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-delegated-embedding-hit"
                )
            )
            allocation = await uow.session.scalar(
                select(DelegationBudgetAllocationModel).where(
                    DelegationBudgetAllocationModel.usage_call_id == "usage-delegated-embedding-hit"
                )
            )
            top_level_claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.delegation_id == delegated.delegation_id
                )
            )
            reservation = await uow.delegations.get_reservation(delegated.delegation_id)
            ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
            terminal_allowed = await uow.shared_budget.terminal_allowed("tenant-a", parent_run_id)
            allocation_state = None if allocation is None else allocation.state
            allocation_impact = None if allocation is None else allocation.token_impact
            top_level_state = None if top_level_claim is None else top_level_claim.state
            top_level_impact = None if top_level_claim is None else top_level_claim.token_impact
            ledger_state = None if ledger is None else ledger.state
            ledger_impact = None if ledger is None else ledger.token_impact
    finally:
        await storage.dispose()

    assert response.cache.hit is True
    assert direct is None
    assert allocation is not None
    assert allocation_state == "settled"
    assert allocation_impact == 0
    assert reconciled.status == "completed"
    assert reconciled.summary is not None
    assert reconciled.summary.input_tokens == 0
    assert reconciled.summary.output_tokens == 0
    assert reconciled.summary.cost_usd == 0
    assert reconciled.summary.budget_status == "within_budget"
    assert reservation.state == "settled"
    assert reservation.settled_input_tokens == 0
    assert reservation.settled_output_tokens == 0
    assert reservation.settled_cost_usd == 0
    assert top_level_claim is not None
    assert top_level_state == "settled"
    assert top_level_impact == 0
    assert ledger is not None
    assert ledger_state == "active"
    assert ledger_impact == 0
    assert terminal_allowed is True


@pytest.mark.asyncio
async def test_not_started_crash_reuses_claim_and_calls_provider_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预约提交后、started 前崩溃可按同 identity 继续，不重复预约。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'not-started.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "not-started-events.jsonl")
    provider = CountingFakeModelProvider()
    service = model_service(storage=storage, sink=sink, provider=provider)
    run_id = await seed_managed_root(storage)
    original_mark = service._mark_side_effect_started  # pyright: ignore[reportPrivateUsage]

    async def crash_before_started(**_: object) -> None:
        raise RuntimeError("injected before started")

    try:
        monkeypatch.setattr(service, "_mark_side_effect_started", crash_before_started)
        with pytest.raises(RuntimeError, match="injected before started"):
            await service.complete(
                model_request(), context=context(run_id), usage_call_id="usage-not-started"
            )
        assert provider.calls == 0
        monkeypatch.setattr(service, "_mark_side_effect_started", original_mark)
        await service.complete(
            model_request(), context=context(run_id), usage_call_id="usage-not-started"
        )
        assert provider.calls == 1
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
        assert ledger is not None and ledger.token_impact == 2
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_started_unknown_recovery_fences_replay_and_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """started 后结果未知必须 needs_review，恢复不得重放 provider。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'started-unknown.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "started-unknown-events.jsonl")
    provider = CountingFakeModelProvider()
    service = model_service(storage=storage, sink=sink, provider=provider)
    run_id = await seed_managed_root(storage)
    original_mark = service._mark_side_effect_started  # pyright: ignore[reportPrivateUsage]

    async def crash_after_started(
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        ownership: BudgetOperationOwnership | None,
    ) -> None:
        await original_mark(
            context=context,
            usage_call_id=usage_call_id,
            ownership=ownership,
        )
        raise RuntimeError("injected after started")

    try:
        monkeypatch.setattr(service, "_mark_side_effect_started", crash_after_started)
        with pytest.raises(RuntimeError, match="injected after started"):
            await service.complete(
                model_request(), context=context(run_id), usage_call_id="usage-started-unknown"
            )
        assert provider.calls == 0
        async with storage.uow() as uow:
            assert (
                await uow.shared_budget.recover_unknown_started(
                    tenant_id="tenant-a", budget_owner_run_id=run_id
                )
                == 1
            )
            assert await uow.shared_budget.terminal_allowed("tenant-a", run_id) is False
            await uow.commit()
        monkeypatch.setattr(service, "_mark_side_effect_started", original_mark)
        with pytest.raises(UsageInvocationReplayError, match="started"):
            await service.complete(
                model_request(), context=context(run_id), usage_call_id="usage-started-unknown"
            )
        assert provider.calls == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_result_committed_crash_only_republishes_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """result+settlement 提交后 event 失败只补投，不再调用 provider。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'result-committed.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "result-committed-events.jsonl")
    provider = CountingFakeModelProvider()
    service = model_service(storage=storage, sink=sink, provider=provider)
    run_id = await seed_managed_root(storage)
    original_publish = service._publish_final  # pyright: ignore[reportPrivateUsage]

    async def fail_publish(**_: object) -> None:
        raise RuntimeError("injected publish failure")

    try:
        monkeypatch.setattr(service, "_publish_final", fail_publish)
        with pytest.raises(RuntimeError, match="injected publish failure"):
            await service.complete(
                model_request(), context=context(run_id), usage_call_id="usage-result-committed"
            )
        assert provider.calls == 1
        monkeypatch.setattr(service, "_publish_final", original_publish)
        replay = await service.complete(
            model_request(), context=context(run_id), usage_call_id="usage-result-committed"
        )
        assert provider.calls == 1
        assert replay.output_text == "fake:abc"
        async with storage.uow() as uow:
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id="usage-result-committed"
            )
            outbox_state = outbox.state
        assert outbox_state == "published"
    finally:
        await storage.dispose()
