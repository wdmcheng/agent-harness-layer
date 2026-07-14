"""0013 canonical run trace 的 PostgreSQL preflight 合同。"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from tests.contracts.run_trace_migration_test_helpers import (
    INVALID_RUN_RELATION_IDS,
    RUN_RELATION_TABLES,
)

from agent_harness.storage import run_migrations


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="PostgreSQL trace migration contract runs when service smoke provides a DSN.",
)
@pytest.mark.parametrize("table", RUN_RELATION_TABLES)
@pytest.mark.parametrize("scenario", ["orphan", "cross_tenant"])
@pytest.mark.asyncio
async def test_0013_postgresql_preflight_rejects_invalid_run_relations_without_mutation(
    table: str,
    scenario: str,
) -> None:
    """真实 PostgreSQL 也必须在 0013 DDL/DML 前拒绝非法 run 关系。"""

    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

    async def snapshot(connection: AsyncConnection) -> tuple[object, ...]:
        execute = connection.execute
        tables = tuple(
            (
                await execute(
                    text(
                        "select table_name from information_schema.tables "
                        "where table_schema='public' order by table_name"
                    )
                )
            ).all()
        )
        columns = tuple(
            (
                await execute(
                    text(
                        "select table_name, column_name, udt_name, is_nullable, column_default "
                        "from information_schema.columns where table_schema='public' "
                        "order by table_name, ordinal_position"
                    )
                )
            ).all()
        )
        indexes = tuple(
            (
                await execute(
                    text(
                        "select tablename, indexname, indexdef from pg_indexes "
                        "where schemaname='public' order by tablename, indexname"
                    )
                )
            ).all()
        )
        constraints = tuple(
            (
                await execute(
                    text(
                        "select conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
                        "from pg_constraint where connamespace='public'::regnamespace "
                        "order by conrelid::regclass::text, conname"
                    )
                )
            ).all()
        )
        revision = (await execute(text("select version_num from alembic_version"))).scalar_one()
        data: list[tuple[str, str]] = []
        for (table_name,) in tables:
            escaped_table = str(table_name).replace('"', '""')
            rows = (
                await connection.exec_driver_sql(
                    "select coalesce("
                    "json_agg(row_to_json(snapshot_row) order by row_to_json(snapshot_row)::text)"
                    f"::text, '[]') from \"{escaped_table}\" snapshot_row"
                )
            ).scalar_one()
            data.append((str(table_name), str(rows)))
        return tables, columns, indexes, constraints, revision, tuple(data)

    base_url = make_url(os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"])
    database_name = f"agent_harness_trace_preflight_{uuid4().hex}"
    admin_url = base_url.set(database="postgres")
    test_url = base_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    await admin_engine.dispose()

    dsn = test_url.render_as_string(hide_password=False)
    engine = create_async_engine(dsn)
    insert_statements = {
        "approvals": (
            "insert into approvals("
            "id, tenant_id, run_id, agent_id, action, resource, reason, status, "
            "metadata_json, trace_id"
            ") values (:record_id, :tenant_id, :run_id, 'agent-a', 'write', 'file:a', "
            "'review', 'waiting', '{}', null)"
        ),
        "artifacts": (
            "insert into artifacts("
            "id, tenant_id, run_id, artifact_type, uri, checksum_sha256, size_bytes, "
            "metadata_json"
            ") values (:record_id, :tenant_id, :run_id, 'result', 'artifact://invalid', "
            "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1, '{}')"
        ),
        "canonical_events": (
            "insert into canonical_events("
            "id, tenant_id, run_id, event_type, seq, terminal, visibility, trace_id, "
            "envelope_json"
            ") values (:record_id, :tenant_id, :run_id, 'run.started', 1, false, "
            "'public', null, '{}')"
        ),
        "checkpoints": (
            "insert into checkpoints("
            "id, tenant_id, run_id, sequence, resume_token, state_json"
            ") values (:record_id, :tenant_id, :run_id, 1, 'invalid-resume', '{}')"
        ),
        "context_assemblies": (
            "insert into context_assemblies("
            "id, tenant_id, run_id, input_refs_json, token_budget, trust_summary_json, "
            "truncation_summary_json, output_ref"
            ") values (:record_id, :tenant_id, :run_id, '[]', 1, '{}', '{}', "
            "'artifact://context')"
        ),
        "eval_cases": (
            "insert into eval_cases("
            "id, tenant_id, name, status, payload_json, run_id, trace_id"
            ") values (:record_id, :tenant_id, 'invalid', 'draft', '{}', :run_id, null)"
        ),
        "trace_refs": (
            "insert into trace_refs("
            "id, tenant_id, run_id, provider, external_trace_id"
            ") values (:record_id, :tenant_id, :run_id, 'provider-a', 'external-a')"
        ),
        "eval_runs": (
            "insert into eval_runs(id, tenant_id, run_id, status) "
            "values (:record_id, :tenant_id, :run_id, 'completed')"
        ),
        "eval_scores": (
            "insert into eval_scores("
            "id, tenant_id, eval_run_id, case_id, run_id, trace_id, metric, value, "
            "metadata_json, provider_status_json"
            ") values (:record_id, :tenant_id, 'support-eval-run', 'support-eval-case', "
            ":run_id, null, 'quality', 1.0, '{}', '[]')"
        ),
        "tool_invocations": (
            "insert into tool_invocations("
            "id, tenant_id, agent_id, run_id, tool_name, args_ref, status, trace_id, "
            "metadata_json"
            ") values (:record_id, :tenant_id, 'agent-a', :run_id, 'write', "
            "'artifact://args', 'completed', null, '{}')"
        ),
        "workspaces": (
            "insert into workspaces("
            "id, tenant_id, agent_id, run_id, root_path, policy_ref, metadata_json"
            ") values (:record_id, :tenant_id, 'agent-a', :run_id, '/tmp/workspace', "
            "'policy://default', '{}')"
        ),
    }
    try:
        await asyncio.to_thread(run_migrations, dsn, "0012a_embedding_cache_tenant_scope")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "insert into tenants(id, display_name) values "
                    "('tenant-a', 'A'), ('tenant-b', 'B')"
                )
            )
            await connection.execute(
                text(
                    "insert into sessions(id, tenant_id, user_id, metadata_json) values "
                    "('session-a', 'tenant-a', 'user-a', '{}'), "
                    "('session-b', 'tenant-b', 'user-b', '{}')"
                )
            )
            await connection.execute(
                text(
                    "insert into agent_runs(id, tenant_id, session_id, agent_id, status, "
                    "input_json, execution_context_json) values "
                    "('root-a', 'tenant-a', 'session-a', 'agent-a', 'created', '{}', '{}')"
                )
            )
            if scenario == "orphan":
                constraint_names = (
                    await connection.execute(
                        text(
                            "select conname from pg_constraint "
                            "where conrelid=to_regclass(:table_name) and contype='f' "
                            "and confrelid='agent_runs'::regclass"
                        ),
                        {"table_name": table},
                    )
                ).scalars()
                for constraint_name in constraint_names:
                    quoted_name = constraint_name.replace('"', '""')
                    await connection.exec_driver_sql(
                        f'alter table "{table}" drop constraint "{quoted_name}"'
                    )
            tenant_id = "tenant-b" if scenario == "cross_tenant" else "tenant-a"
            if table == "eval_scores":
                await connection.execute(
                    text(
                        "insert into eval_cases(id, tenant_id, name, status, payload_json) "
                        "values ('support-eval-case', :tenant_id, 'support', "
                        "'approved', '{}')"
                    ),
                    {"tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        "insert into eval_runs(id, tenant_id, eval_case_id, status) "
                        "values ('support-eval-run', :tenant_id, "
                        "'support-eval-case', 'completed')"
                    ),
                    {"tenant_id": tenant_id},
                )
            await connection.execute(
                text(insert_statements[table]),
                {
                    "record_id": INVALID_RUN_RELATION_IDS[table],
                    "tenant_id": tenant_id,
                    "run_id": "root-a" if scenario == "cross_tenant" else "missing-run",
                },
            )
        async with engine.connect() as connection:
            before = await snapshot(connection)

        await engine.dispose()
        with pytest.raises(RuntimeError, match="preflight failed"):
            await asyncio.to_thread(run_migrations, dsn)

        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            after = await snapshot(connection)
            run_trace_table = (
                await connection.execute(text("select to_regclass('run_trace_bindings')"))
            ).scalar_one()
        assert after == before
        assert after[4] == "0012a_embedding_cache_tenant_scope"
        assert run_trace_table is None
    finally:
        await engine.dispose()
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with admin_engine.connect() as connection:
            await connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        await admin_engine.dispose()
