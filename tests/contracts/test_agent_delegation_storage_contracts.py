"""0015 delegation claim、预算预约与 migration 合同。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import text, update
from tests.contracts.run_trace_migration_test_helpers import migration_config

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_models import DelegationBudgetReservationModel
from agent_harness.storage.delegation_repositories import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationClaimResult,
    DelegationStorageConflict,
)
from agent_harness.storage.event_capacity_repositories import MAX_EVENT_SEQ, EventCapacityExceeded
from agent_harness.storage.repositories import RunCreate, SessionCreate


def sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _create_parent(storage: SQLAlchemyStorage, *, suffix: str = "") -> str:
    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        session = await uow.sessions.ensure(
            SessionCreate(
                session_id=f"session-a{suffix}",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-source",
            )
        )
        parent = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=session.id,
                agent_id="agent-source",
                trace_id=f"trace-parent{suffix}",
            )
        )
        await uow.commit()
        return parent.id


async def _create_child_relation(
    storage: SQLAlchemyStorage,
    *,
    parent_run_id: str,
    suffix: str = "",
) -> str:
    """只写既有 run 父子关系，不借 delegation claim 制造降级证据。"""

    async with storage.uow() as uow:
        child = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=f"session-a{suffix}",
                agent_id="agent-target",
                parent_run_id=parent_run_id,
                trace_id=f"trace-parent{suffix}",
            )
        )
        await uow.commit()
        return child.id


def _claim(parent_run_id: str, **updates: object) -> DelegationClaimCreate:
    payload: dict[str, object] = {
        "tenant_id": "tenant-a",
        "parent_run_id": parent_run_id,
        "source_agent_id": "agent-source",
        "target_agent_id": "agent-target",
        "idempotency_key": "delegation-key",
        "request_hash": "a" * 64,
        "budget_intent": "inherit_parent",
        "child_input": {"query": "safe"},
        "identity": {"user_id": "user-a", "session_id": "session-a"},
        "trace_id": "trace-parent",
        "request_id": "request-a",
        "parent_token_limit": 100,
        "requested_token_reservation": 60,
        "parent_cost_limit": 10.0,
        "requested_cost_reservation": 4.0,
    }
    payload.update(updates)
    return DelegationClaimCreate.model_validate(payload)


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settled_input_tokens", "settled_output_tokens", "settled_cost_usd"),
    [
        (None, 0, 0.0),
        (-1, 0, 0.0),
        (0, 0, -1.0),
        (0, 0, float("inf")),
    ],
    ids=("missing-token", "negative-token", "negative-cost", "non-finite-cost"),
)
async def test_new_claim_fails_closed_on_corrupt_settled_reservation(
    tmp_path: Path,
    settled_input_tokens: int | None,
    settled_output_tokens: int,
    settled_cost_usd: float,
) -> None:
    """即使历史库绕过 CHECK 留下坏结算，新 claim 也不能恢复可消费余额。"""

    dsn = sqlite_dsn(tmp_path / "delegation-corrupt-settled.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        parent_run_id = await _create_parent(storage)
        async with storage.uow() as uow:
            first = await uow.delegations.claim_and_reserve(
                _claim(
                    parent_run_id,
                    requested_token_reservation=10,
                    requested_cost_reservation=1.0,
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            # 模拟旧版本、人工修复或损坏备份绕过新 migration CHECK 的持久化状态；
            # 关闭后再提交，确保后续 claim 仍由 repository 自身执行 fail-closed 校验。
            await uow.session.execute(text("PRAGMA ignore_check_constraints = ON"))
            await uow.session.execute(
                text(
                    "UPDATE delegation_budget_reservations "
                    "SET state = 'settled', settled_input_tokens = :input_tokens, "
                    "settled_output_tokens = :output_tokens, settled_cost_usd = :cost_usd "
                    "WHERE delegation_id = :delegation_id"
                ),
                {
                    "input_tokens": settled_input_tokens,
                    "output_tokens": settled_output_tokens,
                    "cost_usd": settled_cost_usd,
                    "delegation_id": first.delegation.id,
                },
            )
            await uow.session.execute(text("PRAGMA ignore_check_constraints = OFF"))
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(DelegationBudgetExceeded) as captured:
                await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        idempotency_key="delegation-key-second",
                        request_hash="b" * 64,
                        requested_token_reservation=10,
                        requested_cost_reservation=1.0,
                    )
                )
        async with storage.uow() as uow:
            rows = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
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
async def test_terminal_parent_rejects_new_claim_without_reservation_or_outbox(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delegation-terminal-parent.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        parent_run_id = await _create_parent(storage)
        async with storage.uow() as uow:
            await uow.runs.set_status(parent_run_id, "completed", output={"ok": True})
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(DelegationStorageConflict) as captured:
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
            capacity = await uow.event_capacity.snapshot(parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.execution_failed"
    assert claims == []
    assert pending == []
    assert capacity.outstanding_reserved_event_count == 0


@pytest.mark.asyncio
async def test_same_key_different_hash_conflicts_before_new_reservation(tmp_path: Path) -> None:
    path = tmp_path / "delegation-conflict.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        parent_run_id = await _create_parent(storage)
        async with storage.idempotency_request_lock(f"delegation-parent:tenant-a:{parent_run_id}"):
            async with storage.uow() as uow:
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
                await uow.commit()
        async with storage.idempotency_request_lock(f"delegation-parent:tenant-a:{parent_run_id}"):
            async with storage.uow() as uow:
                with pytest.raises(DelegationStorageConflict) as captured:
                    await uow.delegations.claim_and_reserve(
                        _claim(parent_run_id, request_hash="b" * 64)
                    )
        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(parent_run_id)
            rows = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.idempotency_conflict"
    assert capacity.outstanding_reserved_event_count == 3
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_different_keys_reject_second_worst_case_budget(tmp_path: Path) -> None:
    path = tmp_path / "delegation-budget.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        parent_run_id = await _create_parent(storage)
        scope = f"delegation-parent:tenant-a:{parent_run_id}"
        async with storage.idempotency_request_lock(scope):
            async with storage.uow() as uow:
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
                await uow.commit()
        async with storage.idempotency_request_lock(scope):
            async with storage.uow() as uow:
                with pytest.raises(DelegationBudgetExceeded) as captured:
                    await uow.delegations.claim_and_reserve(
                        _claim(
                            parent_run_id,
                            idempotency_key="delegation-key-b",
                            request_hash="b" * 64,
                        )
                    )
        async with storage.uow() as uow:
            rows = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            capacity = await uow.event_capacity.snapshot(parent_run_id)
    finally:
        await storage.dispose()

    assert captured.value.code == "delegation.budget_exceeded"
    assert len(rows) == 1
    assert capacity.outstanding_reserved_event_count == 3


@pytest.mark.asyncio
async def test_sqlite_concurrent_same_and_different_keys_are_parent_serialized(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delegation-concurrent-budget.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        same_parent_run_id = await _create_parent(storage)
        different_parent_run_id = await _create_parent(storage, suffix="-different")

        async def reserve(
            parent_run_id: str,
            key: str,
            request_hash: str,
        ) -> DelegationClaimResult | Exception:
            try:
                scope = f"delegation-parent:tenant-a:{parent_run_id}"
                async with storage.idempotency_request_lock(scope):
                    async with storage.uow() as uow:
                        result = await uow.delegations.claim_and_reserve(
                            _claim(
                                parent_run_id,
                                idempotency_key=key,
                                request_hash=request_hash,
                                trace_id=(
                                    "trace-parent-different"
                                    if parent_run_id == different_parent_run_id
                                    else "trace-parent"
                                ),
                            )
                        )
                        await uow.commit()
                        return result
            except Exception as exc:
                return exc

        same = await asyncio.gather(
            reserve(same_parent_run_id, "same-key", "c" * 64),
            reserve(same_parent_run_id, "same-key", "c" * 64),
        )
        different = await asyncio.gather(
            reserve(different_parent_run_id, "different-a", "d" * 64),
            reserve(different_parent_run_id, "different-b", "e" * 64),
        )
        async with storage.uow() as uow:
            same_capacity = await uow.event_capacity.snapshot(same_parent_run_id)
            different_capacity = await uow.event_capacity.snapshot(different_parent_run_id)
    finally:
        await storage.dispose()

    same_results = [item for item in same if isinstance(item, DelegationClaimResult)]
    different_results = [item for item in different if isinstance(item, DelegationClaimResult)]
    different_failures = [item for item in different if isinstance(item, DelegationBudgetExceeded)]
    assert len(same_results) == 2
    assert [item.created for item in same_results].count(True) == 1
    assert len({item.delegation.id for item in same_results}) == 1
    assert len(different_results) == 1
    assert different_results[0].reservation.reserved_tokens == 60
    assert len(different_failures) == 1
    assert different_failures[0].code == "delegation.budget_exceeded"
    assert same_capacity.outstanding_reserved_event_count == 3
    assert different_capacity.outstanding_reserved_event_count == 3


@pytest.mark.asyncio
async def test_capacity_exhaustion_rolls_back_claim_budget_and_outbox(tmp_path: Path) -> None:
    path = tmp_path / "delegation-capacity.db"
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
    try:
        parent_run_id = await _create_parent(storage)
        async with storage.uow() as uow:
            await uow.event_capacity.reconcile_local_prefix(
                run_id=parent_run_id,
                highest_persisted_seq=MAX_EVENT_SEQ - 3,
            )
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(EventCapacityExceeded):
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
        async with storage.uow() as uow:
            claims = await uow.delegations.list_for_parent(
                tenant_id="tenant-a",
                parent_run_id=parent_run_id,
            )
            capacity = await uow.event_capacity.snapshot(parent_run_id)
            pending = await uow.evidence_outbox.pending(run_id=parent_run_id)
    finally:
        await storage.dispose()

    assert claims == []
    assert pending == []
    assert capacity.highest_persisted_seq == MAX_EVENT_SEQ - 3
    assert capacity.outstanding_reserved_event_count == 0


@pytest.mark.parametrize(
    "x_args",
    [
        [],
        ["allow_empty_evidence_downgrade=false"],
        ["allow_empty_evidence_downgrade=True"],
        ["allow_empty_evidence_downgrade=true", "allow_empty_evidence_downgrade=true"],
        ["allow_empty_evidence_downgrade=true", "unrelated_flag=1"],
    ],
)
def test_0015_downgrade_requires_exact_opt_in(tmp_path: Path, x_args: list[str]) -> None:
    path = tmp_path / f"delegation-downgrade-{len(x_args)}-{hash(tuple(x_args))}.db"
    run_migrations(sqlite_dsn(path))

    with pytest.raises(RuntimeError, match="explicit opt-in"):
        command.downgrade(
            migration_config(sqlite_dsn(path), x_args=x_args),
            "0014_run_evidence_outbox",
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0015_agent_delegation",
        )


def test_0015_empty_database_downgrades_with_exact_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "delegation-empty-downgrade.db"
    run_migrations(sqlite_dsn(path))

    command.downgrade(
        migration_config(
            sqlite_dsn(path),
            x_args=["allow_empty_evidence_downgrade=true"],
        ),
        "0014_run_evidence_outbox",
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0014_run_evidence_outbox",
        )


def test_0015_any_claim_blocks_exact_opt_in_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "delegation-non-empty-downgrade.db"
    run_migrations(sqlite_dsn(path))

    async def seed() -> None:
        storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
        try:
            parent_run_id = await _create_parent(storage)
            async with storage.uow() as uow:
                await uow.delegations.claim_and_reserve(_claim(parent_run_id))
                await uow.commit()
        finally:
            await storage.dispose()

    asyncio.run(seed())

    with pytest.raises(RuntimeError, match="evidence exists"):
        command.downgrade(
            migration_config(
                sqlite_dsn(path),
                x_args=["allow_empty_evidence_downgrade=true"],
            ),
            "0014_run_evidence_outbox",
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from agent_delegations").fetchone() == (1,)
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0015_agent_delegation",
        )


def test_0015_run_relation_alone_blocks_exact_opt_in_downgrade(tmp_path: Path) -> None:
    """独立 run 关系也是 0015 证据，不能因三个新表为空而被删除能力。"""

    path = tmp_path / "delegation-relation-only-downgrade.db"
    run_migrations(sqlite_dsn(path))

    async def seed() -> tuple[str, str]:
        storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))
        try:
            parent_run_id = await _create_parent(storage)
            child_run_id = await _create_child_relation(storage, parent_run_id=parent_run_id)
            return parent_run_id, child_run_id
        finally:
            await storage.dispose()

    parent_run_id, child_run_id = asyncio.run(seed())
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from agent_delegations").fetchone() == (0,)
        assert connection.execute(
            "select count(*) from delegation_budget_reservations"
        ).fetchone() == (0,)
        assert connection.execute("select count(*) from delegation_aggregates").fetchone() == (0,)

    with pytest.raises(RuntimeError, match="evidence exists"):
        command.downgrade(
            migration_config(
                sqlite_dsn(path),
                x_args=["allow_empty_evidence_downgrade=true"],
            ),
            "0014_run_evidence_outbox",
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "select parent_run_id from agent_runs where id = ?", (child_run_id,)
        ).fetchone() == (parent_run_id,)
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0015_agent_delegation",
        )
