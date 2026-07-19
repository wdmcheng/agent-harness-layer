"""0016 在任何 DDL 前验证完整 parent graph。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from tests.contracts.run_trace_migration_test_helpers import seed_identity

from agent_harness.storage import run_migrations


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    tenant_id: str,
    parent_run_id: str | None,
    agent_id: str,
) -> None:
    connection.execute(
        "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,input_json,"
        "parent_run_id) values (?,?,?,?,?,?,?,?)",
        (
            run_id,
            tenant_id,
            f"session-{tenant_id}",
            agent_id,
            "running",
            f"trace-{tenant_id}",
            "{}",
            parent_run_id,
        ),
    )


def _relation(
    connection: sqlite3.Connection,
    *,
    relation_id: str,
    tenant_id: str,
    parent_run_id: str,
    child_run_id: str,
    source_agent_id: str,
    target_agent_id: str,
) -> None:
    connection.execute(
        "insert into agent_delegations(id,tenant_id,parent_run_id,child_run_id,source_agent_id,"
        "target_agent_id,idempotency_key,request_hash,budget_intent,child_input_json,identity_json,"
        "trace_id,status,event_operation_kind,event_registry_version,reserved_event_count) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            relation_id,
            tenant_id,
            parent_run_id,
            child_run_id,
            source_agent_id,
            target_agent_id,
            f"key-{relation_id}",
            "a" * 64,
            "inherit_parent",
            "{}",
            "{}",
            f"trace-{tenant_id}",
            "claimed",
            "delegation",
            "v1",
            3,
        ),
    )


def _allow_duplicate_child_relations(connection: sqlite3.Connection) -> None:
    """移除 0015 child unique，模拟旧 writer 或人工操作留下的损坏关系图。"""

    connection.execute("pragma foreign_keys=off")
    connection.execute(
        "create table agent_delegations_corrupt as select * from agent_delegations where 0"
    )
    connection.execute("drop table agent_delegations")
    connection.execute("alter table agent_delegations_corrupt rename to agent_delegations")


@pytest.mark.parametrize(
    "case",
    [
        "three-level",
        "orphan",
        "cycle",
        "cross-tenant",
        "relation-missing",
        "relation-duplicate",
    ],
)
def test_0016_rejects_invalid_full_parent_graph_before_ddl(tmp_path: Path, case: str) -> None:
    path = tmp_path / f"topology-{case}.sqlite3"
    run_migrations(_dsn(path), "0015_agent_delegation")
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        if case == "cross-tenant":
            seed_identity(connection, "tenant-b")
        if case == "orphan":
            _run(
                connection,
                run_id="child-a",
                tenant_id="tenant-a",
                parent_run_id="missing-root",
                agent_id="agent-b",
            )
        elif case == "cycle":
            _run(
                connection,
                run_id="run-a",
                tenant_id="tenant-a",
                parent_run_id="run-b",
                agent_id="agent-a",
            )
            _run(
                connection,
                run_id="run-b",
                tenant_id="tenant-a",
                parent_run_id="run-a",
                agent_id="agent-b",
            )
        else:
            _run(
                connection,
                run_id="root-a",
                tenant_id="tenant-a",
                parent_run_id=None,
                agent_id="agent-a",
            )
            child_tenant = "tenant-b" if case == "cross-tenant" else "tenant-a"
            _run(
                connection,
                run_id="child-a",
                tenant_id=child_tenant,
                parent_run_id="root-a",
                agent_id="agent-b",
            )
            if case == "three-level":
                _relation(
                    connection,
                    relation_id="delegation-a",
                    tenant_id="tenant-a",
                    parent_run_id="root-a",
                    child_run_id="child-a",
                    source_agent_id="agent-a",
                    target_agent_id="agent-b",
                )
                _run(
                    connection,
                    run_id="grandchild-a",
                    tenant_id="tenant-a",
                    parent_run_id="child-a",
                    agent_id="agent-c",
                )
                _relation(
                    connection,
                    relation_id="delegation-b",
                    tenant_id="tenant-a",
                    parent_run_id="child-a",
                    child_run_id="grandchild-a",
                    source_agent_id="agent-b",
                    target_agent_id="agent-c",
                )
            elif case == "relation-duplicate":
                _allow_duplicate_child_relations(connection)
                for relation_id in ("delegation-a", "delegation-b"):
                    _relation(
                        connection,
                        relation_id=relation_id,
                        tenant_id="tenant-a",
                        parent_run_id="root-a",
                        child_run_id="child-a",
                        source_agent_id="agent-a",
                        target_agent_id="agent-b",
                    )
        connection.commit()

    with pytest.raises(RuntimeError, match="0016 parent graph is invalid"):
        run_migrations(_dsn(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0015_agent_delegation",
        )
        assert connection.execute(
            "select count(*) from sqlite_master where type='table' and name='parent_budget_ledgers'"
        ).fetchone() == (0,)
        if case == "relation-duplicate":
            assert connection.execute(
                "select count(*) from agent_delegations where child_run_id='child-a'"
            ).fetchone() == (2,)
