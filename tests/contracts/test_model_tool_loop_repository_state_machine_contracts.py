"""ModelToolLoopRepository的创建、转换、租约与version CAS合同。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from tests.contracts.model_tool_loop_contract_helpers import (
    initial_model_tool_loop_snapshot,
)

from agent_harness.storage import (
    ModelToolLoopCreate,
    ModelToolLoopRecord,
    ModelToolLoopStorageConflict,
    SQLAlchemyStorage,
    run_migrations,
)
from agent_harness.storage.repositories import RunCreate, SessionCreate


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _run_id(storage: SQLAlchemyStorage) -> str:
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
        await uow.commit()
    return run.id


def _create_data(run_id: str, *, loop_id: str = "a" * 64) -> ModelToolLoopCreate:
    started_at = datetime(2030, 1, 1, tzinfo=UTC)
    return ModelToolLoopCreate(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        loop_id=loop_id,
        request_identity_digest="b" * 64,
        operation_identity_digest="c" * 64,
        catalog_digest="d" * 64,
        **initial_model_tool_loop_snapshot(started_at=started_at),
        owner_lease_digest="e" * 64,
        owner_fence=1,
        owner_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )


def _usage(turns: int, *, tokens: int = 7) -> dict[str, object]:
    return {
        "schema_version": "model-tool-loop-cumulative-usage-v1",
        "turns_completed": turns,
        "total_tokens_used": tokens,
        "total_cost_usd": 0.1,
    }


def _continued_state() -> dict[str, object]:
    return {
        "schema_version": "model-tool-loop-state-v1",
        "next_step": "model_turn",
        "model_usage_call_id": "usage-a",
        "tool_call_id": "tool-a",
        "approval_id": None,
        "checkpoint_ref": None,
        "context_ref": "artifact://context-a",
        "next_request_digest": "f" * 64,
    }


def _model_result_state() -> dict[str, object]:
    return {
        "schema_version": "model-tool-loop-state-v1",
        "next_step": "model_result",
        "model_usage_call_id": "usage-a",
        "tool_call_id": None,
        "approval_id": None,
        "checkpoint_ref": None,
        "context_ref": None,
        "next_request_digest": None,
    }


async def _settle_model_turn(
    storage: SQLAlchemyStorage,
    loop: ModelToolLoopRecord,
) -> ModelToolLoopRecord:
    """通过公开仓储先提交当前model actual，再允许后续step转换。"""

    async with storage.uow() as uow:
        settled = await uow.model_tool_loops.settle_model_turn(
            tenant_id="tenant-a",
            loop_id=loop.loop_id,
            expected_version=loop.version,
            owner_lease_digest=loop.owner_lease_digest,
            owner_fence=loop.owner_fence,
            cumulative_usage=_usage(loop.next_turn_ordinal),
            state=_model_result_state(),
        )
        await uow.commit()
    return settled


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


def _executing_state() -> dict[str, object]:
    return {
        **_waiting_state(),
        "next_step": "tool_execution",
        "approval_id": "approval-a",
    }


def test_loop_state_rejects_unknown_fields_and_illegal_reference_combinations() -> None:
    """耐久状态必须在写入前拒绝未知字段和与next step矛盾的current refs。"""

    base = _create_data("run-a")
    for state in (
        {
            "schema_version": "model-tool-loop-state-v1",
            "next_step": "model_turn",
            "model_usage_call_id": None,
            "tool_call_id": None,
            "approval_id": None,
            "checkpoint_ref": None,
            "context_ref": None,
            "next_request_digest": None,
            "unknown": "must-fail",
        },
        {
            "schema_version": "model-tool-loop-state-v1",
            "next_step": "approval_resume",
            "model_usage_call_id": "usage-a",
            "tool_call_id": None,
            "approval_id": None,
            "checkpoint_ref": None,
            "context_ref": None,
            "next_request_digest": None,
        },
    ):
        with pytest.raises(ValueError):
            ModelToolLoopCreate.model_validate(
                {
                    **base.model_dump(mode="python"),
                    "state": state,
                }
            )


@pytest.mark.asyncio
async def test_create_is_exactly_idempotent_and_conflicting_identity_fails(
    tmp_path: Path,
) -> None:
    """相同identity复用一行，不同preimage在任何副作用前稳定冲突。"""

    dsn = _dsn(tmp_path / "loop-create.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await _run_id(storage)
        async with storage.uow() as uow:
            created = await uow.model_tool_loops.create(_create_data(run_id))
            await uow.commit()
        async with storage.uow() as uow:
            replay = await uow.model_tool_loops.create(_create_data(run_id))
            await uow.commit()
        assert replay.id == created.id
        assert replay.version == 1
        assert replay.status == "active"
        assert replay.next_turn_ordinal == 1

        for update in (
            {"request_identity_digest": "f" * 64},
            {"catalog_digest": "f" * 64},
            {
                "frozen_bounds": _create_data(run_id).frozen_bounds.model_copy(
                    update={"max_turns": 3}
                )
            },
        ):
            conflict = _create_data(run_id).model_copy(update=update)
            async with storage.uow() as uow:
                with pytest.raises(
                    ModelToolLoopStorageConflict,
                    match="model.tool_loop_replay_conflict",
                ):
                    await uow.model_tool_loops.create(conflict)
        async with storage.uow() as uow:
            assert (await uow.model_tool_loop_marker.get()).evidence_seen is True
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_turn_wait_resume_and_completed_terminal_are_monotonic(tmp_path: Path) -> None:
    """合法线性转换逐次递增version/turn，terminal后拒绝任何晚到推进。"""

    dsn = _dsn(tmp_path / "loop-transitions.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await _run_id(storage)
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.create(_create_data(run_id))
            await uow.commit()
        loop = await _settle_model_turn(storage, loop)

        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.commit_turn(
                tenant_id="tenant-a",
                loop_id=loop.loop_id,
                expected_version=2,
                owner_lease_digest=loop.owner_lease_digest,
                owner_fence=loop.owner_fence,
                cumulative_usage=_usage(1),
                state=_continued_state(),
            )
            await uow.commit()
        assert (loop.next_turn_ordinal, loop.version) == (2, 3)

        loop = await _settle_model_turn(storage, loop)
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.wait_for_approval(
                tenant_id="tenant-a",
                loop_id=loop.loop_id,
                expected_version=4,
                owner_lease_digest=loop.owner_lease_digest,
                owner_fence=loop.owner_fence,
                state=_waiting_state(),
            )
            await uow.commit()
        assert (loop.status, loop.version) == ("waiting_approval", 5)

        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.resume_after_approval(
                tenant_id="tenant-a",
                loop_id=loop.loop_id,
                expected_version=5,
                owner_lease_digest=loop.owner_lease_digest,
                owner_fence=loop.owner_fence,
                state=_executing_state(),
            )
            await uow.commit()
        assert (loop.status, loop.version) == ("active", 6)

        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.terminate(
                tenant_id="tenant-a",
                loop_id=loop.loop_id,
                expected_version=6,
                owner_lease_digest=loop.owner_lease_digest,
                owner_fence=loop.owner_fence,
                status="completed",
                result_ref="artifact://final",
                error_ref=None,
            )
            await uow.commit()
        assert (loop.status, loop.result_ref, loop.version) == (
            "completed",
            "artifact://final",
            7,
        )

        async with storage.uow() as uow:
            with pytest.raises(ModelToolLoopStorageConflict):
                await uow.model_tool_loops.commit_turn(
                    tenant_id="tenant-a",
                    loop_id=loop.loop_id,
                    expected_version=7,
                    owner_lease_digest=loop.owner_lease_digest,
                    owner_fence=loop.owner_fence,
                    cumulative_usage=loop.cumulative_usage,
                    state=loop.state,
                )
    finally:
        await storage.dispose()


@pytest.mark.parametrize("status", ["failed", "cancelled", "needs_review"])
@pytest.mark.asyncio
async def test_failure_terminals_and_stale_or_wrong_lease_are_rejected(
    tmp_path: Path,
    status: Literal["failed", "cancelled", "needs_review"],
) -> None:
    """三个非成功终态可提交；旧version、错误lease/fence不能取得推进权。"""

    dsn = _dsn(tmp_path / f"loop-{status}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await _run_id(storage)
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.create(
                _create_data(
                    run_id,
                    loop_id={"failed": "1", "cancelled": "2", "needs_review": "3"}[status] * 64,
                )
            )
            await uow.commit()

        for version, lease, fence in (
            (0, loop.owner_lease_digest, loop.owner_fence),
            (1, "f" * 64, loop.owner_fence),
            (1, loop.owner_lease_digest, 2),
        ):
            async with storage.uow() as uow:
                with pytest.raises(ModelToolLoopStorageConflict):
                    await uow.model_tool_loops.fail(
                        tenant_id="tenant-a",
                        loop_id=loop.loop_id,
                        expected_version=version,
                        owner_lease_digest=lease,
                        owner_fence=fence,
                        status=status,
                        error_ref=f"error://{status}",
                    )

        async with storage.uow() as uow:
            terminal = await uow.model_tool_loops.fail(
                tenant_id="tenant-a",
                loop_id=loop.loop_id,
                expected_version=1,
                owner_lease_digest=loop.owner_lease_digest,
                owner_fence=loop.owner_fence,
                status=status,
                error_ref=f"error://{status}",
            )
            await uow.commit()
        assert (terminal.status, terminal.error_ref, terminal.version) == (
            status,
            f"error://{status}",
            2,
        )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_competing_writer_and_expired_lease_cannot_advance(tmp_path: Path) -> None:
    """同version竞争只有首个writer成功，过期lease即使identity正确也不能推进。"""

    dsn = _dsn(tmp_path / "loop-competition.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        run_id = await _run_id(storage)
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.create(_create_data(run_id))
            expired = await uow.model_tool_loops.create(
                _create_data(run_id, loop_id="9" * 64).model_copy(
                    update={"owner_lease_expires_at": datetime(2020, 1, 1, tzinfo=UTC)}
                )
            )
            await uow.commit()
        loop = await _settle_model_turn(storage, loop)

        async with storage.uow() as winner:
            advanced = await winner.model_tool_loops.commit_turn(
                tenant_id="tenant-a",
                loop_id=loop.loop_id,
                expected_version=2,
                owner_lease_digest=loop.owner_lease_digest,
                owner_fence=loop.owner_fence,
                cumulative_usage=_usage(1),
                state=_continued_state(),
            )
            await winner.commit()
        assert advanced.version == 3

        async with storage.uow() as loser:
            with pytest.raises(ModelToolLoopStorageConflict):
                await loser.model_tool_loops.commit_turn(
                    tenant_id="tenant-a",
                    loop_id=loop.loop_id,
                    expected_version=1,
                    owner_lease_digest=loop.owner_lease_digest,
                    owner_fence=loop.owner_fence,
                    cumulative_usage=_usage(1),
                    state=_continued_state(),
                )

        async with storage.uow() as uow:
            with pytest.raises(ModelToolLoopStorageConflict):
                await uow.model_tool_loops.commit_turn(
                    tenant_id="tenant-a",
                    loop_id=expired.loop_id,
                    expected_version=1,
                    owner_lease_digest=expired.owner_lease_digest,
                    owner_fence=expired.owner_fence,
                    cumulative_usage=_usage(1),
                    state=_continued_state(),
                )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_deadline_expiry_fences_active_and_waiting_without_reviving_lease(
    tmp_path: Path,
) -> None:
    """deadline专用CAS只终结已过期owner，并区分active失败与waiting取消。"""

    dsn = _dsn(tmp_path / "loop-deadline-expiry.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    expired_at = datetime(2031, 1, 1, tzinfo=UTC)
    try:
        run_id = await _run_id(storage)
        async with storage.uow() as uow:
            active = await uow.model_tool_loops.create(_create_data(run_id, loop_id="7" * 64))
            waiting = await uow.model_tool_loops.create(_create_data(run_id, loop_id="8" * 64))
            await uow.commit()
        waiting = await _settle_model_turn(storage, waiting)
        async with storage.uow() as uow:
            waiting = await uow.model_tool_loops.wait_for_approval(
                tenant_id="tenant-a",
                loop_id=waiting.loop_id,
                expected_version=waiting.version,
                owner_lease_digest=waiting.owner_lease_digest,
                owner_fence=waiting.owner_fence,
                state=_waiting_state(),
            )
            await uow.commit()

        async with storage.uow() as uow:
            failed = await uow.model_tool_loops.expire_deadline(
                tenant_id="tenant-a",
                loop_id=active.loop_id,
                expected_status="active",
                expected_version=active.version,
                owner_lease_digest=active.owner_lease_digest,
                owner_fence=active.owner_fence,
                expired_at=expired_at,
                error_ref="error:model.tool_loop_limit_exceeded",
            )
            cancelled = await uow.model_tool_loops.expire_deadline(
                tenant_id="tenant-a",
                loop_id=waiting.loop_id,
                expected_status="waiting_approval",
                expected_version=waiting.version,
                owner_lease_digest=waiting.owner_lease_digest,
                owner_fence=waiting.owner_fence,
                expired_at=expired_at,
                error_ref="error:model.tool_loop_limit_exceeded",
            )
            await uow.commit()

        assert (failed.status, failed.version) == ("failed", 2)
        assert (cancelled.status, cancelled.version) == ("cancelled", 4)
    finally:
        await storage.dispose()
