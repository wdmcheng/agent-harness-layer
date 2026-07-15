"""Usage settlement、outbox 与 event capacity repository 合同测试。"""

from __future__ import annotations

from asyncio import gather
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import update

from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EventCapacityExceeded,
    EvidenceOperationKind,
)
from agent_harness.storage.models import RunEventCapacityModel, RunEvidenceOutboxModel


def sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


_MISSING = object()


def usage_result(
    *,
    run_id: str,
    outcome: str = "completed",
    evidence_updates: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造 repository write-once 合同使用的完整统一 usage result。"""

    evidence: dict[str, object] = {
        "usage_kind": "model",
        "tenant_id": "tenant-a",
        "provider": "fake",
        "model": "fake-basic",
        "input_tokens": 1,
        "output_tokens": 2,
        "cost_usd": None,
        "cost_status": "unavailable",
        "latency_ms": 3,
        "decision": {"provider_called": True},
        "run_id": run_id,
        "agent_id": "agent-a",
        "request_id": None,
        "trace_id": "trace-a",
    }
    evidence.update(evidence_updates or {})
    return {"evidence": evidence, "outcome": outcome}


def usage_started(*, run_id: str) -> dict[str, object]:
    """返回 repository claim 需要持久冻结的 started 身份。"""

    return cast(dict[str, object], usage_result(run_id=run_id)["evidence"])


@pytest.mark.asyncio
async def test_usage_settlement_and_capacity_share_one_uow(tmp_path: Path) -> None:
    path = tmp_path / "usage-uow.db"
    dsn = sqlite_dsn(path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.sessions.ensure(
                SessionCreate(
                    session_id="session-a",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    agent_id="agent-a",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id="session-a",
                    agent_id="agent-a",
                    trace_id="trace-a",
                )
            )
            await uow.event_capacity.ensure_run(tenant_id="tenant-a", run_id=run.id)
            reserved = await uow.event_capacity.reserve(
                run_id=run.id,
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
            )
            outbox = await uow.evidence_outbox.start_usage(
                tenant_id="tenant-a",
                run_id=run.id,
                usage_call_id="usage-a",
                event_id="usage:tenant-a:usage-a:final",
                reserved_event_count=reserved,
                started_evidence=usage_started(run_id=run.id),
            )
            await uow.evidence_outbox.persist_result(
                tenant_id="tenant-a",
                usage_call_id="usage-a",
                result=usage_result(run_id=run.id),
            )
            await uow.commit()

        async with storage.uow() as uow:
            snapshot = await uow.event_capacity.snapshot(run.id)
            pending = await uow.evidence_outbox.pending(run_id=run.id)
            assert snapshot.outstanding_reserved_event_count == 2
            assert snapshot.terminal_reservation == 1
            assert pending[0].id == outbox.id
            assert pending[0].state == "result_persisted"
            await uow.event_capacity.settle(
                run_id=run.id,
                reserved_event_count=reserved,
                consumed=2,
            )
            await uow.evidence_outbox.mark_published(
                tenant_id="tenant-a",
                usage_call_id="usage-a",
            )
            await uow.commit()

        async with storage.uow() as uow:
            settled = await uow.event_capacity.snapshot(run.id)
            assert settled.highest_persisted_seq == 2
            assert settled.outstanding_reserved_event_count == 0
            assert await uow.evidence_outbox.pending(run_id=run.id) == []
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_usage_result_is_write_once_and_needs_review_cannot_be_closed(tmp_path: Path) -> None:
    dsn = sqlite_dsn(tmp_path / "usage-write-once.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.sessions.ensure(
                SessionCreate(
                    session_id="session-a",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    agent_id="agent-a",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id="session-a",
                    agent_id="agent-a",
                    trace_id="trace-a",
                )
            )
            await uow.evidence_outbox.claim_usage(
                tenant_id="tenant-a",
                run_id=run.id,
                usage_call_id="usage-write-once",
                event_id="usage:tenant-a:usage-write-once:final",
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=usage_started(run_id=run.id),
            )
            await uow.evidence_outbox.persist_result(
                tenant_id="tenant-a",
                usage_call_id="usage-write-once",
                result=usage_result(run_id=run.id),
            )
            await uow.commit()

        async with storage.uow() as uow:
            same = await uow.evidence_outbox.persist_result(
                tenant_id="tenant-a",
                usage_call_id="usage-write-once",
                result=usage_result(run_id=run.id),
            )
            assert same.state == "result_persisted"
            await uow.commit()

        async with storage.uow() as uow:
            with pytest.raises(RuntimeError, match="persisted usage result conflict"):
                await uow.evidence_outbox.persist_result(
                    tenant_id="tenant-a",
                    usage_call_id="usage-write-once",
                    result=usage_result(run_id=run.id, outcome="failed"),
                )

        async with storage.uow() as uow:
            await uow.session.execute(
                update(RunEvidenceOutboxModel)
                .where(RunEvidenceOutboxModel.usage_call_id == "usage-write-once")
                .values(state="needs_review")
            )
            await uow.commit()
        async with storage.uow() as uow:
            with pytest.raises(RuntimeError, match="needs_review"):
                await uow.evidence_outbox.persist_result(
                    tenant_id="tenant-a",
                    usage_call_id="usage-write-once",
                    result=usage_result(run_id=run.id),
                )
    finally:
        await storage.dispose()


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        pytest.param("input_tokens", -1, id="negative-token"),
        pytest.param("output_tokens", True, id="bool-token"),
        pytest.param("cost_usd", float("nan"), id="nan-cost"),
        pytest.param("cost_usd", float("inf"), id="infinite-cost"),
        pytest.param("trace_id", _MISSING, id="missing-required-field"),
    ],
)
@pytest.mark.asyncio
async def test_usage_repository_rejects_invalid_evidence_without_settlement_side_effect(
    tmp_path: Path,
    field: str,
    invalid_value: object,
) -> None:
    """repository 必须复用 DTO 不变量，非法 result 保持 started。"""

    dsn = sqlite_dsn(tmp_path / "invalid-usage-result.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.sessions.ensure(
                SessionCreate(
                    session_id="session-a",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    agent_id="agent-a",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id="session-a",
                    agent_id="agent-a",
                    trace_id="trace-a",
                )
            )
            await uow.evidence_outbox.claim_usage(
                tenant_id="tenant-a",
                run_id=run.id,
                usage_call_id="invalid-usage",
                event_id="usage:tenant-a:invalid-usage:final",
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=usage_started(run_id=run.id),
            )
            await uow.commit()

        result = usage_result(run_id=run.id)
        evidence = cast(dict[str, object], result["evidence"])
        if invalid_value is _MISSING:
            evidence.pop(field)
        else:
            evidence[field] = invalid_value
        async with storage.uow() as uow:
            with pytest.raises(ValueError):
                await uow.evidence_outbox.persist_result(
                    tenant_id="tenant-a",
                    usage_call_id="invalid-usage",
                    result=cast(dict[str, Any], result),
                )

        async with storage.uow() as uow:
            settlement = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id="invalid-usage",
            )
            assert settlement.state == "started"
            assert settlement.result_json == {"started": usage_started(run_id=run.id)}
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_usage_claim_rejects_cross_tenant_run_without_capacity_side_effect(
    tmp_path: Path,
) -> None:
    dsn = sqlite_dsn(tmp_path / "usage-tenant-run.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.tenants.ensure("tenant-b")
            await uow.sessions.ensure(
                SessionCreate(
                    session_id="session-a",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    agent_id="agent-a",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id="session-a",
                    agent_id="agent-a",
                    trace_id="trace-a",
                )
            )
            await uow.commit()

        with pytest.raises(ValueError, match="usage tenant does not own run"):
            async with storage.uow() as uow:
                await uow.evidence_outbox.claim_usage(
                    tenant_id="tenant-b",
                    run_id=run.id,
                    usage_call_id="cross-tenant",
                    event_id="usage:tenant-b:cross-tenant:final",
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                    started_evidence=usage_started(run_id=run.id),
                )
                await uow.commit()

        async with storage.uow() as uow:
            snapshot = await uow.event_capacity.snapshot(run.id)
            outbox = await uow.evidence_outbox.list_for_run(run_id=run.id)
        assert snapshot.outstanding_reserved_event_count == 0
        assert outbox == []
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_usage_claim_rejects_empty_call_id_without_capacity_side_effect(
    tmp_path: Path,
) -> None:
    dsn = sqlite_dsn(tmp_path / "usage-empty-call-id.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.sessions.ensure(
                SessionCreate(
                    session_id="session-a",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    agent_id="agent-a",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id="session-a",
                    agent_id="agent-a",
                    trace_id="trace-a",
                )
            )
            await uow.commit()

        with pytest.raises(ValueError, match="usage call id must not be empty"):
            async with storage.uow() as uow:
                await uow.evidence_outbox.claim_usage(
                    tenant_id="tenant-a",
                    run_id=run.id,
                    usage_call_id="",
                    event_id="usage:tenant-a::final",
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                    started_evidence=usage_started(run_id=run.id),
                )

        async with storage.uow() as uow:
            snapshot = await uow.event_capacity.snapshot(run.id)
            outbox = await uow.evidence_outbox.list_for_run(run_id=run.id)
        assert snapshot.outstanding_reserved_event_count == 0
        assert outbox == []
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_sparse_high_sequence_rejects_operation_before_side_effect(tmp_path: Path) -> None:
    path = tmp_path / "capacity-limit.db"
    dsn = sqlite_dsn(path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.sessions.ensure(
                SessionCreate(
                    session_id="session-a",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    agent_id="agent-a",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id="session-a",
                    agent_id="agent-a",
                    trace_id="trace-a",
                )
            )
            await uow.event_capacity.ensure_run(tenant_id="tenant-a", run_id=run.id)
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == run.id)
                .values(highest_persisted_seq=MAX_EVENT_SEQ - 1)
            )
            with pytest.raises(EventCapacityExceeded) as exc_info:
                await uow.event_capacity.reserve(
                    run_id=run.id,
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                )
            assert exc_info.value.code == "event.sequence_exhausted"
            assert await uow.evidence_outbox.pending(run_id=run.id) == []
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_sqlite_capacity_cas_allows_only_one_concurrent_reservation(tmp_path: Path) -> None:
    path = tmp_path / "capacity-concurrency.db"
    dsn = sqlite_dsn(path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.sessions.ensure(
                SessionCreate(
                    session_id="session-a",
                    tenant_id="tenant-a",
                    user_id="user-a",
                    agent_id="agent-a",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id="session-a",
                    agent_id="agent-a",
                    trace_id="trace-a",
                )
            )
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == run.id)
                .values(highest_persisted_seq=MAX_EVENT_SEQ - 3)
            )
            await uow.commit()

        async def reserve_once() -> int | Exception:
            try:
                async with storage.uow() as uow:
                    reserved = await uow.event_capacity.reserve(
                        run_id=run.id,
                        operation_kind=EvidenceOperationKind.MODEL_USAGE,
                    )
                    await uow.commit()
                    return reserved
            except Exception as exc:
                return exc

        results = await gather(reserve_once(), reserve_once())
        assert sum(result == 2 for result in results) == 1
        failures = [result for result in results if isinstance(result, Exception)]
        assert len(failures) == 1
        assert isinstance(failures[0], EventCapacityExceeded)
        async with storage.uow() as uow:
            snapshot = await uow.event_capacity.snapshot(run.id)
            assert snapshot.highest_persisted_seq == MAX_EVENT_SEQ - 3
            assert snapshot.outstanding_reserved_event_count == 2
            assert snapshot.terminal_reservation == 1
    finally:
        await storage.dispose()
