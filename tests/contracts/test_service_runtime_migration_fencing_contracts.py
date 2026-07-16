"""Service runtime 迁移、队列状态与执行 fencing 合同测试。"""

from __future__ import annotations

from tests.contracts.test_service_runtime_storage_contracts import (
    UTC as UTC,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    ApprovalCreate as ApprovalCreate,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    ApprovalModel as ApprovalModel,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    Path as Path,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    RunCreate as RunCreate,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    SessionCreate as SessionCreate,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    _dsn as _dsn,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    datetime as datetime,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    pytest as pytest,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    sqlite3 as sqlite3,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    timedelta as timedelta,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    update as update,
)
from tests.contracts.test_service_runtime_storage_contracts import (
    uuid4 as uuid4,
)


def test_0012_adds_service_runtime_private_columns_and_terminal_index(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "service-runtime.db"
    run_migrations(_dsn(db_path))

    with sqlite3.connect(db_path) as connection:
        revision = connection.execute("select version_num from alembic_version").fetchone()
        run_columns = {
            row[1] for row in connection.execute("pragma table_info(agent_runs)").fetchall()
        }
        approval_columns = {
            row[1] for row in connection.execute("pragma table_info(approvals)").fetchall()
        }
        event_columns = {
            row[1] for row in connection.execute("pragma table_info(canonical_events)").fetchall()
        }
        event_indexes = {
            row[1] for row in connection.execute("pragma index_list(canonical_events)").fetchall()
        }

    assert revision == ("0015_agent_delegation",)
    assert {
        "execution_context_json",
        "queue_operation_id",
        "queue_request_id",
        "queue_effective_idempotency_key",
        "queue_enqueue_state",
        "queue_message_id",
        "execution_owner_id",
        "execution_workflow_id",
    } <= run_columns
    assert {
        "resolution_operation_id",
        "resolution_request_id",
        "resolution_reviewer_id",
        "resolution_decision",
        "resolution_request_hash",
        "resolution_comment",
        "resolution_enqueue_state",
        "resolution_message_id",
        "resolution_workflow_owner_id",
        "resolution_workflow_id",
    } <= approval_columns
    assert "envelope_json" in event_columns
    assert "uq_canonical_events_run_terminal" in event_indexes


@pytest.mark.asyncio
async def test_run_repository_keeps_queue_state_private_and_fences_execution(
    tmp_path: Path,
) -> None:
    dsn = _dsn(tmp_path / "queued-run.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-1")
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id="tenant-1",
                    user_id="user-1",
                    agent_id="agent-1",
                )
            )
            run = await uow.runs.create_queued(
                RunCreate(
                    tenant_id="tenant-1",
                    session_id=session.id,
                    agent_id="agent-1",
                    idempotency_key="client-key",
                    trace_id="trace-client-key",
                    input={"source_ref": "source://one", "trust_level": "trusted"},
                ),
                execution_context={
                    "identity": {
                        "tenant_id": "tenant-1",
                        "user_id": "user-1",
                        "roles": ["operator"],
                        "permissions": ["runs:execute"],
                        "auth_method": "api-key",
                    },
                    "request_id": "req-1",
                    "trace_id": "trace-client-key",
                },
                operation_id="run:placeholder:execute",
                request_id="req-1",
                effective_idempotency_key="client-key",
            )
            await uow.commit()

        assert "queue_enqueue_state" not in run.to_payload()
        async with storage.uow() as uow:
            private = await uow.runs.get_execution(run.id)
            pending = await uow.runs.list_pending_enqueue()
            assert private is not None
            # Repository 必须按真实 run id归一 operation，不能保留调用方 placeholder。
            assert private.operation_id == f"run:{run.id}:execute"
            assert private.enqueue_state == "enqueue_pending"
            assert private.execution_context["identity"]["auth_method"] == "api-key"
            assert [item.run_id for item in pending] == [run.id]
            queued = await uow.runs.mark_queued(
                run_id=run.id,
                operation_id=private.operation_id,
                message_id="1-0",
            )
            claimed = await uow.runs.claim_execution(
                run_id=run.id,
                operation_id=private.operation_id,
                owner_id="owner-1",
                workflow_id="workflow-1",
            )
            wrong_operation_replay = await uow.runs.claim_execution(
                run_id=run.id,
                operation_id="run:wrong:execute",
                owner_id="owner-1",
                workflow_id="workflow-1",
            )
            competing = await uow.runs.claim_execution(
                run_id=run.id,
                operation_id=private.operation_id,
                owner_id="owner-2",
                workflow_id="workflow-2",
            )
            await uow.commit()
        assert queued.enqueue_state == "queued"
        assert claimed is True
        assert wrong_operation_replay is False
        assert competing is False
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_service_approval_private_state_is_mutually_exclusive(tmp_path: Path) -> None:
    dsn = _dsn(tmp_path / "approval-queue.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-1")
            session = await uow.sessions.create(
                SessionCreate(tenant_id="tenant-1", user_id="user-1", agent_id="agent-1")
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-1",
                    session_id=session.id,
                    agent_id="agent-1",
                    trace_id="trace-approval-private",
                )
            )
            approval = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id="tenant-1",
                    run_id=run.id,
                    agent_id="agent-1",
                    action="shell.execute",
                    resource="tool:shell",
                    reason="dangerous",
                    trace_id="trace-approval-private",
                )
            )
            state = await uow.approvals.claim_service_resolution(
                approval_id=approval.approval_id,
                run_id=run.id,
                tenant_id="tenant-1",
                reviewer_id="reviewer-1",
                decision="approve",
                request_hash="a" * 64,
                request_id="req-approve-1",
            )
            await uow.approvals.mark_resolution_queued(
                approval_id=approval.approval_id,
                lease_id=state.lease_id,
                operation_id=state.operation_id,
                message_id="2-0",
            )
            claim_fields = {
                "approval_id": approval.approval_id,
                "tenant_id": "tenant-1",
                "run_id": run.id,
                "lease_id": state.lease_id,
                "operation_id": state.operation_id,
                "request_id": state.request_id,
                "message_id": "2-0",
                "workflow_owner_id": "invalid-owner",
                "workflow_id": "invalid-workflow",
            }
            for field, invalid in (
                ("tenant_id", "other-tenant"),
                ("run_id", str(uuid4())),
                ("request_id", "other-request"),
                ("message_id", "other-message"),
            ):
                assert not await uow.approvals.claim_resolution_execution(
                    **{**claim_fields, field: invalid}
                )
            await uow.session.execute(
                update(ApprovalModel)
                .where(ApprovalModel.id == approval.approval_id)
                .values(resolution_reviewer_id="")
            )
            assert not await uow.approvals.claim_resolution_execution(**claim_fields)
            await uow.session.execute(
                update(ApprovalModel)
                .where(ApprovalModel.id == approval.approval_id)
                .values(resolution_reviewer_id="reviewer-1", resolution_request_hash="")
            )
            assert not await uow.approvals.claim_resolution_execution(**claim_fields)
            await uow.session.execute(
                update(ApprovalModel)
                .where(ApprovalModel.id == approval.approval_id)
                .values(resolution_request_hash="a" * 64)
            )
            owned = await uow.approvals.claim_resolution_execution(
                approval_id=approval.approval_id,
                tenant_id="tenant-1",
                run_id=run.id,
                lease_id=state.lease_id,
                operation_id=state.operation_id,
                request_id=state.request_id,
                message_id="2-0",
                workflow_owner_id="owner-1",
                workflow_id="workflow-1",
            )
            await uow.commit()

        assert "resolution_enqueue_state" not in approval.to_payload()
        assert owned is True
        async with storage.uow() as uow:
            taken = await uow.approvals.takeover_service_resolution(
                approval_id=approval.approval_id,
                run_id=run.id,
                tenant_id="tenant-1",
                reviewer_id="reviewer-1",
                decision="approve",
                request_hash="a" * 64,
                request_id="req-approve-2",
                expired_before=datetime.now(tz=UTC) + timedelta(seconds=1),
            )
            await uow.commit()
        assert taken is not None
        assert taken.lease_id != state.lease_id
        assert taken.operation_id.endswith(f"lease:{taken.lease_id}")
        assert taken.request_id == "req-approve-2"
        assert taken.enqueue_state == "enqueue_pending"
        assert taken.workflow_id is None
    finally:
        await storage.dispose()
