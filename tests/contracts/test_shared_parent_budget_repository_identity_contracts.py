"""Shared ledger identity、catalog 与 frozen target 合同。"""

# 场景文件复用统一 ledger/identity 夹具，避免测试前提在拆分后漂移。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_repository_contracts import *


@pytest.mark.asyncio
async def test_direct_claim_replay_conflict_and_root_isolation(tmp_path: Path) -> None:
    dsn = sqlite_dsn(tmp_path / "shared-budget.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root_a = await create_root(storage, suffix="a")
        root_b = await create_root(storage, suffix="b")

        claim = DirectBudgetClaim(
            tenant_id="tenant-a",
            budget_owner_run_id=root_a,
            usage_call_id="usage-a",
            identity=identity(run_id=root_a),
            token_reservation=60,
            cost_reservation=Decimal("4.00"),
        )
        async with storage.uow() as uow:
            first = await uow.shared_budget.claim_direct(claim)
            await uow.commit()
        assert first.replayed is False

        async with storage.uow() as uow:
            replay = await uow.shared_budget.claim_direct(claim)
            ledger_a = await uow.shared_budget.get_ledger("tenant-a", root_a)
            ledger_b = await uow.shared_budget.get_ledger("tenant-a", root_b)
        assert replay.replayed is True
        assert ledger_a is not None and ledger_a.token_impact == 60
        assert ledger_b is not None and ledger_b.token_impact == 0

        conflicting = claim.model_copy(
            update={"identity": identity(run_id=root_a, fingerprint="request-b")}
        )
        async with storage.uow() as uow:
            with pytest.raises(BudgetOperationConflict, match="budget.operation_conflict"):
                await uow.shared_budget.claim_direct(conflicting)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "target-missing",
        "agent-mismatch",
        "budget-over-owner",
        "route-price-missing",
        "fallback-route-missing",
    ],
)
async def test_ledger_creation_rejects_incomplete_target_catalog(
    tmp_path: Path,
    case: str,
) -> None:
    """所有显式 target sub-snapshot 必须在首次 ledger INSERT 前完整可证。"""

    dsn = sqlite_dsn(tmp_path / f"invalid-catalog-{case}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        baseline = await create_root(storage, suffix=f"catalog-baseline-{case}")
        async with storage.uow() as uow:
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id=f"session-catalog-baseline-{case}",
                    agent_id="agent-a",
                    trace_id=f"trace-invalid-catalog-{case}",
                )
            )
            baseline_snapshot = await uow.shared_budget.get_tree_snapshot("tenant-a", baseline)
            assert baseline_snapshot is not None
            snapshot = corrupt_tree_catalog(baseline_snapshot, case)
            owner = snapshot["owner"]
            assert isinstance(owner, dict)
            owner["root_run_id"] = run.id
            with pytest.raises(BudgetReservationRejected) as rejected:
                await uow.shared_budget.create_ledger(
                    LedgerCreate(
                        tenant_id="tenant-a",
                        budget_owner_run_id=run.id,
                        token_limit=100,
                        cost_limit=Decimal("10.00"),
                        registry_version="registry-v1",
                        config_version="config-v1",
                        catalog_version="catalog-v1",
                        snapshot_id=f"snapshot:{run.id}",
                        snapshot=snapshot,
                    )
                )
            assert rejected.value.reason == "snapshot_invalid"
            assert await uow.shared_budget.get_ledger("tenant-a", run.id) is None
            await uow.commit()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_direct_replay_rejects_corrupted_persisted_identity_detail(tmp_path: Path) -> None:
    """独立 hash 列不可信；durable JSON 与 denormalized detail 必须重新绑定。"""

    dsn = sqlite_dsn(tmp_path / "direct-identity-integrity.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="direct-identity-integrity")
        claim = DirectBudgetClaim(
            tenant_id="tenant-a",
            budget_owner_run_id=root,
            usage_call_id="usage-direct-identity-integrity",
            identity=identity(run_id=root),
            token_reservation=60,
            cost_reservation=Decimal("4.00"),
        )
        async with storage.uow() as uow:
            await uow.shared_budget.claim_direct(claim)
            await uow.commit()
        async with storage.uow() as uow:
            model = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == "usage-direct-identity-integrity"
                )
            )
            assert model is not None and model.identity_json is not None
            corrupted = dict(model.identity_json)
            corrupted["provider"] = "tampered-provider"
            model.identity_json = corrupted
            model.agent_id = "tampered-agent"
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(BudgetOperationConflict):
                await uow.shared_budget.preflight_direct(claim)
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
        assert ledger is not None and ledger.token_impact == 60
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_frozen_target_ceiling_only_tightens_owner_hard_limit(tmp_path: Path) -> None:
    """Owner 允许 100 时，root agent 自身冻结 ceiling=50 仍必须拒绝 60。"""

    dsn = sqlite_dsn(tmp_path / "target-ceiling.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(
            storage,
            suffix="target-ceiling",
            agent_a_token_limit=50,
        )
        async with storage.uow() as uow:
            with pytest.raises(BudgetReservationRejected) as rejected:
                await uow.shared_budget.claim_direct(
                    DirectBudgetClaim(
                        tenant_id="tenant-a",
                        budget_owner_run_id=root,
                        usage_call_id="usage-target-ceiling",
                        identity=identity(run_id=root),
                        token_reservation=60,
                        cost_reservation=Decimal("4.00"),
                    )
                )
            assert rejected.value.reason == "hard_limit_ineligible"
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
        assert ledger is not None and ledger.token_impact == 0
    finally:
        await storage.dispose()
