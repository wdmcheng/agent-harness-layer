"""取消、预算、审批、工具与模型终态竞争的唯一提交合同。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.contracts.model_tool_loop_contract_helpers import (
    initial_model_tool_loop_snapshot,
)

from agent_harness.storage import (
    ModelToolInvocationClaimCreate,
    ModelToolLoopCreate,
    ModelToolLoopRecord,
    ModelToolLoopStorageConflict,
    SQLAlchemyStorage,
    run_migrations,
)
from agent_harness.storage.repositories import (
    ContextAssemblyCreate,
    RunCreate,
    SessionCreate,
)


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _loop(storage: SQLAlchemyStorage, *, loop_id: str) -> ModelToolLoopRecord:
    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        session = await uow.sessions.create(
            SessionCreate(tenant_id="tenant-a", user_id="user-a", agent_id="agent-a")
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=session.id,
                agent_id="agent-a",
                trace_id=f"trace-{session.id}",
            )
        )
        loop = await uow.model_tool_loops.create(
            ModelToolLoopCreate(
                tenant_id="tenant-a",
                run_id=run.id,
                agent_id="agent-a",
                loop_id=loop_id,
                request_identity_digest="b" * 64,
                operation_identity_digest="c" * 64,
                catalog_digest="d" * 64,
                **initial_model_tool_loop_snapshot(),
                owner_lease_digest="e" * 64,
                owner_fence=1,
                owner_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
        await uow.commit()
    return loop


def _waiting_state() -> dict[str, object]:
    return {
        "schema_version": "model-tool-loop-state-v1",
        "next_step": "approval_resume",
        "model_usage_call_id": "usage-a",
        "tool_call_id": "tool-a",
        "approval_id": None,
        "checkpoint_ref": "artifact://checkpoint-a",
        "context_ref": None,
        "next_request_digest": None,
    }


async def _settle_model_turn(
    storage: SQLAlchemyStorage,
    loop: ModelToolLoopRecord,
) -> ModelToolLoopRecord:
    """终态竞争前先耐久当前model actual，避免跳过usage owner。"""

    async with storage.uow() as uow:
        settled = await uow.model_tool_loops.settle_model_turn(
            tenant_id="tenant-a",
            loop_id=loop.loop_id,
            expected_version=loop.version,
            owner_lease_digest=loop.owner_lease_digest,
            owner_fence=loop.owner_fence,
            cumulative_usage={
                "schema_version": "model-tool-loop-cumulative-usage-v1",
                "turns_completed": loop.next_turn_ordinal,
                "total_tokens_used": 1,
                "total_cost_usd": 0.0,
            },
            state={
                "schema_version": "model-tool-loop-state-v1",
                "next_step": "model_result",
                "model_usage_call_id": "usage-a",
                "tool_call_id": None,
                "approval_id": None,
                "checkpoint_ref": None,
                "context_ref": None,
                "next_request_digest": None,
            },
        )
        await uow.commit()
    return settled


@pytest.mark.asyncio
async def test_cancellation_fences_waiting_approval_resume(tmp_path: Path) -> None:
    """waiting审批与取消竞争只允许一个version winner，败方零副作用。"""

    dsn = _dsn(tmp_path / "terminal-waiting.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    effects: list[str] = []
    try:
        loop = await _loop(storage, loop_id="1" * 64)
        loop = await _settle_model_turn(storage, loop)
        async with storage.uow() as uow:
            waiting = await uow.model_tool_loops.wait_for_approval(
                tenant_id="tenant-a",
                loop_id=loop.loop_id,
                expected_version=2,
                owner_lease_digest=loop.owner_lease_digest,
                owner_fence=loop.owner_fence,
                state=_waiting_state(),
            )
            await uow.commit()

        async with storage.uow() as uow:
            cancelled = await uow.model_tool_loops.cancel(
                tenant_id="tenant-a",
                loop_id=loop.loop_id,
                expected_status="waiting_approval",
                expected_version=waiting.version,
                owner_lease_digest=loop.owner_lease_digest,
                owner_fence=loop.owner_fence,
                error_ref="error://cancelled",
            )
            await uow.commit()
        effects.append("cancel-terminal")
        assert cancelled.status == "cancelled"

        async with storage.uow() as uow:
            with pytest.raises(ModelToolLoopStorageConflict):
                await uow.model_tool_loops.resume_after_approval(
                    tenant_id="tenant-a",
                    loop_id=loop.loop_id,
                    expected_version=waiting.version,
                    owner_lease_digest=loop.owner_lease_digest,
                    owner_fence=loop.owner_fence,
                    state={
                        **_waiting_state(),
                        "next_step": "tool_execution",
                        "approval_id": "approval-a",
                    },
                )
        async with storage.uow() as uow:
            run = await uow.runs.get(loop.run_id)
        assert run is not None
        with pytest.raises(ModelToolLoopStorageConflict):
            async with storage.uow() as uow:
                await uow.tool_invocations.create_model_claim(
                    ModelToolInvocationClaimCreate(
                        tenant_id="tenant-a",
                        agent_id="agent-a",
                        run_id=loop.run_id,
                        tool_name="search",
                        args_ref="artifact://late-args",
                        arguments_hash="5" * 64,
                        trace_id=run.trace_id,
                        request_id="request-late",
                        loop_id=loop.loop_id,
                        turn_ordinal=1,
                        tool_call_id="6" * 64,
                        binding={"binding_digest": "7" * 64},
                        execution_lease_digest="8" * 64,
                        execution_fence=1,
                        execution_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                    )
                )
        with pytest.raises(ModelToolLoopStorageConflict):
            async with storage.uow() as uow:
                await uow.context_assemblies.create(
                    ContextAssemblyCreate(
                        tenant_id="tenant-a",
                        run_id=loop.run_id,
                        input_refs=["artifact://tool-result"],
                        token_budget=10,
                        trust_summary={"untrusted": 1},
                        truncation_summary={},
                        output_ref="artifact://late-context",
                        loop_id=loop.loop_id,
                        turn_ordinal=1,
                        tool_call_id="6" * 64,
                        input_identity_digest="9" * 64,
                        output_digest="a" * 64,
                    )
                )
        assert effects == ["cancel-terminal"]
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("winner", ["budget", "tool", "model"])
async def test_active_terminal_and_turn_competitors_have_one_winner(
    tmp_path: Path,
    winner: str,
) -> None:
    """预算终止、工具轮次提交和模型final共享同一version围栏。"""

    dsn = _dsn(tmp_path / f"terminal-{winner}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    effects: list[str] = []
    try:
        loop = await _loop(
            storage,
            loop_id={"budget": "2", "tool": "3", "model": "4"}[winner] * 64,
        )
        if winner in {"tool", "model"}:
            loop = await _settle_model_turn(storage, loop)
        if winner == "budget":
            async with storage.uow() as uow:
                await uow.model_tool_loops.fail(
                    tenant_id="tenant-a",
                    loop_id=loop.loop_id,
                    expected_version=1,
                    owner_lease_digest=loop.owner_lease_digest,
                    owner_fence=loop.owner_fence,
                    status="failed",
                    error_ref="error://budget-exhausted",
                )
                await uow.commit()
            effects.append("budget-terminal")
        elif winner == "tool":
            async with storage.uow() as uow:
                await uow.model_tool_loops.commit_turn(
                    tenant_id="tenant-a",
                    loop_id=loop.loop_id,
                    expected_version=2,
                    owner_lease_digest=loop.owner_lease_digest,
                    owner_fence=loop.owner_fence,
                    cumulative_usage={
                        "schema_version": "model-tool-loop-cumulative-usage-v1",
                        "turns_completed": 1,
                        "total_tokens_used": 1,
                        "total_cost_usd": 0.0,
                    },
                    state={
                        "schema_version": "model-tool-loop-state-v1",
                        "next_step": "model_turn",
                        "model_usage_call_id": "usage-a",
                        "tool_call_id": "tool-a",
                        "approval_id": None,
                        "checkpoint_ref": None,
                        "context_ref": "artifact://context-a",
                        "next_request_digest": "f" * 64,
                    },
                )
                await uow.commit()
            effects.append("tool-turn")
        else:
            async with storage.uow() as uow:
                await uow.model_tool_loops.terminate(
                    tenant_id="tenant-a",
                    loop_id=loop.loop_id,
                    expected_version=2,
                    owner_lease_digest=loop.owner_lease_digest,
                    owner_fence=loop.owner_fence,
                    status="completed",
                    result_ref="artifact://model-final",
                    error_ref=None,
                )
                await uow.commit()
            effects.append("model-terminal")

        async with storage.uow() as uow:
            with pytest.raises(ModelToolLoopStorageConflict):
                await uow.model_tool_loops.terminate(
                    tenant_id="tenant-a",
                    loop_id=loop.loop_id,
                    expected_version=1,
                    owner_lease_digest=loop.owner_lease_digest,
                    owner_fence=loop.owner_fence,
                    status="completed",
                    result_ref="artifact://late-final",
                    error_ref=None,
                )
        assert effects == [
            {"budget": "budget-terminal", "tool": "tool-turn", "model": "model-terminal"}[winner]
        ]
    finally:
        await storage.dispose()
