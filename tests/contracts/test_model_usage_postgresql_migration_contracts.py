"""Evidence outbox 0014 的真实 PostgreSQL migration 合同测试。"""

from __future__ import annotations

import asyncio
import json
import os
from argparse import Namespace

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.storage import run_migrations
from agent_harness.storage.migrations.runner import alembic_config

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL migration 合同需要 AGENT_HARNESS_TEST_POSTGRES_DSN。",
)

REVISION_0013A = "0013a_run_trace_event_hardening"
REVISION_0014 = "0014_run_evidence_outbox"
OPT_IN = "allow_empty_evidence_downgrade=true"


async def _upgrade(dsn: str, revision: str = REVISION_0014) -> None:
    """在线程中运行 Alembic 升级，避免阻塞异步 PostgreSQL 合同场景。"""

    await asyncio.to_thread(run_migrations, dsn, revision)


async def _downgrade(dsn: str, x_args: list[str]) -> None:
    """以显式 Alembic ``-x`` 参数执行回退，供 opt-in 安全门禁场景复用。"""

    config = alembic_config(dsn)
    config.cmd_opts = Namespace(x=x_args)
    await asyncio.to_thread(command.downgrade, config, REVISION_0013A)


async def _seed_run(
    connection: AsyncConnection,
    *,
    run_id: str,
    status: str,
    trace_id: str,
) -> None:
    """直接写入 0013A 兼容的 tenant/session/run/trace 基线，供 0014 回填验证。"""

    tenant_id = f"tenant-{run_id}"
    session_id = f"session-{run_id}"
    await connection.execute(
        text("insert into tenants(id, display_name) values (:tenant_id, :tenant_id)"),
        {"tenant_id": tenant_id},
    )
    await connection.execute(
        text(
            "insert into sessions(id, tenant_id, user_id, metadata_json) values ("
            ":session_id, :tenant_id, :user_id, cast('{}' as json))"
        ),
        {"session_id": session_id, "tenant_id": tenant_id, "user_id": f"user-{run_id}"},
    )
    await connection.execute(
        text(
            "insert into agent_runs("
            "id, tenant_id, session_id, agent_id, status, trace_id, input_json, "
            "execution_context_json) values ("
            ":run_id, :tenant_id, :session_id, 'agent-a', :status, :trace_id, "
            "cast('{}' as json), cast(:execution_context as json))"
        ),
        {
            "run_id": run_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "status": status,
            "trace_id": trace_id,
            "execution_context": json.dumps({"trace_id": trace_id}),
        },
    )
    await connection.execute(
        text(
            "insert into run_trace_bindings(trace_id, tenant_id, root_run_id) values ("
            ":trace_id, :tenant_id, :run_id)"
        ),
        {"trace_id": trace_id, "tenant_id": tenant_id, "run_id": run_id},
    )


async def _seed_event(
    connection: AsyncConnection,
    *,
    event_id: str,
    run_id: str,
    trace_id: str,
    seq: int,
    terminal: bool = False,
) -> None:
    """写入升级前 canonical event 行，控制 terminal 与序号以断言容量回填结果。"""

    await connection.execute(
        text(
            "insert into canonical_events("
            "id, tenant_id, run_id, stream_id, event_type, seq, terminal, visibility, "
            "trace_id, record_scope, envelope_json) values ("
            ":event_id, :tenant_id, :run_id, :run_id, :event_type, :seq, :terminal, "
            ":visibility, :trace_id, 'run', cast('{}' as json))"
        ),
        {
            "event_id": event_id,
            "tenant_id": f"tenant-{run_id}",
            "run_id": run_id,
            "event_type": "run.completed" if terminal else "run.started",
            "seq": seq,
            "terminal": terminal,
            "visibility": "public" if terminal else "internal",
            "trace_id": trace_id,
        },
    )


@pytest.mark.asyncio
async def test_0014_postgresql_upgrade_backfills_terminal_idle_and_active_capacity() -> None:
    """验证 0014 升级为终态、空闲和活动 run 回填不同的容量与预约快照。"""

    async with isolated_database("usage_migration_backfill") as dsn:
        await _upgrade(dsn, REVISION_0013A)
        engine = create_async_engine(dsn)
        async with engine.begin() as connection:
            await _seed_run(connection, run_id="terminal", status="completed", trace_id="trace-t")
            await _seed_event(
                connection,
                event_id="terminal-event",
                run_id="terminal",
                trace_id="trace-t",
                seq=9,
                terminal=True,
            )
            await _seed_run(connection, run_id="idle", status="created", trace_id="trace-i")
            await _seed_run(connection, run_id="active", status="waiting", trace_id="trace-a")
            await connection.execute(
                text(
                    "insert into approvals("
                    "id, tenant_id, run_id, agent_id, action, resource, reason, status, "
                    "resolution_state, trace_id, metadata_json) values ("
                    "'approval-active', 'tenant-active', 'active', 'agent-a', 'write', "
                    "'file:a', 'review', 'waiting', 'claimed', 'trace-a', cast('{}' as json))"
                )
            )
            await connection.execute(
                text(
                    "insert into tool_invocations("
                    "id, tenant_id, agent_id, run_id, tool_name, args_ref, execution_state, "
                    "status, trace_id, metadata_json) values ("
                    "'tool-active', 'tenant-active', 'agent-a', 'active', 'write', "
                    "'artifact://args', 'executing', 'running', 'trace-a', cast('{}' as json))"
                )
            )
        await engine.dispose()

        await _upgrade(dsn)
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "select run_id, highest_persisted_seq, outstanding_reserved_event_count, "
                        "terminal_reservation from run_event_capacity order by run_id"
                    )
                )
            ).all()
        await engine.dispose()
        assert rows == [("active", 0, 4, 1), ("idle", 0, 0, 1), ("terminal", 9, 0, 0)]


@pytest.mark.asyncio
async def test_0014_postgresql_unknown_state_fails_before_schema_mutation() -> None:
    """验证未知审批状态使升级在 schema 变更前失败，保留原 revision 和源数据。"""

    async with isolated_database("usage_migration_unknown") as dsn:
        await _upgrade(dsn, REVISION_0013A)
        engine = create_async_engine(dsn)
        async with engine.begin() as connection:
            await _seed_run(
                connection, run_id="invalid", status="waiting", trace_id="trace-invalid"
            )
            await connection.execute(
                text(
                    "insert into approvals("
                    "id, tenant_id, run_id, agent_id, action, resource, reason, status, "
                    "resolution_state, trace_id, metadata_json) values ("
                    "'approval-invalid', 'tenant-invalid', 'invalid', 'agent-a', 'write', "
                    "'file:a', 'review', 'waiting', 'unknown', 'trace-invalid', cast('{}' as json))"
                )
            )
        await engine.dispose()

        with pytest.raises(RuntimeError, match="approval operation state is unknown"):
            await _upgrade(dsn)

        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("select version_num from alembic_version"))
            ).scalar_one()
            capacity_table = (
                await connection.execute(text("select to_regclass('public.run_event_capacity')"))
            ).scalar_one()
            approval_state = (
                await connection.execute(
                    text("select resolution_state from approvals where id='approval-invalid'")
                )
            ).scalar_one()
        await engine.dispose()
        assert revision == REVISION_0013A
        assert capacity_table is None
        assert approval_state == "unknown"


@pytest.mark.parametrize(
    "x_args",
    [
        [],
        ["allow_empty_evidence_downgrade=false"],
        ["allow_empty_evidence_downgrade=True"],
        [OPT_IN, OPT_IN],
        [OPT_IN, "unrelated_flag=1"],
    ],
)
@pytest.mark.asyncio
async def test_0014_postgresql_downgrade_requires_exact_opt_in(x_args: list[str]) -> None:
    """验证 downgrade 仅接受单个精确 opt-in，大小写、重复或附带参数都被拒绝。"""

    async with isolated_database("usage_migration_opt_in") as dsn:
        await _upgrade(dsn)
        with pytest.raises(RuntimeError, match="explicit opt-in"):
            await _downgrade(dsn, x_args)
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("select version_num from alembic_version"))
            ).scalar_one()
        await engine.dispose()
        assert revision == REVISION_0014


@pytest.mark.asyncio
async def test_0014_postgresql_empty_database_downgrades_with_exact_opt_in() -> None:
    """验证空数据库在精确 opt-in 下可以安全回退并删除 outbox 表。"""

    async with isolated_database("usage_migration_empty") as dsn:
        await _upgrade(dsn)
        await _downgrade(dsn, [OPT_IN])
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            revision = (
                await connection.execute(text("select version_num from alembic_version"))
            ).scalar_one()
            outbox_table = (
                await connection.execute(text("select to_regclass('public.run_evidence_outbox')"))
            ).scalar_one()
        await engine.dispose()
        assert revision == REVISION_0013A
        assert outbox_table is None


@pytest.mark.parametrize("state", ["started", "result_persisted", "published"])
@pytest.mark.asyncio
async def test_0014_postgresql_any_outbox_state_blocks_downgrade(state: str) -> None:
    """验证任一 durable outbox 状态存在时都阻止回退，避免删除仍需恢复的证据。"""

    async with isolated_database("usage_migration_outbox") as dsn:
        await _upgrade(dsn)
        engine = create_async_engine(dsn)
        async with engine.begin() as connection:
            await _seed_run(connection, run_id="evidence", status="created", trace_id="trace-e")
            await connection.execute(
                text(
                    "insert into run_event_capacity("
                    "run_id, tenant_id, highest_persisted_seq, outstanding_reserved_event_count, "
                    "terminal_reservation) values ('evidence', 'tenant-evidence', 0, 2, 1)"
                )
            )
            await connection.execute(
                text(
                    "insert into run_evidence_outbox("
                    "id, tenant_id, run_id, usage_call_id, event_id, operation_kind, state, "
                    "reserved_event_count) values ("
                    "'outbox-evidence', 'tenant-evidence', 'evidence', 'usage-evidence', "
                    "'usage:tenant-evidence:usage-evidence:final', 'model_usage', :state, 2)"
                ),
                {"state": state},
            )
        await engine.dispose()

        with pytest.raises(RuntimeError, match="evidence exists"):
            await _downgrade(dsn, [OPT_IN])
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            persisted_state = (
                await connection.execute(
                    text("select state from run_evidence_outbox where id='outbox-evidence'")
                )
            ).scalar_one()
        await engine.dispose()
        assert persisted_state == state
