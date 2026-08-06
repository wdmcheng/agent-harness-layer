"""模型工具执行未知状态的单调needs-review恢复合同。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from tests.contracts.run_trace_contract_helpers import seed_persisted_run
from tests.contracts.test_model_tool_execution_claim_permit_contracts import (
    _claim,  # pyright: ignore[reportPrivateUsage]
    _seed_loop,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.storage import (
    ModelToolLoopRecord,
    SQLAlchemyStorage,
    ToolInvocationRecord,
    run_migrations,
)
from agent_harness.storage.models import ToolInvocationModel
from agent_harness.tools import (
    ModelToolExecutionClaimService,
    ModelToolExecutionNeedsReview,
)


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _states(
    storage: SQLAlchemyStorage,
    *,
    tool_call_id: str,
    loop_id: str,
) -> tuple[ToolInvocationRecord, ModelToolLoopRecord]:
    """读取脱离ORM的tool与loop快照，避免断言依赖session生命周期。"""

    async with storage.uow() as uow:
        tool = await uow.tool_invocations.get_by_tool_call_id(tool_call_id)
        loop = await uow.model_tool_loops.get("default", loop_id)
    assert tool is not None
    assert loop is not None
    return tool, loop


@pytest.mark.asyncio
async def test_executing_recovery_persists_claim_and_loop_needs_review(
    tmp_path: Path,
) -> None:
    """handler可能已取得执行权时不重放，并在同一恢复分支保留未知围栏。"""

    dsn = _dsn(tmp_path / "executing-needs-review.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    effects: list[str] = []
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-model-tool-claim")
        await _seed_loop(storage, run_id)
        claim = _claim(run_id, lease_expires_at=now + timedelta(minutes=1))
        async with storage.uow() as uow:
            await uow.tool_invocations.create_model_claim(claim)
            await uow.tool_invocations.begin_model_execution(data=claim, now=now)
            await uow.commit()

        with pytest.raises(ModelToolExecutionNeedsReview):
            await ModelToolExecutionClaimService(storage).acquire(claim, now=now)
        tool, loop = await _states(
            storage,
            tool_call_id=claim.tool_call_id,
            loop_id=claim.loop_id,
        )
        assert tool.execution_state == "needs_review"
        assert tool.result_ref is None
        review = tool.metadata["model_tool_execution_review"]
        assert review["schema_version"] == "model-tool-execution-review-v1"
        assert review["reason"] == "executing_without_result"
        assert len(review["evidence_digest"]) == 64
        assert loop.status == "needs_review"
        assert loop.result_ref is None
        assert loop.error_ref == f"model-tool-execution-review:{review['evidence_digest']}"
        assert effects == []
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proof_update",
    [
        None,
        {"schema_version": "tool-handler-not-started-v2"},
        {"proof_digest": "0" * 64},
    ],
    ids=("evidence-missing", "version-unknown", "digest-mismatch"),
)
async def test_invalid_takeover_proof_fails_closed_to_needs_review(
    tmp_path: Path,
    proof_update: dict[str, object] | None,
) -> None:
    """缺失、未知版本或摘要篡改proof都不能取得permit或再次执行handler。"""

    dsn = _dsn(tmp_path / f"invalid-proof-{proof_update is None}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-model-tool-claim")
        await _seed_loop(storage, run_id)
        original = _claim(
            run_id,
            lease_digest="a" * 64,
            lease_expires_at=now - timedelta(seconds=1),
        )
        contender = original.model_copy(
            update={
                "execution_lease_digest": "b" * 64,
                "execution_fence": 2,
                "execution_lease_expires_at": now + timedelta(minutes=1),
            }
        )
        async with storage.uow() as uow:
            existing, _ = await uow.tool_invocations.create_model_claim(original)
            taken = await uow.tool_invocations.takeover_expired_model_claim(
                existing=existing,
                data=contender,
                now=now,
            )
            proof = taken.not_started_proof
            assert proof is not None
            tampered = None if proof_update is None else {**proof, **proof_update}
            await uow.session.execute(
                update(ToolInvocationModel)
                .where(ToolInvocationModel.id == taken.id)
                .values(not_started_proof_json=tampered)
            )
            await uow.commit()

        with pytest.raises(ModelToolExecutionNeedsReview):
            await ModelToolExecutionClaimService(storage).acquire(contender, now=now)
        tool, loop = await _states(
            storage,
            tool_call_id=contender.tool_call_id,
            loop_id=contender.loop_id,
        )
        assert tool.execution_state == "needs_review"
        assert tool.handler_started_at is None
        assert loop.status == "needs_review"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_unknown_event_version_uses_same_needs_review_branch(tmp_path: Path) -> None:
    """事件恢复器发现未知版本时可复用同一耐久关闭入口，不跳过审计围栏。"""

    dsn = _dsn(tmp_path / "unknown-event-version.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-model-tool-claim")
        await _seed_loop(storage, run_id)
        claim = _claim(run_id, lease_expires_at=now + timedelta(minutes=1))
        async with storage.uow() as uow:
            await uow.tool_invocations.create_model_claim(claim)
            await uow.commit()

        service = ModelToolExecutionClaimService(storage)
        await service.mark_recovery_unknown(
            tool_call_id=claim.tool_call_id,
            reason="event_version_unknown",
        )
        tool, loop = await _states(
            storage,
            tool_call_id=claim.tool_call_id,
            loop_id=claim.loop_id,
        )
        review = tool.metadata["model_tool_execution_review"]
        assert review["reason"] == "event_version_unknown"
        assert tool.execution_state == "needs_review"
        assert loop.status == "needs_review"
    finally:
        await storage.dispose()
