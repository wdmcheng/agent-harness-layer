"""模型策略、审计与既有 approval continuation 的组合合同。"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from tests.contracts.controlled_real_model_policy_approval_test_support import (
    policy_flow,
    reserve_competing_budget,
)

from agent_harness.approvals import ApprovalStateConflict
from agent_harness.models import (
    ModelInvocationService,
    ModelRequest,
    ModelRouter,
)
from agent_harness.runtime import (
    ApprovalGrant,
    InvalidRunTransition,
    RunStatus,
)


@pytest.mark.asyncio
async def test_model_policy_coordinates_and_audit_precede_reservation(tmp_path: Path) -> None:
    """exact model.invoke policy audit 必须在 settlement/provider 前完成。"""

    storage, _approval, orchestrator, _identity, provider, _executor = await policy_flow(
        tmp_path, require_approval=False
    )
    try:
        result = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        assert result.status == RunStatus.COMPLETED
        assert provider.calls == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_require_approval_creates_durable_checkpoint_with_zero_model_side_effects(
    tmp_path: Path,
) -> None:
    """require-approval 复用标准 checkpoint/ApprovalRecord，等待期 provider 为零。"""

    storage, approval, orchestrator, identity, provider, _executor = await policy_flow(
        tmp_path, require_approval=True
    )
    try:
        waiting = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        records = await approval.list_for_run(actor=identity, run_id=waiting.run_id)
        assert waiting.status == RunStatus.WAITING
        assert waiting.resume_token is not None
        assert len(records) == 1
        assert records[0].action == "model.invoke"
        assert records[0].metadata["continuation"]["kind"] == "policy_approval"
        assert provider.calls == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_bound_approval_grant_rechecks_hard_budget_and_invokes_provider_once(
    tmp_path: Path,
) -> None:
    """余额变化后 continuation 重查 hard budget：够用只发一次，不足则零副作用。"""

    storage, approval, orchestrator, identity, provider, executor = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem="approval-current-balance-pass",
    )
    try:
        waiting = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        assert (
            await reserve_competing_budget(
                storage,
                run_id=waiting.run_id,
                tokens=40,
                usage_call_id="usage-competing-pass",
            )
            == 40
        )
        record = (await approval.list_for_run(actor=identity, run_id=waiting.run_id))[0]
        resolved = await approval.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=record.approval_id,
            request_id="approve-once",
        )
        assert resolved.run is not None
        assert resolved.run.status == RunStatus.COMPLETED
        assert executor.resume_calls == 1
        assert provider.calls == 1
    finally:
        await storage.dispose()

    (
        exhausted_storage,
        exhausted_approval,
        exhausted_orchestrator,
        exhausted_identity,
        exhausted_provider,
        exhausted_executor,
    ) = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem="approval-current-balance-reject",
    )
    try:
        waiting = await exhausted_orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        assert (
            await reserve_competing_budget(
                exhausted_storage,
                run_id=waiting.run_id,
                tokens=55,
                usage_call_id="usage-competing-reject",
            )
            == 55
        )
        record = (
            await exhausted_approval.list_for_run(
                actor=exhausted_identity,
                run_id=waiting.run_id,
            )
        )[0]
        resolved = await exhausted_approval.approve(
            actor=exhausted_identity,
            run_id=waiting.run_id,
            approval_id=record.approval_id,
            request_id="approve-after-balance-exhausted",
        )
        assert resolved.run is not None
        assert resolved.run.status == RunStatus.FAILED
        assert exhausted_executor.resume_calls == 1
        assert exhausted_provider.calls == 0
    finally:
        await exhausted_storage.dispose()


@pytest.mark.asyncio
async def test_mismatched_stale_or_replayed_grant_fails_closed(tmp_path: Path) -> None:
    """已消费 lease 的 service replay 与公开 stale grant 都必须在 provider 前拒绝。"""

    storage, approval, orchestrator, identity, provider, _executor = await policy_flow(
        tmp_path, require_approval=True
    )
    try:
        waiting = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        record = (await approval.list_for_run(actor=identity, run_id=waiting.run_id))[0]
        assert waiting.resume_token is not None
        await approval.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=record.approval_id,
            request_id="approve-first",
        )
        with pytest.raises(ApprovalStateConflict):
            await approval.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=record.approval_id,
                request_id="approve-replay",
            )
        async with storage.uow() as uow:
            consumed = await uow.approvals.get_resolution(record.approval_id)
        assert consumed is not None
        stale_grant = ApprovalGrant(
            approval_id=record.approval_id,
            lease_id=consumed.lease_id,
            tenant_id=record.tenant_id,
            identity_id=identity.user_id,
            agent_id=record.agent_id,
            run_id=record.run_id,
            action=record.action,
            resource=record.resource,
            arguments_hash=str(record.metadata["arguments_hash"]),
        )
        with pytest.raises(InvalidRunTransition):
            await orchestrator.resume_run(
                waiting.resume_token,
                expected_run_id=waiting.run_id,
                identity=identity,
                approval_grant=stale_grant,
            )
        assert provider.calls == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("approval_id", "forged-approval-id"),
        ("lease_id", "forged-lease-id"),
    ],
)
async def test_forged_model_approval_identity_never_reaches_provider(
    tmp_path: Path,
    field: str,
    forged_value: str,
) -> None:
    """公开 resume seam 必须把 approval/lease 身份绑定到 durable resolution。"""

    storage, _approval, orchestrator, identity, provider, _executor = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem=f"forged-{field}",
    )
    try:
        waiting = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        assert waiting.resume_token is not None
        async with storage.uow() as uow:
            record = (await uow.approvals.list_by_run(waiting.run_id))[0]
            lease = await uow.approvals.claim_resolution(
                approval_id=record.approval_id,
                run_id=record.run_id,
                tenant_id=record.tenant_id,
                request_id=f"forged-{field}-claim",
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=record.approval_id,
            lease_id=lease.lease_id,
            tenant_id=record.tenant_id,
            identity_id=str(record.metadata["identity_id"]),
            agent_id=record.agent_id,
            run_id=record.run_id,
            action=record.action,
            resource=record.resource,
            arguments_hash=str(record.metadata["arguments_hash"]),
        ).model_copy(update={field: forged_value})

        with pytest.raises(InvalidRunTransition):
            await orchestrator.resume_run(
                waiting.resume_token,
                expected_run_id=waiting.run_id,
                identity=identity,
                approval_grant=grant,
                defer_terminal=True,
            )

        assert provider.calls == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["approval_id", "lease_id"])
async def test_bound_model_service_rejects_forged_durable_approval_identity(
    tmp_path: Path,
    field: str,
) -> None:
    """业务 executor 即使持有 bound service，也不能自行构造批准能力。"""

    storage, _approval, orchestrator, identity, provider, executor = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem=f"bound-forged-{field}",
    )
    try:
        waiting = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        assert executor.bound_model is not None
        async with storage.uow() as uow:
            record = (await uow.approvals.list_by_run(waiting.run_id))[0]
            lease = await uow.approvals.claim_resolution(
                approval_id=record.approval_id,
                run_id=record.run_id,
                tenant_id=record.tenant_id,
                request_id=f"bound-forged-{field}-claim",
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=record.approval_id,
            lease_id=lease.lease_id,
            tenant_id=record.tenant_id,
            identity_id=identity.user_id,
            agent_id=record.agent_id,
            run_id=record.run_id,
            action=record.action,
            resource=record.resource,
            arguments_hash=str(record.metadata["arguments_hash"]),
        ).model_copy(update={field: f"forged-{field}"})

        with pytest.raises(ValueError, match="approval grant"):
            await executor.bound_model.complete_approved(
                ModelRequest(provider="fake", prompt="需要审批", max_output_tokens=2),
                operation_key="primary-model-call",
                grant=grant,
            )

        assert provider.calls == 0
    finally:
        await storage.dispose()


def test_model_invocation_public_complete_has_no_boolean_approval_bypass() -> None:
    """调用方不能用普通 bool 绕过 Policy/HITL，批准只能来自全绑定 grant。"""

    assert "soft_approved" not in inspect.signature(ModelInvocationService.complete).parameters


def test_model_router_public_planning_has_no_boolean_approval_bypass() -> None:
    """公开 Router 只能冻结未审批计划，不能把调用方布尔值当作授权能力。"""

    assert "approved" not in inspect.signature(ModelRouter.plan).parameters
    assert "approved" not in inspect.signature(ModelRouter.plan_from_snapshot).parameters
