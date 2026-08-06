"""模型工具循环deadline与terminal owner证据的公开合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contracts.test_policy_gated_model_tool_loop_event_contracts import (
    _event_loop_fixture,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.models.tool_intent import tool_loop_identity_digest
from agent_harness.runtime import ModelToolLoopError, ModelToolLoopLimitOverrides
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


@pytest.mark.asyncio
async def test_durable_deadline_persists_limit_terminal_instead_of_replay_conflict(
    tmp_path: Path,
) -> None:
    """wall clock跨过deadline后仍由原owner写稳定failed，不能留下active循环。"""

    (
        storage,
        _sink,
        provider,
        bound,
        request,
        run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path, model_delay_seconds=1.1)
    operation_key = "durable-deadline-terminal"
    loop_id = tool_loop_identity_digest(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
        operation_key=operation_key,
    )
    try:
        with pytest.raises(ModelToolLoopError) as failure:
            await bound.run(
                request,
                operation_key=operation_key,
                limits=ModelToolLoopLimitOverrides(
                    max_turns=None,
                    max_total_tokens=None,
                    max_total_cost_usd=None,
                    max_tool_output_bytes=None,
                    max_duration_seconds=1,
                ),
            )

        assert failure.value.code == "model.tool_loop_limit_exceeded"
        assert provider.send_count == 1
        assert handler_count() == 0
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.get("tenant-a", loop_id)
        assert loop is not None
        assert loop.status == "failed"
        assert loop.error_ref == "error:model.tool_loop_limit_exceeded"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_pending_owner_evidence_blocks_public_loop_completion(tmp_path: Path) -> None:
    """任一未决owner outbox与容量预约都必须在completed CAS前阻断公开loop。"""

    (
        storage,
        _sink,
        provider,
        bound,
        request,
        run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path)
    operation_key = "terminal-prerequisite"
    loop_id = tool_loop_identity_digest(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
        operation_key=operation_key,
    )
    try:
        async with storage.uow() as uow:
            await uow.event_capacity.reserve(
                run_id=run_id,
                operation_kind=EvidenceOperationKind.CONTEXT_ASSEMBLY,
            )
            await uow.evidence_outbox.stage_reserved_group(
                tenant_id="tenant-a",
                run_id=run_id,
                group_id="terminal-prerequisite-pending",
                items=(
                    {
                        "event_id": "terminal-prerequisite-pending:started",
                        "operation_kind": EvidenceOperationKind.CONTEXT_ASSEMBLY.value,
                        "sequence_in_group": 1,
                        "reserved_event_count": 1,
                    },
                    {
                        "event_id": "terminal-prerequisite-pending:final",
                        "operation_kind": EvidenceOperationKind.CONTEXT_ASSEMBLY.value,
                        "sequence_in_group": 2,
                        "reserved_event_count": 1,
                    },
                ),
            )
            await uow.commit()
        provider.final_text = True

        with pytest.raises(ModelToolLoopError) as failure:
            await bound.run(request, operation_key=operation_key)

        assert failure.value.code == "model.tool_loop_needs_review"
        assert provider.send_count == 0
        assert handler_count() == 0
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.get("tenant-a", loop_id)
        assert loop is not None and loop.status == "needs_review"
    finally:
        await storage.dispose()
