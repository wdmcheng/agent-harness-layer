"""真实 PostgreSQL 0016 全拓扑 DDL 前拒绝合同。"""

# 复用隔离数据库、migration runner 与 PostgreSQL text helper。
# ruff: noqa: F403, F405
from tests.contracts.test_shared_parent_budget_postgresql_contracts import *


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "topology_case",
    [
        "three-level",
        "orphan",
        "cycle",
        "cross-tenant",
        "relation-missing",
        "relation-duplicate",
    ],
)
async def test_postgresql_0016_rejects_invalid_parent_graph_before_ddl(
    topology_case: str,
) -> None:
    """坏节点不能因不属于任何合法 root 分类而逃过全表扫描。"""

    async with isolated_database(f"shared_budget_topology_{topology_case}") as dsn:
        await asyncio.to_thread(run_migrations, dsn, "0015_agent_delegation")
        engine = create_async_engine(dsn)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "insert into tenants(id,display_name) values "
                    "('tenant-a','tenant-a'),('tenant-b','tenant-b')"
                )
            )
            await connection.execute(
                text(
                    "insert into sessions(id,tenant_id,user_id,metadata_json) values "
                    "('session-a','tenant-a','user-a',cast('{}' as jsonb)),"
                    "('session-b','tenant-b','user-b',cast('{}' as jsonb))"
                )
            )
            await connection.execute(
                text(
                    "insert into run_trace_bindings(trace_id,tenant_id,root_run_id) values "
                    "('trace-a','tenant-a','root-a'),('trace-b','tenant-b','root-b')"
                )
            )
            if topology_case in {"orphan", "cross-tenant"}:
                # 模拟被旧 writer/人工修复破坏的 pre-0016 数据；0016 仍须自行 fail closed。
                await connection.execute(
                    text("alter table agent_runs drop constraint fk_agent_runs_parent_tenant")
                )

            async def insert_run(
                run_id: str,
                *,
                tenant_id: str = "tenant-a",
                parent_run_id: str | None = None,
                agent_id: str = "agent-a",
                trace_id: str = "trace-a",
            ) -> None:
                await connection.execute(
                    text(
                        "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,"
                        "input_json,parent_run_id) values (:id,:tenant_id,:session_id,:agent_id,"
                        "'running',:trace_id,cast('{}' as jsonb),:parent_run_id)"
                    ),
                    {
                        "id": run_id,
                        "tenant_id": tenant_id,
                        "session_id": "session-a" if tenant_id == "tenant-a" else "session-b",
                        "agent_id": agent_id,
                        "trace_id": trace_id,
                        "parent_run_id": parent_run_id,
                    },
                )

            async def insert_relation(
                relation_id: str,
                *,
                parent_run_id: str,
                child_run_id: str,
                source_agent_id: str,
                target_agent_id: str,
            ) -> None:
                await connection.execute(
                    text(
                        "insert into agent_delegations("
                        "id,tenant_id,parent_run_id,child_run_id,source_agent_id,target_agent_id,"
                        "idempotency_key,request_hash,budget_intent,child_input_json,identity_json,"
                        "trace_id,status,error_json,event_operation_kind,event_registry_version,"
                        "reserved_event_count) values ("
                        ":id,'tenant-a',:parent_run_id,:child_run_id,:source_agent_id,"
                        ":target_agent_id,:idempotency_key,:request_hash,'inherit_parent',"
                        "cast('{}' as jsonb),cast('{}' as jsonb),'trace-a','claimed',"
                        "cast('null' as jsonb),'delegation','v1',3)"
                    ),
                    {
                        "id": relation_id,
                        "parent_run_id": parent_run_id,
                        "child_run_id": child_run_id,
                        "source_agent_id": source_agent_id,
                        "target_agent_id": target_agent_id,
                        "idempotency_key": f"key-{relation_id}",
                        "request_hash": "a" * 64,
                    },
                )

            if topology_case == "three-level":
                await insert_run("root-a")
                await insert_run(
                    "root-b",
                    tenant_id="tenant-b",
                    agent_id="agent-b",
                    trace_id="trace-b",
                )
                await insert_run("child-a", parent_run_id="root-a", agent_id="agent-b")
                await insert_run("grandchild-a", parent_run_id="child-a", agent_id="agent-c")
                await insert_relation(
                    "relation-a",
                    parent_run_id="root-a",
                    child_run_id="child-a",
                    source_agent_id="agent-a",
                    target_agent_id="agent-b",
                )
                await insert_relation(
                    "relation-b",
                    parent_run_id="child-a",
                    child_run_id="grandchild-a",
                    source_agent_id="agent-b",
                    target_agent_id="agent-c",
                )
            elif topology_case == "orphan":
                await insert_run("root-a")
                await insert_run(
                    "root-b",
                    tenant_id="tenant-b",
                    agent_id="agent-b",
                    trace_id="trace-b",
                )
                await insert_run("child-a", parent_run_id="ghost-root", agent_id="agent-b")
            elif topology_case == "cycle":
                await insert_run("root-a")
                await insert_run(
                    "root-b",
                    tenant_id="tenant-b",
                    agent_id="agent-b",
                    trace_id="trace-b",
                )
                await insert_run("run-a", agent_id="agent-a")
                await insert_run("run-b", agent_id="agent-b")
                await connection.execute(
                    text(
                        "update agent_runs set parent_run_id=case id "
                        "when 'run-a' then 'run-b' else 'run-a' end "
                        "where id in ('run-a','run-b')"
                    )
                )
                await insert_relation(
                    "relation-a",
                    parent_run_id="run-a",
                    child_run_id="run-b",
                    source_agent_id="agent-a",
                    target_agent_id="agent-b",
                )
                await insert_relation(
                    "relation-b",
                    parent_run_id="run-b",
                    child_run_id="run-a",
                    source_agent_id="agent-b",
                    target_agent_id="agent-a",
                )
            elif topology_case == "cross-tenant":
                await insert_run("root-a")
                await insert_run(
                    "root-b",
                    tenant_id="tenant-b",
                    agent_id="agent-b",
                    trace_id="trace-b",
                )
                await insert_run(
                    "child-b",
                    tenant_id="tenant-b",
                    parent_run_id="root-a",
                    agent_id="agent-b",
                    trace_id="trace-b",
                )
            else:
                await insert_run("root-a")
                await insert_run(
                    "root-b",
                    tenant_id="tenant-b",
                    agent_id="agent-b",
                    trace_id="trace-b",
                )
                await insert_run("child-a", parent_run_id="root-a", agent_id="agent-b")
                if topology_case == "relation-duplicate":
                    # 模拟旧 writer/人工操作绕过 0015 unique；0016 仍须先做全图拒绝。
                    await connection.execute(
                        text(
                            "alter table agent_delegations drop constraint "
                            "uq_agent_delegations_child_run"
                        )
                    )
                    for relation_id in ("relation-a", "relation-b"):
                        await insert_relation(
                            relation_id,
                            parent_run_id="root-a",
                            child_run_id="child-a",
                            source_agent_id="agent-a",
                            target_agent_id="agent-b",
                        )
        await engine.dispose()

        with pytest.raises(RuntimeError, match="0016 parent graph is invalid"):
            await asyncio.to_thread(run_migrations, dsn)
        assert await asyncio.to_thread(get_current_revision, dsn) == "0015_agent_delegation"
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            table_name = await connection.scalar(
                text("select to_regclass('parent_budget_ledgers')")
            )
            duplicate_count = await connection.scalar(
                text("select count(*) from agent_delegations where child_run_id='child-a'")
            )
        await engine.dispose()
        assert table_name is None
        if topology_case == "relation-duplicate":
            assert duplicate_count == 2


@pytest.mark.asyncio
async def test_postgresql_0016_rejects_completed_delegation_without_child_before_ddl() -> None:
    """真实 PostgreSQL 同样在 DDL 前拒绝 completed-without-child 的历史矛盾。"""

    async with isolated_database("shared_budget_completed_without_child") as dsn:
        await asyncio.to_thread(run_migrations, dsn, "0015_agent_delegation")
        engine = create_async_engine(dsn)
        async with engine.begin() as connection:
            await connection.execute(
                text("insert into tenants(id,display_name) values ('tenant-a','tenant-a')")
            )
            await connection.execute(
                text(
                    "insert into sessions(id,tenant_id,user_id,metadata_json) values "
                    "('session-a','tenant-a','user-a',cast('{}' as jsonb))"
                )
            )
            await connection.execute(
                text(
                    "insert into run_trace_bindings(trace_id,tenant_id,root_run_id) "
                    "values ('trace-a','tenant-a','root-a')"
                )
            )
            await connection.execute(
                text(
                    "insert into agent_runs(id,tenant_id,session_id,agent_id,status,trace_id,"
                    "input_json) values ('root-a','tenant-a','session-a','agent-a','completed',"
                    "'trace-a',cast('{}' as jsonb))"
                )
            )
            await connection.execute(
                text(
                    "insert into run_event_capacity(run_id,tenant_id,highest_persisted_seq,"
                    "outstanding_reserved_event_count,terminal_reservation) "
                    "values ('root-a','tenant-a',1,0,0)"
                )
            )
            await connection.execute(
                text(
                    "insert into canonical_events("
                    "id,tenant_id,run_id,stream_id,event_type,seq,terminal,visibility,trace_id,"
                    "record_scope,envelope_json) values ('terminal-root-a','tenant-a','root-a',"
                    "'stream-root-a','run.completed',1,true,'public','trace-a','run',"
                    "cast('{}' as jsonb))"
                )
            )
            await connection.execute(
                text(
                    "insert into agent_delegations("
                    "id,tenant_id,parent_run_id,child_run_id,source_agent_id,target_agent_id,"
                    "idempotency_key,request_hash,budget_intent,child_input_json,identity_json,"
                    "trace_id,status,event_operation_kind,event_registry_version,"
                    "reserved_event_count) values ('delegation-a','tenant-a','root-a',null,"
                    "'agent-a','agent-b','key-a','request-hash-a','inherit_parent',"
                    "cast('{}' as jsonb),cast('{}' as jsonb),'trace-a','completed',"
                    "'delegation','v1',3)"
                )
            )
            await connection.execute(
                text(
                    "insert into delegation_budget_reservations("
                    "id,delegation_id,tenant_id,parent_run_id,reserved_tokens,reserved_cost_usd,"
                    "settled_input_tokens,settled_output_tokens,settled_cost_usd,state) values ("
                    "'reservation-a','delegation-a','tenant-a','root-a',10,null,5,5,0,'settled')"
                )
            )
        await engine.dispose()

        with pytest.raises(RuntimeError, match="0016 parent graph is invalid"):
            await asyncio.to_thread(run_migrations, dsn)
        assert await asyncio.to_thread(get_current_revision, dsn) == "0015_agent_delegation"
        engine = create_async_engine(dsn)
        async with engine.connect() as connection:
            table_name = await connection.scalar(
                text("select to_regclass('parent_budget_ledgers')")
            )
        await engine.dispose()
        assert table_name is None
