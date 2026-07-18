"""Shared ledger settlement、recovery 与非法 usage 合同。"""

# 场景文件复用统一 ledger/identity 夹具，避免测试前提在拆分后漂移。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_repository_contracts import *


@pytest.mark.asyncio
async def test_actual_over_promotes_parent_to_needs_review_without_double_charge(
    tmp_path: Path,
) -> None:
    """Child actual-over 只提升 top-level 差额，不能把 allocation 再加一次。"""

    dsn = sqlite_dsn(tmp_path / "allocation-over.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="allocation-over")
        delegation_id, child_id = await create_delegation(
            storage, root_id=root, suffix="allocation-over"
        )
        async with storage.uow() as uow:
            await uow.shared_budget.allocate(
                AllocationBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    delegation_id=delegation_id,
                    usage_call_id="allocation-over",
                    identity=allocation_identity(
                        root_id=root,
                        child_id=child_id,
                        delegation_id=delegation_id,
                    ),
                    token_reservation=20,
                    cost_reservation=Decimal("1.00"),
                )
            )
            await uow.shared_budget.mark_allocation_started(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=delegation_id,
                usage_call_id="allocation-over",
            )
            settled = await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=delegation_id,
                usage_call_id="allocation-over",
                actual_tokens=70,
                actual_cost=Decimal("5.00"),
                cost_status="reported",
                result={"provider_called": True},
            )
            await uow.commit()
        assert settled.state == "needs_review"
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
            top = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.delegation_id == delegation_id
                )
            )
            terminal_allowed = await uow.shared_budget.terminal_allowed("tenant-a", root)
            top_impact = None if top is None else top.token_impact
            top_state = None if top is None else top.state
        assert ledger is not None
        assert ledger.token_impact == 70
        assert ledger.cost_impact == Decimal("5.00")
        assert ledger.state == "needs_review"
        assert top_impact == 70 and top_state == "needs_review"
        assert terminal_allowed is False
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_multiple_delegations_actual_over_use_conservative_top_level_delta(
    tmp_path: Path,
) -> None:
    """多个 child actual-over 只按各自 top-level max 汇总，并保留真实超额。"""

    dsn = sqlite_dsn(tmp_path / "multi-allocation-over.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(
            storage,
            suffix="multi-allocation-over",
            agent_b_token_limit=40,
        )
        first_id, first_child = await create_delegation(
            storage,
            root_id=root,
            suffix="multi-allocation-over-a",
            token_reservation=40,
            parent_suffix="multi-allocation-over",
        )
        second_id, second_child = await create_delegation(
            storage,
            root_id=root,
            suffix="multi-allocation-over-b",
            token_reservation=40,
            parent_suffix="multi-allocation-over",
        )
        async with storage.uow() as uow:
            for delegation_id, child_id, slot in (
                (first_id, first_child, "multi-over-a"),
                (second_id, second_child, "multi-over-b"),
            ):
                await uow.shared_budget.allocate(
                    AllocationBudgetClaim(
                        tenant_id="tenant-a",
                        budget_owner_run_id=root,
                        delegation_id=delegation_id,
                        usage_call_id=slot,
                        identity=allocation_identity(
                            root_id=root,
                            child_id=child_id,
                            delegation_id=delegation_id,
                        ),
                        token_reservation=20,
                        cost_reservation=Decimal("1.00"),
                    )
                )
                await uow.shared_budget.mark_allocation_started(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    delegation_id=delegation_id,
                    usage_call_id=slot,
                )
            await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=first_id,
                usage_call_id="multi-over-a",
                actual_tokens=50,
                actual_cost=Decimal("5.00"),
                cost_status="reported",
                result={"provider_called": True},
            )
            await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=second_id,
                usage_call_id="multi-over-b",
                actual_tokens=60,
                actual_cost=Decimal("6.00"),
                cost_status="reported",
                result={"provider_called": True},
            )
            await uow.commit()
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
            top_rows = list(
                await uow.session.scalars(
                    select(BudgetOperationClaimModel)
                    .where(BudgetOperationClaimModel.delegation_id.in_([first_id, second_id]))
                    .order_by(BudgetOperationClaimModel.delegation_id)
                )
            )
            top_impacts = sorted(row.token_impact for row in top_rows)
        assert ledger is not None
        assert ledger.token_impact == 110
        assert ledger.cost_impact == Decimal("11.00")
        assert ledger.state == "needs_review"
        assert top_impacts == [50, 60]
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_delegation_settlement_cannot_downgrade_uncertain_allocation(
    tmp_path: Path,
) -> None:
    """Child needs_review 后，可信 aggregate 也不得释放顶层保守 reservation。"""

    dsn = sqlite_dsn(tmp_path / "allocation-review-fence.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="allocation-review-fence")
        delegation_id, child_id = await create_delegation(
            storage, root_id=root, suffix="allocation-review-fence"
        )
        async with storage.uow() as uow:
            await uow.shared_budget.allocate(
                AllocationBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    delegation_id=delegation_id,
                    usage_call_id="uncertain-child",
                    identity=allocation_identity(
                        root_id=root,
                        child_id=child_id,
                        delegation_id=delegation_id,
                    ),
                    token_reservation=20,
                    cost_reservation=Decimal("1.00"),
                )
            )
            await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=delegation_id,
                usage_call_id="uncertain-child",
                actual_tokens=None,
                actual_cost=None,
                cost_status="unavailable",
                result={"provider_called": True},
            )
            settled = await uow.shared_budget.settle_delegation(
                delegation_id=delegation_id,
                actual_tokens=30,
                actual_cost=Decimal("2.00"),
                cost_status="reported",
                needs_review=False,
                result={"aggregate_status": "complete"},
            )
            await uow.commit()

        assert settled is not None
        assert settled.state == "needs_review"
        assert settled.token_impact == 60
        assert settled.cost_impact == Decimal("4.00")
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
        assert ledger is not None
        assert ledger.state == "needs_review"
        assert ledger.token_impact == 60
        assert ledger.cost_impact == Decimal("4.00")
    finally:
        await storage.dispose()


@pytest.mark.parametrize(
    ("terminal_tokens", "terminal_cost"),
    [
        (10, Decimal("1.00")),
        (12, Decimal("0.50")),
    ],
    ids=["token-mismatch", "cost-mismatch"],
)
@pytest.mark.asyncio
async def test_delegation_terminal_aggregate_must_match_settled_allocations(
    tmp_path: Path,
    terminal_tokens: int,
    terminal_cost: Decimal,
) -> None:
    """Terminal aggregate 与 child allocation 任一启用维度矛盾都必须封锁。"""

    dsn = sqlite_dsn(tmp_path / f"aggregate-mismatch-{terminal_tokens}-{terminal_cost}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        suffix = f"aggregate-mismatch-{terminal_tokens}"
        root = await create_root(storage, suffix=suffix)
        delegation_id, child_id = await create_delegation(
            storage,
            root_id=root,
            suffix=suffix,
        )
        async with storage.uow() as uow:
            await uow.shared_budget.allocate(
                AllocationBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    delegation_id=delegation_id,
                    usage_call_id="aggregate-mismatch-child",
                    identity=allocation_identity(
                        root_id=root,
                        child_id=child_id,
                        delegation_id=delegation_id,
                    ),
                    token_reservation=20,
                    cost_reservation=Decimal("1.00"),
                )
            )
            await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=delegation_id,
                usage_call_id="aggregate-mismatch-child",
                actual_tokens=12,
                actual_cost=Decimal("1.00"),
                cost_status="reported",
                result={"provider_called": True},
            )
            settled = await uow.shared_budget.settle_delegation(
                delegation_id=delegation_id,
                actual_tokens=terminal_tokens,
                actual_cost=terminal_cost,
                cost_status="reported",
                needs_review=False,
                result={"aggregate_status": "complete"},
            )
            terminal_allowed = await uow.shared_budget.terminal_allowed("tenant-a", root)
            await uow.commit()

        assert settled is not None
        assert settled.state == "needs_review"
        assert settled.token_impact == 60
        assert settled.cost_impact == Decimal("4.00")
        assert terminal_allowed is False
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
        assert ledger is not None
        assert ledger.state == "needs_review"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_recovery_marks_started_direct_and_allocation_unknown(tmp_path: Path) -> None:
    """第二崩溃窗口同时覆盖 direct 与 child allocation，且保留原预约。"""

    dsn = sqlite_dsn(tmp_path / "started-recovery.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="started-recovery")
        direct_identity = (
            identity(run_id=root)
            .model_copy(
                update={
                    "trusted_token_bound": 20,
                    "trusted_cost_bound": Decimal("1.00"),
                }
            )
            .rehashed()
        )
        async with storage.uow() as uow:
            await uow.shared_budget.claim_direct(
                DirectBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    usage_call_id="started-direct",
                    identity=direct_identity,
                    token_reservation=20,
                    cost_reservation=Decimal("1.00"),
                )
            )
            await uow.shared_budget.mark_direct_started(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                usage_call_id="started-direct",
            )
            await uow.commit()
        delegation_id, child_id = await create_delegation(
            storage, root_id=root, suffix="started-recovery"
        )
        async with storage.uow() as uow:
            await uow.shared_budget.allocate(
                AllocationBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    delegation_id=delegation_id,
                    usage_call_id="started-allocation",
                    identity=allocation_identity(
                        root_id=root,
                        child_id=child_id,
                        delegation_id=delegation_id,
                    ),
                    token_reservation=20,
                    cost_reservation=Decimal("1.00"),
                )
            )
            await uow.shared_budget.mark_allocation_started(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=delegation_id,
                usage_call_id="started-allocation",
            )
            assert (
                await uow.shared_budget.recover_unknown_started(
                    tenant_id="tenant-a", budget_owner_run_id=root
                )
                == 2
            )
            await uow.commit()
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
            terminal_allowed = await uow.shared_budget.terminal_allowed("tenant-a", root)
        assert ledger is not None
        assert ledger.token_impact == 80
        assert ledger.state == "needs_review"
        assert terminal_allowed is False
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_illegal_usage_is_rejected_even_when_cost_is_disabled(tmp_path: Path) -> None:
    """关闭 cost 只忽略合法 unavailable，不放过非法数值或 status 组合。"""

    dsn = sqlite_dsn(tmp_path / "illegal-cost-disabled.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="illegal-cost-disabled", cost_limit=None)
        operation = (
            identity(run_id=root)
            .model_copy(
                update={
                    "cost_enabled": False,
                    "trusted_token_bound": 20,
                    "trusted_cost_bound": None,
                }
            )
            .rehashed()
        )
        async with storage.uow() as uow:
            await uow.shared_budget.claim_direct(
                DirectBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    usage_call_id="illegal-cost-disabled",
                    identity=operation,
                    token_reservation=20,
                    cost_reservation=None,
                )
            )
            with pytest.raises(ValueError, match="cost_usd/cost_status"):
                await uow.shared_budget.settle_direct(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    usage_call_id="illegal-cost-disabled",
                    actual_tokens=10,
                    actual_cost=Decimal("1"),
                    cost_status="unavailable",
                    result={"provider_called": True},
                )
    finally:
        await storage.dispose()
