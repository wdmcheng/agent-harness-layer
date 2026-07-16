"""模型用量输入拒绝与 SQLite 容量并发合同测试。"""

from __future__ import annotations

from tests.contracts.test_model_usage_repository_contracts import (
    MAX_EVENT_SEQ as MAX_EVENT_SEQ,
)
from tests.contracts.test_model_usage_repository_contracts import (
    EventCapacityExceeded as EventCapacityExceeded,
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
    RunEventCapacityModel as RunEventCapacityModel,
)
from tests.contracts.test_model_usage_repository_contracts import (
    SessionCreate as SessionCreate,
)
from tests.contracts.test_model_usage_repository_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_model_usage_repository_contracts import (
    gather as gather,
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
    usage_started as usage_started,
)


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
