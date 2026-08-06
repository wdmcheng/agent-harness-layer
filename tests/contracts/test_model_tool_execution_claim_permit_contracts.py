"""模型驱动工具claim、lease/fence与一次性执行许可合同。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.contracts.model_tool_loop_contract_helpers import (
    initial_model_tool_loop_snapshot,
)
from tests.contracts.run_trace_contract_helpers import seed_persisted_run

from agent_harness.storage import (
    ModelToolInvocationClaimCreate,
    ModelToolLoopCreate,
    SQLAlchemyStorage,
    ToolInvocationReplayConflict,
    run_migrations,
)
from agent_harness.tools import (
    ModelToolExecutionClaimActive,
    ModelToolExecutionClaimService,
    ModelToolExecutionNeedsReview,
    ToolExecutionPermit,
)


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _claim(
    run_id: str,
    *,
    lease_digest: str = "e" * 64,
    lease_expires_at: datetime,
    approval_id: str | None = None,
) -> ModelToolInvocationClaimCreate:
    """构造绑定全部模型工具身份、但不包含原始业务结果的claim。"""

    binding = {
        "schema_version": "model-tool-call-binding-v1",
        "catalog_digest": "5" * 64,
    }
    canonical = json.dumps(
        binding,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    binding["binding_digest"] = hashlib.sha256(canonical.encode()).hexdigest()
    return ModelToolInvocationClaimCreate(
        tenant_id="default",
        agent_id="agent-a",
        run_id=run_id,
        tool_name="search",
        args_ref="artifact://args-a",
        approval_id=approval_id,
        arguments_hash="1" * 64,
        trace_id="trace-model-tool-claim",
        request_id="request-a",
        loop_id="2" * 64,
        turn_ordinal=1,
        tool_call_id="3" * 64,
        binding=binding,
        execution_lease_digest=lease_digest,
        execution_fence=1,
        execution_lease_expires_at=lease_expires_at,
        metadata={"reserved_event_count": 2},
    )


async def _seed_loop(storage: SQLAlchemyStorage, run_id: str) -> None:
    """为tool claim建立仍可写的durable loop owner。"""

    async with storage.uow() as uow:
        await uow.model_tool_loops.create(
            ModelToolLoopCreate(
                tenant_id="default",
                run_id=run_id,
                agent_id="agent-a",
                loop_id="2" * 64,
                request_identity_digest="a" * 64,
                operation_identity_digest="b" * 64,
                catalog_digest="c" * 64,
                **initial_model_tool_loop_snapshot(),
                owner_lease_digest="d" * 64,
                owner_fence=1,
                owner_lease_expires_at=datetime(2031, 1, 1, tzinfo=UTC),
            )
        )
        await uow.commit()


@pytest.mark.asyncio
async def test_exact_claim_replay_and_binding_conflict_are_closed(tmp_path: Path) -> None:
    """相同tool_call_id只复用逐值相同row，绑定漂移不覆盖旧claim。"""

    dsn = _dsn(tmp_path / "claim-replay.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-model-tool-claim")
        await _seed_loop(storage, run_id)
        data = _claim(run_id, lease_expires_at=now + timedelta(minutes=1))
        async with storage.uow() as uow:
            created, was_created = await uow.tool_invocations.create_model_claim(data)
            await uow.commit()
        async with storage.uow() as uow:
            replay, replay_created = await uow.tool_invocations.create_model_claim(data)
            await uow.commit()
        assert was_created is True
        assert replay_created is False
        assert replay.id == created.id
        assert replay.execution_state == "claimed"
        assert replay.handler_started_at is None

        conflict = data.model_copy(
            update={
                "binding": {
                    "schema_version": "model-tool-call-binding-v1",
                    "binding_digest": "9" * 64,
                    "catalog_digest": "5" * 64,
                }
            }
        )
        async with storage.uow() as uow:
            with pytest.raises(
                ToolInvocationReplayConflict,
                match="tool.execution_replay_conflict",
            ):
                await uow.tool_invocations.create_model_claim(conflict)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_active_claim_blocks_takeover_and_expired_claim_rotates_fence(
    tmp_path: Path,
) -> None:
    """活跃claimed不能接管；过期claimed只有CAS赢家取得新fence与proof。"""

    dsn = _dsn(tmp_path / "claim-takeover.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-model-tool-claim")
        await _seed_loop(storage, run_id)
        active = _claim(run_id, lease_expires_at=now + timedelta(minutes=1))
        service = ModelToolExecutionClaimService(storage)
        first = await service.acquire(active, now=now)
        assert isinstance(first, ToolExecutionPermit)
        await service.require_handler_permit(first, now=now)
        with pytest.raises(ModelToolExecutionNeedsReview):
            await service.require_handler_permit(first, now=now)

        live_claim = _claim(
            run_id,
            lease_digest="7" * 64,
            lease_expires_at=now + timedelta(minutes=1),
        ).model_copy(update={"tool_call_id": "8" * 64})
        async with storage.uow() as uow:
            await uow.tool_invocations.create_model_claim(live_claim)
            await uow.commit()
        live_contender = live_claim.model_copy(
            update={
                "execution_lease_digest": "9" * 64,
                "execution_fence": 2,
            }
        )
        with pytest.raises(ModelToolExecutionClaimActive):
            await service.acquire(live_contender, now=now)

        expired = _claim(
            run_id,
            lease_digest="a" * 64,
            lease_expires_at=now - timedelta(seconds=1),
        ).model_copy(update={"tool_call_id": "6" * 64})
        async with storage.uow() as uow:
            row, _ = await uow.tool_invocations.create_model_claim(expired)
            await uow.commit()
        contender = expired.model_copy(
            update={
                "execution_lease_digest": "b" * 64,
                "execution_fence": 2,
                "execution_lease_expires_at": now + timedelta(minutes=1),
            }
        )
        winner = await service.acquire(contender, now=now)
        assert isinstance(winner, ToolExecutionPermit)
        assert winner.execution_fence == 2
        async with storage.uow() as uow:
            taken = await uow.tool_invocations.get(row.id)
        assert taken is not None
        assert taken.not_started_proof is not None
        assert taken.not_started_proof["prior_fence"] == 1
        assert taken.not_started_proof["next_fence"] == 2

        stale = ModelToolExecutionClaimService(storage)
        with pytest.raises(ModelToolExecutionNeedsReview):
            await stale.acquire(expired, now=now)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_terminal_result_replays_without_returning_a_second_permit(
    tmp_path: Path,
) -> None:
    """owner先封存确定结果后，completed/failed只返回耐久结果引用。"""

    dsn = _dsn(tmp_path / "claim-terminal.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-model-tool-claim")
        await _seed_loop(storage, run_id)
        data = _claim(run_id, lease_expires_at=now + timedelta(minutes=1))
        service = ModelToolExecutionClaimService(storage)
        permit = await service.acquire(data, now=now)
        assert isinstance(permit, ToolExecutionPermit)
        await service.require_handler_permit(permit, now=now)

        await service.complete(
            permit,
            result_ref="artifact://tool-result-a",
            execution_state="completed",
            status="completed",
        )
        replay = await ModelToolExecutionClaimService(storage).acquire(data, now=now)
        assert not isinstance(replay, ToolExecutionPermit)
        assert replay.execution_state == "completed"
        assert replay.result_ref == "artifact://tool-result-a"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_claimed_to_executing_commit_unknown_never_returns_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """executing提交未确认时当前进程拿不到permit，事务回滚后仍保持claimed。"""

    dsn = _dsn(tmp_path / "claim-commit-unknown.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-model-tool-claim")
        await _seed_loop(storage, run_id)
        data = _claim(run_id, lease_expires_at=now + timedelta(minutes=1))
        async with storage.uow() as uow:
            row, _ = await uow.tool_invocations.create_model_claim(data)
            await uow.commit()
            uow_type = type(uow)

        async def uncertain_commit(_uow: object) -> None:
            raise RuntimeError("injected commit acknowledgement loss")

        monkeypatch.setattr(uow_type, "commit", uncertain_commit)
        with pytest.raises(RuntimeError, match="injected commit acknowledgement loss"):
            await ModelToolExecutionClaimService(storage).acquire(data, now=now)
        async with storage.uow() as uow:
            persisted = await uow.tool_invocations.get(row.id)
        assert persisted is not None
        assert persisted.execution_state == "claimed"
        assert persisted.handler_started_at is None
    finally:
        await storage.dispose()
