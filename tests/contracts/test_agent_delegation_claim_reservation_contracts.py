"""Agent 委派 claim、预算预留与 ordered outbox 合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_storage_contracts import (
    DelegationBudgetExceeded as DelegationBudgetExceeded,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    DelegationBudgetReservationModel as DelegationBudgetReservationModel,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    Path as Path,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    _claim as _claim,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    _create_parent as _create_parent,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    pytest as pytest,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    sqlite3 as sqlite3,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    update as update,
)


def test_0015_migration_creates_delegation_evidence_tables(tmp_path: Path) -> None:
    path = tmp_path / "delegation-migration.db"
    run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        revision = connection.execute("select version_num from alembic_version").fetchone()

    assert {
        "agent_delegations",
        "delegation_budget_reservations",
        "delegation_aggregates",
    } <= tables
    assert revision == ("0015_agent_delegation",)


@pytest.mark.asyncio
async def test_same_key_replays_one_claim_budget_and_event_reservation(tmp_path: Path) -> None:
    path = tmp_path / "delegation-replay.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        parent_run_id = await _create_parent(storage)
        async with storage.idempotency_request_lock(f"delegation-parent:tenant-a:{parent_run_id}"):
            async with storage.uow() as uow:
                first = await uow.delegations.claim_and_reserve(_claim(parent_run_id))
                await uow.commit()
        async with storage.idempotency_request_lock(f"delegation-parent:tenant-a:{parent_run_id}"):
            async with storage.uow() as uow:
                replay = await uow.delegations.claim_and_reserve(_claim(parent_run_id))
                capacity = await uow.event_capacity.snapshot(parent_run_id)
                group = await uow.evidence_outbox.ordered_group(
                    group_id=f"delegation:{first.delegation.id}:evidence"
                )
                group_reservations = [item.reserved_event_count for item in group]
                group_sequences = [item.sequence_in_group for item in group]
                rows = await uow.delegations.list_for_parent(
                    tenant_id="tenant-a",
                    parent_run_id=parent_run_id,
                )
    finally:
        await storage.dispose()

    assert first.created is True
    assert replay.created is False
    assert replay.delegation.id == first.delegation.id
    assert replay.reservation.id == first.reservation.id
    assert replay.reservation.reserved_tokens == 60
    assert replay.reservation.reserved_cost_usd == 4.0
    assert capacity.outstanding_reserved_event_count == 3
    assert group_reservations == [1, 1, 1]
    assert group_sequences == [1, 2, 3]
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_ordered_delegation_outbox_rejects_skipped_predecessor(tmp_path: Path) -> None:
    """单 item 发布接口也必须强制 claimed、child、final 的 durable 前序。"""

    dsn = sqlite_dsn(tmp_path / "delegation-ordered-publish.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        parent_run_id = await _create_parent(storage)
        async with storage.uow() as uow:
            created = await uow.delegations.claim_and_reserve(_claim(parent_run_id))
            await uow.commit()
        claimed_id = f"delegation:{created.delegation.id}:claimed"
        child_id = f"delegation:{created.delegation.id}:child"
        final_id = f"delegation:{created.delegation.id}:final"
        async with storage.uow() as uow:
            with pytest.raises(LookupError, match="predecessor"):
                await uow.evidence_outbox.mark_event_published(event_id=child_id)
            await uow.evidence_outbox.mark_event_published(event_id=claimed_id)
            with pytest.raises(LookupError, match="predecessor"):
                await uow.evidence_outbox.mark_event_published(event_id=final_id)
            await uow.evidence_outbox.mark_event_published(event_id=child_id)
            await uow.evidence_outbox.mark_event_published(event_id=final_id)
            await uow.commit()
        async with storage.uow() as uow:
            group = await uow.evidence_outbox.ordered_group(
                group_id=f"delegation:{created.delegation.id}:evidence"
            )
            group_states = [item.state for item in group]
    finally:
        await storage.dispose()

    assert group_states == ["published", "published", "published"]


@pytest.mark.asyncio
async def test_new_claim_rejects_when_worst_case_exceeds_parent_remaining_budget(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delegation-effective-budget.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        parent_run_id = await _create_parent(storage)
        async with storage.uow() as uow:
            with pytest.raises(DelegationBudgetExceeded) as captured:
                await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        requested_token_reservation=150,
                        requested_cost_reservation=None,
                    )
                )
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert claims == []
    assert pending == []
    assert capacity.outstanding_reserved_event_count == 0


@pytest.mark.asyncio
async def test_finite_parent_cost_rejects_unbounded_target_without_side_effects(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delegation-unbounded-cost.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        parent_run_id = await _create_parent(storage)
        async with storage.uow() as uow:
            with pytest.raises(DelegationBudgetExceeded) as captured:
                await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        requested_token_reservation=10,
                        requested_cost_reservation=None,
                    )
                )
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert claims == []
    assert pending == []
    assert capacity.outstanding_reserved_event_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_state", ["reserved", "needs_review"])
async def test_finite_parent_fails_closed_on_active_unknown_cost_reservation(
    tmp_path: Path,
    legacy_state: str,
) -> None:
    """配置从无限收紧为有限时，旧 active null cost 不能被当成零余额影响。"""

    dsn = sqlite_dsn(tmp_path / f"delegation-cost-reload-{legacy_state}.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        parent_run_id = await _create_parent(storage)
        async with storage.uow() as uow:
            first = await uow.delegations.claim_and_reserve(
                _claim(
                    parent_run_id,
                    requested_token_reservation=10,
                    parent_cost_limit=None,
                    requested_cost_reservation=None,
                )
            )
            if legacy_state == "needs_review":
                await uow.session.execute(
                    update(DelegationBudgetReservationModel)
                    .where(DelegationBudgetReservationModel.delegation_id == first.delegation.id)
                    .values(state="needs_review")
                )
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(DelegationBudgetExceeded) as captured:
                await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        idempotency_key="delegation-key-second",
                        request_hash="b" * 64,
                        requested_token_reservation=10,
                        parent_cost_limit=1.0,
                        requested_cost_reservation=1.0,
                    )
                )
        async with storage.uow() as uow:
            rows = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert len(rows) == 1
