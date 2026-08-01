"""Bound completion 的多候选推进、attempt 与保守围栏合同。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
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

from agent_harness.models import (
    ModelProviderInvocationError,
    ModelRequest,
    model_route_operation_identity_digest,
)
from agent_harness.storage.evidence_models import RunEvidenceOutboxModel
from agent_harness.storage.shared_budget import BudgetOperationConflict


async def _invoke_chain_mode(fixture: Any, *, stream: bool) -> Any:
    """经同一 public bound seam 执行 completion 或 stream，不触碰私有控制器。"""

    request = ModelRequest(
        capability="text_stream" if stream else "text_completion",
        prompt="hello",
        max_output_tokens=8,
    )
    method = fixture.bound.stream if stream else fixture.bound.complete
    return await method(request, operation_key=fixture.operation_key)


@pytest.mark.asyncio
async def test_concurrent_exact_chain_operation_never_starts_a_second_provider_attempt(
    tmp_path: Path,
) -> None:
    """两个并发相同 stable key 只能有一个 winner；loser只读耐久状态而不重发。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    request = ModelRequest(prompt="hello", max_output_tokens=8)
    try:
        outcomes = await asyncio.gather(
            fixture.bound.complete(request, operation_key=fixture.operation_key),
            fixture.bound.complete(request, operation_key=fixture.operation_key),
            return_exceptions=True,
        )

        assert sum(not isinstance(item, BaseException) for item in outcomes) >= 1
        assert fixture.provider.trace == [
            "prepare:real_primary",
            "send:real_primary",
        ]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.selected_ordinal == 1
        assert [item.attempt for item in state.attempt_lifecycle] == [1]
    finally:
        await fixture.storage.dispose()


@pytest.mark.parametrize(
    ("first", "second", "expected_trace"),
    [
        (
            "trusted_business_not_started",
            "client_not_started",
            [
                "prepare:real_primary",
                "send:real_primary",
                "prepare:real_secondary",
                "prepare:real_tertiary",
                "send:real_tertiary",
            ],
        ),
        (
            "client_not_started",
            "trusted_business_not_started",
            [
                "prepare:real_primary",
                "prepare:real_secondary",
                "send:real_secondary",
                "prepare:real_tertiary",
                "send:real_tertiary",
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_bound_completion_advances_once_for_each_trusted_not_started_proof_order(
    tmp_path: Path,
    first: str,
    second: str,
    expected_trace: list[str],
) -> None:
    """三候选两种可信顺序都只推进一次，并把全局 attempt 3 选为完成者。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        scripts={
            ROUTE_A["deployment_id"]: [first],
            ROUTE_B["deployment_id"]: [second],
            ROUTE_C["deployment_id"]: ["completed"],
        },
    )
    try:
        response = await fixture.bound.complete(
            ModelRequest(prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        assert response.model == ROUTE_C["model_id"]
        assert response.output_text == "completed:real_tertiary"
        assert fixture.provider.trace == expected_trace
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            outbox = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            outbox_result = cast(dict[str, Any], outbox.result_json)
        assert state is not None
        assert state.selected_ordinal == 3
        assert state.active_ordinal is None
        assert state.waiting_approval_ordinal is None
        assert state.current_reservation.token_bound == 0
        assert state.current_reservation.cost_bound is None
        assert state.operation_identity_digest == model_route_operation_identity_digest(
            tenant_id="tenant-a",
            run_id=fixture.run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
            operation_key=fixture.operation_key,
        )
        assert [item.attempt for item in state.attempt_lifecycle] == [1, 2, 3]
        assert [item.lifecycle_state for item in state.attempt_lifecycle] == [
            "not_started_proven",
            "not_started_proven",
            "settled",
        ]
        assert (
            outbox_result["evidence"]["decision"]["route_chain"]["state"]["selected_ordinal"] == 3
        )
        assert [item["attempt"] for item in outbox_result["evidence"]["decision"]["attempts"]] == [
            1,
            2,
            3,
        ]
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_bound_completion_appends_each_same_route_retry_proof_before_transfer(
    tmp_path: Path,
) -> None:
    """同 route 两次受信 retry 必须逐 attempt proof-close，耗尽后才转移到 B。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: [
                "trusted_business_not_started",
                "trusted_business_not_started",
            ],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        response = await fixture.bound.complete(
            ModelRequest(prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        assert response.model == ROUTE_B["model_id"]
        assert fixture.provider.trace == [
            "prepare:real_primary",
            "send:real_primary",
            "prepare:real_primary",
            "send:real_primary",
            "prepare:real_secondary",
            "send:real_secondary",
        ]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert [item.attempt for item in state.candidates[0].not_started_proofs] == [1, 2]
        assert [item.attempt for item in state.attempt_lifecycle] == [1, 2, 3]
        assert state.selected_ordinal == 2
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_bound_completion_replays_committed_chain_without_provider_reentry(
    tmp_path: Path,
) -> None:
    """同 usage identity 的 result-persisted/published 重放只解析耐久 chain evidence。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    request = ModelRequest(prompt="hello", max_output_tokens=8)
    try:
        first = await fixture.bound.complete(request, operation_key=fixture.operation_key)
        trace = list(fixture.provider.trace)

        replayed = await fixture.bound.complete(request, operation_key=fixture.operation_key)

        assert replayed == first
        assert fixture.provider.trace == trace
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "chain_id",
        "attempt_candidate",
        "proof_digest",
        "unknown_state",
    ],
)
async def test_committed_chain_replay_rejects_identity_lifecycle_and_proof_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    """已提交 evidence 任一 chain/lifecycle/proof 漂移都必须在重放前关闭失败。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    request = ModelRequest(prompt="hello", max_output_tokens=8)
    try:
        await fixture.bound.complete(request, operation_key=fixture.operation_key)
        trace = list(fixture.provider.trace)
        async with fixture.storage.uow() as uow:
            item = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            tampered = cast(dict[str, Any], deepcopy(item.result_json))
            route_chain = tampered["evidence"]["decision"]["route_chain"]
            if mutation == "chain_id":
                route_chain["state"]["chain_id"] = "0" * 64
            elif mutation == "attempt_candidate":
                route_chain["state"]["attempt_lifecycle"][0]["candidate_ordinal"] = 2
            elif mutation == "proof_digest":
                route_chain["state"]["candidates"][0]["not_started_proofs"][0]["proof_digest"] = (
                    "0" * 64
                )
            else:
                route_chain["state"]["attempt_lifecycle"][0]["lifecycle_state"] = "unknown"
                route_chain["state"]["attempt_lifecycle"][0]["not_started_proof_digest"] = None
            await uow.session.execute(
                update(RunEvidenceOutboxModel)
                .where(RunEvidenceOutboxModel.id == item.id)
                .values(result_json=tampered)
            )
            await uow.commit()

        with pytest.raises(BudgetOperationConflict):
            await fixture.bound.complete(request, operation_key=fixture.operation_key)
        assert fixture.provider.trace == trace
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_route_chain_exhaustion_is_actual_zero_and_replays_without_provider_reentry(
    tmp_path: Path,
) -> None:
    """全部候选可信未开始时只结算一次稳定 exhausted，不留下 reservation。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["client_not_started"],
        },
    )
    request = ModelRequest(prompt="hello", max_output_tokens=8)
    try:
        with pytest.raises(ModelProviderInvocationError) as first_error:
            await fixture.bound.complete(request, operation_key=fixture.operation_key)
        trace = list(fixture.provider.trace)

        with pytest.raises(ModelProviderInvocationError) as replay_error:
            await fixture.bound.complete(request, operation_key=fixture.operation_key)

        assert first_error.value.code == replay_error.value.code == "model.route_chain_exhausted"
        assert first_error.value.detail == replay_error.value.detail
        assert first_error.value.detail is not None
        assert first_error.value.detail.to_payload() == {
            "schema_version": "model-route-chain-exhausted-v1",
            "chain_id": first_error.value.detail.chain_id,
            "causes": [
                {"ordinal": 1, "cause": "not_started_failure"},
                {"ordinal": 2, "cause": "not_started_failure"},
            ],
        }
        assert first_error.value.provider_called is replay_error.value.provider_called is False
        assert first_error.value.attempt_count == replay_error.value.attempt_count == 2
        assert fixture.provider.trace == trace
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
        assert state is not None
        assert ledger is not None
        assert state.active_ordinal is None
        assert state.current_reservation.token_bound == 0
        assert state.transitions[-1].state == "terminated"
        assert state.transitions[-1].reason == "route_exhausted"
        assert ledger.token_impact == 0
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_bound_completion_fences_successors_after_ambiguous_timeout(
    tmp_path: Path,
) -> None:
    """已发送但无可信未开始证明的 timeout 永久围栏 B，并保留 needs-review。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["ambiguous_timeout"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await fixture.bound.complete(
                ModelRequest(prompt="hello", max_output_tokens=8),
                operation_key=fixture.operation_key,
            )

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert fixture.provider.trace == [
            "prepare:real_primary",
            "send:real_primary",
        ]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
        assert state is not None
        assert ledger is not None
        assert state.attempt_lifecycle[0].lifecycle_state == "unknown"
        assert state.active_ordinal == 1
        assert ledger.state == "needs_review"
    finally:
        await fixture.storage.dispose()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_each_candidate_receives_its_own_frozen_deadline(
    tmp_path: Path,
    stream: bool,
) -> None:
    """A 安全关闭所耗时间不得侵占 B 自己冻结的 total timeout。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        total_timeout_ms_by_deployment={
            ROUTE_A["deployment_id"]: 1000,
            ROUTE_B["deployment_id"]: 1000,
        },
        prepare_delays_seconds={
            ROUTE_A["deployment_id"]: 0.6,
            ROUTE_B["deployment_id"]: 0.6,
        },
    )
    try:
        response = await _invoke_chain_mode(fixture, stream=stream)

        assert response.model == ROUTE_B["model_id"]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert [item.lifecycle_state for item in state.attempt_lifecycle] == [
            "not_started_proven",
            "settled",
        ]
    finally:
        await fixture.storage.dispose()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_prepare_deadline_without_provider_observation_keeps_called_false(
    tmp_path: Path,
    stream: bool,
) -> None:
    """prepare 超时只证明 started identity；不得捏造 request 或 provider 调用。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        total_timeout_ms_by_deployment={ROUTE_A["deployment_id"]: 100},
        prepare_delays_seconds={ROUTE_A["deployment_id"]: 0.2},
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await _invoke_chain_mode(fixture, stream=stream)

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert exc_info.value.provider_called is False
        assert exc_info.value.attempt_count == 1
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        lifecycle = state.attempt_lifecycle[0]
        assert lifecycle.lifecycle_state == "unknown"
        assert not any(
            (
                lifecycle.request_sent,
                lifecycle.http_response_observed,
                lifecycle.response_identity_observed,
                lifecycle.usage_observed,
                lifecycle.text_observed,
                lifecycle.delta_observed,
            )
        )
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_outcome", ["crash_before_send", "crash_after_send"])
async def test_chain_attempt_started_crash_windows_never_replay_or_advance(
    tmp_path: Path,
    crash_outcome: str,
) -> None:
    """attempt 2 started 后两个崩溃窗口都保留同一 identity，重放不调用 B/C。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: [crash_outcome],
            ROUTE_C["deployment_id"]: ["completed"],
        },
    )
    request = ModelRequest(prompt="hello", max_output_tokens=8)
    try:
        with pytest.raises(SimulatedProcessCrash):
            await fixture.bound.complete(request, operation_key=fixture.operation_key)
        trace_after_crash = list(fixture.provider.trace)

        with pytest.raises(ModelProviderInvocationError) as replay_error:
            await fixture.bound.complete(request, operation_key=fixture.operation_key)

        assert replay_error.value.code == "model.provider_side_effect_unknown"
        assert fixture.provider.trace == trace_after_crash
        assert not any("real_tertiary" in item for item in fixture.provider.trace)
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
        assert state is not None
        assert ledger is not None
        assert [item.attempt for item in state.attempt_lifecycle] == [1, 2]
        assert state.attempt_lifecycle[1].lifecycle_state in {"started", "unknown"}
        assert state.active_ordinal == 2
        assert ledger.state == "needs_review"
    finally:
        await fixture.storage.dispose()
