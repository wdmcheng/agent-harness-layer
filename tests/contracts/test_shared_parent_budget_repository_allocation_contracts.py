"""Delegation allocation 与跨账本原子组合合同。"""

# 场景文件复用统一 ledger/identity 夹具，避免测试前提在拆分后漂移。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_repository_contracts import *


@pytest.mark.asyncio
async def test_child_allocation_replays_without_parent_double_charge(tmp_path: Path) -> None:
    dsn = sqlite_dsn(tmp_path / "allocation.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="allocation")
        delegation_id, child_id = await create_delegation(
            storage, root_id=root, suffix="allocation"
        )
        allocation = AllocationBudgetClaim(
            tenant_id="tenant-a",
            budget_owner_run_id=root,
            delegation_id=delegation_id,
            usage_call_id="child-usage-a",
            identity=allocation_identity(
                root_id=root,
                child_id=child_id,
                delegation_id=delegation_id,
            ),
            token_reservation=20,
            cost_reservation=Decimal("1.00"),
        )
        async with storage.uow() as uow:
            first = await uow.shared_budget.allocate(allocation)
            await uow.shared_budget.mark_allocation_started(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=delegation_id,
                usage_call_id="child-usage-a",
            )
            await uow.shared_budget.settle_allocation(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=delegation_id,
                usage_call_id="child-usage-a",
                actual_tokens=12,
                actual_cost=Decimal("0.75"),
                cost_status="reported",
                result={"provider_called": True},
            )
            await uow.commit()
        assert first.replayed is False

        async with storage.uow() as uow:
            replay = await uow.shared_budget.allocate(allocation)
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
        assert replay.replayed is True
        assert ledger is not None
        assert ledger.token_impact == 60
        assert ledger.cost_impact == Decimal("4.00")

        conflict = allocation.model_copy(
            update={
                "identity": allocation_identity(
                    root_id=root,
                    child_id=child_id,
                    delegation_id=delegation_id,
                    fingerprint="child-request-b",
                )
            }
        )
        async with storage.uow() as uow:
            with pytest.raises(BudgetOperationConflict):
                await uow.shared_budget.allocate(conflict)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_allocation_replay_rejects_corrupted_persisted_identity_detail(
    tmp_path: Path,
) -> None:
    """Child replay 同样重算 identity，并绑定 child、agent 与 delegation。"""

    dsn = sqlite_dsn(tmp_path / "allocation-identity-integrity.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="allocation-identity-integrity")
        delegation_id, child_id = await create_delegation(
            storage, root_id=root, suffix="allocation-identity-integrity"
        )
        allocation = AllocationBudgetClaim(
            tenant_id="tenant-a",
            budget_owner_run_id=root,
            delegation_id=delegation_id,
            usage_call_id="usage-allocation-identity-integrity",
            identity=allocation_identity(
                root_id=root,
                child_id=child_id,
                delegation_id=delegation_id,
            ),
            token_reservation=20,
            cost_reservation=Decimal("1.00"),
        )
        async with storage.uow() as uow:
            await uow.shared_budget.allocate(allocation)
            await uow.commit()
        async with storage.uow() as uow:
            model = await uow.session.scalar(
                select(DelegationBudgetAllocationModel).where(
                    DelegationBudgetAllocationModel.usage_call_id
                    == "usage-allocation-identity-integrity"
                )
            )
            assert model is not None
            corrupted = dict(model.identity_json)
            corrupted["provider"] = "tampered-provider"
            model.identity_json = corrupted
            model.agent_id = "tampered-agent"
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(BudgetOperationConflict):
                await uow.shared_budget.preflight_allocation(allocation)
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
        assert ledger is not None and ledger.token_impact == 60
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_atomic_delegation_rejects_target_missing_from_explicit_edge(
    tmp_path: Path,
) -> None:
    """即使 target sub-snapshot 存在，原子 storage seam 也不得从 agents 推断 edge。"""

    dsn = sqlite_dsn(tmp_path / "delegation-explicit-edge.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(storage, suffix="delegation-explicit-edge")
        async with storage.uow() as uow:
            ledger = await uow.session.get(ParentBudgetLedgerModel, ("tenant-a", root))
            assert ledger is not None
            snapshot = dict(ledger.snapshot_json)
            owner = dict(snapshot["owner"])
            owner["delegation_targets"] = []
            snapshot["owner"] = owner
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
        async with storage.uow() as uow:
            with pytest.raises(DelegationStorageConflict, match="delegation.execution_failed"):
                await uow.delegations.claim_and_reserve(
                    DelegationClaimCreate(
                        tenant_id="tenant-a",
                        parent_run_id=root,
                        source_agent_id="agent-a",
                        target_agent_id="agent-b",
                        idempotency_key="delegation-explicit-edge",
                        request_hash="e" * 64,
                        budget_intent="inherit_parent",
                        child_input={"query": "must fail closed"},
                        identity={"user_id": "user-a"},
                        trace_id="trace-delegation-explicit-edge",
                        request_id="request-delegation-explicit-edge",
                        parent_token_limit=100,
                        requested_token_reservation=20,
                        parent_cost_limit=10.0,
                        requested_cost_reservation=1.0,
                    )
                )
            relation = await uow.session.scalar(
                select(AgentDelegationModel).where(
                    AgentDelegationModel.parent_run_id == root,
                    AgentDelegationModel.idempotency_key == "delegation-explicit-edge",
                )
            )
        assert relation is None
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_delegation_0014_0015_0016_claim_is_one_uow(tmp_path: Path) -> None:
    dsn = sqlite_dsn(tmp_path / "delegation-uow.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        root = await create_root(
            storage,
            suffix="delegation-uow",
            agent_b_token_limit=60,
            agent_b_cost_limit=Decimal("4.00"),
        )
        async with storage.uow() as uow:
            result = await uow.delegations.claim_and_reserve(
                DelegationClaimCreate(
                    tenant_id="tenant-a",
                    parent_run_id=root,
                    source_agent_id="agent-a",
                    target_agent_id="agent-b",
                    idempotency_key="delegation-uow",
                    request_hash="f" * 64,
                    budget_intent="inherit_parent",
                    child_input={"query": "safe"},
                    identity={"user_id": "user-a", "session_id": "session-delegation-uow"},
                    trace_id="trace-delegation-uow",
                    request_id="request-a",
                    parent_token_limit=100,
                    requested_token_reservation=60,
                    parent_cost_limit=10.0,
                    requested_cost_reservation=4.0,
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", root)
            top = await uow.shared_budget.reserve_delegation(
                tenant_id="tenant-a",
                budget_owner_run_id=root,
                delegation_id=result.delegation.id,
                request_hash="f" * 64,
                token_reservation=60,
                cost_reservation=Decimal("4.00"),
            )
            group = await uow.evidence_outbox.ordered_group(
                group_id=f"delegation:{result.delegation.id}:evidence"
            )
        assert ledger is not None
        assert ledger.token_impact == 60
        assert ledger.cost_impact == Decimal("4.00")
        assert top.replayed is True
        assert len(group) == 3
    finally:
        await storage.dispose()
