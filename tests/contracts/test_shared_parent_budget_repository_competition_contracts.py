"""SQLite direct/delegation 原子竞争合同。"""

# 场景文件复用统一 ledger/identity 夹具，避免测试前提在拆分后漂移。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_repository_contracts import *


@pytest.mark.asyncio
async def test_sqlite_true_concurrency_commits_only_safe_direct_combination(
    tmp_path: Path,
) -> None:
    """两个并发 direct 预约争夺同一账本时只能保留一个安全组合，另一个必须被原子拒绝。"""

    dsn = sqlite_dsn(tmp_path / "direct-race.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="direct-race")

        async def compete(suffix: str) -> str:
            """以独立 UoW 提交一次预算 claim，并将容量拒绝转为可排序结果供并发断言。"""

            operation = identity(run_id=root, fingerprint=f"request-{suffix}")
            async with storage.uow() as uow:
                try:
                    await uow.shared_budget.claim_direct(
                        DirectBudgetClaim(
                            tenant_id="tenant-a",
                            budget_owner_run_id=root,
                            usage_call_id=f"usage-{suffix}",
                            identity=operation,
                            token_reservation=60,
                            cost_reservation=Decimal("4.00"),
                        )
                    )
                except BudgetReservationRejected:
                    return "rejected"
                await uow.commit()
                return "committed"

        outcomes = await asyncio.gather(compete("a"), compete("b"))
        assert sorted(outcomes) == ["committed", "rejected"]
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
        assert ledger is not None
        assert ledger.token_impact == 60
        assert ledger.cost_impact == Decimal("4.00")
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_direct_and_delegation_compete_and_terminal_is_fenced(tmp_path: Path) -> None:
    """direct 已占预算后 delegation 不得绕过同一 owner 余额，且未闭合影响必须阻止终态。"""

    dsn = sqlite_dsn(tmp_path / "competition.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="competition")
        async with storage.uow() as uow:
            await uow.shared_budget.claim_direct(
                DirectBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    usage_call_id="usage-a",
                    identity=identity(run_id=root),
                    token_reservation=60,
                    cost_reservation=Decimal("4.00"),
                )
            )
            delegation_id = str(uuid4())
            uow.session.add(
                AgentDelegationModel(
                    id=delegation_id,
                    tenant_id="tenant-a",
                    parent_run_id=root,
                    child_run_id=None,
                    source_agent_id="agent-a",
                    target_agent_id="agent-b",
                    idempotency_key="delegation-a",
                    request_hash="a" * 64,
                    budget_intent="inherit_parent",
                    child_input_json={"query": "safe"},
                    identity_json={"user_id": "user-a"},
                    trace_id="trace-competition",
                    request_id="request-a",
                    status="claimed",
                    error_json=None,
                    event_operation_kind="delegation",
                    event_registry_version="v1",
                    reserved_event_count=4,
                )
            )
            await uow.session.flush()
            with pytest.raises(DelegationBudgetExceeded, match="delegation.budget_exceeded"):
                await uow.shared_budget.reserve_delegation(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    delegation_id=delegation_id,
                    request_hash="a" * 64,
                    identity=delegation_identity(
                        root_id=root,
                        delegation_id=delegation_id,
                        idempotency_key="delegation-a",
                        request_hash="a" * 64,
                        token_bound=50,
                        cost_bound=Decimal("3.00"),
                    ),
                    token_reservation=50,
                    cost_reservation=Decimal("3.00"),
                )
            assert await uow.shared_budget.terminal_allowed("tenant-a", root) is False
            await uow.commit()

        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
        assert ledger is not None
        assert ledger.token_impact == 60
        assert ledger.cost_impact == Decimal("4.00")
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_cost_disabled_ignores_legal_unavailable_cost(tmp_path: Path) -> None:
    """成本功能关闭时，可用性为 unknown 的合法成本不应被虚构为金额或阻塞 token 结算。"""

    dsn = sqlite_dsn(tmp_path / "cost-disabled.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="cost-disabled", cost_limit=None)
        async with storage.uow() as uow:
            operation = identity(run_id=root).model_copy(
                update={
                    "cost_enabled": False,
                    "trusted_token_bound": 20,
                    "trusted_cost_bound": None,
                }
            )
            result = await uow.shared_budget.claim_direct(
                DirectBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root,
                    usage_call_id="usage-cost-disabled",
                    identity=operation.rehashed(),
                    token_reservation=20,
                    cost_reservation=None,
                )
            )
            await uow.shared_budget.settle_direct(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                usage_call_id="usage-cost-disabled",
                actual_tokens=12,
                actual_cost=None,
                cost_status="unavailable",
                result={"provider_called": True},
            )
            await uow.commit()
        assert result.replayed is False
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
            assert await uow.shared_budget.terminal_allowed("tenant-a", root) is True
        assert ledger is not None
        assert ledger.token_impact == 12
        assert ledger.cost_impact == Decimal("0")
        assert ledger.state == "active"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_delegation_then_direct_competes_on_same_owner(tmp_path: Path) -> None:
    """先占用 delegation 后，root direct 仍必须读取同一 parent 余额。"""

    dsn = sqlite_dsn(tmp_path / "delegation-then-direct.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="delegation-first")
        await create_delegation(storage, root_id=root, suffix="delegation-first")
        direct_identity = (
            identity(run_id=root)
            .model_copy(
                update={
                    "trusted_token_bound": 50,
                    "trusted_cost_bound": Decimal("3.00"),
                }
            )
            .rehashed()
        )
        async with storage.uow() as uow:
            with pytest.raises(BudgetReservationRejected, match="budget.reservation_rejected"):
                await uow.shared_budget.claim_direct(
                    DirectBudgetClaim(
                        tenant_id="tenant-a",
                        budget_owner_run_id=root,
                        usage_call_id="usage-after-delegation",
                        identity=direct_identity,
                        token_reservation=50,
                        cost_reservation=Decimal("3.00"),
                    )
                )
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
        assert ledger is not None
        assert ledger.token_impact == 60
        assert ledger.cost_impact == Decimal("4.00")
    finally:
        await storage.dispose()
