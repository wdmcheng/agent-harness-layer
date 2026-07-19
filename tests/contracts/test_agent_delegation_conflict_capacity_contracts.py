"""Agent 委派冲突、并发与容量回滚合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_storage_contracts import (
    MAX_EVENT_SEQ as MAX_EVENT_SEQ,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    DelegationBudgetExceeded as DelegationBudgetExceeded,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    DelegationClaimResult as DelegationClaimResult,
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
    asyncio as asyncio,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    pytest as pytest,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_agent_delegation_storage_contracts import (
    text as text,
)


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
                    _trusted_token_bound=60,
                    _trusted_cost_bound=4.0,
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
    """父运行已终态时必须在任何预算预约和证据 outbox 写入前拒绝新的委派请求。"""

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
    """相同幂等键携带不同语义载荷必须先报冲突，不能消耗额外预算或创建第二条关系。"""

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
    """不同幂等键仍共享父预算，第二个最坏情况预约必须在持久化前被拒绝。"""

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
    """SQLite 下同父运行的请求须序列化：相同键重放同一 claim，不同键竞争同一预算。"""

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
            """在父运行幂等锁内执行一次 claim，并把竞争异常返回给汇总断言而非吞掉。"""

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
    """证据容量不足必须使 claim、预算预约和 outbox 同时回滚，不能留下半完成委派。"""

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
