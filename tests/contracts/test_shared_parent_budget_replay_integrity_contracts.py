"""共享预算 claim 与 usage outbox 的精确重放交叉完整性合同。"""

# 复用 frozen root、provider 与 delegation 夹具，场景只关注跨记录矛盾。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_invocation_contracts import *


@pytest.mark.asyncio
async def test_model_replay_rejects_started_claim_with_published_outbox(tmp_path: Path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model-replay-conflict.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    provider = CountingFakeModelProvider()
    service = model_service(
        storage=storage,
        sink=LocalJsonlEventSink(tmp_path / "model-replay-conflict-events.jsonl"),
        provider=provider,
    )
    run_id = await seed_managed_root(storage)
    try:
        await service.complete(
            model_request(),
            context=context(run_id),
            usage_call_id="usage-model-replay-conflict",
        )
        async with storage.uow() as uow:
            await uow.session.execute(
                update(BudgetOperationClaimModel)
                .where(BudgetOperationClaimModel.usage_call_id == "usage-model-replay-conflict")
                .values(state="reserved", side_effect_state="started", result_json=None)
            )
            await uow.commit()

        with pytest.raises(BudgetOperationConflict):
            await service.complete(
                model_request(),
                context=context(run_id),
                usage_call_id="usage-model-replay-conflict",
            )
        assert provider.calls == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_direct_embedding_replay_rejects_started_claim_with_published_outbox(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-replay-conflict.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    service = EmbeddingInvocationService(
        provider=LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage)),
        storage=storage,
        event_bus=EventBus(
            sink=LocalJsonlEventSink(tmp_path / "embedding-replay-conflict-events.jsonl"),
            run_trace_resolver=resolve_trace,
        ),
        shared_budget=TestIdentityRuntime(),
        input_token_price_usd=Decimal("0"),
        price_source_ref="catalog:local:mock-small",
        price_source_version="catalog-v1",
    )
    run_id = await seed_managed_root(storage)
    request = EmbeddingRequest(input="direct replay conflict", tenant_id="tenant-a")
    try:
        await service.embed(
            request,
            context=context(run_id),
            usage_call_id="usage-embedding-replay-conflict",
        )
        async with storage.uow() as uow:
            await uow.session.execute(
                update(BudgetOperationClaimModel)
                .where(BudgetOperationClaimModel.usage_call_id == "usage-embedding-replay-conflict")
                .values(state="reserved", side_effect_state="started", result_json=None)
            )
            await uow.commit()

        with pytest.raises(BudgetOperationConflict):
            await service.embed(
                request,
                context=context(run_id),
                usage_call_id="usage-embedding-replay-conflict",
            )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_allocated_embedding_replay_rejects_started_claim_with_published_outbox(
    tmp_path: Path,
) -> None:
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
                child_input={"prompt": "allocated replay conflict"},
                idempotency_key="allocated-replay-conflict",
                request_id="request-a",
            ),
            identity=delegation_identity(),
        )
        service = EmbeddingInvocationService(
            provider=LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage)),
            storage=storage,
            event_bus=EventBus(sink=sink),
            shared_budget=TestIdentityRuntime(),
        )
        request = EmbeddingRequest(input="allocated replay conflict", tenant_id="tenant-a")
        usage_context = UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=delegated.child_run_id,
            agent_id="agent-target",
            request_id="request-a",
            trace_id="trace-parent",
        )
        await service.embed(
            request,
            context=usage_context,
            usage_call_id="usage-allocated-replay-conflict",
        )
        async with storage.uow() as uow:
            await uow.session.execute(
                update(DelegationBudgetAllocationModel)
                .where(
                    DelegationBudgetAllocationModel.usage_call_id
                    == "usage-allocated-replay-conflict"
                )
                .values(state="reserved", side_effect_state="started", result_json=None)
            )
            await uow.commit()

        with pytest.raises(BudgetOperationConflict):
            await service.embed(
                request,
                context=usage_context,
                usage_call_id="usage-allocated-replay-conflict",
            )
    finally:
        await storage.dispose()
