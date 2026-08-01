"""候选策略、预算跳过与初始扫描的 completion 合同。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    ROUTE_C,
    bound_failover_invocation,
)

from agent_harness.models import (
    ModelApprovalRequired,
    ModelProviderInvocationError,
    ModelRequest,
)
from agent_harness.storage.shared_budget_models import ParentBudgetLedgerModel


class _DenyPolicy:
    """固定 deny 的最小 policy seam，证明换候选不能绕过授权。"""

    async def evaluate(self, _check: object) -> SimpleNamespace:
        return SimpleNamespace(decision="deny", reason="fixture deny")


class _AllowThenDenyPolicy:
    """首候选 allow、后继 deny，锁定逐候选重新授权与原子释放。"""

    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, _check: object) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            decision="allow" if self.calls == 1 else "deny",
            reason="fixture decision",
        )


class _SequencePolicy:
    """按 ordinal 返回固定决定，锁定初始扫描不会复用前一候选授权。"""

    def __init__(self, decisions: list[str]) -> None:
        self.decisions = list(decisions)

    async def evaluate(self, _check: object) -> SimpleNamespace:
        return SimpleNamespace(decision=self.decisions.pop(0), reason="fixture decision")


@pytest.mark.asyncio
async def test_policy_deny_cannot_be_bypassed_by_a_successor(tmp_path: Path) -> None:
    """候选 A 的 Harness policy deny 是全局终止，不创建 attempt/client 或调用 B。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        policy_engine=_DenyPolicy(),
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await fixture.bound.complete(
                ModelRequest(prompt="hello", max_output_tokens=8),
                operation_key=fixture.operation_key,
            )

        assert exc_info.value.code == "model.policy_denied"
        assert fixture.provider.trace == []
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is None
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_successor_policy_deny_releases_proven_source_without_calling_successor(
    tmp_path: Path,
) -> None:
    """A 的 actual-zero proof 后 B 必须重新授权；deny 以唯一 terminal tuple 结算。"""

    policy = _AllowThenDenyPolicy()
    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        policy_engine=policy,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await fixture.bound.complete(
                ModelRequest(prompt="hello", max_output_tokens=8),
                operation_key=fixture.operation_key,
            )

        assert exc_info.value.code == "model.policy_denied"
        assert policy.calls == 2
        assert fixture.provider.trace == ["prepare:real_primary"]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.active_ordinal is None
        assert state.current_reservation.token_bound == 0
        assert [item.state for item in state.candidates] == ["not_started", "denied"]
        assert state.transitions[-1].state == "terminated"
        assert state.transitions[-1].reason == "policy_denied"
        assert state.transitions[-1].from_ordinal == 1
        assert state.transitions[-1].to_ordinal is None
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_balance_ineligible_successor_is_skipped_without_attempt_or_transition(
    tmp_path: Path,
) -> None:
    """A 释放后 B 上界超过当前余额时，B 零副作用跳过并由 A 直达 C。"""

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
        assert second_bound > first_bound
        assert third_bound == first_bound
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

        response = await fixture.bound.complete(
            ModelRequest(prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        assert response.output_text == "completed:real_tertiary"
        assert fixture.provider.trace == [
            "prepare:real_primary",
            "prepare:real_tertiary",
            "send:real_tertiary",
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
        assert state.candidates[1].reason == "balance"
        assert state.candidates[1].not_started_proofs == ()
        assert [item.candidate_ordinal for item in state.attempt_lifecycle] == [1, 3]
        assert state.transitions[-1].from_ordinal == 1
        assert state.transitions[-1].to_ordinal == 3
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_initial_balance_ineligible_candidate_is_skipped_before_any_attempt(
    tmp_path: Path,
) -> None:
    """首候选上界超过余额时不得创建 attempt；初始 transition 直接激活 B。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed", "completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        first_bound, second_bound = fixture.candidate_token_bounds
        assert first_bound > second_bound
        async with fixture.storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
            assert ledger is not None
            remaining = (first_bound + second_bound) // 2
            await uow.session.execute(
                update(ParentBudgetLedgerModel)
                .where(
                    ParentBudgetLedgerModel.tenant_id == "tenant-a",
                    ParentBudgetLedgerModel.budget_owner_run_id == fixture.run_id,
                )
                .values(token_impact=ledger.token_limit - remaining)
            )
            await uow.commit()

        response = await fixture.bound.complete(
            ModelRequest(prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        assert response.output_text == "completed:real_secondary"
        assert fixture.provider.trace == [
            "prepare:real_secondary",
            "send:real_secondary",
        ]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert [item.state for item in state.candidates] == [
            "budget_ineligible",
            "completed",
        ]
        assert state.candidates[0].reason == "balance"
        assert [item.candidate_ordinal for item in state.attempt_lifecycle] == [2]
        assert state.transitions[0].from_ordinal is None
        assert state.transitions[0].to_ordinal == 2
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_initial_balance_scan_exhaustion_is_zero_impact_and_provider_free(
    tmp_path: Path,
) -> None:
    """初始扫描全部余额不足时直接耗尽，禁止 attempt、provider 与残留 reservation。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        async with fixture.storage.uow() as uow:
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

        with pytest.raises(ModelProviderInvocationError) as captured:
            await fixture.bound.complete(
                ModelRequest(prompt="hello", max_output_tokens=8),
                operation_key=fixture.operation_key,
            )

        assert captured.value.code == "model.route_chain_exhausted"
        assert captured.value.detail is not None
        assert [item.cause for item in captured.value.detail.causes] == [
            "balance",
            "balance",
        ]
        assert fixture.provider.trace == []
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
        assert state is not None
        assert ledger is not None
        assert [item.state for item in state.candidates] == [
            "budget_ineligible",
            "budget_ineligible",
        ]
        assert all(item.reason == "balance" for item in state.candidates)
        assert state.attempt_lifecycle == ()
        assert state.current_reservation.token_bound == 0
        assert ledger.token_impact == ledger.token_limit
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_decision", "expected"),
    [("require_approval", "approval"), ("deny", "model.policy_denied")],
)
async def test_initial_balance_skip_reauthorizes_the_next_candidate(
    tmp_path: Path,
    second_decision: str,
    expected: str,
) -> None:
    """A balance skip 后 B 必须重新授权，并保持无 attempt、无伪 source transition。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed", "completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        policy_engine=_SequencePolicy(["allow", second_decision]),
    )
    try:
        first_bound, second_bound = fixture.candidate_token_bounds
        async with fixture.storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
            assert ledger is not None
            remaining = (first_bound + second_bound) // 2
            await uow.session.execute(
                update(ParentBudgetLedgerModel)
                .where(
                    ParentBudgetLedgerModel.tenant_id == "tenant-a",
                    ParentBudgetLedgerModel.budget_owner_run_id == fixture.run_id,
                )
                .values(token_impact=ledger.token_limit - remaining)
            )
            await uow.commit()

        if expected == "approval":
            with pytest.raises(ModelApprovalRequired):
                await fixture.bound.complete(
                    ModelRequest(prompt="hello", max_output_tokens=8),
                    operation_key=fixture.operation_key,
                )
        else:
            with pytest.raises(ModelProviderInvocationError) as captured:
                await fixture.bound.complete(
                    ModelRequest(prompt="hello", max_output_tokens=8),
                    operation_key=fixture.operation_key,
                )
            assert captured.value.code == expected

        assert fixture.provider.trace == []
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.candidates[0].state == "budget_ineligible"
        assert state.candidates[0].reason == "balance"
        assert state.attempt_lifecycle == ()
        if expected == "approval":
            assert state.waiting_approval_ordinal == 2
            assert state.transitions[0].from_ordinal is None
            assert state.transitions[0].to_ordinal == 2
        else:
            assert state.candidates[1].state == "denied"
            assert state.transitions == ()
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_initial_soft_budget_candidate_is_skipped_without_policy_or_provider(
    tmp_path: Path,
) -> None:
    """候选级 soft threshold 只跳过 A，不得把整条 chain 误转成 approval。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        soft_token_limits={ROUTE_A["model_id"]: 1, ROUTE_B["model_id"]: 4096},
    )
    try:
        response = await fixture.bound.complete(
            ModelRequest(prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        assert response.output_text == "completed:real_secondary"
        assert fixture.provider.trace == [
            "prepare:real_secondary",
            "send:real_secondary",
        ]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.candidates[0].state == "budget_ineligible"
        assert state.candidates[0].reason == "soft_budget"
        assert [item.candidate_ordinal for item in state.attempt_lifecycle] == [2]
        assert state.transitions[0].from_ordinal is None
        assert state.transitions[0].to_ordinal == 2
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("static_case", ["capability", "input_bound", "hard_budget"])
async def test_initial_static_ineligible_candidate_is_skipped_before_policy_and_provider(
    tmp_path: Path,
    static_case: str,
) -> None:
    """A 的动态 hard eligibility 失败只关闭 A；B 仍可按冻结顺序执行。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
        first_capabilities=["text_stream"] if static_case == "capability" else None,
        first_max_output_tokens=4 if static_case == "input_bound" else None,
        max_attempts_by_deployment=(
            {ROUTE_A["deployment_id"]: 400} if static_case == "hard_budget" else None
        ),
    )
    try:
        response = await fixture.bound.complete(
            ModelRequest(prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        assert response.output_text == "completed:real_secondary"
        assert fixture.provider.trace == [
            "prepare:real_secondary",
            "send:real_secondary",
        ]
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
        assert state is not None
        assert state.candidates[0].state == "static_ineligible"
        assert state.candidates[0].reason == "static_ineligible"
        assert state.candidates[0].not_started_proofs == ()
        assert [item.candidate_ordinal for item in state.attempt_lifecycle] == [2]
        assert state.transitions[0].from_ordinal is None
        assert state.transitions[0].to_ordinal == 2
    finally:
        await fixture.storage.dispose()
