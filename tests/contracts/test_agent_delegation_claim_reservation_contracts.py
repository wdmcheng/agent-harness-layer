"""Agent 委派 claim、预算预留与 ordered outbox 合同测试。"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from tests.contracts.test_agent_delegation_storage_contracts import (
    MAX_EVENT_SEQ as MAX_EVENT_SEQ,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    DelegationBudgetExceeded as DelegationBudgetExceeded,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    DelegationBudgetReservationModel as DelegationBudgetReservationModel,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    DelegationStorageConflict as DelegationStorageConflict,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    EventCapacityExceeded as EventCapacityExceeded,
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

from agent_harness.storage.models import RunEventCapacityModel
from agent_harness.storage.shared_budget import BudgetReservationRejected
from agent_harness.storage.shared_budget_models import ParentBudgetLedgerModel
from agent_harness.storage.shared_budget_repositories import SharedBudgetRepository


def test_0015_migration_creates_delegation_evidence_tables(tmp_path: Path) -> None:
    """迁移必须创建委派关系、预约和聚合表，并将数据库置于当前 revision 供后续合同依赖。"""

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
    assert revision == ("0017_model_route_chain_state",)


@pytest.mark.asyncio
async def test_active_delegation_without_ledger_fails_closed(tmp_path: Path) -> None:
    """活动 root 丢失 0016 ledger 时不允许降级到 0015 独立预算。"""

    dsn = sqlite_dsn(tmp_path / "delegation-missing-ledger.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        parent_run_id = await _create_parent(storage, target_token_limit=100)
        async with storage.uow() as uow:
            await uow.session.execute(
                delete(ParentBudgetLedgerModel).where(
                    ParentBudgetLedgerModel.budget_owner_run_id == parent_run_id
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(DelegationStorageConflict) as rejected:
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
        async with storage.uow() as uow:
            count = await uow.session.scalar(
                select(func.count()).select_from(DelegationBudgetReservationModel)
            )
        assert rejected.value.code == "delegation.execution_failed"
        assert count == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_delegation_sequence_state_invalid_precedes_budget(tmp_path: Path) -> None:
    """Delegation 同时命中 sequence 损坏与超额时，固定返回 sequence state。"""

    dsn = sqlite_dsn(tmp_path / "delegation-sequence-priority.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        parent_run_id = await _create_parent(storage, target_token_limit=100)
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == parent_run_id)
                .values(terminal_reservation=0)
            )
            await uow.session.execute(
                update(ParentBudgetLedgerModel)
                .where(ParentBudgetLedgerModel.budget_owner_run_id == parent_run_id)
                .values(state="needs_review")
            )
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(DelegationStorageConflict) as rejected:
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
        assert rejected.value.code == "event.sequence_state_invalid"
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == parent_run_id)
                .values(terminal_reservation=1)
            )
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(DelegationBudgetExceeded):
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_concurrent_direct_winner_maps_late_shared_budget_rejection_to_delegation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锁后余额被 direct 占用时，delegation seam 仍返回自己的稳定预算错误。"""

    dsn = sqlite_dsn(tmp_path / "delegation-late-budget-race.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    parent_run_id = await _create_parent(storage, target_token_limit=100)

    async def reject_after_delegation_rows_are_staged(
        self: SharedBudgetRepository, **_: object
    ) -> object:
        del self
        raise BudgetReservationRejected(reason="balance_insufficient")

    monkeypatch.setattr(
        SharedBudgetRepository,
        "reserve_delegation",
        reject_after_delegation_rows_are_staged,
    )
    try:
        async with storage.uow() as uow:
            with pytest.raises(DelegationBudgetExceeded) as rejected:
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a", parent_run_id=parent_run_id
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
        assert rejected.value.code == "delegation.budget_exceeded"
        assert claims == []
        assert ledger is not None and ledger.token_impact == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preconsume_budget", "expected_error"),
    [
        (True, DelegationBudgetExceeded),
        (False, EventCapacityExceeded),
    ],
    ids=["budget-precedes-capacity", "capacity-only"],
)
async def test_delegation_budget_and_capacity_priority_matrix(
    tmp_path: Path,
    preconsume_budget: bool,
    expected_error: type[Exception],
) -> None:
    """Delegation 新 claim 的 budget 与 capacity 检查顺序在两种组合下固定。"""

    dsn = sqlite_dsn(tmp_path / f"delegation-priority-{preconsume_budget}.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        parent_run_id = await _create_parent(
            storage,
            target_token_limit=60,
        )
        if preconsume_budget:
            async with storage.uow() as uow:
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
                await uow.commit()
        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == parent_run_id)
                .values(highest_persisted_seq=MAX_EVENT_SEQ - (6 if preconsume_budget else 3))
            )
            await uow.commit()
        async with storage.uow() as uow:
            baseline_claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            baseline_ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
        async with storage.uow() as uow:
            with pytest.raises(expected_error):
                await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        idempotency_key=(
                            "delegation-key-second" if preconsume_budget else "delegation-key"
                        ),
                        request_hash="b" * 64 if preconsume_budget else "a" * 64,
                    )
                )
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
        assert [claim.id for claim in claims] == [claim.id for claim in baseline_claims]
        assert ledger is not None and baseline_ledger is not None
        assert ledger.token_impact == baseline_ledger.token_impact
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_same_key_replays_one_claim_budget_and_event_reservation(tmp_path: Path) -> None:
    """相同委派幂等键应重放同一 claim、预算预约及 ordered outbox，不得新增任何持久化份额。"""

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
    """第二个最坏情况委派超出根剩余预算时必须保持 claim、outbox 和容量快照完全不变。"""

    path = tmp_path / "delegation-effective-budget.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        parent_run_id = await _create_parent(storage, target_token_limit=60)
        async with storage.uow() as uow:
            first = await uow.delegations.claim_and_reserve(_claim(parent_run_id))
            await uow.commit()
        async with storage.uow() as uow:
            baseline_pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
            baseline_pending_ids = [item.id for item in baseline_pending]
            baseline_capacity = await uow.event_capacity.snapshot(parent_run_id)
        async with storage.uow() as uow:
            with pytest.raises(DelegationBudgetExceeded) as captured:
                await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        idempotency_key="delegation-key-second",
                        request_hash="b" * 64,
                        requested_token_reservation=60,
                        requested_cost_reservation=None,
                        _trusted_cost_bound=4.0,
                    )
                )
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
            pending_ids = [item.id for item in pending]
            capacity = await uow.event_capacity.snapshot(parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert [claim.id for claim in claims] == [first.delegation.id]
    assert pending_ids == baseline_pending_ids
    assert capacity == baseline_capacity


@pytest.mark.asyncio
async def test_finite_parent_cost_uses_owner_ceiling_for_null_target_ceiling(
    tmp_path: Path,
) -> None:
    """目标成本上限为空并不表示无限；有限 parent 成本上限仍是 delegation 预约的可信上界。"""

    path = tmp_path / "delegation-unbounded-cost.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        parent_run_id = await _create_parent(storage, target_cost_limit=None)
        async with storage.uow() as uow:
            created = await uow.delegations.claim_and_reserve(
                _claim(
                    parent_run_id,
                    requested_token_reservation=10,
                    requested_cost_reservation=None,
                    _trusted_token_bound=60,
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
            ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
    finally:
        await storage.dispose()

    assert created.reservation.reserved_cost_usd == 10.0
    assert [claim.id for claim in claims] == [created.delegation.id]
    assert len(pending) == 3
    assert capacity.outstanding_reserved_event_count == 3
    assert ledger is not None and float(ledger.cost_impact) == 10.0


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_state", ["reserved", "needs_review"])
async def test_frozen_cost_disabled_mode_ignores_current_config_enablement(
    tmp_path: Path,
    legacy_state: str,
) -> None:
    """Root 冻结 cost-disabled 后，调用方配置不能在同一 tree 中重新启用 cost。"""

    dsn = sqlite_dsn(tmp_path / f"delegation-cost-reload-{legacy_state}.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        parent_run_id = await _create_parent(
            storage,
            cost_limit=None,
            target_token_limit=10,
            target_cost_limit=None,
        )
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
            second = await uow.delegations.claim_and_reserve(
                _claim(
                    parent_run_id,
                    idempotency_key="delegation-key-second",
                    request_hash="b" * 64,
                    requested_token_reservation=10,
                    parent_cost_limit=1.0,
                    requested_cost_reservation=1.0,
                    _cost_enabled=False,
                    _trusted_cost_bound=None,
                )
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
            await uow.commit()
        async with storage.uow() as uow:
            rows = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
    finally:
        await storage.dispose()

    assert second.created is True
    assert ledger is not None and ledger.cost_limit is None
    assert ledger.cost_impact == 0
    assert len(rows) == 2
