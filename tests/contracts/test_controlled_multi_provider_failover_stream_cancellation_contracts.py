"""受控多供应商回退的 streaming 取消与结算合同。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    BoundFailoverFixture,
    bound_failover_invocation,
)

from agent_harness.events import CanonicalEventType
from agent_harness.models import ModelProviderInvocationError, ModelRequest, ModelResponse
from agent_harness.storage.shared_budget_models import BudgetOperationClaimModel


async def _invoke_stream(fixture: BoundFailoverFixture) -> ModelResponse:
    """只通过可信 bound façade 进入 streaming 生产路径。"""

    request = ModelRequest(capability="text_stream", prompt="hello", max_output_tokens=8)
    return await fixture.bound.stream(request, operation_key=fixture.operation_key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_called", "expected_lifecycle", "expected_claim"),
    [
        ("cancelled_on_iterate_stopped_complete", True, "settled", "settled"),
        ("cancelled_on_iterate_stopped_partial", True, "unknown", "needs_review"),
        ("cancelled_on_iterate_stopped_null", True, "unknown", "needs_review"),
        ("cancelled_on_iterate_unknown_partial", True, "unknown", "needs_review"),
        ("cancelled_on_iterate_unknown_null", False, "unknown", "needs_review"),
    ],
)
async def test_stream_cancellation_classifies_close_result_before_settlement(
    tmp_path: Path,
    outcome: str,
    expected_called: bool,
    expected_lifecycle: str,
    expected_claim: str,
) -> None:
    """stream取消只按close DTO结算：完整stopped走actual，其余保持待复核。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: [outcome],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        expected_code = (
            "model.invocation_cancelled"
            if outcome == "cancelled_on_iterate_stopped_complete"
            else "model.provider_side_effect_unknown"
        )
        with pytest.raises(ModelProviderInvocationError) as raised:
            await _invoke_stream(fixture)

        assert raised.value.code == expected_code
        assert raised.value.provider_called is expected_called
        assert raised.value.attempt_count == 1
        assert fixture.provider.trace == [
            "prepare_stream:real_primary",
            "close_stream:real_primary",
        ]
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
                None
                if claim is None
                else (
                    claim.state,
                    claim.token_impact,
                    claim.actual_tokens,
                    claim.side_effect_state,
                    claim.result_json,
                )
            )
            group = await uow.evidence_outbox.ordered_group(
                group_id=f"model-stream:{fixture.usage_call_id}"
            )
            group_states = [item.state for item in group]
        assert state is not None
        assert ledger is not None
        assert claim_snapshot is not None
        assert state.attempt_lifecycle[0].lifecycle_state == expected_lifecycle
        assert claim_snapshot[0] == expected_claim
        if expected_claim == "settled":
            lifecycle = state.attempt_lifecycle[0]
            candidate = state.candidates[0]
            assert lifecycle.side_effect_state == "result_committed"
            assert lifecycle.request_sent is True
            assert lifecycle.usage_observed is True
            assert lifecycle.completion_observed is False
            assert candidate.state == "cancelled"
            assert candidate.reason == "invocation_cancelled"
            assert candidate.side_effect_state == "result_committed"
            assert state.selected_ordinal is None
            assert state.active_ordinal is None
            assert state.waiting_approval_ordinal is None
            assert state.evidence_route_ordinal == 1
            assert state.current_reservation.token_bound == 0
            assert [item.state for item in state.transitions] == ["activated"]
            assert claim_snapshot[1:4] == (2, 2, "result_committed")
            result = claim_snapshot[4]
            assert result is not None
            assert result["outcome"] == "cancelled"
            failure = cast(dict[str, object], result["failure"])
            assert failure["error_code"] == "model.invocation_cancelled"
            assert ledger.state == "active"
            assert ledger.token_impact == 2
            assert group_states and set(group_states) == {"cancelled"}
            public_events = await fixture.sink.read(run_id=fixture.run_id)
            assert not any(
                item.event_type is CanonicalEventType.MODEL_OUTPUT_COMPLETED
                for item in public_events
            )
            trace_before_replay = list(fixture.provider.trace)
            with pytest.raises(ModelProviderInvocationError) as replayed:
                await _invoke_stream(fixture)
            assert replayed.value.code == "model.invocation_cancelled"
            assert replayed.value.provider_called is True
            assert replayed.value.attempt_count == 1
            assert fixture.provider.trace == trace_before_replay
        else:
            assert state.selected_ordinal is None
            assert state.active_ordinal == 1
            assert state.current_reservation.token_bound == fixture.candidate_token_bounds[0]
            assert claim_snapshot[1] == fixture.candidate_token_bounds[0]
            assert ledger.state == "needs_review"
    finally:
        await fixture.storage.dispose()
