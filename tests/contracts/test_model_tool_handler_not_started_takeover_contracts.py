"""过期claimed工具调用的可信未开始证据与换租CAS合同。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.contracts.run_trace_contract_helpers import seed_persisted_run
from tests.contracts.test_model_tool_execution_claim_permit_contracts import (
    _claim,  # pyright: ignore[reportPrivateUsage]
    _seed_loop,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.storage import (
    SQLAlchemyStorage,
    ToolHandlerNotStartedProof,
    ToolInvocationReplayConflict,
    run_migrations,
)


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def test_tool_handler_not_started_proof_has_exact_canonical_shape() -> None:
    """公开DTO逐值冻结换租证据，并拒绝摘要、reason或额外字段漂移。"""

    expiry = datetime(2030, 1, 1, tzinfo=UTC)
    proof = ToolHandlerNotStartedProof.build(
        tool_call_id="3" * 64,
        binding_digest="4" * 64,
        prior_fence=1,
        next_fence=2,
        previous_lease_expires_at=expiry,
    )
    preimage = {
        "schema_version": "tool-handler-not-started-v1",
        "tool_call_id": "3" * 64,
        "binding_digest": "4" * 64,
        "prior_fence": 1,
        "next_fence": 2,
        "previous_lease_expires_at": "2030-01-01T00:00:00+00:00",
        "reason": "claim_lease_expired",
    }
    expected_digest = hashlib.sha256(
        json.dumps(
            preimage,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    assert proof.model_dump(mode="json") == {
        **preimage,
        "proof_digest": expected_digest,
    }

    for update in (
        {"proof_digest": "0" * 64},
        {"reason": "worker_timeout"},
        {"next_fence": 3},
        {"prior_fence": True},
        {"unexpected": True},
    ):
        with pytest.raises(ValidationError):
            ToolHandlerNotStartedProof.model_validate({**proof.model_dump(mode="json"), **update})


@pytest.mark.asyncio
async def test_only_one_worker_can_take_over_expired_claim_and_old_fence_stops(
    tmp_path: Path,
) -> None:
    """两个worker争抢同一过期claim时，仅CAS赢家持有新lease且旧owner不能推进。"""

    dsn = _dsn(tmp_path / "expired-claim-race.sqlite3")
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
        async with storage.uow() as uow:
            existing, _ = await uow.tool_invocations.create_model_claim(original)
            await uow.commit()

        contenders = (
            original.model_copy(
                update={
                    "execution_lease_digest": "b" * 64,
                    "execution_fence": 2,
                    "execution_lease_expires_at": now + timedelta(minutes=1),
                }
            ),
            original.model_copy(
                update={
                    "execution_lease_digest": "c" * 64,
                    "execution_fence": 2,
                    "execution_lease_expires_at": now + timedelta(minutes=1),
                }
            ),
        )

        async def compete(index: int) -> tuple[str, int]:
            """用独立UoW竞争同一旧lease快照，提交结果只暴露winner索引。"""

            async with storage.uow() as uow:
                try:
                    await uow.tool_invocations.takeover_expired_model_claim(
                        existing=existing,
                        data=contenders[index],
                        now=now,
                    )
                except ToolInvocationReplayConflict:
                    return "rejected", index
                await uow.commit()
                return "committed", index

        outcomes = await asyncio.gather(compete(0), compete(1))
        assert sorted(status for status, _ in outcomes) == ["committed", "rejected"]
        winner_index = next(index for status, index in outcomes if status == "committed")
        winner = contenders[winner_index]

        async with storage.uow() as uow:
            taken = await uow.tool_invocations.get(existing.id)
        assert taken is not None
        assert taken.execution_lease_digest == winner.execution_lease_digest
        assert taken.execution_fence == 2
        proof = ToolHandlerNotStartedProof.model_validate(taken.not_started_proof)
        assert proof.tool_call_id == original.tool_call_id
        assert proof.binding_digest == original.binding["binding_digest"]
        assert proof.prior_fence == 1
        assert proof.next_fence == 2
        assert proof.previous_lease_expires_at == "2029-12-31T23:59:59+00:00"
        assert proof.reason == "claim_lease_expired"

        async with storage.uow() as uow:
            with pytest.raises(ToolInvocationReplayConflict):
                await uow.tool_invocations.begin_model_execution(data=original, now=now)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_active_or_executing_claim_never_downgrades_for_takeover(tmp_path: Path) -> None:
    """活跃claimed和已经executing的row都不能由换租入口改写或补伪造proof。"""

    dsn = _dsn(tmp_path / "claim-takeover-guards.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-model-tool-claim")
        await _seed_loop(storage, run_id)
        active = _claim(run_id, lease_expires_at=now + timedelta(minutes=1))
        async with storage.uow() as uow:
            active_row, _ = await uow.tool_invocations.create_model_claim(active)
            await uow.commit()
        contender = active.model_copy(
            update={
                "execution_lease_digest": "f" * 64,
                "execution_fence": 2,
                "execution_lease_expires_at": now + timedelta(minutes=2),
            }
        )
        async with storage.uow() as uow:
            with pytest.raises(ToolInvocationReplayConflict):
                await uow.tool_invocations.takeover_expired_model_claim(
                    existing=active_row,
                    data=contender,
                    now=now,
                )

        async with storage.uow() as uow:
            executing = await uow.tool_invocations.begin_model_execution(
                data=active,
                now=now,
            )
            await uow.commit()
        after_expiry = active.execution_lease_expires_at + timedelta(seconds=1)
        async with storage.uow() as uow:
            with pytest.raises(ToolInvocationReplayConflict):
                await uow.tool_invocations.takeover_expired_model_claim(
                    existing=executing,
                    data=contender,
                    now=after_expiry,
                )
            persisted = await uow.tool_invocations.get(executing.id)
        assert persisted is not None
        assert persisted.execution_state == "executing"
        assert persisted.handler_started_at is not None
        assert persisted.handler_started_at.replace(tzinfo=UTC) == now
        assert persisted.not_started_proof is None
    finally:
        await storage.dispose()
