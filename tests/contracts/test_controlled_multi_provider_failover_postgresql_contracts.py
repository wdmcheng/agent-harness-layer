"""受控多供应商回退 route-chain state 的真实 PostgreSQL allocation/CAS 合同。"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from alembic import command
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import create_async_engine
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    SimulatedProcessCrash,
    bound_failover_invocation,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.test_shared_parent_budget_repository_contracts import (
    create_delegation,
    create_root,
)
from tests.contracts.test_shared_parent_budget_route_chain_allocation_contracts import (
    assert_transfer_transition_matches_reservations,
)
from tests.contracts.test_shared_parent_budget_route_chain_repository_contracts import (
    CHAIN_USAGE_ID,
    allocation_proven_state,
    allocation_route_state,
    allocation_transferred_state,
    allocation_v2_identity,
    assert_nonterminal_mutation_preserves_reservation,
    create_route_chain_operation,
    second_started_route_state,
)
from tests.contracts.test_shared_parent_budget_route_chain_transition_contracts import (
    INTEGRITY_VIOLATIONS,
    IntegrityViolation,
    assert_initial_and_approved_state_integrity,
)

from agent_harness.models import ModelProviderInvocationError, ModelRequest
from agent_harness.storage import SQLAlchemyStorage, get_current_revision, run_migrations
from agent_harness.storage.migrations.runner import alembic_config
from agent_harness.storage.run_models import AgentRunModel
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    BudgetOperationConflict,
    DirectBudgetClaim,
    OperationIdentity,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="route-chain CAS 合同需要真实 PostgreSQL。",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
async def test_postgresql_transfer_transition_bounds_match_reservations(
    ownership_kind: Literal["direct", "allocation"],
) -> None:
    """真实PostgreSQL也必须逐值绑定transition与前后reservation。"""

    async with isolated_database(f"route_transfer_bounds_{ownership_kind}") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            run_id = await create_route_chain_operation(
                storage,
                ownership_kind=ownership_kind,
                suffix=f"rtb-{ownership_kind[0]}",
            )
            await assert_transfer_transition_matches_reservations(storage, run_id=run_id)
        finally:
            await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
@pytest.mark.parametrize("violation_kind", INTEGRITY_VIOLATIONS)
async def test_postgresql_initial_and_approved_state_integrity(
    ownership_kind: Literal["direct", "allocation"],
    violation_kind: IntegrityViolation,
) -> None:
    """真实PostgreSQL也拒绝创建、审批与恢复不变量分裂。"""

    async with isolated_database(f"route_{violation_kind}_{ownership_kind}") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            await assert_initial_and_approved_state_integrity(
                storage,
                ownership_kind=ownership_kind,
                violation_kind=violation_kind,
            )
        finally:
            await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
@pytest.mark.parametrize("mutation", ["attempt_started", "proof", "delta", "close_unknown"])
async def test_postgresql_nonterminal_route_mutations_cannot_lower_reservation(
    ownership_kind: Literal["direct", "allocation"],
    mutation: Literal["attempt_started", "proof", "delta", "close_unknown"],
) -> None:
    """真实PostgreSQL也必须在改账前冻结started/unknown的当前reservation。"""

    async with isolated_database(f"route_reservation_{ownership_kind}") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            run_id = await create_route_chain_operation(
                storage,
                ownership_kind=ownership_kind,
                suffix=f"rr-{ownership_kind[0]}-{mutation}",
            )
            await assert_nonterminal_mutation_preserves_reservation(
                storage,
                run_id=run_id,
                mutation=mutation,
            )
        finally:
            await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership_kind", ["direct", "allocation"])
async def test_postgresql_next_attempt_requires_previous_lifecycle_to_be_proven(
    ownership_kind: Literal["direct", "allocation"],
) -> None:
    """真实PostgreSQL的owner锁同样拒绝在悬空started后追加下一attempt。"""

    async with isolated_database(f"route_lifecycle_order_{ownership_kind}") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            run_id = await create_route_chain_operation(
                storage,
                ownership_kind=ownership_kind,
                suffix=f"pgo-{ownership_kind[0]}",
            )
            async with storage.uow() as uow:
                await uow.shared_budget.append_model_route_attempt_started(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=allocation_route_state(started=True),
                )
                await uow.commit()
            async with storage.uow() as uow:
                with pytest.raises(BudgetOperationConflict):
                    await uow.shared_budget.append_model_route_attempt_started(
                        tenant_id="tenant-a",
                        run_id=run_id,
                        usage_call_id=CHAIN_USAGE_ID,
                        state=second_started_route_state(),
                    )
        finally:
            await storage.dispose()


def _direct_v2_identity(root_id: str) -> OperationIdentity:
    """构造与 PostgreSQL root ledger 对齐的 direct route-chain 身份。"""

    return OperationIdentity.from_semantic_request(
        tenant_id="tenant-a",
        fingerprint_key=b"test-only-budget-fingerprint-key",
        fingerprint_key_version="test-v1",
        ownership_kind="direct",
        run_id=root_id,
        agent_id="agent-a",
        delegation_claim_id=None,
        usage_kind="model",
        operation_slot=CHAIN_USAGE_ID,
        semantic_request={"prompt_ref": "route-chain-direct"},
        tree_snapshot_id=f"snapshot:{root_id}",
        agent_sub_snapshot_id=f"snapshot:{root_id}:agent-a",
        provider="fake",
        model="fake-basic",
        price_source_ref="price:fake",
        price_source_version="v1",
        cache_key_digest=None,
        cost_enabled=True,
        trusted_token_bound=20,
        trusted_cost_bound=Decimal("1.00"),
        route_chain_digest="a" * 64,
        route_candidate_count=2,
    )


@pytest.mark.asyncio
async def test_postgresql_0017_empty_upgrade_and_null_only_downgrade() -> None:
    """真实 PostgreSQL 空库可升级到0017并无损降回0016。"""

    async with isolated_database("controlled_multi_provider_route_chain_migration") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        assert await asyncio.to_thread(get_current_revision, dsn) == (
            "0017_model_route_chain_state"
        )
        await asyncio.to_thread(
            command.downgrade,
            alembic_config(dsn),
            "0016_shared_parent_budget_ledger",
        )
        assert await asyncio.to_thread(get_current_revision, dsn) == (
            "0016_shared_parent_budget_ledger"
        )
        engine = create_async_engine(dsn)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "select table_name, column_name from information_schema.columns "
                        "where table_name in "
                        "('budget_operation_claims','delegation_budget_allocations') "
                        "and column_name = 'route_chain_state_json'"
                    )
                )
                assert rows.all() == []
        finally:
            await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_direct_route_chain_concurrent_replay_and_transfer() -> None:
    """Direct 路径在真实 PostgreSQL 串行化同一 started，并对称完成proof/transfer。"""

    async with isolated_database("controlled_multi_provider_route_chain_direct") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            root_id = await create_root(storage, suffix="pg-route-chain-direct")
            initial = allocation_route_state()
            async with storage.uow() as uow:
                await uow.shared_budget.claim_direct(
                    DirectBudgetClaim(
                        tenant_id="tenant-a",
                        budget_owner_run_id=root_id,
                        usage_call_id=CHAIN_USAGE_ID,
                        identity=_direct_v2_identity(root_id),
                        token_reservation=20,
                        cost_reservation=Decimal("1.00"),
                        route_chain_state=initial,
                    )
                )
                await uow.commit()

            async def append_started() -> bool:
                async with storage.uow() as uow:
                    result = await uow.shared_budget.append_model_route_attempt_started(
                        tenant_id="tenant-a",
                        run_id=root_id,
                        usage_call_id=CHAIN_USAGE_ID,
                        state=allocation_route_state(started=True),
                    )
                    await uow.commit()
                    return result.replayed

            assert sorted(await asyncio.gather(append_started(), append_started())) == [False, True]

            async with storage.uow() as uow:
                await uow.shared_budget.append_model_route_not_started_proof(
                    tenant_id="tenant-a",
                    run_id=root_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=allocation_proven_state(),
                )
                await uow.commit()
            async with storage.uow() as uow:
                transferred = await uow.shared_budget.transfer_model_route_reservation(
                    tenant_id="tenant-a",
                    run_id=root_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=allocation_transferred_state(),
                )
                await uow.commit()
            assert transferred.token_impact == 10
            assert transferred.cost_impact == Decimal("0.50")
        finally:
            await storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_stream_route_chain_transfers_before_first_delta(
    tmp_path: Path,
) -> None:
    """真实PostgreSQL上的stream proof/transfer仍只让第二候选产出。"""

    async with isolated_database("controlled_multi_provider_route_chain_stream") as dsn:
        fixture = await bound_failover_invocation(
            tmp_path,
            storage_dsn=dsn,
            route_count=2,
            scripts={
                ROUTE_A["deployment_id"]: ["client_not_started"],
                ROUTE_B["deployment_id"]: ["completed"],
            },
        )
        try:
            response = await fixture.bound.stream(
                ModelRequest(capability="text_stream", prompt="postgres", max_output_tokens=8),
                operation_key=fixture.operation_key,
            )
            assert response.model == ROUTE_B["model_id"]
            async with fixture.storage.uow() as uow:
                state = await uow.shared_budget.get_model_route_chain_state(
                    tenant_id="tenant-a",
                    usage_call_id=fixture.usage_call_id,
                )
            assert state is not None
            assert state.selected_ordinal == 2
            assert [item.lifecycle_state for item in state.attempt_lifecycle] == [
                "not_started_proven",
                "settled",
            ]
        finally:
            await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_stream_cancelled_actual_settlement_replays_without_provider(
    tmp_path: Path,
) -> None:
    """真实PostgreSQL同一UoW保存取消actual、清空reservation并按stable key零调用重放。"""

    async with isolated_database("controlled_multi_provider_route_chain_cancelled") as dsn:
        fixture = await bound_failover_invocation(
            tmp_path,
            storage_dsn=dsn,
            route_count=2,
            scripts={
                ROUTE_A["deployment_id"]: ["cancelled_on_iterate_stopped_complete"],
                ROUTE_B["deployment_id"]: ["completed"],
            },
        )
        request = ModelRequest(capability="text_stream", prompt="postgres", max_output_tokens=8)
        try:
            with pytest.raises(ModelProviderInvocationError) as cancelled:
                await fixture.bound.stream(request, operation_key=fixture.operation_key)
            assert cancelled.value.code == "model.invocation_cancelled"
            trace = list(fixture.provider.trace)
            async with fixture.storage.uow() as uow:
                state = await uow.shared_budget.get_model_route_chain_state(
                    tenant_id="tenant-a",
                    usage_call_id=fixture.usage_call_id,
                )
            assert state is not None
            assert state.candidates[0].state == "cancelled"
            assert state.selected_ordinal is None
            assert state.active_ordinal is None
            assert state.current_reservation.token_bound == 0

            with pytest.raises(ModelProviderInvocationError) as replayed:
                await fixture.bound.stream(request, operation_key=fixture.operation_key)
            assert replayed.value.code == "model.invocation_cancelled"
            assert fixture.provider.trace == trace
        finally:
            await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_outcome", ["crash_before_send", "crash_after_send"])
async def test_postgresql_stream_started_crash_never_replays_or_transfers(
    tmp_path: Path,
    crash_outcome: str,
) -> None:
    """真实PostgreSQL在两个started崩溃窗口都保留原reservation并围栏后继。"""

    async with isolated_database(f"controlled_multi_provider_stream_{crash_outcome}") as dsn:
        fixture = await bound_failover_invocation(
            tmp_path,
            storage_dsn=dsn,
            route_count=2,
            scripts={
                ROUTE_A["deployment_id"]: [crash_outcome],
                ROUTE_B["deployment_id"]: ["completed"],
            },
        )
        request = ModelRequest(capability="text_stream", prompt="postgres", max_output_tokens=8)
        try:
            with pytest.raises(SimulatedProcessCrash):
                await fixture.bound.stream(request, operation_key=fixture.operation_key)
            trace_after_crash = list(fixture.provider.trace)
            with pytest.raises(ModelProviderInvocationError):
                await fixture.bound.stream(request, operation_key=fixture.operation_key)
            assert fixture.provider.trace == trace_after_crash
            assert not any("real_secondary" in item for item in fixture.provider.trace)
            async with fixture.storage.uow() as uow:
                state = await uow.shared_budget.get_model_route_chain_state(
                    tenant_id="tenant-a",
                    usage_call_id=fixture.usage_call_id,
                )
            assert state is not None
            assert len(state.attempt_lifecycle) == 1
            assert state.attempt_lifecycle[0].lifecycle_state in {"started", "unknown"}
        finally:
            await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_postgresql_allocation_route_chain_started_replay_and_proof_fence() -> None:
    """PostgreSQL 保存同一 v2 state，并拒绝无 proof 的 allocation transfer。"""

    async with isolated_database("controlled_multi_provider_route_chain") as dsn:
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            root_id = await create_root(storage, suffix="pg-route-chain")
            delegation_id, child_id = await create_delegation(
                storage,
                root_id=root_id,
                suffix="pg-route-chain",
            )
            async with storage.uow() as uow:
                await uow.session.execute(
                    update(AgentRunModel)
                    .where(AgentRunModel.id == child_id)
                    .values(idempotency_key=f"delegation:{delegation_id}")
                )
                await uow.commit()

            initial = allocation_route_state()
            claim = AllocationBudgetClaim(
                tenant_id="tenant-a",
                budget_owner_run_id=root_id,
                delegation_id=delegation_id,
                usage_call_id=CHAIN_USAGE_ID,
                identity=allocation_v2_identity(
                    root_id=root_id,
                    child_id=child_id,
                    delegation_id=delegation_id,
                ),
                token_reservation=20,
                cost_reservation=Decimal("1.00"),
                route_chain_state=initial,
            )
            async with storage.uow() as uow:
                await uow.shared_budget.allocate(claim)
                await uow.commit()

            started = allocation_route_state(started=True)
            async with storage.uow() as uow:
                appended = await uow.shared_budget.append_model_route_attempt_started(
                    tenant_id="tenant-a",
                    run_id=child_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=started,
                )
                await uow.commit()
            assert appended.route_chain_state == started

            async with storage.uow() as uow:
                replay = await uow.shared_budget.append_model_route_attempt_started(
                    tenant_id="tenant-a",
                    run_id=child_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=started,
                )
            assert replay.replayed is True

            candidates = list(started.candidates)
            candidates[0] = candidates[0].model_copy(update={"state": "not_started"})
            candidates[1] = candidates[1].model_copy(update={"state": "active"})
            reservation = started.current_reservation.model_copy(
                update={"candidate_ordinal": 2, "token_bound": 10, "cost_bound": 0.5}
            )
            transition = started.transitions[0].model_copy(
                update={
                    "sequence": 2,
                    "from_ordinal": 1,
                    "to_ordinal": 2,
                    "state": "transferred",
                    "reason": "client_not_started",
                    "released_token_bound": 20,
                    "released_cost_bound": 1.0,
                    "reserved_token_bound": 10,
                    "reserved_cost_bound": 0.5,
                }
            )
            # 绕过 DTO 重校验，保留 PostgreSQL 仓储边界的独立纵深防御合同。
            invalid = started.model_copy(
                update={
                    "active_ordinal": 2,
                    "evidence_route_ordinal": 2,
                    "attempt_lifecycle": (),
                    "candidates": tuple(candidates),
                    "current_reservation": reservation,
                    "transitions": (*started.transitions, transition),
                }
            )
            async with storage.uow() as uow:
                with pytest.raises(BudgetOperationConflict):
                    await uow.shared_budget.transfer_model_route_reservation(
                        tenant_id="tenant-a",
                        run_id=child_id,
                        usage_call_id=CHAIN_USAGE_ID,
                        state=invalid,
                    )

            async with storage.uow() as uow:
                await uow.shared_budget.append_model_route_not_started_proof(
                    tenant_id="tenant-a",
                    run_id=child_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=allocation_proven_state(),
                )
                await uow.commit()
            transferred = allocation_transferred_state()
            async with storage.uow() as uow:
                result = await uow.shared_budget.transfer_model_route_reservation(
                    tenant_id="tenant-a",
                    run_id=child_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=transferred,
                )
                await uow.commit()
            assert result.route_chain_state == transferred
            assert result.token_impact == 10

            async with storage.uow() as uow:
                replayed = await uow.shared_budget.transfer_model_route_reservation(
                    tenant_id="tenant-a",
                    run_id=child_id,
                    usage_call_id=CHAIN_USAGE_ID,
                    state=transferred,
                )
                await uow.commit()
            assert replayed.replayed is True

            with pytest.raises(RuntimeError, match=r"^storage\.route_chain_state_present$"):
                await asyncio.to_thread(
                    command.downgrade,
                    alembic_config(dsn),
                    "0016_shared_parent_budget_ledger",
                )
            assert await asyncio.to_thread(get_current_revision, dsn) == (
                "0017_model_route_chain_state"
            )
        finally:
            await storage.dispose()
