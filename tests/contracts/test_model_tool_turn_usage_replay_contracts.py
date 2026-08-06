"""模型工具循环按 loop/turn 身份重放既有 usage settlement 的合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from tests.contracts.test_tool_intent_model_catalog_config_contracts import (
    _tool_catalog,  # pyright: ignore[reportPrivateUsage]
)
from tests.contracts.test_tool_intent_usage_settlement_contracts import (
    _fixture,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.identity import IdentityContext
from agent_harness.models import (
    model_route_operation_identity_digest,
    stable_usage_call_id,
)
from agent_harness.models.usage import UsageEvidenceContext, UsageInvocationReplayError


@pytest.mark.asyncio
async def test_loop_turn_usage_exact_replay_is_single_settlement_with_bound_identity(
    tmp_path: Path,
) -> None:
    """崩溃重放只能复用同一 loop/turn settlement，不能重发或另建用量账本。"""

    storage, _sink, provider, service, _bound, request, run_id = await _fixture(tmp_path)
    context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    loop_id = "a" * 64
    turn_ordinal = 1
    operation_key = f"model-tool-loop:{loop_id}:turn:{turn_ordinal}"
    usage_call_id = stable_usage_call_id(context=context, operation_key=operation_key)
    operation_digest = model_route_operation_identity_digest(
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        agent_id=context.agent_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        operation_key=operation_key,
    )
    actor = IdentityContext(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        roles=["member"],
    )

    try:
        first = await service.complete_tool_loop_turn(
            request,
            context=context,
            usage_call_id=usage_call_id,
            loop_id=loop_id,
            turn_ordinal=turn_ordinal,
            operation_identity_digest=operation_digest,
            tool_catalog=_tool_catalog(),
            actor=actor,
            loop_token_bound=10_000,
            loop_cost_bound=1.0,
        )
        replay = await service.complete_tool_loop_turn(
            request,
            context=context,
            usage_call_id=usage_call_id,
            loop_id=loop_id,
            turn_ordinal=turn_ordinal,
            operation_identity_digest=operation_digest,
            tool_catalog=_tool_catalog(),
            actor=actor,
            loop_token_bound=10_000,
            loop_cost_bound=1.0,
        )
        usage = await service.read_tool_loop_turn_usage(
            context=context,
            usage_call_id=usage_call_id,
            loop_id=loop_id,
            turn_ordinal=turn_ordinal,
        )
        usage_replay = await service.read_tool_loop_turn_usage(
            context=context,
            usage_call_id=usage_call_id,
            loop_id=loop_id,
            turn_ordinal=turn_ordinal,
        )

        assert first == replay
        assert usage == usage_replay
        assert (usage.input_tokens, usage.output_tokens, usage.cost_usd) == (7, 3, 0.0001)
        assert provider.prepare_count == provider.send_count == 1

        async with storage.engine.connect() as connection:
            table_names = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_table_names()
            )
            settlement_count = await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM run_evidence_outbox "
                    "WHERE tenant_id = :tenant_id AND usage_call_id = :usage_call_id"
                ),
                {"tenant_id": context.tenant_id, "usage_call_id": usage_call_id},
            )
        assert settlement_count == 1
        assert "run_evidence_outbox" in table_names
        assert not any("tool_loop_usage" in table_name for table_name in table_names)

        for wrong_loop_id, wrong_turn_ordinal in (
            ("b" * 64, turn_ordinal),
            (loop_id, turn_ordinal + 1),
        ):
            with pytest.raises(UsageInvocationReplayError):
                await service.read_tool_loop_turn_usage(
                    context=context,
                    usage_call_id=usage_call_id,
                    loop_id=wrong_loop_id,
                    turn_ordinal=wrong_turn_ordinal,
                )
        assert provider.prepare_count == provider.send_count == 1
    finally:
        await storage.dispose()
