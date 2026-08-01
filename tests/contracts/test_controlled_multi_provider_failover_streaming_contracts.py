"""Bound streaming 的首 delta 前转移与全局 attempt 围栏合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import update
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    ROUTE_C,
    SimulatedProcessCrash,
    bound_failover_invocation,
)

from agent_harness.events import CanonicalEventType
from agent_harness.models import ModelProviderInvocationError, ModelRequest
from agent_harness.storage.shared_budget_models import ParentBudgetLedgerModel


@pytest.mark.asyncio
async def test_bound_stream_transfers_before_first_delta_and_uses_selected_global_attempt(
    tmp_path: Path,
) -> None:
    """首候选 client-not-started 后，B 的 delta/completed 必须统一使用全局 attempt 2。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        response = await fixture.bound.stream(
            ModelRequest(capability="text_stream", prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        assert response.model == ROUTE_B["model_id"]
        assert fixture.provider.trace == [
            "prepare_stream:real_primary",
            "prepare_stream:real_secondary",
            "iterate:real_secondary",
        ]
        events = await fixture.sink.read(run_id=fixture.run_id)
        public_stream = [
            item
            for item in events
            if item.event_type
            in {
                CanonicalEventType.MODEL_OUTPUT_DELTA,
                CanonicalEventType.MODEL_OUTPUT_COMPLETED,
            }
        ]
        assert [cast(dict[str, Any], item.payload)["attempt"] for item in public_stream] == [
            2,
            2,
        ]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.selected_ordinal == 2
        assert state.active_ordinal is None
        assert state.current_reservation.token_bound == 0
        assert state.current_reservation.cost_bound is None
        assert state.delta_fenced is True
        assert [item.attempt for item in state.attempt_lifecycle] == [1, 2]
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_stream_ambiguous_observation_fences_successor_before_any_public_delta(
    tmp_path: Path,
) -> None:
    """已开始迭代但无可信 proof 的 timeout 即使零 delta，也绝不能进入 B。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["ambiguous_timeout"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        with pytest.raises(ModelProviderInvocationError):
            await fixture.bound.stream(
                ModelRequest(capability="text_stream", prompt="hello", max_output_tokens=8),
                operation_key=fixture.operation_key,
            )

        assert fixture.provider.trace == [
            "prepare_stream:real_primary",
            "iterate:real_primary",
        ]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.active_ordinal == 1
        assert state.selected_ordinal is None
        assert state.attempt_lifecycle[0].lifecycle_state == "unknown"
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_committed_reader_reconnect_never_enters_candidate_controller(
    tmp_path: Path,
) -> None:
    """重复读取 committed stream 只能返回持久化事件，不能重新 prepare 或 iterate。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        first_response = await fixture.bound.stream(
            ModelRequest(capability="text_stream", prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )
        trace_after_invocation = list(fixture.provider.trace)

        replayed_response = await fixture.bound.stream(
            ModelRequest(capability="text_stream", prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        first = await fixture.sink.read(run_id=fixture.run_id)
        second = await fixture.sink.read(run_id=fixture.run_id)

        assert [item.event_id for item in second] == [item.event_id for item in first]
        assert replayed_response == first_response
        assert fixture.provider.trace == trace_after_invocation
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_outcome", ["crash_before_send", "crash_after_send"])
async def test_stream_attempt_started_crash_windows_never_replay_or_transfer(
    tmp_path: Path,
    crash_outcome: str,
) -> None:
    """无 delta 的 started 崩溃也不能重拉同一 stream attempt 或切到后继。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: [crash_outcome],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    request = ModelRequest(capability="text_stream", prompt="hello", max_output_tokens=8)
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
        assert state.selected_ordinal is None
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_first_observed_delta_fences_successor_when_delta_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider delta 一经观察即有内存围栏；intent UoW 失败也不能借机切到 B。"""

    from agent_harness.storage.evidence_repositories import EvidenceOutboxRepository

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )

    async def fail_delta_persistence(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated delta intent commit failure")

    monkeypatch.setattr(
        EvidenceOutboxRepository,
        "persist_stream_event",
        fail_delta_persistence,
    )
    try:
        with pytest.raises(RuntimeError, match="delta intent"):
            await fixture.bound.stream(
                ModelRequest(capability="text_stream", prompt="hello", max_output_tokens=8),
                operation_key=fixture.operation_key,
            )

        assert fixture.provider.trace == [
            "prepare_stream:real_primary",
            "iterate:real_primary",
        ]
        assert not any("real_secondary" in item for item in fixture.provider.trace)
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.delta_fenced is True
        assert state.attempt_lifecycle[0].lifecycle_state == "unknown"
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_first_delta_intent_and_durable_fence_rollback_as_one_uow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首 delta intent 写入硬崩溃时，fence 与 intent 同回滚，不留下半提交窗口。"""

    from agent_harness.storage.evidence_repositories import EvidenceOutboxRepository

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )

    async def crash_during_delta_uow(*_args: object, **_kwargs: object) -> None:
        raise SimulatedProcessCrash("delta intent transaction crashed")

    monkeypatch.setattr(
        EvidenceOutboxRepository,
        "persist_stream_event",
        crash_during_delta_uow,
    )
    try:
        with pytest.raises(SimulatedProcessCrash):
            await fixture.bound.stream(
                ModelRequest(capability="text_stream", prompt="hello", max_output_tokens=8),
                operation_key=fixture.operation_key,
            )

        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.delta_fenced is False
        assert state.attempt_lifecycle[0].lifecycle_state == "started"
        assert fixture.provider.trace == [
            "prepare_stream:real_primary",
            "iterate:real_primary",
        ]
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_stream_balance_skip_reaches_first_eligible_successor_without_attempt(
    tmp_path: Path,
) -> None:
    """流式 A proof 后 B 余额不足时，B 不得 prepare/iterate，首 delta 只能来自 C。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=3,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed", "completed"],
            ROUTE_C["deployment_id"]: ["completed"],
        },
    )
    try:
        first_bound, second_bound, third_bound = fixture.candidate_token_bounds
        assert second_bound > first_bound and third_bound == first_bound
        async with fixture.storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
            assert ledger is not None
            await uow.session.execute(
                update(ParentBudgetLedgerModel)
                .where(
                    ParentBudgetLedgerModel.tenant_id == "tenant-a",
                    ParentBudgetLedgerModel.budget_owner_run_id == fixture.run_id,
                )
                .values(token_impact=ledger.token_limit - first_bound)
            )
            await uow.commit()

        response = await fixture.bound.stream(
            ModelRequest(capability="text_stream", prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        assert response.output_text == "delta:real_tertiary"
        assert fixture.provider.trace == [
            "prepare_stream:real_primary",
            "prepare_stream:real_tertiary",
            "iterate:real_tertiary",
        ]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert [item.state for item in state.candidates] == [
            "not_started",
            "budget_ineligible",
            "completed",
        ]
        assert [item.candidate_ordinal for item in state.attempt_lifecycle] == [1, 3]
        assert state.transitions[-1].from_ordinal == 1
        assert state.transitions[-1].to_ordinal == 3
    finally:
        await fixture.storage.dispose()
