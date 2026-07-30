"""可信 composition 与 durable stream approval public-seam 合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from tests.contracts.controlled_real_model_policy_approval_test_support import policy_flow

from agent_harness.approvals import ApprovalStateConflict
from agent_harness.models import (
    BoundModelInvocationService,
    FakeModelStreamScript,
    ModelRequest,
    ModelStreamCloseResult,
    ModelStreamUsage,
    ModelUsageEvidence,
    UsageEvidenceContext,
    UsageInvocationReplayError,
    stable_usage_call_id,
)
from agent_harness.runtime import ApprovalGrant, RunStatus
from agent_harness.storage.models import RunEvidenceOutboxModel
from agent_harness.storage.shared_budget_models import BudgetOperationClaimModel
from agent_harness.storage.stream_evidence_repositories import stream_group_id


@pytest.mark.asyncio
async def test_stream_approval_uses_bound_composition_and_one_stable_approved_slot(
    tmp_path: Path,
) -> None:
    """普通入口先零副作用等待；批准后只调用一次并固定 approval identity。"""

    storage, approval, orchestrator, identity, provider, executor = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem="stream-approval-public-seam",
        streaming=True,
    )
    try:
        waiting = await orchestrator.start_run(
            agent_id="agent-a",
            input={"prompt": "x"},
            request_id="stream-approval-request",
            trace_id="stream-approval-trace",
        )
        assert waiting.status is RunStatus.WAITING
        assert isinstance(executor.bound_model, BoundModelInvocationService)
        assert provider.calls == 0
        async with storage.uow() as uow:
            waiting_capacity = await uow.event_capacity.snapshot(waiting.run_id)
        assert waiting_capacity.outstanding_reserved_event_count == 0

        record = (await approval.list_for_run(actor=identity, run_id=waiting.run_id))[0]
        resolved = await approval.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=record.approval_id,
            request_id="approve-stream-once",
        )
        assert resolved.run is not None and resolved.run.status is RunStatus.COMPLETED
        assert executor.resume_calls == provider.calls == 1

        async with storage.uow() as uow:
            usage_row = await uow.session.scalar(
                select(RunEvidenceOutboxModel).where(
                    RunEvidenceOutboxModel.run_id == waiting.run_id,
                    RunEvidenceOutboxModel.operation_kind == "model_usage",
                )
            )
            assert usage_row is not None and usage_row.result_json is not None
            usage_call_id = cast(str, usage_row.usage_call_id)
            started = ModelUsageEvidence.model_validate(usage_row.result_json["started"])
        expected = stable_usage_call_id(
            context=UsageEvidenceContext(
                tenant_id=started.tenant_id,
                run_id=started.run_id,
                agent_id=started.agent_id,
                request_id=started.request_id,
                trace_id=started.trace_id,
            ),
            operation_key=f"approved:{record.approval_id}",
        )
        assert usage_call_id == expected

        with pytest.raises(ApprovalStateConflict):
            await approval.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=record.approval_id,
                request_id="approve-stream-replay",
            )
        assert provider.calls == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "close_result"),
    [
        (
            "unknown-partial-output",
            ModelStreamCloseResult(
                state="unknown",
                usage=ModelStreamUsage(
                    finality="partial",
                    input_tokens=2,
                    output_tokens=None,
                    cost_usd=None,
                    cost_status="unavailable",
                    latency_ms=5,
                ),
            ),
        ),
        (
            "stopped-complete-input",
            ModelStreamCloseResult(
                state="stopped",
                usage=ModelStreamUsage(
                    finality="complete",
                    input_tokens=None,
                    output_tokens=1,
                    cost_usd=None,
                    cost_status="unavailable",
                    latency_ms=5,
                ),
            ),
        ),
        (
            "stopped-complete-output",
            ModelStreamCloseResult(
                state="stopped",
                usage=ModelStreamUsage(
                    finality="complete",
                    input_tokens=2,
                    output_tokens=None,
                    cost_usd=None,
                    cost_status="unavailable",
                    latency_ms=5,
                ),
            ),
        ),
        (
            "stopped-complete-cost",
            ModelStreamCloseResult(
                state="stopped",
                usage=ModelStreamUsage(
                    finality="complete",
                    input_tokens=2,
                    output_tokens=1,
                    cost_usd=None,
                    cost_status="unavailable",
                    latency_ms=5,
                ),
            ),
        ),
    ],
)
async def test_untrusted_stream_usage_fences_usage_and_shared_budget_before_replay(
    tmp_path: Path,
    case_id: str,
    close_result: ModelStreamCloseResult,
) -> None:
    """不完整计量即使标记 complete 也同 UoW 围栏 usage、父账本与 exact replay。"""

    pause_gate = asyncio.Event()
    pull_started = asyncio.Event()
    script = FakeModelStreamScript(
        fragments=("partial",),
        pause_gate=pause_gate,
        pull_started=pull_started,
        close_result=close_result,
    )
    storage, _approval, orchestrator, _identity, provider, executor = await policy_flow(
        tmp_path,
        require_approval=False,
        database_stem=f"stream-untrusted-shared-budget-{case_id}",
        streaming=True,
        stream_script=script,
    )
    try:
        task = asyncio.create_task(
            orchestrator.start_run(
                agent_id="agent-a",
                input={"prompt": "x"},
                request_id="stream-partial-budget-request",
                trace_id="stream-partial-budget-trace",
            )
        )
        await asyncio.wait_for(pull_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(RuntimeError, match="pending evidence blocks terminal"):
            await task
        assert executor.bound_model is not None

        async with storage.uow() as uow:
            usage = await uow.session.scalar(
                select(RunEvidenceOutboxModel).where(
                    RunEvidenceOutboxModel.operation_kind == "model_usage",
                )
            )
            assert usage is not None and usage.result_json is not None
            run_id = usage.run_id
            usage_call_id = cast(str, usage.usage_call_id)
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == usage_call_id
                )
            )
            group = await uow.evidence_outbox.ordered_group(group_id=stream_group_id(usage_call_id))
            ledger = await uow.shared_budget.get_ledger("default", run_id)
            capacity = await uow.event_capacity.snapshot(run_id)
            run = await uow.runs.get(run_id)
            usage_state = usage.state
            usage_review = usage.result_json["attempt_review"]
            claim_state = claim.state if claim is not None else None
            claim_side_effect_state = claim.side_effect_state if claim is not None else None
            claim_result = claim.result_json if claim is not None else None
            run_status = run.status if run is not None else None
            ledger_state = ledger.state if ledger is not None else None
            group_states = [item.state for item in group]
            outstanding = capacity.outstanding_reserved_event_count

        assert claim is not None and ledger is not None and run is not None
        assert run_status == RunStatus.RUNNING.value
        assert usage_state == claim_state == ledger_state == "needs_review"
        assert claim_side_effect_state == "result_committed"
        assert claim_result == {"attempt_review": usage_review}
        assert group_states == ["started"] * 65
        assert outstanding == 66

        with pytest.raises(UsageInvocationReplayError, match="needs_review"):
            await executor.bound_model.stream(
                ModelRequest(
                    provider="fake",
                    capability="text_stream",
                    prompt="需要审批",
                    max_output_tokens=2,
                ),
                operation_key="primary-model-call",
            )
        assert provider.calls == 1
    finally:
        pause_gate.set()
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("approval_id", "forged-approval"),
        ("lease_id", "forged-lease"),
        ("tenant_id", "forged-tenant"),
        ("identity_id", "forged-user"),
        ("agent_id", "forged-agent"),
        ("run_id", "forged-run"),
        ("action", "forged.action"),
        ("resource", "forged:resource"),
        ("arguments_hash", "0" * 64),
    ],
)
async def test_stream_approved_rejects_every_forged_grant_field_before_side_effects(
    tmp_path: Path,
    field: str,
    forged: str,
) -> None:
    """业务 executor 持有 bound façade 也不能伪造或移植 durable grant。"""

    storage, _approval, orchestrator, identity, provider, executor = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem=f"stream-forged-{field}",
        streaming=True,
    )
    try:
        waiting = await orchestrator.start_run(
            agent_id="agent-a",
            input={"prompt": "x"},
            request_id=f"stream-forged-{field}",
            trace_id=f"stream-forged-{field}",
        )
        assert executor.bound_model is not None
        async with storage.uow() as uow:
            record = (await uow.approvals.list_by_run(waiting.run_id))[0]
            lease = await uow.approvals.claim_resolution(
                approval_id=record.approval_id,
                run_id=record.run_id,
                tenant_id=record.tenant_id,
                request_id=f"stream-forged-{field}-claim",
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
        ).model_copy(update={field: forged})

        with pytest.raises(ValueError, match="approval grant"):
            await executor.bound_model.stream_approved(
                ModelRequest(
                    provider="fake",
                    capability="text_stream",
                    prompt="需要审批",
                    max_output_tokens=2,
                ),
                operation_key="business-cannot-expand-approval",
                grant=cast(Any, grant),
            )

        async with storage.uow() as uow:
            capacity = await uow.event_capacity.snapshot(waiting.run_id)
        assert provider.calls == 0
        assert capacity.outstanding_reserved_event_count == 0
    finally:
        await storage.dispose()
