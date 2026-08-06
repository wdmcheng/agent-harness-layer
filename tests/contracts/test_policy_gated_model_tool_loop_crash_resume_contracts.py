"""模型工具循环 crash replay 与 active resume 事件合同。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.model_tool_loop_contract_helpers import initial_model_tool_loop_snapshot
from tests.contracts.test_policy_gated_model_tool_loop_event_contracts import (
    _event_loop_fixture,
)
from tests.contracts.test_tool_intent_model_catalog_config_contracts import _tool_catalog

from agent_harness.artifacts import FileArtifactStore
from agent_harness.context import ContextAssemblyService, ContextFragment
from agent_harness.events import CanonicalEventType
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ToolIntentTurnResult,
    UsageEvidenceContext,
    stable_usage_call_id,
    structured_digest,
)
from agent_harness.models.route_chain_identity import model_route_operation_identity_digest
from agent_harness.models.tool_intent import tool_loop_identity_digest
from agent_harness.storage import ModelToolLoopCreate
from agent_harness.tools import (
    ToolRuntimeContext,
)


@pytest.mark.asyncio
async def test_model_result_crash_replay_does_not_double_account_loop_usage(
    tmp_path: Path,
) -> None:
    """model actual已投影后崩溃，exact replay只能解释结果，不能再次累计同一轮。"""

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
        model_turns,
    ) = await _event_loop_fixture(tmp_path)
    operation_key = "model-result-crash-recovery"
    loop_id = tool_loop_identity_digest(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
        operation_key=operation_key,
    )
    usage_context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    first_usage_call_id = stable_usage_call_id(
        context=usage_context,
        operation_key=f"{operation_key}:model-turn:1",
    )
    catalog = _tool_catalog()
    try:
        async with storage.uow() as uow:
            await uow.model_tool_loops.create(
                ModelToolLoopCreate(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    loop_id=loop_id,
                    request_identity_digest=structured_digest(request.to_payload()),
                    operation_identity_digest=model_route_operation_identity_digest(
                        tenant_id="tenant-a",
                        run_id=run_id,
                        agent_id="agent-a",
                        request_id="request-a",
                        trace_id="trace-a",
                        operation_key=operation_key,
                    ),
                    catalog_digest=catalog.catalog_digest,
                    **initial_model_tool_loop_snapshot(started_at=datetime.now(UTC)),
                    owner_lease_digest="e" * 64,
                    owner_fence=1,
                    owner_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                )
            )
            await uow.commit()

        first_result = await model_turns.complete_tool_loop_turn(
            request,
            context=usage_context,
            usage_call_id=first_usage_call_id,
            loop_id=loop_id,
            turn_ordinal=1,
            operation_identity_digest=model_route_operation_identity_digest(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                request_id="request-a",
                trace_id="trace-a",
                operation_key=f"{operation_key}:model-turn:1",
            ),
            tool_catalog=catalog,
            actor=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
                roles=["member"],
            ),
            loop_token_bound=4096,
            loop_cost_bound=1.0,
        )
        assert isinstance(first_result, ToolIntentTurnResult)
        first_usage = await model_turns.read_tool_loop_turn_usage(
            context=usage_context,
            usage_call_id=first_usage_call_id,
            loop_id=loop_id,
            turn_ordinal=1,
        )
        assert first_usage.input_tokens is not None
        assert first_usage.output_tokens is not None
        first_tokens = first_usage.input_tokens + first_usage.output_tokens
        async with storage.uow() as uow:
            active = await uow.model_tool_loops.get("tenant-a", loop_id)
            assert active is not None
            await uow.model_tool_loops.settle_model_turn(
                tenant_id="tenant-a",
                loop_id=loop_id,
                expected_version=active.version,
                owner_lease_digest=active.owner_lease_digest,
                owner_fence=active.owner_fence,
                cumulative_usage={
                    "schema_version": "model-tool-loop-cumulative-usage-v1",
                    "turns_completed": 1,
                    "total_tokens_used": first_tokens,
                    "total_cost_usd": first_usage.cost_usd,
                },
                state={
                    "schema_version": "model-tool-loop-state-v1",
                    "next_step": "model_result",
                    "model_usage_call_id": first_usage_call_id,
                    "tool_call_id": None,
                    "approval_id": None,
                    "checkpoint_ref": None,
                    "context_ref": None,
                    "next_request_digest": None,
                },
            )
            await uow.commit()

        recovered = await bound.run(request, operation_key=operation_key)

        assert recovered.output_text == "done"
        assert provider.send_count == 2
        assert handler_count() == 1
        second_usage_call_id = stable_usage_call_id(
            context=usage_context,
            operation_key=f"{operation_key}:model-turn:2",
        )
        second_usage = await model_turns.read_tool_loop_turn_usage(
            context=usage_context,
            usage_call_id=second_usage_call_id,
            loop_id=loop_id,
            turn_ordinal=2,
        )
        assert second_usage.input_tokens is not None
        assert second_usage.output_tokens is not None
        async with storage.uow() as uow:
            completed = await uow.model_tool_loops.get("tenant-a", loop_id)
        assert completed is not None and completed.status == "completed"
        assert completed.cumulative_usage.turns_completed == 2
        assert completed.cumulative_usage.total_tokens_used == (
            first_tokens + second_usage.input_tokens + second_usage.output_tokens
        )
        expected_cost = (first_usage.cost_usd or 0.0) + (second_usage.cost_usd or 0.0)
        assert completed.cumulative_usage.total_cost_usd == expected_cost
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_active_loop_resumes_from_durable_next_turn_without_reexecuting_tool(
    tmp_path: Path,
) -> None:
    """公开run从已闭合的turn 1恢复turn 2，并沿用累计usage与冻结deadline。"""

    (
        storage,
        sink,
        provider,
        bound,
        request,
        run_id,
        handler_count,
        registry,
        loop_events,
        model_turns,
    ) = await _event_loop_fixture(tmp_path)
    operation_key = "active-turn-recovery"
    loop_id = tool_loop_identity_digest(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
        operation_key=operation_key,
    )
    usage_context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    usage_call_id = stable_usage_call_id(
        context=usage_context,
        operation_key=f"{operation_key}:model-turn:1",
    )
    catalog = _tool_catalog()
    started_at = datetime.now(UTC)
    try:
        async with storage.uow() as uow:
            await uow.model_tool_loops.create(
                ModelToolLoopCreate(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    loop_id=loop_id,
                    request_identity_digest=structured_digest(request.to_payload()),
                    operation_identity_digest=model_route_operation_identity_digest(
                        tenant_id="tenant-a",
                        run_id=run_id,
                        agent_id="agent-a",
                        request_id="request-a",
                        trace_id="trace-a",
                        operation_key=operation_key,
                    ),
                    catalog_digest=catalog.catalog_digest,
                    **initial_model_tool_loop_snapshot(started_at=started_at),
                    owner_lease_digest="d" * 64,
                    owner_fence=1,
                    owner_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                )
            )
            await uow.commit()

        raw_turn = await model_turns.complete_tool_loop_turn(
            request,
            context=usage_context,
            usage_call_id=usage_call_id,
            loop_id=loop_id,
            turn_ordinal=1,
            operation_identity_digest=model_route_operation_identity_digest(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                request_id="request-a",
                trace_id="trace-a",
                operation_key=f"{operation_key}:model-turn:1",
            ),
            tool_catalog=catalog,
            actor=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
                roles=["member"],
            ),
            loop_token_bound=4096,
            loop_cost_bound=1.0,
        )
        assert isinstance(raw_turn, ToolIntentTurnResult)
        intent = raw_turn.intent
        resolved = registry.resolve_intent(intent, catalog=catalog)
        tool_result = await registry.call(
            resolved,
            context=ToolRuntimeContext(
                actor=IdentityContext(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    session_id="session-a",
                    roles=["member"],
                ),
                agent_id="agent-a",
                run_id=run_id,
                request_id="request-a",
                trace_id="trace-a",
            ),
            intent=intent,
            catalog=catalog,
            events=loop_events,
        )
        content = json.dumps(
            tool_result.result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fragment = ContextFragment(
            source_ref=tool_result.source_ref,
            trust_level="untrusted",
            content=content,
            token_estimate=max(1, (len(content.encode("utf-8")) + 3) // 4),
            kind="tool_result",
            artifact_ref=tool_result.artifact_ref,
            truncation=tool_result.truncation,
            injection_summary=cast(list[str], tool_result.truncation["prompt_injection_signals"]),
        )
        context_step = await loop_events.begin_context(
            context=usage_context,
            identity_id="user-a",
            intent=intent,
            fragment=fragment,
        )
        assembly = await ContextAssemblyService(
            storage=storage,
            artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        ).assemble(
            tenant_id="tenant-a",
            run_id=run_id,
            fragments=[fragment],
            token_budget=request.max_output_tokens,
            loop_id=loop_id,
            turn_ordinal=1,
            tool_call_id=intent.tool_call_id,
        )
        await loop_events.finish_context(step=context_step, result=assembly)
        next_request = request.model_copy(
            update={
                "prompt": json.dumps(
                    {
                        "schema_version": "model-tool-loop-next-turn-v1",
                        "original_prompt": request.prompt,
                        "context_assembly": {
                            "output_ref": assembly.output_ref,
                            "input_refs": assembly.input_refs,
                            "trust_level": "untrusted",
                            "trust_summary": assembly.trust_summary,
                            "injection_summary": [
                                signal
                                for retained in assembly.retained_fragments
                                for signal in (retained.injection_summary or [])
                            ],
                            "assembled_text": assembly.assembled_text,
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            }
        )
        usage = await model_turns.read_tool_loop_turn_usage(
            context=usage_context,
            usage_call_id=usage_call_id,
            loop_id=loop_id,
            turn_ordinal=1,
        )
        assert usage.input_tokens is not None
        assert usage.output_tokens is not None
        async with storage.uow() as uow:
            existing = await uow.model_tool_loops.get("tenant-a", loop_id)
            assert existing is not None
            settled = await uow.model_tool_loops.settle_model_turn(
                tenant_id="tenant-a",
                loop_id=loop_id,
                expected_version=existing.version,
                owner_lease_digest=existing.owner_lease_digest,
                owner_fence=existing.owner_fence,
                cumulative_usage={
                    "schema_version": "model-tool-loop-cumulative-usage-v1",
                    "turns_completed": 1,
                    "total_tokens_used": usage.input_tokens + usage.output_tokens,
                    "total_cost_usd": usage.cost_usd,
                },
                state={
                    "schema_version": "model-tool-loop-state-v1",
                    "next_step": "model_result",
                    "model_usage_call_id": usage_call_id,
                    "tool_call_id": None,
                    "approval_id": None,
                    "checkpoint_ref": None,
                    "context_ref": None,
                    "next_request_digest": None,
                },
            )
            await uow.commit()
        async with storage.uow() as uow:
            await uow.model_tool_loops.commit_turn(
                tenant_id="tenant-a",
                loop_id=loop_id,
                expected_version=settled.version,
                owner_lease_digest=settled.owner_lease_digest,
                owner_fence=settled.owner_fence,
                cumulative_usage={
                    "schema_version": "model-tool-loop-cumulative-usage-v1",
                    "turns_completed": 1,
                    "total_tokens_used": usage.input_tokens + usage.output_tokens,
                    "total_cost_usd": usage.cost_usd,
                },
                state={
                    "schema_version": "model-tool-loop-state-v1",
                    "next_step": "model_turn",
                    "model_usage_call_id": usage_call_id,
                    "tool_call_id": intent.tool_call_id,
                    "approval_id": None,
                    "checkpoint_ref": None,
                    "context_ref": assembly.output_ref,
                    "next_request_digest": structured_digest(next_request.to_payload()),
                },
            )
            await uow.commit()

        recovered = await bound.run(request, operation_key=operation_key)

        assert recovered.output_text == "done"
        assert provider.send_count == 2
        assert handler_count() == 1
        model_started = [
            event
            for event in await sink.read(run_id=run_id)
            if event.event_type == CanonicalEventType.MODEL_REQUEST_STARTED
        ]
        assert [
            cast(dict[str, Any], event.payload)["correlation"]["turn_ordinal"]
            for event in model_started
        ] == [1, 2]
    finally:
        await storage.dispose()
