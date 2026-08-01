"""Route-chain approval checkpoint 损坏时的关闭失败合同。"""

from __future__ import annotations

from typing import Any

import pytest
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    bound_failover_invocation,
)

from agent_harness.identity import IdentityContext
from agent_harness.models import ModelApprovalRequired, ModelRequest
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import ApprovalGrant
from agent_harness.storage import CheckpointCreate
from agent_harness.storage.approval_records import ApprovalCreate


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["missing", "kind_only", "shape_tampered", "checkpoint_mismatch", "state_mismatch"],
)
async def test_chain_approval_checkpoint_corruption_fails_before_new_side_effects(
    tmp_path: Any,
    corruption: str,
) -> None:
    """损坏 continuation 不得 rekey、调用 provider 或新增预算影响。"""

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
        with pytest.raises(ModelApprovalRequired) as captured:
            await fixture.bound.complete(request, operation_key=fixture.operation_key)
        approval_request = captured.value.request
        original = dict(approval_request.continuation)
        metadata_continuation: object = original
        checkpoint_continuation: object = original
        if corruption == "missing":
            metadata_continuation = None
            checkpoint_continuation = None
        elif corruption == "kind_only":
            metadata_continuation = {"kind": "policy_approval"}
            checkpoint_continuation = {"kind": "policy_approval"}
        elif corruption == "shape_tampered":
            damaged = {key: value for key, value in original.items() if key != "usage_call_id"}
            metadata_continuation = damaged
            checkpoint_continuation = damaged
        elif corruption == "checkpoint_mismatch":
            checkpoint_continuation = {
                **original,
                "operation_identity_digest": "f" * 64,
            }
        else:
            mismatched = {**original, "operation_identity_digest": "f" * 64}
            metadata_continuation = mismatched
            checkpoint_continuation = mismatched

        identity = IdentityContext.local_default()
        resume_token = f"resume-{corruption}"
        async with fixture.storage.uow() as uow:
            await uow.checkpoints.create(
                CheckpointCreate(
                    tenant_id="tenant-a",
                    run_id=fixture.run_id,
                    sequence=1,
                    resume_token=resume_token,
                    state={
                        "kind": "agent_executor_approval",
                        "continuation": checkpoint_continuation,
                    },
                )
            )
            record = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id="tenant-a",
                    run_id=fixture.run_id,
                    agent_id="agent-a",
                    action=approval_request.action,
                    resource=approval_request.resource,
                    reason=approval_request.reason,
                    resume_token=resume_token,
                    requested_by=identity.user_id,
                    trace_id="trace-a",
                    request_id="request-a",
                    metadata={
                        "identity_id": identity.user_id,
                        "arguments_hash": approval_request.arguments_hash,
                        "continuation": metadata_continuation,
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
            agent_id="agent-a",
            run_id=fixture.run_id,
            action=approval_request.action,
            resource=approval_request.resource,
            arguments_hash=approval_request.arguments_hash,
        )

        with pytest.raises(ValueError):
            await fixture.bound.complete_approved(
                request,
                operation_key="caller-supplied-after-reload",
                grant=grant,
            )

        assert fixture.provider.trace == []
        async with fixture.storage.uow() as uow:
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                run_id=fixture.run_id,
                usage_call_id=fixture.usage_call_id,
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", fixture.run_id)
        assert state is not None and state.waiting_approval_ordinal == 1
        assert ledger is not None and ledger.token_impact == 0
    finally:
        await fixture.storage.dispose()
