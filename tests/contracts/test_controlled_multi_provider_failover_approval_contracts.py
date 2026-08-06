"""Route-chain approval continuation 的原 usage/operation identity 合同。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import update
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    bound_failover_invocation,
)

from agent_harness.identity import IdentityContext
from agent_harness.models import (
    BoundModelInvocationService,
    ModelApprovalRequired,
    ModelDecision,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    UsageEvidenceContext,
    stable_usage_call_id,
)
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import ApprovalGrant
from agent_harness.storage.approval_records import ApprovalCreate
from agent_harness.storage.shared_budget_models import ParentBudgetLedgerModel


class _ApprovedServiceRecorder:
    """只记录 approved seam 收到的稳定 identity，不执行 provider 或存储副作用。"""

    def __init__(self, *, identity: tuple[str, str]) -> None:
        self.identity = identity
        self.calls: list[tuple[str, str, str]] = []

    async def approved_invocation_identity(self, **_kwargs: object) -> tuple[str, str]:
        """返回已由 durable artifact 恢复的身份，隔离 facade 参数转发。"""

        return self.identity

    async def complete_with_approval(self, *_args: object, **kwargs: Any) -> ModelResponse:
        self.calls.append(
            (
                "complete",
                str(kwargs["usage_call_id"]),
                str(kwargs["route_operation_identity_digest"]),
            )
        )
        return _response()

    async def stream_with_approval(self, *_args: object, **kwargs: Any) -> ModelResponse:
        self.calls.append(
            (
                "stream",
                str(kwargs["usage_call_id"]),
                str(kwargs["route_operation_identity_digest"]),
            )
        )
        return _response()


class _SequencePolicy:
    """按候选顺序返回固定决策，验证获批 balance skip 后必须重新授权。"""

    def __init__(self, decisions: list[str]) -> None:
        self.decisions = list(decisions)

    async def evaluate(self, _check: object) -> SimpleNamespace:
        return SimpleNamespace(decision=self.decisions.pop(0), reason="fixture decision")


def _response() -> ModelResponse:
    return ModelResponse(
        provider="openai-compatible",
        model="fixture-text-1",
        output_text="approved",
        decision=ModelDecision(action="call", estimated_tokens=1),
        token_usage={"input_tokens": 1, "output_tokens": 0},
    )


def _grant() -> ApprovalGrant:
    return ApprovalGrant(
        approval_id="approval-a",
        lease_id="lease-a",
        tenant_id="tenant-a",
        identity_id="identity-a",
        session_id="local-session",
        agent_id="agent-a",
        run_id="run-a",
        action="model.invoke",
        resource="agent:agent-a:model",
        arguments_hash="a" * 64,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_chain_approval_resume_reuses_preapproval_operation_identity(
    streaming: bool,
) -> None:
    """approved continuation 必须复用原 operation key，禁止 `approved:<id>` rekey。"""

    context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    expected = stable_usage_call_id(context=context, operation_key="primary-model-call")
    expected_digest = "b" * 64
    recorder = _ApprovedServiceRecorder(identity=(expected, expected_digest))
    bound = BoundModelInvocationService(
        service=recorder,  # type: ignore[arg-type]
        context=context,
        identity=IdentityContext.local_default(),
    )
    operation_key = "primary-model-call"
    request = ModelRequest(prompt="需要审批", max_output_tokens=8)

    if streaming:
        await bound.stream_approved(request, operation_key=operation_key, grant=_grant())
    else:
        await bound.complete_approved(request, operation_key=operation_key, grant=_grant())

    assert recorder.calls == [("stream" if streaming else "complete", expected, expected_digest)]
    assert expected != stable_usage_call_id(
        context=context,
        operation_key="approved:approval-a",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_first_candidate_approval_uses_zero_impact_carrier_and_resumes_same_claim(
    tmp_path: Any,
    streaming: bool,
) -> None:
    """首候选审批前零预留；有效 lease 只激活原 ordinal 和原 usage identity。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        policy_engine=PolicyEngine(
            provider=YamlPolicyProvider(require_approval_actions=["model.invoke"]),
        ),
    )
    request = ModelRequest(
        capability="text_stream" if streaming else "text_completion",
        prompt="需要审批",
        max_output_tokens=8,
    )
    invoke = fixture.bound.stream if streaming else fixture.bound.complete
    resume = fixture.bound.stream_approved if streaming else fixture.bound.complete_approved
    try:
        with pytest.raises(ModelApprovalRequired) as captured:
            await invoke(request, operation_key=fixture.operation_key)

        approval_request = captured.value.request
        assert fixture.provider.trace == []
        async with fixture.storage.uow() as uow:
            waiting = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                run_id=fixture.run_id,
                usage_call_id=fixture.usage_call_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
        assert waiting is not None
        assert waiting.waiting_approval_ordinal == 1
        assert waiting.active_ordinal is None
        assert waiting.current_reservation.token_bound == 0
        assert waiting.current_reservation.cost_bound is None
        assert ledger is not None and ledger.token_impact == 0

        identity = IdentityContext.local_default()
        async with fixture.storage.uow() as uow:
            record = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id="tenant-a",
                    run_id=fixture.run_id,
                    agent_id="agent-a",
                    action=approval_request.action,
                    resource=approval_request.resource,
                    reason=approval_request.reason,
                    requested_by=identity.user_id,
                    trace_id="trace-a",
                    request_id="request-a",
                    metadata={
                        "identity_id": identity.user_id,
                        "arguments_hash": approval_request.arguments_hash,
                        "continuation": approval_request.continuation,
                    },
                )
            )
            lease = await uow.approvals.claim_resolution(
                approval_id=record.approval_id,
                run_id=fixture.run_id,
                tenant_id="tenant-a",
                request_id="approve-request-a",
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=record.approval_id,
            lease_id=lease.lease_id,
            tenant_id="tenant-a",
            identity_id=identity.user_id,
            session_id=identity.session_id,
            agent_id="agent-a",
            run_id=fixture.run_id,
            action=approval_request.action,
            resource=approval_request.resource,
            arguments_hash=approval_request.arguments_hash,
        )

        fixture.agent_policy.fallback_routes = ()
        wrong_operation_key = "caller-supplied-after-reload"
        response = await resume(
            request,
            operation_key=wrong_operation_key,
            grant=grant,
        )
        assert response.output_text
        assert len(fixture.provider.trace) >= 1
        async with fixture.storage.uow() as uow:
            completed = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                run_id=fixture.run_id,
                usage_call_id=fixture.usage_call_id,
            )
            wrong_identity = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                run_id=fixture.run_id,
                usage_call_id=stable_usage_call_id(
                    context=UsageEvidenceContext(
                        tenant_id="tenant-a",
                        run_id=fixture.run_id,
                        agent_id="agent-a",
                        request_id="request-a",
                        trace_id="trace-a",
                    ),
                    operation_key=wrong_operation_key,
                ),
            )
        assert completed is not None
        assert wrong_identity is None
        assert completed.selected_ordinal == 1
        assert completed.candidates[0].approval_request_binding_digest
        assert completed.candidates[0].approval_grant_binding_digest
        assert [item.state for item in completed.transitions[:2]] == [
            "waiting_approval",
            "approved",
        ]
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_approved_balance_skip_preserves_bindings_and_reauthorizes_successor(
    tmp_path: Any,
) -> None:
    """A 获批后余额不足不得写 approved；B 必须以独立 request binding 再次暂停。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        policy_engine=PolicyEngine(
            provider=YamlPolicyProvider(require_approval_actions=["model.invoke"]),
        ),
    )
    request = ModelRequest(prompt="需要审批", max_output_tokens=8)
    try:
        with pytest.raises(ModelApprovalRequired) as first_pause:
            await fixture.bound.complete(request, operation_key=fixture.operation_key)
        approval_request = first_pause.value.request
        identity = IdentityContext.local_default()
        async with fixture.storage.uow() as uow:
            record = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id="tenant-a",
                    run_id=fixture.run_id,
                    agent_id="agent-a",
                    action=approval_request.action,
                    resource=approval_request.resource,
                    reason=approval_request.reason,
                    requested_by=identity.user_id,
                    trace_id="trace-a",
                    request_id="request-a",
                    metadata={
                        "identity_id": identity.user_id,
                        "arguments_hash": approval_request.arguments_hash,
                        "continuation": approval_request.continuation,
                    },
                )
            )
            lease = await uow.approvals.claim_resolution(
                approval_id=record.approval_id,
                run_id=fixture.run_id,
                tenant_id="tenant-a",
                request_id="approve-request-a",
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
            assert ledger is not None
            await uow.session.execute(
                update(ParentBudgetLedgerModel)
                .where(
                    ParentBudgetLedgerModel.tenant_id == "tenant-a",
                    ParentBudgetLedgerModel.budget_owner_run_id == fixture.run_id,
                )
                .values(token_impact=ledger.token_limit)
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=record.approval_id,
            lease_id=lease.lease_id,
            tenant_id="tenant-a",
            identity_id=identity.user_id,
            session_id=identity.session_id,
            agent_id="agent-a",
            run_id=fixture.run_id,
            action=approval_request.action,
            resource=approval_request.resource,
            arguments_hash=approval_request.arguments_hash,
        )

        with pytest.raises(ModelApprovalRequired) as second_pause:
            await fixture.bound.complete_approved(
                request,
                operation_key=fixture.operation_key,
                grant=grant,
            )

        assert second_pause.value.request.continuation["candidate_ordinal"] == 2
        assert fixture.provider.trace == []
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.active_ordinal is None
        assert state.waiting_approval_ordinal == 2
        assert state.current_reservation.token_bound == 0
        assert state.candidates[0].state == "budget_ineligible"
        assert state.candidates[0].reason == "balance"
        assert state.candidates[0].approval_request_binding_digest
        assert state.candidates[0].approval_grant_binding_digest
        assert state.candidates[1].state == "waiting_approval"
        assert [item.state for item in state.transitions] == [
            "waiting_approval",
            "waiting_approval",
        ]
        assert state.transitions[-1].from_ordinal == 1
        assert state.transitions[-1].released_token_bound == 0
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("successor_decision", "expected"),
    [
        ("allow", "completed"),
        ("deny", "model.policy_denied"),
        ("allow_exhausted", "model.route_chain_exhausted"),
    ],
)
async def test_approved_balance_skip_successor_terminal_branches_are_canonical(
    tmp_path: Any,
    successor_decision: str,
    expected: str,
) -> None:
    """获批 A balance skip 后，B 的 allow、deny、全耗尽各自产生唯一稳定结果。"""

    second_policy = "allow" if successor_decision == "allow_exhausted" else successor_decision
    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed", "completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        policy_engine=_SequencePolicy(["require_approval", second_policy]),
    )
    request = ModelRequest(prompt="需要审批", max_output_tokens=8)
    try:
        with pytest.raises(ModelApprovalRequired) as first_pause:
            await fixture.bound.complete(request, operation_key=fixture.operation_key)
        approval_request = first_pause.value.request
        identity = IdentityContext.local_default()
        async with fixture.storage.uow() as uow:
            record = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id="tenant-a",
                    run_id=fixture.run_id,
                    agent_id="agent-a",
                    action=approval_request.action,
                    resource=approval_request.resource,
                    reason=approval_request.reason,
                    requested_by=identity.user_id,
                    trace_id="trace-a",
                    request_id="request-a",
                    metadata={
                        "identity_id": identity.user_id,
                        "arguments_hash": approval_request.arguments_hash,
                        "continuation": approval_request.continuation,
                    },
                )
            )
            lease = await uow.approvals.claim_resolution(
                approval_id=record.approval_id,
                run_id=fixture.run_id,
                tenant_id="tenant-a",
                request_id="approve-request-a",
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
            assert ledger is not None
            first_bound, second_bound = fixture.candidate_token_bounds
            assert first_bound > second_bound
            remaining = (
                0 if successor_decision == "allow_exhausted" else (first_bound + second_bound) // 2
            )
            await uow.session.execute(
                update(ParentBudgetLedgerModel)
                .where(
                    ParentBudgetLedgerModel.tenant_id == "tenant-a",
                    ParentBudgetLedgerModel.budget_owner_run_id == fixture.run_id,
                )
                .values(token_impact=ledger.token_limit - remaining)
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=record.approval_id,
            lease_id=lease.lease_id,
            tenant_id="tenant-a",
            identity_id=identity.user_id,
            session_id=identity.session_id,
            agent_id="agent-a",
            run_id=fixture.run_id,
            action=approval_request.action,
            resource=approval_request.resource,
            arguments_hash=approval_request.arguments_hash,
        )

        if expected == "completed":
            response = await fixture.bound.complete_approved(
                request,
                operation_key=fixture.operation_key,
                grant=grant,
            )
            assert response.output_text == "completed:real_secondary"
        else:
            with pytest.raises(ModelProviderInvocationError) as captured:
                await fixture.bound.complete_approved(
                    request,
                    operation_key=fixture.operation_key,
                    grant=grant,
                )
            assert captured.value.code == expected

        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.candidates[0].state == "budget_ineligible"
        assert state.candidates[0].approval_grant_binding_digest
        assert all(item.state != "approved" for item in state.transitions)
        if expected == "completed":
            assert state.selected_ordinal == 2
            assert state.transitions[-1].state == "transferred"
            assert state.transitions[-1].reason == "balance"
        elif expected == "model.policy_denied":
            assert state.candidates[1].state == "denied"
            assert state.transitions[-1].reason == "policy_denied"
        else:
            assert state.candidates[1].state == "budget_ineligible"
            assert state.transitions[-1].reason == "route_exhausted"
    finally:
        await fixture.storage.dispose()
