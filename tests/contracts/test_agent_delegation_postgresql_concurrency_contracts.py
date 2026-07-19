"""PostgreSQL Agent 委派并发与 durable relation 合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_postgresql_contracts import (
    DelegationStorageConflict as DelegationStorageConflict,
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
    asyncio as asyncio,
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
async def test_postgresql_same_key_concurrency_reuses_one_claim_and_reservation() -> None:
    """验证并发相同幂等键在真实 PostgreSQL 中只创建一份 claim 与预算预约。"""

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
                """在独立 UoW 内提交同一 claim，用于放大唯一约束竞争窗口。"""

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
