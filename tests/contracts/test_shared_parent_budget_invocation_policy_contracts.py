"""Shared budget 软策略、hard limit 与 fallback 合同。"""

# 场景文件复用同一完整 frozen-root 夹具，避免复制不可变 snapshot 构造。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_invocation_contracts import *

from agent_harness.storage import ApprovalCreate


@pytest.mark.asyncio
async def test_approved_continuation_rechecks_current_parent_balance(tmp_path: Path) -> None:
    """Soft approval 不占额度；等待期间余额被消费后 continuation 必须 hard reject。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'approved-balance.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    provider = CountingFakeModelProvider()
    sink = LocalJsonlEventSink(tmp_path / "approved-balance-events.jsonl")
    request = model_request()
    try:
        run_id = await seed_managed_root(
            storage,
            token_limit=10,
            soft_token_limit=4,
        )
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_model="fake-basic",
                    max_tokens_per_call=4,
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
        first = await service.complete(
            request,
            context=context(run_id),
            usage_call_id="usage-awaiting-approval",
        )
        assert first.decision.action == "policy_required"
        arguments_hash = hashlib.sha256(
            json.dumps(
                request.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        async with storage.uow() as uow:
            approval = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    action="model.invoke",
                    resource="agent:agent-a:model",
                    reason="shared budget soft policy contract",
                    requested_by="user-a",
                    trace_id="trace-a",
                    request_id="request-a",
                    metadata={
                        "identity_id": "user-a",
                        "arguments_hash": arguments_hash,
                    },
                )
            )
            lease = await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=run_id,
                tenant_id="tenant-a",
                request_id="approve-after-budget-change",
            )
            await uow.commit()

        # durable lease 已建立后才消费竞争额度，精确模拟人工审批等待窗口。
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            assert ledger is not None and ledger.token_impact == 0
            competing_identity = OperationIdentity.from_semantic_request(
                tenant_id="tenant-a",
                fingerprint_key=b"test-only-budget-fingerprint-key",
                fingerprint_key_version="test-v1",
                ownership_kind="direct",
                run_id=run_id,
                agent_id="agent-a",
                delegation_claim_id=None,
                usage_kind="model",
                operation_slot="competing-operation",
                semantic_request={"prompt": "competing"},
                tree_snapshot_id=f"snapshot:{run_id}",
                agent_sub_snapshot_id=f"snapshot:{run_id}:agent-a",
                provider="fake",
                model="fake-basic",
                price_source_ref="catalog:fake",
                price_source_version="catalog-v1",
                cache_key_digest=None,
                cost_enabled=False,
                trusted_token_bound=6,
                trusted_cost_bound=None,
            )
            await uow.shared_budget.claim_direct(
                DirectBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=run_id,
                    usage_call_id="usage-competing",
                    identity=competing_identity,
                    token_reservation=6,
                    cost_reservation=None,
                )
            )
            await uow.commit()
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
        grant = ApprovalGrant(
            approval_id=approval.approval_id,
            lease_id=lease.lease_id,
            tenant_id="tenant-a",
            identity_id="user-a",
            session_id="session-a",
            agent_id="agent-a",
            run_id=run_id,
            action="model.invoke",
            resource="agent:agent-a:model",
            arguments_hash=arguments_hash,
        )
        with pytest.raises(BudgetReservationRejected) as rejected:
            await bound.complete_approved(
                request,
                operation_key="approved-model",
                grant=grant,
            )
        assert rejected.value.reason == "balance_insufficient"
        assert provider.calls == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_model_claim_outbox_and_settlement_share_atomic_owner(tmp_path: Path) -> None:
    """验证模型 claim、用量证据和结算在同一父账本所有者下原子收敛。

    这防止 provider 调用完成后只留下 outbox、只更新账本或遗失 claim 状态，
    从而使重试与预算审计面对互相矛盾的事实。
    """
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'model.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "model-events.jsonl")
    try:
        run_id = await seed_managed_root(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_model="fake-basic",
                    max_tokens_per_call=100,
                    input_token_price_usd=Decimal("0"),
                    output_token_price_usd=Decimal("0"),
                    price_source_ref="catalog:fake",
                    price_source_version="catalog-v1",
                ),
                providers={"fake": FakeModelProvider()},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
        )
        await service.complete(
            ModelRequest(
                provider="fake",
                prompt="abc",
                estimated_input_tokens=1,
                max_output_tokens=2,
            ),
            context=context(run_id),
            usage_call_id="usage-model-a",
        )
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id="usage-model-a"
            )
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-model-a"
                )
            )
            outbox_state = outbox.state
            claim_state = None if claim is None else claim.state
            claim_side_effect = None if claim is None else claim.side_effect_state
            claim_reserved_tokens = None if claim is None else claim.reserved_tokens
            claim_token_impact = None if claim is None else claim.token_impact
        assert ledger is not None and ledger.token_impact == 2
        assert outbox_state == "published"
        assert claim_state == "settled"
        assert claim_side_effect == "result_committed"
        assert claim_reserved_tokens == 5
        assert claim_token_impact == 2
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_soft_policy_required_does_not_hold_parent_budget(tmp_path: Path) -> None:
    """Soft review 只产出可审计拒绝，不能提前建立 shared claim。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'soft-policy.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "soft-policy-events.jsonl")
    provider = CountingFakeModelProvider()
    try:
        run_id = await seed_managed_root(storage, soft_token_limit=4)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_model="fake-basic",
                    max_tokens_per_call=4,
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
        response = await service.complete(
            model_request(), context=context(run_id), usage_call_id="usage-soft-review"
        )
        assert response.decision.action == "policy_required"
        assert provider.calls == 0
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-soft-review"
                )
            )
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id="usage-soft-review"
            )
            outbox_state = outbox.state
            outbox_error_code = outbox.error_code
        assert ledger is not None and ledger.token_impact == 0
        assert claim is None
        assert outbox_state == "published"
        assert outbox_error_code == "model.policy_required"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_static_hard_limit_rejects_before_soft_policy_and_provider(tmp_path: Path) -> None:
    """同一 intent 同时超过 soft threshold 与 frozen hard limit 时先 hard reject。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'hard-before-soft.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "hard-before-soft-events.jsonl")
    provider = CountingFakeModelProvider()
    try:
        run_id = await seed_managed_root(
            storage,
            token_limit=4,
            soft_token_limit=4,
        )
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_model="fake-basic",
                    max_tokens_per_call=4,
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
        with pytest.raises(BudgetReservationRejected) as rejected:
            await service.complete(
                model_request(), context=context(run_id), usage_call_id="usage-hard-reject"
            )
        assert rejected.value.reason == "hard_limit_ineligible"
        assert provider.calls == 0
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-hard-reject"
                )
            )
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id="usage-hard-reject"
            )
            outbox_state = outbox.state
            outbox_error_code = outbox.error_code
            outbox_result = outbox.result_json
        assert ledger is not None and ledger.token_impact == 0
        assert claim is None
        assert outbox_state == "published"
        assert outbox_error_code == BudgetReservationRejected.code
        assert outbox_result is not None
        rejection_decision = outbox_result["evidence"]["decision"]
        assert rejection_decision["budget_rejection_reason"] == "hard_limit_ineligible"
        assert rejection_decision["provider_called"] is False
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_cost_enabled_embedding_without_trusted_price_records_hard_rejection(
    tmp_path: Path,
) -> None:
    """启用 cost 维度却无可信价格时，cache miss 必须在 provider 前 fail closed。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'embedding-unbounded-cost.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    sink = LocalJsonlEventSink(tmp_path / "embedding-unbounded-cost-events.jsonl")
    try:
        run_id = await seed_managed_root(
            storage,
            cost_limit=Decimal("1"),
            embedding_price=None,
        )
        service = EmbeddingInvocationService(
            provider=LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage)),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
            input_token_price_usd=None,
            price_source_ref="catalog:local:mock-small",
            price_source_version="catalog-v1",
        )
        with pytest.raises(BudgetReservationRejected) as rejected:
            await service.embed(
                EmbeddingRequest(input="uncached", tenant_id="tenant-a"),
                context=context(run_id),
                usage_call_id="usage-embedding-unbounded",
            )
        assert rejected.value.reason == "intent_unbounded"
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-embedding-unbounded"
                )
            )
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a", usage_call_id="usage-embedding-unbounded"
            )
            outbox_state = outbox.state
            outbox_error_code = outbox.error_code
            outbox_result = outbox.result_json
        assert ledger is not None and ledger.token_impact == 0
        assert ledger.cost_impact == 0
        assert claim is None
        assert outbox_state == "published"
        assert outbox_error_code == BudgetReservationRejected.code
        assert outbox_result is not None
        rejection_decision = outbox_result["evidence"]["decision"]
        assert rejection_decision["budget_rejection_reason"] == "intent_unbounded"
        assert rejection_decision["provider_called"] is False
    finally:
        await storage.dispose()
