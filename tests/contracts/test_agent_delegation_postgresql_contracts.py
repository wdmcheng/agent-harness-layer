"""真实 PostgreSQL 下 delegation parent lock、并发幂等与预算竞争合同。"""

from __future__ import annotations

import asyncio
import os

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.run_trace_migration_test_helpers import migration_config

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_repositories import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationStorageConflict,
)
from agent_harness.storage.repositories import RunCreate, SessionCreate

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="delegation 并发合同需要真实 PostgreSQL。",
)


async def _parent(storage: SQLAlchemyStorage, *, suffix: str) -> str:
    async with storage.uow() as uow:
        await uow.tenants.ensure(f"tenant-{suffix}")
        session = await uow.sessions.create(
            SessionCreate(
                tenant_id=f"tenant-{suffix}",
                user_id="user-a",
                agent_id="agent-source",
            )
        )
        parent = await uow.runs.create(
            RunCreate(
                tenant_id=f"tenant-{suffix}",
                session_id=session.id,
                agent_id="agent-source",
                trace_id=f"trace-{suffix}",
            )
        )
        await uow.commit()
        return parent.id


def _claim(
    parent_run_id: str,
    *,
    suffix: str,
    key: str,
    request_hash: str,
    reserved_tokens: int,
    parent_limit: int = 100,
    parent_cost_limit: float | None = None,
    requested_cost_reservation: float | None = None,
) -> DelegationClaimCreate:
    return DelegationClaimCreate(
        tenant_id=f"tenant-{suffix}",
        parent_run_id=parent_run_id,
        source_agent_id="agent-source",
        target_agent_id="agent-target",
        idempotency_key=key,
        request_hash=request_hash,
        budget_intent="inherit_parent",
        child_input={"prompt": "safe"},
        identity={"user_id": "user-a"},
        trace_id=f"trace-{suffix}",
        parent_token_limit=parent_limit,
        requested_token_reservation=reserved_tokens,
        parent_cost_limit=parent_cost_limit,
        requested_cost_reservation=requested_cost_reservation,
    )


@pytest.mark.asyncio
async def test_postgresql_same_key_concurrency_reuses_one_claim_and_reservation() -> None:
    async with isolated_database("delegation_same_key") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="same")
            claim = _claim(
                parent_run_id,
                suffix="same",
                key="same-key",
                request_hash="a" * 64,
                reserved_tokens=60,
            )

            async def create_once() -> tuple[str, str, bool]:
                async with storage.uow() as uow:
                    result = await uow.delegations.claim_and_reserve(claim)
                    await uow.commit()
                    return result.delegation.id, result.reservation.id, result.created

            results = await asyncio.gather(*(create_once() for _ in range(6)))
            async with storage.uow() as uow:
                rows = await uow.delegations.list_for_parent(
                    tenant_id="tenant-same",
                    parent_run_id=parent_run_id,
                )
                capacity = await uow.event_capacity.snapshot(parent_run_id)
        finally:
            await storage.dispose()

    assert len({delegation_id for delegation_id, _, _ in results}) == 1
    assert len({reservation_id for _, reservation_id, _ in results}) == 1
    assert sum(created for _, _, created in results) == 1
    assert len(rows) == 1
    assert capacity.outstanding_reserved_event_count == 3


@pytest.mark.asyncio
async def test_postgresql_terminal_parent_rejects_claim_without_side_effects() -> None:
    """真实 PostgreSQL 的 parent row lock 后也必须在任何 claim 副作用前拒绝终态。"""

    async with isolated_database("delegation_terminal_parent") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="terminal")
            async with storage.uow() as uow:
                await uow.runs.set_status(parent_run_id, "completed", output={"ok": True})
                await uow.commit()
            async with storage.uow() as uow:
                with pytest.raises(DelegationStorageConflict) as captured:
                    await uow.delegations.claim_and_reserve(
                        _claim(
                            parent_run_id,
                            suffix="terminal",
                            key="late-key",
                            request_hash="a" * 64,
                            reserved_tokens=60,
                        )
                    )
            async with storage.uow() as uow:
                rows = await uow.delegations.list_for_parent(
                    tenant_id="tenant-terminal",
                    parent_run_id=parent_run_id,
                )
                pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
                capacity = await uow.event_capacity.snapshot(parent_run_id)
        finally:
            await storage.dispose()

    assert captured.value.code == "delegation.execution_failed"
    assert rows == []
    assert pending == []
    assert capacity.outstanding_reserved_event_count == 0


@pytest.mark.asyncio
async def test_postgresql_rejects_corrupted_child_relation_before_settlement() -> None:
    """真实 PostgreSQL 必须在锁内 relation 对账后才写聚合或结算预算。"""

    async with isolated_database("delegation_relation_settlement") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="relation")
            async with storage.uow() as uow:
                parent = await uow.runs.get(parent_run_id)
                assert parent is not None
                other_parent = await uow.runs.create(
                    RunCreate(
                        tenant_id="tenant-relation",
                        session_id=parent.session_id,
                        agent_id="agent-source",
                        trace_id="trace-other-relation",
                    )
                )
                other_parent_run_id = other_parent.id
                claimed = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="relation",
                        key="relation-key",
                        request_hash="r" * 64,
                        reserved_tokens=20,
                        parent_cost_limit=10.0,
                        requested_cost_reservation=10.0,
                    )
                )
                child = await uow.runs.create(
                    RunCreate(
                        tenant_id="tenant-relation",
                        session_id=parent.session_id,
                        agent_id="agent-target",
                        idempotency_key=f"delegation:{claimed.delegation.id}",
                        parent_run_id=parent_run_id,
                        trace_id="trace-relation",
                    )
                )
                await uow.runs.set_status(child.id, "completed", output={"ok": True})
                await uow.delegations.attach_child(
                    delegation_id=claimed.delegation.id,
                    child_run_id=child.id,
                )
                await uow.commit()
            async with storage.uow() as uow:
                await uow.session.execute(
                    text(
                        "UPDATE agent_runs SET parent_run_id = :other_parent "
                        "WHERE id = :child_run_id"
                    ),
                    {"other_parent": other_parent_run_id, "child_run_id": child.id},
                )
                await uow.commit()
            async with storage.uow() as uow:
                with pytest.raises(DelegationStorageConflict) as captured:
                    await uow.delegations.save_aggregation(
                        delegation_id=claimed.delegation.id,
                        summary={
                            "parent_run_id": parent_run_id,
                            "children": [
                                {
                                    "run_id": child.id,
                                    "agent_id": "agent-target",
                                    "status": "completed",
                                    "usage_evidence_refs": ["usage:relation"],
                                    "trace_refs": ["trace-relation"],
                                }
                            ],
                            "input_tokens": 3,
                            "output_tokens": 2,
                            "latency_ms": 5,
                            "cost_usd": 0.5,
                            "budget_status": "within_budget",
                            "trace_refs": ["trace-relation"],
                        },
                        evidence_refs=["usage:relation", "trace-relation"],
                        needs_review=False,
                    )
            async with storage.uow() as uow:
                aggregates = await uow.delegations.list_aggregates_for_parent(
                    tenant_id="tenant-relation",
                    parent_run_id=parent_run_id,
                )
                reservation = await uow.delegations.get_reservation(claimed.delegation.id)
        finally:
            await storage.dispose()

    assert captured.value.code == "delegation.execution_failed"
    assert aggregates == []
    assert reservation.state == "reserved"
    assert reservation.settled_input_tokens is None
    assert reservation.settled_output_tokens is None
    assert reservation.settled_cost_usd is None


@pytest.mark.asyncio
async def test_postgresql_claim_replay_rejects_durable_target_drift() -> None:
    """真实 PostgreSQL 重放必须把 claim 不可变语义绑定到首次 request hash。"""

    async with isolated_database("delegation_replay_integrity") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="replay-integrity")
            claim_data = _claim(
                parent_run_id,
                suffix="replay-integrity",
                key="replay-integrity-key",
                request_hash="i" * 64,
                reserved_tokens=20,
            )
            async with storage.uow() as uow:
                created = await uow.delegations.claim_and_reserve(claim_data)
                await uow.commit()
            async with storage.uow() as uow:
                await uow.session.execute(
                    text(
                        "UPDATE agent_delegations SET target_agent_id = :drifted_target "
                        "WHERE id = :delegation_id"
                    ),
                    {
                        "drifted_target": "agent-source",
                        "delegation_id": created.delegation.id,
                    },
                )
                await uow.commit()
            async with storage.uow() as uow:
                with pytest.raises(DelegationStorageConflict) as captured:
                    await uow.delegations.claim_and_reserve(claim_data)
            async with storage.uow() as uow:
                rows = await uow.delegations.list_for_parent(
                    tenant_id="tenant-replay-integrity",
                    parent_run_id=parent_run_id,
                )
                pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
        finally:
            await storage.dispose()

    assert captured.value.code == "delegation.execution_failed"
    assert len(rows) == 1
    assert rows[0].child_run_id is None
    assert len(pending) == 3


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
async def test_postgresql_unlimited_parent_preserves_finite_target_cost_ceiling() -> None:
    """PostgreSQL reservation 在 parent 无限时仍须保存有限 target ceiling。"""

    async with isolated_database("delegation_unlimited_parent_finite_target") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="unlimited-parent")
            async with storage.uow() as uow:
                claimed = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="unlimited-parent",
                        key="finite-target-key",
                        request_hash="f" * 64,
                        reserved_tokens=20,
                        parent_cost_limit=None,
                        requested_cost_reservation=1.0,
                    )
                )
                await uow.commit()
            async with storage.uow() as uow:
                reservation = await uow.delegations.get_reservation(claimed.delegation.id)
        finally:
            await storage.dispose()

    assert reservation.reserved_cost_usd == 1.0


@pytest.mark.asyncio
async def test_postgresql_finite_parent_cost_rejects_unbounded_target() -> None:
    """真实 parent lock 内不能把 target 的无限成本 ceiling 缩成剩余额度。"""

    async with isolated_database("delegation_unbounded_cost") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="unbounded-cost")
            async with storage.uow() as uow:
                with pytest.raises(DelegationBudgetExceeded) as captured:
                    await uow.delegations.claim_and_reserve(
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
            async with storage.uow() as uow:
                rows = await uow.delegations.list_for_parent(
                    tenant_id="tenant-unbounded-cost",
                    parent_run_id=parent_run_id,
                )
                pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
                capacity = await uow.event_capacity.snapshot(parent_run_id)
        finally:
            await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert rows == []
    assert pending == []
    assert capacity.outstanding_reserved_event_count == 0


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


@pytest.mark.asyncio
async def test_postgresql_different_keys_compete_and_original_replay_keeps_first_budget() -> None:
    async with isolated_database("delegation_parent_budget") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="budget")

            async def reserve(
                key: str,
                request_hash: str,
            ) -> DelegationClaimResult | Exception:
                try:
                    async with storage.uow() as uow:
                        result = await uow.delegations.claim_and_reserve(
                            _claim(
                                parent_run_id,
                                suffix="budget",
                                key=key,
                                request_hash=request_hash,
                                reserved_tokens=60,
                            )
                        )
                        await uow.commit()
                        return result
                except Exception as exc:
                    return exc

            competing = await asyncio.gather(
                reserve("key-a", "a" * 64),
                reserve("key-b", "b" * 64),
            )
            results = [result for result in competing if isinstance(result, DelegationClaimResult)]
            failures = [
                result for result in competing if isinstance(result, DelegationBudgetExceeded)
            ]
            assert len(results) == 1
            assert len(failures) == 1
            winner = results[0]
            async with storage.uow() as uow:
                replay = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="budget",
                        key=winner.delegation.idempotency_key,
                        request_hash=winner.delegation.request_hash,
                        reserved_tokens=60,
                    )
                )
                rows = await uow.delegations.list_for_parent(
                    tenant_id="tenant-budget",
                    parent_run_id=parent_run_id,
                )
                capacity = await uow.event_capacity.snapshot(parent_run_id)
        finally:
            await storage.dispose()

    assert replay.created is False
    assert replay.delegation.id == winner.delegation.id
    assert replay.reservation.id == winner.reservation.id
    assert replay.reservation.reserved_tokens == 60
    assert failures[0].code == "delegation.budget_exceeded"
    assert len(rows) == 1
    assert capacity.outstanding_reserved_event_count == 3


@pytest.mark.asyncio
async def test_postgresql_original_key_replays_after_other_key_changes_balance() -> None:
    async with isolated_database("delegation_stable_replay") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="replay")
            async with storage.uow() as uow:
                first = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="replay",
                        key="key-a",
                        request_hash="a" * 64,
                        reserved_tokens=30,
                    )
                )
                await uow.commit()
            async with storage.uow() as uow:
                await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="replay",
                        key="key-b",
                        request_hash="b" * 64,
                        reserved_tokens=60,
                    )
                )
                await uow.commit()
            async with storage.uow() as uow:
                replay = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="replay",
                        key="key-a",
                        request_hash="a" * 64,
                        # 当前只剩 10；replay 必须忽略新的派生值并复用首次 30。
                        reserved_tokens=100,
                    )
                )
                with pytest.raises(DelegationBudgetExceeded) as captured:
                    await uow.delegations.claim_and_reserve(
                        _claim(
                            parent_run_id,
                            suffix="replay",
                            key="key-c",
                            request_hash="c" * 64,
                            reserved_tokens=20,
                        )
                    )
        finally:
            await storage.dispose()

    assert replay.created is False
    assert replay.delegation.id == first.delegation.id
    assert replay.reservation.id == first.reservation.id
    assert replay.reservation.reserved_tokens == 30
    assert captured.value.code == "delegation.budget_exceeded"


@pytest.mark.asyncio
async def test_0015_postgresql_empty_database_downgrades_with_exact_opt_in() -> None:
    async with isolated_database("delegation_downgrade_empty") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        await asyncio.to_thread(
            command.downgrade,
            migration_config(dsn, x_args=["allow_empty_evidence_downgrade=true"]),
            "0014_run_evidence_outbox",
        )
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("select version_num from alembic_version"))
            ).scalar_one()
            delegation_table = (
                await connection.execute(text("select to_regclass('public.agent_delegations')"))
            ).scalar_one()
        await engine.dispose()

    assert revision == "0014_run_evidence_outbox"
    assert delegation_table is None


@pytest.mark.asyncio
async def test_0015_postgresql_claim_blocks_exact_opt_in_downgrade() -> None:
    async with isolated_database("delegation_downgrade_evidence") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="downgrade")
            async with storage.uow() as uow:
                await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="downgrade",
                        key="key-a",
                        request_hash="a" * 64,
                        reserved_tokens=30,
                    )
                )
                await uow.commit()
        finally:
            await storage.dispose()

        with pytest.raises(RuntimeError, match="evidence exists"):
            await asyncio.to_thread(
                command.downgrade,
                migration_config(dsn, x_args=["allow_empty_evidence_downgrade=true"]),
                "0014_run_evidence_outbox",
            )
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("select version_num from alembic_version"))
            ).scalar_one()
            claim_count = (
                await connection.execute(text("select count(*) from agent_delegations"))
            ).scalar_one()
        await engine.dispose()

    assert revision == "0015_agent_delegation"
    assert claim_count == 1


@pytest.mark.asyncio
async def test_0015_postgresql_run_relation_alone_blocks_exact_opt_in_downgrade() -> None:
    """真实 PostgreSQL 下，既有 run 父子关系必须独立阻止 0015 降级。"""

    async with isolated_database("delegation_downgrade_relation_only") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="relation-only")
            async with storage.uow() as uow:
                parent = await uow.runs.get(parent_run_id)
                assert parent is not None
                child = await uow.runs.create(
                    RunCreate(
                        tenant_id=parent.tenant_id,
                        session_id=parent.session_id,
                        agent_id="agent-target",
                        parent_run_id=parent.id,
                        trace_id=parent.trace_id,
                    )
                )
                await uow.commit()
        finally:
            await storage.dispose()

        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            delegation_counts: list[int] = []
            for table_name in (
                "agent_delegations",
                "delegation_budget_reservations",
                "delegation_aggregates",
            ):
                result = await connection.execute(text(f"select count(*) from {table_name}"))
                delegation_counts.append(result.scalar_one())
        await engine.dispose()
        assert delegation_counts == [0, 0, 0]

        with pytest.raises(RuntimeError, match="evidence exists"):
            await asyncio.to_thread(
                command.downgrade,
                migration_config(dsn, x_args=["allow_empty_evidence_downgrade=true"]),
                "0014_run_evidence_outbox",
            )
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("select version_num from alembic_version"))
            ).scalar_one()
            stored_parent_run_id = (
                await connection.execute(
                    text("select parent_run_id from agent_runs where id = :run_id"),
                    {"run_id": child.id},
                )
            ).scalar_one()
        await engine.dispose()

    assert revision == "0015_agent_delegation"
    assert stored_parent_run_id == parent_run_id
