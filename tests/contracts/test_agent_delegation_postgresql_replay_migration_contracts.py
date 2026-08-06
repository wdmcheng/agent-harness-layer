"""PostgreSQL Agent 委派预算重放与迁移降级合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_delegation_postgresql_contracts import (
    DelegationBudgetExceeded as DelegationBudgetExceeded,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    DelegationClaimResult as DelegationClaimResult,
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
    command as command,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    create_async_engine as create_async_engine,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    isolated_database as isolated_database,
)
from tests.contracts.test_agent_delegation_postgresql_contracts import (
    migration_config as migration_config,
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
async def test_postgresql_different_keys_compete_and_original_replay_keeps_first_budget() -> None:
    """不同 key 在 PostgreSQL 竞争父预算时只允许一个胜者，原 key 重放必须保留首次预约。"""

    async with isolated_database("delegation_parent_budget") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(storage, suffix="budget")

            async def reserve(
                key: str,
                request_hash: str,
            ) -> DelegationClaimResult | Exception:
                """在独立事务中执行一次委派预约并返回竞争异常，供并发结果精确分类。"""

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
    """其他 key 改变剩余余额后，原 key 重放仍必须复用历史预约而不是重新按当前余额计算。"""

    async with isolated_database("delegation_stable_replay") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        try:
            parent_run_id = await _parent(
                storage,
                suffix="replay",
                target_token_limit=30,
                cost_limit=None,
            )
            async with storage.uow() as uow:
                first = await uow.delegations.claim_and_reserve(
                    _claim(
                        parent_run_id,
                        suffix="replay",
                        key="key-a",
                        request_hash="a" * 64,
                        reserved_tokens=30,
                        parent_cost_limit=None,
                        requested_cost_reservation=None,
                        trusted_token_bound=30,
                    )
                )
                await uow.commit()
            async with storage.uow() as uow:
                for key, request_hash in (("key-b", "b" * 64), ("key-c", "c" * 64)):
                    await uow.delegations.claim_and_reserve(
                        _claim(
                            parent_run_id,
                            suffix="replay",
                            key=key,
                            request_hash=request_hash,
                            reserved_tokens=30,
                            parent_cost_limit=None,
                            requested_cost_reservation=None,
                            trusted_token_bound=30,
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
                        # 当前只剩 10；replay 必须忽略旧 DTO 的派生值并复用首次 30。
                        reserved_tokens=100,
                        parent_cost_limit=None,
                        requested_cost_reservation=None,
                        trusted_token_bound=30,
                    )
                )
                with pytest.raises(DelegationBudgetExceeded) as captured:
                    await uow.delegations.claim_and_reserve(
                        _claim(
                            parent_run_id,
                            suffix="replay",
                            key="key-d",
                            request_hash="d" * 64,
                            reserved_tokens=20,
                            parent_cost_limit=None,
                            requested_cost_reservation=None,
                            trusted_token_bound=30,
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
    """无委派证据的 PostgreSQL 数据库在精确确认后可安全降级，验证迁移可逆边界。"""

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
async def test_0016_postgresql_claim_blocks_exact_opt_in_downgrade() -> None:
    """存在 0016 委派 claim 时即使传入确认开关也禁止降级，避免删除耐久预算证据。"""

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

    assert revision == "0018_model_tool_loop_state"
    assert claim_count == 1


@pytest.mark.asyncio
async def test_0016_postgresql_run_relation_alone_blocks_exact_opt_in_downgrade() -> None:
    """真实 PostgreSQL 下，既有 run 父子关系必须独立阻止 0016 降级。"""

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

    assert revision == "0018_model_tool_loop_state"
    assert stored_parent_run_id == parent_run_id
