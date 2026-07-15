"""0014 usage settlement、outbox 与 event capacity migration 合同测试。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from tests.contracts.run_trace_migration_test_helpers import migration_config

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EventCapacityExceeded,
    EvidenceOperationKind,
    operation_event_capacity,
)


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


def _prepare_0013a(path: Path) -> None:
    run_migrations(sqlite_dsn(path), "0013a_run_trace_event_hardening")


def _seed_0013a_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    trace_id: str,
) -> None:
    tenant_id = f"tenant-{run_id}"
    session_id = f"session-{run_id}"
    connection.execute(
        "insert into tenants(id, display_name) values (?, ?)",
        (tenant_id, tenant_id),
    )
    connection.execute(
        "insert into sessions(id, tenant_id, user_id, metadata_json) values (?, ?, ?, '{}')",
        (session_id, tenant_id, f"user-{run_id}"),
    )
    connection.execute(
        "insert into agent_runs("
        "id, tenant_id, session_id, agent_id, status, trace_id, input_json, "
        "execution_context_json) values (?, ?, ?, 'agent-a', ?, ?, '{}', ?)",
        (run_id, tenant_id, session_id, status, trace_id, json.dumps({"trace_id": trace_id})),
    )
    connection.execute(
        "insert into run_trace_bindings(trace_id, tenant_id, root_run_id) values (?, ?, ?)",
        (trace_id, tenant_id, run_id),
    )


def _seed_0013a_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    run_id: str,
    trace_id: str,
    seq: int,
    terminal: bool = False,
) -> None:
    tenant_id = f"tenant-{run_id}"
    event_type = "run.completed" if terminal else "run.started"
    connection.execute(
        "insert into canonical_events("
        "id, tenant_id, run_id, stream_id, event_type, seq, terminal, visibility, "
        "trace_id, record_scope, envelope_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'run', '{}')",
        (
            event_id,
            tenant_id,
            run_id,
            run_id,
            event_type,
            seq,
            int(terminal),
            "public" if terminal else "internal",
            trace_id,
        ),
    )


def test_0014_migration_creates_outbox_and_capacity_tables(tmp_path: Path) -> None:
    path = tmp_path / "usage-migration.db"
    run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        revision = connection.execute("select version_num from alembic_version").fetchone()

    assert {"run_evidence_outbox", "run_event_capacity"} <= tables
    assert revision == ("0014_run_evidence_outbox",)


def test_0014_usage_tables_bind_tenant_and_run_to_the_same_parent_row(tmp_path: Path) -> None:
    """组合外键阻止 tenant 与 run 分别合法、组合却越权的持久化记录。"""

    path = tmp_path / "usage-tenant-run-foreign-keys.db"
    run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        for table_name in ("run_event_capacity", "run_evidence_outbox"):
            grouped_columns: dict[int, set[tuple[str, str]]] = {}
            for foreign_key in connection.execute(f"pragma foreign_key_list({table_name})"):
                foreign_key_id, _, parent_table, source_column, parent_column, *_ = foreign_key
                if parent_table == "agent_runs":
                    grouped_columns.setdefault(foreign_key_id, set()).add(
                        (source_column, parent_column)
                    )

            assert {("run_id", "id"), ("tenant_id", "tenant_id")} in grouped_columns.values()


def test_operation_capacity_registry_is_typed_and_rejects_raw_business_input() -> None:
    assert operation_event_capacity(EvidenceOperationKind.MODEL_USAGE) == 2
    assert operation_event_capacity(EvidenceOperationKind.APPROVAL_RESOLUTION) == 1
    with pytest.raises(ValueError, match="unknown event operation kind"):
        operation_event_capacity(cast(Any, "model_usage"))


def test_0014_upgrade_backfills_terminal_and_active_operation_capacity(tmp_path: Path) -> None:
    path = tmp_path / "usage-backfill.db"
    _prepare_0013a(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        with connection:
            _seed_0013a_run(
                connection,
                run_id="terminal",
                status="completed",
                trace_id="trace-terminal",
            )
            _seed_0013a_event(
                connection,
                event_id="terminal-event",
                run_id="terminal",
                trace_id="trace-terminal",
                seq=9,
                terminal=True,
            )
            _seed_0013a_run(
                connection,
                run_id="idle",
                status="created",
                trace_id="trace-idle",
            )
            _seed_0013a_run(
                connection,
                run_id="active",
                status="waiting",
                trace_id="trace-active",
            )
            connection.execute(
                "insert into approvals("
                "id, tenant_id, run_id, agent_id, action, resource, reason, status, "
                "resolution_state, trace_id, metadata_json) values ("
                "'approval-active', 'tenant-active', 'active', 'agent-a', 'write', "
                "'file:a', 'review', 'waiting', 'claimed', 'trace-active', '{}')"
            )
            connection.execute(
                "insert into tool_invocations("
                "id, tenant_id, agent_id, run_id, tool_name, args_ref, execution_state, "
                "status, trace_id, metadata_json) values ("
                "'tool-active', 'tenant-active', 'agent-a', 'active', 'write', "
                "'artifact://args', 'executing', 'running', 'trace-active', '{}')"
            )

    run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "select run_id, highest_persisted_seq, outstanding_reserved_event_count, "
            "terminal_reservation from run_event_capacity order by run_id"
        ).fetchall()
    assert rows == [
        ("active", 0, 4, 1),
        ("idle", 0, 0, 1),
        ("terminal", 9, 0, 0),
    ]


def test_0014_upgrade_uses_sparse_max_sequence_and_rejects_new_operation(tmp_path: Path) -> None:
    path = tmp_path / "usage-sparse-upgrade.db"
    _prepare_0013a(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        with connection:
            _seed_0013a_run(
                connection,
                run_id="sparse",
                status="created",
                trace_id="trace-sparse",
            )
            _seed_0013a_event(
                connection,
                event_id="sparse-1",
                run_id="sparse",
                trace_id="trace-sparse",
                seq=1,
            )
            _seed_0013a_event(
                connection,
                event_id="sparse-high",
                run_id="sparse",
                trace_id="trace-sparse",
                seq=MAX_EVENT_SEQ - 1,
            )
    run_migrations(sqlite_dsn(path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(path))

    async def verify() -> None:
        try:
            async with storage.uow() as uow:
                snapshot = await uow.event_capacity.snapshot("sparse")
                assert snapshot.highest_persisted_seq == MAX_EVENT_SEQ - 1
                assert snapshot.terminal_reservation == 1
                with pytest.raises(EventCapacityExceeded):
                    await uow.event_capacity.reserve(
                        run_id="sparse",
                        operation_kind=EvidenceOperationKind.MODEL_USAGE,
                    )
        finally:
            await storage.dispose()

    import asyncio

    asyncio.run(verify())


@pytest.mark.parametrize("invalid_state", ["unknown", "approved_pending_unknown"])
def test_0014_upgrade_rejects_unknown_active_state_before_ddl(
    tmp_path: Path,
    invalid_state: str,
) -> None:
    path = tmp_path / f"usage-invalid-{invalid_state}.db"
    _prepare_0013a(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        with connection:
            _seed_0013a_run(
                connection,
                run_id="invalid",
                status="waiting",
                trace_id="trace-invalid",
            )
            connection.execute(
                "insert into approvals("
                "id, tenant_id, run_id, agent_id, action, resource, reason, status, "
                "resolution_state, trace_id, metadata_json) values ("
                "'approval-invalid', 'tenant-invalid', 'invalid', 'agent-a', 'write', "
                "'file:a', 'review', 'waiting', ?, 'trace-invalid', '{}')",
                (invalid_state,),
            )

    with pytest.raises(RuntimeError, match="approval operation state is unknown"):
        run_migrations(sqlite_dsn(path))

    with sqlite3.connect(path) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0013a_run_trace_event_hardening",
        )
        assert connection.execute(
            "select count(*) from sqlite_master where type='table' and name='run_event_capacity'"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "x_args",
    [
        [],
        ["allow_empty_evidence_downgrade=false"],
        ["allow_empty_evidence_downgrade=True"],
        ["allow_empty_evidence_downgrade=true", "allow_empty_evidence_downgrade=true"],
        ["allow_empty_evidence_downgrade=true", "unrelated_flag=1"],
    ],
)
def test_0014_downgrade_requires_exact_opt_in(tmp_path: Path, x_args: list[str]) -> None:
    path = tmp_path / f"usage-downgrade-{len(x_args)}-{hash(tuple(x_args))}.db"
    run_migrations(sqlite_dsn(path))
    with pytest.raises(RuntimeError, match="explicit opt-in"):
        command.downgrade(
            migration_config(sqlite_dsn(path), x_args=x_args),
            "0013a_run_trace_event_hardening",
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0014_run_evidence_outbox",
        )


def test_0014_empty_database_downgrades_with_exact_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "usage-empty-downgrade.db"
    run_migrations(sqlite_dsn(path))
    command.downgrade(
        migration_config(
            sqlite_dsn(path),
            x_args=["allow_empty_evidence_downgrade=true"],
        ),
        "0013a_run_trace_event_hardening",
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0013a_run_trace_event_hardening",
        )
        assert connection.execute(
            "select count(*) from sqlite_master where type='table' "
            "and name in ('run_evidence_outbox', 'run_event_capacity')"
        ).fetchone() == (0,)


@pytest.mark.parametrize("state", ["started", "result_persisted", "published"])
def test_0014_any_outbox_state_blocks_downgrade(tmp_path: Path, state: str) -> None:
    path = tmp_path / f"usage-outbox-{state}.db"
    run_migrations(sqlite_dsn(path))
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        with connection:
            _seed_0013a_run(
                connection,
                run_id="evidence",
                status="created",
                trace_id="trace-evidence",
            )
            connection.execute(
                "insert into run_evidence_outbox("
                "id, tenant_id, run_id, usage_call_id, event_id, operation_kind, state, "
                "reserved_event_count) values ("
                "'outbox-evidence', 'tenant-evidence', 'evidence', 'usage-evidence', "
                "'usage:tenant-evidence:usage-evidence:final', 'model_usage', ?, 2)",
                (state,),
            )

    with pytest.raises(RuntimeError, match="evidence exists"):
        command.downgrade(
            migration_config(
                sqlite_dsn(path),
                x_args=["allow_empty_evidence_downgrade=true"],
            ),
            "0013a_run_trace_event_hardening",
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0014_run_evidence_outbox",
        )
        assert connection.execute(
            "select state from run_evidence_outbox where id='outbox-evidence'"
        ).fetchone() == (state,)
