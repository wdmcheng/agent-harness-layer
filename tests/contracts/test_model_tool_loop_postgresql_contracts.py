"""0018 model tool loop 在真实 PostgreSQL 上的迁移与恢复合同。"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from tests.contracts.run_trace_migration_test_helpers import migration_config
from tests.contracts.run_trace_revision_hardening_postgresql_helpers import (
    postgres_database,
)
from tests.contracts.test_model_tool_loop_terminal_fencing_contracts import (
    _loop,  # pyright: ignore[reportPrivateUsage]
)
from tests.contracts.test_policy_gated_model_tool_loop_approved_resume_recovery_contracts import (
    assert_database_approved_exact_replay_recovers_pending_final_event,
)
from tests.contracts.test_policy_gated_model_tool_loop_sqlite_resume_contracts import (
    assert_database_reload_resumes_exact_snapshot,
)

from agent_harness.storage import (
    ModelToolInvocationClaimCreate,
    ModelToolLoopStorageConflict,
    SQLAlchemyStorage,
    get_current_revision,
    run_migrations,
)

REVISION_0017 = "0017_model_route_chain_state"
REVISION_0018 = "0018_model_tool_loop_state"
MARKER_KEY = "model-tool-loop-v1"

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实PostgreSQL 0018合同需要AGENT_HARNESS_TEST_POSTGRES_DSN。",
)


@pytest.mark.asyncio
async def test_postgresql_0018_preserves_legacy_rows_and_allows_empty_downgrade() -> None:
    """0017遗留数据前滚逐值保留；无v1 evidence时仍可显式回退。"""

    async with postgres_database("agent_harness_tool_loop_legacy") as (dsn, engine):
        await asyncio.to_thread(run_migrations, dsn, REVISION_0017)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text("insert into tenants(id, display_name) values ('tenant-a', 'Tenant A')")
            )
            await connection.execute(
                sa.text(
                    "insert into tool_invocations("
                    "id, tenant_id, agent_id, tool_name, args_ref, result_ref, status, "
                    "metadata_json"
                    ") values ('tool-legacy', 'tenant-a', 'agent-a', 'search', "
                    "'artifact://args', 'artifact://result', 'completed', '{}')"
                )
            )
            await connection.execute(
                sa.text(
                    "insert into context_assemblies("
                    "id, tenant_id, input_refs_json, token_budget, trust_summary_json, "
                    "truncation_summary_json, output_ref) values ("
                    "'context-legacy', 'tenant-a', '[]', 8, '{}', '{}', 'artifact://context')"
                )
            )
        await asyncio.to_thread(run_migrations, dsn)

        async with engine.connect() as connection:
            tool = (
                await connection.execute(
                    sa.text(
                        "select id, tenant_id, agent_id, tool_name, args_ref, result_ref, status "
                        "from tool_invocations where id = 'tool-legacy'"
                    )
                )
            ).one()
            context = (
                await connection.execute(
                    sa.text(
                        "select id, tenant_id, token_budget, output_ref "
                        "from context_assemblies where id = 'context-legacy'"
                    )
                )
            ).one()
            marker = (
                await connection.execute(
                    sa.text("select marker_key, evidence_seen from model_tool_loop_schema_marker")
                )
            ).one()
        assert tuple(tool) == (
            "tool-legacy",
            "tenant-a",
            "agent-a",
            "search",
            "artifact://args",
            "artifact://result",
            "completed",
        )
        assert tuple(context) == ("context-legacy", "tenant-a", 8, "artifact://context")
        assert tuple(marker) == (MARKER_KEY, False)

        await asyncio.to_thread(command.downgrade, migration_config(dsn), REVISION_0017)
        assert await asyncio.to_thread(get_current_revision, dsn) == REVISION_0017

        await asyncio.to_thread(run_migrations, dsn)
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    "update model_tool_loop_schema_marker set evidence_seen = true "
                    "where marker_key = :marker_key"
                ),
                {"marker_key": MARKER_KEY},
            )
        with pytest.raises(RuntimeError, match="^storage.model_tool_loop_evidence_present$"):
            await asyncio.to_thread(command.downgrade, migration_config(dsn), REVISION_0017)
        assert await asyncio.to_thread(get_current_revision, dsn) == REVISION_0018
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    sa.text(
                        "select evidence_seen from model_tool_loop_schema_marker "
                        "where marker_key = :marker_key"
                    ),
                    {"marker_key": MARKER_KEY},
                )
            ).scalar_one() is True


@pytest.mark.asyncio
async def test_postgresql_reload_resumes_exact_snapshot_and_executes_tool_at_most_once(
    tmp_path: Path,
) -> None:
    """真实PostgreSQL重载复用与SQLite相同的审批、上下文和副作用计数合同。"""

    async with postgres_database("agent_harness_tool_loop_resume") as (dsn, _engine):
        await asyncio.to_thread(run_migrations, dsn)
        await assert_database_reload_resumes_exact_snapshot(
            dsn=dsn,
            tmp_path=tmp_path,
        )


@pytest.mark.asyncio
async def test_postgresql_approved_exact_replay_recovers_pending_final_event(
    tmp_path: Path,
) -> None:
    """真实PostgreSQL审批恢复补投原final event并保持handler恰好一次。"""

    async with postgres_database("agent_harness_approved_event_recovery") as (dsn, _engine):
        await asyncio.to_thread(run_migrations, dsn)
        await assert_database_approved_exact_replay_recovers_pending_final_event(
            dsn=dsn,
            tmp_path=tmp_path,
        )


@pytest.mark.asyncio
async def test_postgresql_repository_concurrency_cancel_and_budget_fencing() -> None:
    """真实行锁/CAS只容许一个轮次提交，并让取消与预算终态阻断后续副作用。"""

    async with postgres_database("agent_harness_tool_loop_fencing") as (dsn, _engine):
        await asyncio.to_thread(run_migrations, dsn)
        storage = SQLAlchemyStorage(dsn)
        try:
            loop = await _loop(storage, loop_id="7" * 64)

            async def commit_turn(label: str) -> object:
                try:
                    async with storage.uow() as uow:
                        record = await uow.model_tool_loops.settle_model_turn(
                            tenant_id="tenant-a",
                            loop_id=loop.loop_id,
                            expected_version=1,
                            owner_lease_digest=loop.owner_lease_digest,
                            owner_fence=loop.owner_fence,
                            cumulative_usage={
                                "schema_version": "model-tool-loop-cumulative-usage-v1",
                                "turns_completed": 1,
                                "total_tokens_used": 1,
                                "total_cost_usd": 0.0,
                            },
                            state={
                                "schema_version": "model-tool-loop-state-v1",
                                "next_step": "model_result",
                                "model_usage_call_id": f"usage-{label}",
                                "tool_call_id": None,
                                "approval_id": None,
                                "checkpoint_ref": None,
                                "context_ref": None,
                                "next_request_digest": None,
                            },
                        )
                        await uow.commit()
                    return record
                except ModelToolLoopStorageConflict as exc:
                    return exc

            results = await asyncio.gather(commit_turn("worker-a"), commit_turn("worker-b"))
            assert sum(not isinstance(result, Exception) for result in results) == 1
            assert sum(isinstance(result, ModelToolLoopStorageConflict) for result in results) == 1

            async with storage.uow() as uow:
                current = await uow.model_tool_loops.get("tenant-a", loop.loop_id)
            assert current is not None and current.version == 2
            async with storage.uow() as uow:
                cancelled = await uow.model_tool_loops.cancel(
                    tenant_id="tenant-a",
                    loop_id=loop.loop_id,
                    expected_status="active",
                    expected_version=current.version,
                    owner_lease_digest=loop.owner_lease_digest,
                    owner_fence=loop.owner_fence,
                    error_ref="error://cancelled",
                )
                await uow.commit()
            assert cancelled.status == "cancelled"
            async with storage.uow() as uow:
                persisted_run = await uow.runs.get(loop.run_id)
            assert persisted_run is not None

            with pytest.raises(ModelToolLoopStorageConflict):
                async with storage.uow() as uow:
                    await uow.tool_invocations.create_model_claim(
                        ModelToolInvocationClaimCreate(
                            tenant_id="tenant-a",
                            agent_id="agent-a",
                            run_id=loop.run_id,
                            tool_name="search",
                            args_ref="artifact://late-args",
                            arguments_hash="8" * 64,
                            trace_id=persisted_run.trace_id,
                            request_id="request-late",
                            loop_id=loop.loop_id,
                            turn_ordinal=2,
                            tool_call_id="9" * 64,
                            binding={"binding_digest": "a" * 64},
                            execution_lease_digest="b" * 64,
                            execution_fence=1,
                            execution_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                        )
                    )

            budget_loop = await _loop(storage, loop_id="c" * 64)
            async with storage.uow() as uow:
                failed = await uow.model_tool_loops.fail(
                    tenant_id="tenant-a",
                    loop_id=budget_loop.loop_id,
                    expected_version=1,
                    owner_lease_digest=budget_loop.owner_lease_digest,
                    owner_fence=budget_loop.owner_fence,
                    status="failed",
                    error_ref="error://budget-exhausted",
                )
                await uow.commit()
            assert failed.status == "failed"
            with pytest.raises(ModelToolLoopStorageConflict):
                async with storage.uow() as uow:
                    await uow.model_tool_loops.terminate(
                        tenant_id="tenant-a",
                        loop_id=budget_loop.loop_id,
                        expected_version=1,
                        owner_lease_digest=budget_loop.owner_lease_digest,
                        owner_fence=budget_loop.owner_fence,
                        status="completed",
                        result_ref="artifact://late-final",
                        error_ref=None,
                    )
        finally:
            await storage.dispose()
