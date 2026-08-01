"""最后 proof 原子转移与 successor exactly-once 恢复合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from sqlalchemy import select, update
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    BoundFailoverFixture,
    SimulatedProcessCrash,
    bound_failover_invocation,
)

from agent_harness.models import ModelProviderInvocationError, ModelRequest, ModelResponse
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.shared_budget import BudgetOperationConflict
from agent_harness.storage.shared_budget_models import BudgetOperationClaimModel

InvocationMode = Literal["completion", "stream"]


async def _invoke(fixture: BoundFailoverFixture, mode: InvocationMode) -> ModelResponse:
    """只通过可信 bound façade 进入 completion/stream 两条生产路径。"""

    capability = "text_stream" if mode == "stream" else "text_completion"
    request = ModelRequest(capability=capability, prompt="hello", max_output_tokens=8)
    if mode == "stream":
        return await fixture.bound.stream(request, operation_key=fixture.operation_key)
    return await fixture.bound.complete(request, operation_key=fixture.operation_key)


async def _tamper_transferred_history(
    fixture: BoundFailoverFixture,
    *,
    tamper_kind: Literal["attempt_identity", "proof_digest"],
) -> None:
    """模拟数据库中形状合法但canonical摘要被同步篡改的耐久历史。"""

    async with fixture.storage.uow() as uow:
        model = await uow.session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.tenant_id == "tenant-a",
                BudgetOperationClaimModel.usage_call_id == fixture.usage_call_id,
            )
        )
        assert model is not None
        payload = cast(dict[str, Any], deepcopy(model.route_chain_state_json))
        lifecycles = cast(list[dict[str, Any]], payload["attempt_lifecycle"])
        candidates = cast(list[dict[str, Any]], payload["candidates"])
        if tamper_kind == "attempt_identity":
            lifecycles[0]["attempt_identity_digest"] = "0" * 64
        else:
            proofs = cast(list[dict[str, Any]], candidates[0]["not_started_proofs"])
            proofs[0]["proof_digest"] = "1" * 64
            lifecycles[0]["not_started_proof_digest"] = "1" * 64
        await uow.session.execute(
            update(BudgetOperationClaimModel)
            .where(
                BudgetOperationClaimModel.tenant_id == "tenant-a",
                BudgetOperationClaimModel.usage_call_id == fixture.usage_call_id,
            )
            .values(route_chain_state_json=payload)
        )
        await uow.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completion", "stream"])
async def test_final_proof_and_successor_transfer_share_one_durable_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: InvocationMode,
) -> None:
    """最后 proof 不得以旧候选仍 active 的中间状态先行提交。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    original_commit = SQLAlchemyUnitOfWork.commit
    proven_commit_active_ordinals: list[int | None] = []

    async def observe_route_state_before_commit(uow: SQLAlchemyUnitOfWork) -> None:
        """从同一公共 repository 读取待提交状态，记录 proof 的事务边界。"""

        state = await uow.shared_budget.get_model_route_chain_state(
            tenant_id="tenant-a",
            usage_call_id=fixture.usage_call_id,
        )
        if state is not None and any(
            item.lifecycle_state == "not_started_proven" for item in state.attempt_lifecycle
        ):
            proven_commit_active_ordinals.append(state.active_ordinal)
        await original_commit(uow)

    monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", observe_route_state_before_commit)
    try:
        response = await _invoke(fixture, mode)

        assert response.model == ROUTE_B["model_id"]
        assert 2 in proven_commit_active_ordinals
        assert 1 not in proven_commit_active_ordinals
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completion", "stream"])
@pytest.mark.parametrize("tamper_kind", ["attempt_identity", "proof_digest"])
async def test_committed_transfer_recovery_rejects_tampered_canonical_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: InvocationMode,
    tamper_kind: Literal["attempt_identity", "proof_digest"],
) -> None:
    """恢复前必须按冻结chain重算历史摘要，引用同步伪造也不得授权B。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    original_commit = SQLAlchemyUnitOfWork.commit
    transfer_committed = False

    async def commit_transfer_then_crash(uow: SQLAlchemyUnitOfWork) -> None:
        nonlocal transfer_committed
        state = await uow.shared_budget.get_model_route_chain_state(
            tenant_id="tenant-a",
            usage_call_id=fixture.usage_call_id,
        )
        crash_after_commit = (
            not transfer_committed
            and state is not None
            and state.active_ordinal == 2
            and bool(state.attempt_lifecycle)
        )
        await original_commit(uow)
        if crash_after_commit:
            transfer_committed = True
            raise SimulatedProcessCrash("transfer committed before successor attempt")

    monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", commit_transfer_then_crash)
    try:
        with pytest.raises(SimulatedProcessCrash):
            await _invoke(fixture, mode)
        monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", original_commit)
        await _tamper_transferred_history(fixture, tamper_kind=tamper_kind)
        trace_before_replay = list(fixture.provider.trace)

        with pytest.raises(BudgetOperationConflict):
            await _invoke(fixture, mode)

        assert fixture.provider.trace == trace_before_replay
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completion", "stream"])
async def test_dangling_successor_started_reports_durable_attempt_summary(
    tmp_path: Path,
    mode: InvocationMode,
) -> None:
    """后继started但尚未send时围栏重放，并如实报告两条identity和零请求。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["crash_before_send"],
        },
    )
    try:
        with pytest.raises(SimulatedProcessCrash):
            await _invoke(fixture, mode)

        with pytest.raises(ModelProviderInvocationError) as raised:
            await _invoke(fixture, mode)

        assert raised.value.code == "model.provider_side_effect_unknown"
        assert raised.value.provider_called is False
        assert raised.value.attempt_count == 2
        expected_prefix = "prepare_stream" if mode == "stream" else "prepare"
        assert fixture.provider.trace == [
            f"{expected_prefix}:{ROUTE_A['deployment_id']}",
            f"{expected_prefix}:{ROUTE_B['deployment_id']}",
        ]
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completion", "stream"])
async def test_pre_send_cancellation_closes_unknown_and_retains_review_reservation(
    tmp_path: Path,
    mode: InvocationMode,
) -> None:
    """started 后的外部取消也必须耐久收口，且不得把零请求伪报为已调用。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["cancelled_before_send"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as raised:
            await _invoke(fixture, mode)

        assert raised.value.code == "model.provider_side_effect_unknown"
        assert raised.value.provider_called is False
        assert raised.value.attempt_count == 1
        expected_prepare = "prepare_stream" if mode == "stream" else "prepare"
        assert fixture.provider.trace == [f"{expected_prepare}:{ROUTE_A['deployment_id']}"]

        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.tenant_id == "tenant-a",
                    BudgetOperationClaimModel.usage_call_id == fixture.usage_call_id,
                )
            )
            claim_snapshot = (
                None if claim is None else (claim.state, claim.reserved_tokens, claim.token_impact)
            )
        assert state is not None
        assert ledger is not None
        assert claim_snapshot is not None
        attempt = state.attempt_lifecycle[0]
        assert attempt.lifecycle_state == "unknown"
        assert attempt.request_sent is False
        assert attempt.http_response_observed is False
        assert attempt.response_identity_observed is False
        assert attempt.usage_observed is False
        assert attempt.text_observed is False
        assert attempt.delta_observed is False
        assert state.active_ordinal == 1
        assert state.current_reservation.token_bound == fixture.candidate_token_bounds[0]
        assert claim_snapshot == (
            "needs_review",
            fixture.candidate_token_bounds[0],
            fixture.candidate_token_bounds[0],
        )
        assert ledger.state == "needs_review"
        assert ledger.token_impact == fixture.candidate_token_bounds[0]
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "outcome", "expected_trace"),
    [
        ("completion", "cancelled_on_send", ["prepare:real_primary", "close:real_primary"]),
        (
            "stream",
            "cancelled_on_iterate_not_started",
            ["prepare_stream:real_primary", "close_stream:real_primary"],
        ),
    ],
)
async def test_post_prepare_cancellation_does_not_invent_provider_observations(
    tmp_path: Path,
    mode: InvocationMode,
    outcome: str,
    expected_trace: list[str],
) -> None:
    """prepared对象只证明本地资源存在，不能替代request/response/result/delta事实。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: [outcome],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as raised:
            await _invoke(fixture, mode)

        assert raised.value.code == "model.provider_side_effect_unknown"
        assert raised.value.provider_called is False
        assert raised.value.attempt_count == 1
        assert fixture.provider.trace == expected_trace
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        attempt = state.attempt_lifecycle[0]
        assert attempt.lifecycle_state == "unknown"
        assert attempt.request_sent is False
        assert attempt.http_response_observed is False
        assert attempt.response_identity_observed is False
        assert attempt.usage_observed is False
        assert attempt.text_observed is False
        assert attempt.delta_observed is False
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "outcome", "expected_called", "expected_trace"),
    [
        (
            "completion",
            "cancelled_on_send_close_failure",
            False,
            ["prepare:real_primary", "close:real_primary"],
        ),
        (
            "stream",
            "cancelled_on_iterate_close_failure",
            False,
            ["prepare_stream:real_primary", "close_stream:real_primary"],
        ),
        (
            "completion",
            "ambiguous_timeout_cleanup_failure",
            True,
            ["prepare:real_primary", "send:real_primary", "close:real_primary"],
        ),
        (
            "stream",
            "ambiguous_timeout_cleanup_failure",
            True,
            [
                "prepare_stream:real_primary",
                "iterate:real_primary",
                "close_stream:real_primary",
            ],
        ),
    ],
)
async def test_cleanup_failure_does_not_override_stable_safe_error(
    tmp_path: Path,
    mode: InvocationMode,
    outcome: str,
    expected_called: bool,
    expected_trace: list[str],
) -> None:
    """取消或unknown已稳定分类后，cleanup失败不得逃逸raw异常或启动后继。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: [outcome],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as raised:
            await _invoke(fixture, mode)

        assert raised.value.code == "model.provider_side_effect_unknown"
        assert raised.value.provider_called is expected_called
        assert raised.value.attempt_count == 1
        assert fixture.provider.trace == expected_trace
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completion", "stream"])
@pytest.mark.parametrize("leading_not_started", [False, True])
async def test_success_cleanup_failure_enters_review_before_first_public_return(
    tmp_path: Path,
    mode: InvocationMode,
    leading_not_started: bool,
) -> None:
    """成功响应后的 cleanup 不确定性必须在首次返回前原子进入待复核。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: (
                ["client_not_started", "completed"] if leading_not_started else ["completed"]
            ),
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    fixture.provider.cleanup_failures.add(ROUTE_A["deployment_id"])
    expected_trace = (
        [
            "prepare_stream:real_primary",
            "iterate:real_primary",
            "close_stream:real_primary",
        ]
        if mode == "stream"
        else ["prepare:real_primary", "send:real_primary", "close:real_primary"]
    )
    if leading_not_started:
        expected_trace.insert(0, expected_trace[0])
    expected_attempt = 2 if leading_not_started else 1
    try:
        with pytest.raises(ModelProviderInvocationError) as first_error:
            await _invoke(fixture, mode)

        assert first_error.value.code == "model.provider_side_effect_unknown"
        assert first_error.value.failure_domain == "runtime"
        assert first_error.value.provider_called is True
        assert first_error.value.attempt_count == expected_attempt
        assert fixture.provider.trace == expected_trace

        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.tenant_id == "tenant-a",
                    BudgetOperationClaimModel.usage_call_id == fixture.usage_call_id,
                )
            )
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            usage_snapshot = (usage.state, usage.error_code, usage.result_json)
            claim_snapshot = (
                (
                    claim.state,
                    claim.side_effect_state,
                    claim.result_json,
                )
                if claim is not None
                else None
            )
        assert state is not None
        assert ledger is not None
        assert claim is not None
        assert len(state.attempt_lifecycle) == expected_attempt
        if leading_not_started:
            assert state.attempt_lifecycle[0].lifecycle_state == "not_started_proven"
        attempt = state.attempt_lifecycle[-1]
        assert attempt.attempt == expected_attempt
        assert attempt.lifecycle_state == "unknown"
        assert attempt.request_sent is True
        assert attempt.http_response_observed is True
        assert attempt.http_status is None
        assert attempt.response_identity_observed is False
        assert attempt.usage_observed is True
        assert attempt.text_observed is True
        assert attempt.delta_observed is (mode == "stream")
        assert attempt.completion_observed is True
        assert state.active_ordinal == 1
        assert state.selected_ordinal is None
        usage_state, usage_error_code, usage_result = usage_snapshot
        assert usage_state == "needs_review"
        assert usage_error_code == "model.provider_side_effect_unknown"
        assert usage_result is not None
        assert set(usage_result) == {"started", "attempt_review"}
        review = usage_result["attempt_review"]
        assert review["attempts"][0]["attempt"] == expected_attempt
        assert review["budget_charge"]["unresolved_attempts"] == [expected_attempt]
        assert claim_snapshot == (
            "needs_review",
            "result_committed",
            {"attempt_review": usage_result["attempt_review"]},
        )
        assert ledger.state == "needs_review"

        trace_before_replay = list(fixture.provider.trace)
        with pytest.raises(ModelProviderInvocationError) as replay_error:
            await _invoke(fixture, mode)
        assert replay_error.value.code == "model.provider_side_effect_unknown"
        assert replay_error.value.provider_called is True
        assert replay_error.value.attempt_count == expected_attempt
        assert fixture.provider.trace == trace_before_replay
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["completion", "stream"])
async def test_committed_transfer_recovers_unstarted_successor_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: InvocationMode,
) -> None:
    """A→B 已提交、B attempt 尚不存在时，重放必须只启动 B 一次。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    original_commit = SQLAlchemyUnitOfWork.commit
    transfer_ack_lost = False

    async def commit_transfer_then_crash(uow: SQLAlchemyUnitOfWork) -> None:
        """在 successor reservation 已提交、started identity 未创建的唯一窗口硬崩溃。"""

        nonlocal transfer_ack_lost
        state = await uow.shared_budget.get_model_route_chain_state(
            tenant_id="tenant-a",
            usage_call_id=fixture.usage_call_id,
        )
        crash_after_commit = (
            not transfer_ack_lost
            and state is not None
            and state.active_ordinal == 2
            and state.attempt_lifecycle
            and all(
                item.candidate_ordinal == 1 and item.lifecycle_state == "not_started_proven"
                for item in state.attempt_lifecycle
            )
        )
        await original_commit(uow)
        if crash_after_commit:
            transfer_ack_lost = True
            raise SimulatedProcessCrash("successor reservation committed before started identity")

    monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", commit_transfer_then_crash)
    try:
        with pytest.raises(SimulatedProcessCrash):
            await _invoke(fixture, mode)

        response = await _invoke(fixture, mode)

        assert transfer_ack_lost is True
        assert response.model == ROUTE_B["model_id"]
        if mode == "stream":
            assert fixture.provider.trace == [
                "prepare_stream:real_primary",
                "prepare_stream:real_secondary",
                "iterate:real_secondary",
            ]
        else:
            assert fixture.provider.trace == [
                "prepare:real_primary",
                "prepare:real_secondary",
                "send:real_secondary",
            ]
    finally:
        await fixture.storage.dispose()
