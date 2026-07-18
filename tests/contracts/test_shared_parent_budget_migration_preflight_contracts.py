"""0016 fresh、legacy closure 与 pending-work preflight 合同。"""

# 场景文件共享同一 SQLite migration helper 与 canonical hash 规则。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_migration_contracts import *


def test_0016_fresh_upgrade_and_empty_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite3"
    run_migrations(sqlite_dsn(path))
    assert get_current_revision(sqlite_dsn(path)) == "0016_shared_parent_budget_ledger"
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
    assert {
        "parent_budget_ledgers",
        "budget_operation_claims",
        "delegation_budget_allocations",
    } <= tables

    command.downgrade(
        migration_config(sqlite_dsn(path), x_args=["allow_empty_evidence_downgrade=true"]),
        "0015_agent_delegation",
    )
    assert get_current_revision(sqlite_dsn(path)) == "0015_agent_delegation"


def test_0016_rejects_active_legacy_tree_before_ddl(tmp_path: Path) -> None:
    path = tmp_path / "active-legacy.sqlite3"
    run_migrations(sqlite_dsn(path), "0015_agent_delegation")
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        connection.execute(
            "insert into run_trace_bindings(trace_id,tenant_id,root_run_id) "
            "values ('trace-a','tenant-a','root-a')"
        )
        connection.execute(
            "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,input_json) "
            "values ('root-a','tenant-a','session-tenant-a','agent-a','running','trace-a','{}')"
        )
        connection.execute(
            "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
            "outstanding_reserved_event_count,terminal_reservation) "
            "values ('root-a','tenant-a',0,0,1)"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="active without immutable budget snapshot"):
        run_migrations(sqlite_dsn(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "select count(*) from sqlite_master where type='table' and name='parent_budget_ledgers'"
        ).fetchone() == (0,)
        assert connection.execute("select version_num from alembic_version").fetchone() == (
            "0015_agent_delegation",
        )


def test_0016_preserves_strictly_closed_legacy_tree_without_ledger(tmp_path: Path) -> None:
    path = tmp_path / "closed-legacy.sqlite3"
    run_migrations(sqlite_dsn(path), "0015_agent_delegation")
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        connection.execute(
            "insert into run_trace_bindings(trace_id,tenant_id,root_run_id) "
            "values ('trace-a','tenant-a','root-a')"
        )
        connection.execute(
            "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,input_json) "
            "values ('root-a','tenant-a','session-tenant-a','agent-a','completed','trace-a','{}')"
        )
        connection.execute(
            "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
            "outstanding_reserved_event_count,terminal_reservation) "
            "values ('root-a','tenant-a',1,0,0)"
        )
        connection.commit()

    run_migrations(sqlite_dsn(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute("select count(*) from parent_budget_ledgers").fetchone() == (0,)
        assert connection.execute("select status from agent_runs where id='root-a'").fetchone() == (
            "completed",
        )


def test_0016_downgrade_refuses_any_shared_budget_evidence(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite3"
    run_migrations(sqlite_dsn(path))
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        connection.execute(
            "insert into run_trace_bindings(trace_id,tenant_id,root_run_id) "
            "values ('trace-a','tenant-a','root-a')"
        )
        connection.execute(
            "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,input_json) "
            "values ('root-a','tenant-a','session-tenant-a','agent-a','running','trace-a','{}')"
        )
        connection.execute(
            "insert into parent_budget_ledgers("
            "tenant_id,budget_owner_run_id,token_limit,cost_limit,cost_enabled,token_impact,"
            "cost_impact,state,version,registry_version,config_version,catalog_version,"
            "snapshot_id,snapshot_hash,snapshot_json) values ("
            "'tenant-a','root-a',100,null,0,0,0,'active',0,'r','c','k','s',"
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','{}')"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="shared budget evidence exists"):
        command.downgrade(
            migration_config(sqlite_dsn(path), x_args=["allow_empty_evidence_downgrade=true"]),
            "0015_agent_delegation",
        )
    assert get_current_revision(sqlite_dsn(path)) == "0016_shared_parent_budget_ledger"


@pytest.mark.parametrize(
    "pending_kind",
    [
        "child",
        "approval",
        "root-queue",
        "root-queued",
        "root-owned",
        "child-queue",
        "approval-enqueue",
        "approval-owned",
    ],
)
def test_0016_rejects_terminal_root_with_pending_tree_work(
    tmp_path: Path,
    pending_kind: str,
) -> None:
    """Root status 不能掩盖 child、approval 或 recovery 尚未封闭。"""

    path = tmp_path / f"pending-{pending_kind}.sqlite3"
    run_migrations(sqlite_dsn(path), "0015_agent_delegation")
    with sqlite3.connect(path) as connection:
        seed_identity(connection, "tenant-a")
        connection.execute(
            "insert into run_trace_bindings(trace_id,tenant_id,root_run_id) "
            "values ('trace-a','tenant-a','root-a')"
        )
        connection.execute(
            "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,input_json) "
            "values ('root-a','tenant-a','session-tenant-a','agent-a','completed','trace-a','{}')"
        )
        if pending_kind in {"root-queue", "root-queued", "root-owned"}:
            connection.execute(
                "update agent_runs set queue_operation_id='run:root-a:execute',"
                "queue_enqueue_state=?,queue_message_id=?,execution_owner_id=?,"
                "execution_workflow_id=? where id='root-a'",
                (
                    "enqueue_pending" if pending_kind == "root-queue" else "queued",
                    None if pending_kind == "root-queue" else "message-root-a",
                    "worker-a" if pending_kind == "root-owned" else None,
                    "workflow-a" if pending_kind == "root-owned" else None,
                ),
            )
        connection.execute(
            "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
            "outstanding_reserved_event_count,terminal_reservation) "
            "values ('root-a','tenant-a',1,0,0)"
        )
        if pending_kind in {"child", "child-queue"}:
            connection.execute(
                "insert into agent_runs("
                "id,tenant_id,session_id,agent_id,status,parent_run_id,trace_id,input_json,"
                "queue_operation_id,queue_enqueue_state"
                ") values ("
                "'child-a','tenant-a','session-tenant-a','agent-b',?,'root-a','trace-a','{}',?,?"
                ")",
                (
                    "running" if pending_kind == "child" else "completed",
                    None if pending_kind == "child" else "run:child-a:execute",
                    None if pending_kind == "child" else "enqueue_pending",
                ),
            )
            connection.execute(
                "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
                "outstanding_reserved_event_count,terminal_reservation) "
                "values ('child-a','tenant-a',0,0,?)",
                (1 if pending_kind == "child" else 0,),
            )
        else:
            if pending_kind in {"approval", "approval-enqueue", "approval-owned"}:
                connection.execute(
                    "insert into approvals("
                    "id,tenant_id,run_id,agent_id,action,resource,reason,status,trace_id,"
                    "metadata_json,resolution_state,resolution_operation_id,"
                    "resolution_enqueue_state,resolution_workflow_owner_id,"
                    "resolution_workflow_id) values ("
                    "'approval-a','tenant-a','root-a','agent-a','tool.call','tool:a',"
                    "'pending',?,'trace-a','{}',?,?,?,?,?)",
                    (
                        "waiting" if pending_kind == "approval" else "approved",
                        None if pending_kind == "approval" else "completed",
                        None if pending_kind == "approval" else "approval:resolve",
                        None if pending_kind == "approval" else "enqueue_pending",
                        "worker-a" if pending_kind == "approval-owned" else None,
                        "workflow-a" if pending_kind == "approval-owned" else None,
                    ),
                )
        connection.commit()

    expected = (
        "active without immutable budget snapshot|pending approval recovery|pending queue recovery"
    )
    with pytest.raises(RuntimeError, match=expected):
        run_migrations(sqlite_dsn(path))
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "select count(*) from sqlite_master where type='table' and name='parent_budget_ledgers'"
        ).fetchone() == (0,)
