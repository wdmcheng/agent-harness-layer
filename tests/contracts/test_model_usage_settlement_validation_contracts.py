"""模型用量结算、容量与证据校验合同测试。"""

from __future__ import annotations

from tests.contracts.test_model_usage_repository_contracts import (
    _MISSING as _MISSING,
)
from tests.contracts.test_model_usage_repository_contracts import (
    Any as Any,
)
from tests.contracts.test_model_usage_repository_contracts import (
    EvidenceOperationKind as EvidenceOperationKind,
)
from tests.contracts.test_model_usage_repository_contracts import (
    Path as Path,
)
from tests.contracts.test_model_usage_repository_contracts import (
    RunCreate as RunCreate,
)
from tests.contracts.test_model_usage_repository_contracts import (
    RunEvidenceOutboxModel as RunEvidenceOutboxModel,
)
from tests.contracts.test_model_usage_repository_contracts import (
    SessionCreate as SessionCreate,
)
from tests.contracts.test_model_usage_repository_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_model_usage_repository_contracts import (
    cast as cast,
)
from tests.contracts.test_model_usage_repository_contracts import (
    pytest as pytest,
)
from tests.contracts.test_model_usage_repository_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_model_usage_repository_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_model_usage_repository_contracts import (
    update as update,
)
from tests.contracts.test_model_usage_repository_contracts import (
    usage_result as usage_result,
)
from tests.contracts.test_model_usage_repository_contracts import (
    usage_started as usage_started,
)


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
