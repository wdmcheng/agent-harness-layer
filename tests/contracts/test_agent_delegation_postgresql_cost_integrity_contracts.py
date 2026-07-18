"""PostgreSQL Agent 委派成本精度与损坏数据边界合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_postgresql_contracts import (
    DelegationBudgetExceeded as DelegationBudgetExceeded,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    RunCreate as RunCreate,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    _claim as _claim,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    _parent as _parent,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    create_async_engine as create_async_engine,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    isolated_database as isolated_database,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    pytest as pytest,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    pytestmark as pytestmark,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    text as text,
)


@pytest.mark.asyncio
async def test_postgresql_sub_micro_cost_round_trips_without_fixed_scale_loss() -> None:
    """PostgreSQL 账本必须无损往返合同允许的有限小额 float cost。"""

    small_cost = 0.000_000_4
    async with isolated_database("delegation_sub_micro_cost") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="sub-micro")
            async with storage.uow() as uow:
                parent = await uow.runs.get(parent_run_id)
                assert parent is not None
                claimed = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="sub-micro",
                        key="sub-micro-key",
                        request_hash="m" * 64,
                        reserved_tokens=20,
                        parent_cost_limit=1.0,
                        requested_cost_reservation=1.0,
                    )
                )
                child = await uow.runs.create(
                    RunCreate(
                        tenant_id="tenant-sub-micro",
                        session_id=parent.session_id,
                        agent_id="agent-target",
                        idempotency_key=f"delegation:{claimed.delegation.id}",
                        parent_run_id=parent_run_id,
                        trace_id="trace-sub-micro",
                    )
                )
                await uow.runs.set_status(child.id, "completed", output={"ok": True})
                await uow.delegations.attach_child(
                    delegation_id=claimed.delegation.id,
                    child_run_id=child.id,
                )
                summary = {
                    "parent_run_id": parent_run_id,
                    "children": [
                        {
                            "run_id": child.id,
                            "agent_id": "agent-target",
                            "status": "completed",
                            "usage_evidence_refs": ["usage:sub-micro"],
                            "trace_refs": ["trace-sub-micro"],
                        }
                    ],
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "latency_ms": 5,
                    "cost_usd": small_cost,
                    "budget_status": "within_budget",
                    "trace_refs": ["trace-sub-micro"],
                }
                aggregate = await uow.delegations.save_aggregation(
                    delegation_id=claimed.delegation.id,
                    summary=summary,
                    evidence_refs=["usage:sub-micro", "trace-sub-micro"],
                    needs_review=False,
                )
                await uow.commit()
            async with storage.uow() as uow:
                reservation = await uow.delegations.get_reservation(claimed.delegation.id)
        finally:
            await storage.dispose()

    assert aggregate.summary["cost_usd"] == small_cost
    assert reservation.settled_cost_usd == small_cost


@pytest.mark.asyncio
async def test_postgresql_cost_disabled_owner_keeps_delegation_cost_disabled() -> None:
    """Owner 关闭 cost 时 target 不得重新启用该维度。"""

    async with isolated_database("delegation_unlimited_parent_finite_target") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(
                storage,
                suffix="unlimited-parent",
                cost_limit=None,
            )
            async with storage.uow() as uow:
                claimed = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="unlimited-parent",
                        key="finite-target-key",
                        request_hash="f" * 64,
                        reserved_tokens=20,
                        parent_cost_limit=None,
                        requested_cost_reservation=None,
                    )
                )
                await uow.commit()
            async with storage.uow() as uow:
                reservation = await uow.delegations.get_reservation(claimed.delegation.id)
                ledger = await uow.shared_budget.get_ledger(
                    "tenant-unlimited-parent",
                    parent_run_id,
                )
        finally:
            await storage.dispose()

    assert reservation.reserved_cost_usd is None
    assert ledger is not None
    assert ledger.cost_limit is None
    assert ledger.cost_impact == 0


@pytest.mark.asyncio
async def test_postgresql_finite_owner_cost_is_inherited_by_null_target_ceiling() -> None:
    """Target cost ceiling 为 null 时继承已启用的 owner ceiling。"""

    async with isolated_database("delegation_unbounded_cost") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="unbounded-cost")
            async with storage.uow() as uow:
                claimed = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="unbounded-cost",
                        key="unbounded-cost-key",
                        request_hash="a" * 64,
                        reserved_tokens=10,
                        parent_cost_limit=10.0,
                        requested_cost_reservation=None,
                    )
                )
                await uow.commit()
            async with storage.uow() as uow:
                rows = await uow.delegations.list_for_parent(
                    tenant_id="tenant-unbounded-cost",
                    parent_run_id=parent_run_id,
                )
                pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
                capacity = await uow.event_capacity.snapshot(parent_run_id)
                ledger = await uow.shared_budget.get_ledger(
                    "tenant-unbounded-cost",
                    parent_run_id,
                )
        finally:
            await storage.dispose()

    assert len(rows) == 1
    assert rows[0].id == claimed.delegation.id
    assert claimed.reservation.reserved_cost_usd == 10.0
    assert len(pending) == 3
    assert capacity.outstanding_reserved_event_count == 3
    assert ledger is not None
    assert ledger.cost_impact == 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "settled_input_tokens", "settled_output_tokens", "settled_cost_usd"),
    [
        ("missing_token", None, 0, 0.0),
        ("negative_token", -1, 0, 0.0),
        ("negative_cost", 0, 0, -1.0),
        ("nan_cost", 0, 0, float("nan")),
        ("infinite_cost", 0, 0, float("inf")),
    ],
)
async def test_postgresql_corrupt_settled_reservation_fails_closed(
    case: str,
    settled_input_tokens: int | None,
    settled_output_tokens: int,
    settled_cost_usd: float,
) -> None:
    """真实 PostgreSQL 即使模拟旧库绕过新 CHECK，预算读取仍拒绝坏结算。"""

    async with isolated_database(f"delegation_corrupt_settled_{case}") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix=case)
            async with storage.uow() as uow:
                first = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix=case,
                        key="first-key",
                        request_hash="a" * 64,
                        reserved_tokens=10,
                        parent_cost_limit=10.0,
                        requested_cost_reservation=1.0,
                    )
                )
                constraints = set(
                    await uow.session.scalars(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'delegation_budget_reservations'::regclass"
                        )
                    )
                )
                await uow.commit()
            assert {
                "ck_delegation_budget_settled_input",
                "ck_delegation_budget_settled_output",
                "ck_delegation_budget_settled_cost",
                "ck_delegation_budget_settled_complete",
            } <= constraints

            engine = create_async_engine(dsn)
            try:
                async with engine.begin() as connection:
                    # isolated database 内临时移除新约束，模拟旧版本、人工修复或损坏备份；
                    # repository 仍必须在任何新 claim 副作用前执行第二层 fail-closed。
                    for constraint in (
                        "ck_delegation_budget_settled_input",
                        "ck_delegation_budget_settled_output",
                        "ck_delegation_budget_settled_cost",
                        "ck_delegation_budget_settled_complete",
                    ):
                        await connection.execute(
                            text(
                                "ALTER TABLE delegation_budget_reservations "
                                f"DROP CONSTRAINT {constraint}"
                            )
                        )
                    await connection.execute(
                        text(
                            "UPDATE delegation_budget_reservations "
                            "SET state = 'settled', settled_input_tokens = :input_tokens, "
                            "settled_output_tokens = :output_tokens, "
                            "settled_cost_usd = :cost_usd "
                            "WHERE delegation_id = :delegation_id"
                        ),
                        {
                            "input_tokens": settled_input_tokens,
                            "output_tokens": settled_output_tokens,
                            "cost_usd": settled_cost_usd,
                            "delegation_id": first.delegation.id,
                        },
                    )
            finally:
                await engine.dispose()

            async with storage.uow() as uow:
                with pytest.raises(DelegationBudgetExceeded) as captured:
                    await uow.delegations.claim_and_reserve(
                        _claim(
                            parent_run_id,
                            suffix=case,
                            key="second-key",
                            request_hash="b" * 64,
                            reserved_tokens=10,
                            parent_cost_limit=10.0,
                            requested_cost_reservation=1.0,
                        )
                    )
            async with storage.uow() as uow:
                rows = await uow.delegations.list_for_parent(
                    tenant_id=f"tenant-{case}",
                    parent_run_id=parent_run_id,
                )
                pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
                capacity = await uow.event_capacity.snapshot(parent_run_id)
        finally:
            await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert len(rows) == 1
    assert len(pending) == 3
    assert capacity.outstanding_reserved_event_count == 3
