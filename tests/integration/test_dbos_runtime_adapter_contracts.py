"""DBOS 2.26.0 受控 adapter 的 workflow identity 与真实 PostgreSQL 合同。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from uuid import uuid4

import pytest

from agent_harness.adapters.runtime.dbos import (
    DBOSOperation,
    DBOSServiceRuntimeAdapter,
    workflow_id_for_operation,
)
from agent_harness.events import CanonicalEvent, PostgreSQLEventSink
from agent_harness.runtime import RunStatus
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import (
    RunCreate,
    RunExecutionRecord,
    RunRecord,
    SessionCreate,
)


def test_workflow_id_is_stable_per_tenant_operation() -> None:
    first = workflow_id_for_operation("tenant-a", "run:r:execute")
    assert first == workflow_id_for_operation("tenant-a", "run:r:execute")
    assert first != workflow_id_for_operation("tenant-b", "run:r:execute")
    assert first != workflow_id_for_operation("tenant-a", "run:r:approval:a:lease:l")


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="DBOS真实合同需要PostgreSQL system database。",
)
@pytest.mark.asyncio
async def test_dbos_real_workflow_reuses_operation_id() -> None:
    calls: list[str] = []

    async def handler(operation: DBOSOperation) -> dict[str, str]:
        calls.append(operation.operation_id)
        return {"run_id": operation.run_id}

    adapter = DBOSServiceRuntimeAdapter(
        system_database_url=os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"],
        handlers={"execute_run": handler},
        executor_id="agent-harness-service-worker-test",
    )
    run_id = f"run-dbos-{uuid4()}"
    operation = DBOSOperation(
        kind="execute_run",
        tenant_id="tenant-dbos",
        run_id=run_id,
        operation_id=f"run:{run_id}:execute",
    )
    try:
        await adapter.start()
        first = await adapter.execute(operation)
        second = await adapter.execute(operation)
    finally:
        await adapter.close()

    assert first.status == "succeeded"
    assert first.result == {"run_id": run_id}
    assert second == first
    assert calls == [operation.operation_id]


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="DBOS真实合同需要PostgreSQL system database。",
)
@pytest.mark.asyncio
async def test_dbos_maps_persisted_error_to_deterministic_outcome() -> None:
    async def handler(_operation: DBOSOperation) -> dict[str, str]:
        raise RuntimeError("deterministic handler failure")

    adapter = DBOSServiceRuntimeAdapter(
        system_database_url=os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"],
        handlers={"execute_run": handler},
        executor_id="agent-harness-service-worker-error-test",
    )
    run_id = f"run-dbos-error-{uuid4()}"
    try:
        await adapter.start()
        outcome = await adapter.execute(
            DBOSOperation(
                kind="execute_run",
                tenant_id="tenant-dbos-error",
                run_id=run_id,
                operation_id=f"run:{run_id}:execute",
            )
        )
    finally:
        await adapter.close()

    assert outcome.status == "deterministic_failed"
    assert outcome.error_code == "dbos.error"
    assert outcome.result is None


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="DBOS singleton合同需要PostgreSQL advisory lock。",
)
@pytest.mark.asyncio
async def test_dbos_rejects_parallel_same_executor_id() -> None:
    async def handler(operation: DBOSOperation) -> dict[str, str]:
        return {"run_id": operation.run_id}

    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    first = DBOSServiceRuntimeAdapter(
        system_database_url=dsn,
        handlers={"execute_run": handler},
        executor_id="agent-harness-singleton-contract",
    )
    second = DBOSServiceRuntimeAdapter(
        system_database_url=dsn,
        handlers={"execute_run": handler},
        executor_id="agent-harness-singleton-contract",
    )
    try:
        await first.start()
        with pytest.raises(RuntimeError, match="parallel worker requires Conductor"):
            await second.start()
    finally:
        await second.close()
        await first.close()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="DBOS hard-crash recovery需要PostgreSQL system database。",
)
def test_dbos_hard_crash_recovers_pending_workflow(tmp_path: Path) -> None:
    """A在应用 owner 落库后硬退出；B重入同 run 并只写一个 terminal。"""

    dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    run_migrations(dsn)
    run_key = f"hard-crash-{uuid4()}"
    tenant_id = f"tenant-hard-crash-{uuid4()}"

    async def prepare_run() -> tuple[SQLAlchemyStorage, str]:
        storage = SQLAlchemyStorage.from_dsn(dsn, cross_event_loop=True)
        async with storage.uow() as uow:
            await uow.tenants.ensure(tenant_id)
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id=tenant_id,
                    user_id="hard-crash-user",
                    agent_id="fake-agent",
                )
            )
            run = await uow.runs.create_queued(
                RunCreate(
                    tenant_id=tenant_id,
                    session_id=session.id,
                    agent_id="fake-agent",
                    idempotency_key=run_key,
                    input={"source_ref": "source://hard-crash", "trust_level": "trusted"},
                ),
                execution_context={
                    "identity": {
                        "tenant_id": tenant_id,
                        "user_id": "hard-crash-user",
                        "session_id": "hard-crash-session",
                        "roles": ["operator"],
                        "permissions": ["runs:execute"],
                        "auth_method": "api-key",
                    },
                    "request_id": f"request-{run_key}",
                    "trace_id": f"trace-{run_key}",
                },
                operation_id="run:pending:execute",
                request_id=f"request-{run_key}",
                effective_idempotency_key=run_key,
            )
            private = await uow.runs.get_execution(run.id)
            assert private is not None
            await uow.runs.mark_queued(
                run_id=run.id,
                operation_id=private.operation_id,
                message_id=f"message-{run_key}",
            )
            await uow.commit()
        return storage, run.id

    storage, run_id = asyncio.run(prepare_run())
    operation_id = f"run:{run_id}:execute"
    workflow_id = workflow_id_for_operation(tenant_id, operation_id)

    script = textwrap.dedent(
        """
        import asyncio
        import os
        from pathlib import Path
        from agent_harness.adapters.runtime.dbos import (
            DBOSOperation,
            DBOSServiceRuntimeAdapter,
            workflow_id_for_operation,
        )
        from agent_harness.events import EventBus, PostgreSQLEventSink
        from agent_harness.runtime import (
            AgentExecutionResult,
            RunOrchestrator,
        )
        from agent_harness.storage import SQLAlchemyStorage

        class Executor:
            async def run(self, request, context):
                del request, context
                with Path(os.environ["DBOS_SIDE_EFFECT"]).open("a", encoding="utf-8") as stream:
                    stream.write("executed\\n")
                return AgentExecutionResult.completed({"result": "recovered"})

            async def resume(self, request, context, grant):
                del request, context, grant
                raise AssertionError("hard-crash run must not resume")

        async def main():
            marker = Path(os.environ["DBOS_MARKER"])
            mode = os.environ["DBOS_MODE"]
            dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
            tenant_id = os.environ["DBOS_TENANT_ID"]
            run_id = os.environ["DBOS_RUN_ID"]
            operation_id = f"run:{run_id}:execute"
            workflow_id = workflow_id_for_operation(tenant_id, operation_id)
            storage = SQLAlchemyStorage.from_dsn(dsn, cross_event_loop=True)
            orchestrator = RunOrchestrator(
                storage=storage,
                event_bus=EventBus(sink=PostgreSQLEventSink(storage)),
                executor_resolver=lambda _agent_id: Executor(),
            )

            async def handler(operation):
                if mode == "crash":
                    async with storage.uow() as uow:
                        claimed = await uow.runs.claim_execution(
                            run_id=operation.run_id,
                            operation_id=operation.operation_id,
                            owner_id=workflow_id,
                            workflow_id=workflow_id,
                        )
                        await uow.commit()
                    if not claimed:
                        raise RuntimeError("application owner was not persisted")
                    marker.with_suffix(".started").write_text(
                        f"{operation.operation_id}|{workflow_id}", encoding="utf-8"
                    )
                    os._exit(23)
                result = await orchestrator.execute_run(
                    run_id=operation.run_id,
                    tenant_id=operation.tenant_id,
                    operation_id=operation.operation_id,
                    owner_id=workflow_id,
                    workflow_id=workflow_id,
                )
                marker.write_text(
                    f"{operation.operation_id}|{workflow_id}|{result.status.value}",
                    encoding="utf-8",
                )
                return result.to_payload()

            adapter = DBOSServiceRuntimeAdapter(
                system_database_url=dsn,
                handlers={"execute_run": handler},
                executor_id=os.environ["DBOS_EXECUTOR_ID"],
            )
            try:
                await adapter.start()
                if mode == "crash":
                    await adapter.execute(DBOSOperation(
                        kind="execute_run",
                        tenant_id=tenant_id,
                        run_id=run_id,
                        operation_id=operation_id,
                    ))
                else:
                    for _ in range(200):
                        if marker.exists():
                            return
                        await asyncio.sleep(0.05)
                    raise RuntimeError("pending workflow was not recovered")
            finally:
                await adapter.close()
                await storage.dispose()

        asyncio.run(main())
        """
    )
    marker = tmp_path / "recovered.txt"
    side_effect = tmp_path / "side-effect.txt"
    executor_id = f"agent-harness-recovery-{uuid4()}"
    environment = {
        **os.environ,
        "DBOS_MARKER": str(marker),
        "DBOS_SIDE_EFFECT": str(side_effect),
        "DBOS_EXECUTOR_ID": executor_id,
        "DBOS_RUN_ID": run_id,
        "DBOS_TENANT_ID": tenant_id,
    }
    try:
        crashed = subprocess.run(
            [sys.executable, "-c", script],
            env={**environment, "DBOS_MODE": "crash"},
            check=False,
            timeout=30,
        )
        assert crashed.returncode == 23
        assert marker.with_suffix(".started").read_text() == f"{operation_id}|{workflow_id}"

        async def read_application_state() -> tuple[
            RunRecord | None,
            RunExecutionRecord | None,
            list[CanonicalEvent],
        ]:
            async with storage.uow() as uow:
                run = await uow.runs.get(run_id)
                private = await uow.runs.get_execution(run_id)
            events = await PostgreSQLEventSink(storage).read(run_id=run_id)
            return run, private, list(events)

        crashed_run, crashed_private, crashed_events = asyncio.run(read_application_state())
        assert crashed_run is not None and crashed_run.status == RunStatus.RUNNING
        assert crashed_private is not None
        assert crashed_private.owner_id == workflow_id
        assert crashed_private.workflow_id == workflow_id
        assert crashed_events == []

        recovered = subprocess.run(
            [sys.executable, "-c", script],
            env={**environment, "DBOS_MODE": "recover"},
            check=False,
            timeout=30,
            text=True,
            capture_output=True,
        )
        assert recovered.returncode == 0, recovered.stderr
        assert marker.read_text() == f"{operation_id}|{workflow_id}|completed"

        completed_run, completed_private, completed_events = asyncio.run(read_application_state())
        assert completed_run is not None and completed_run.status == RunStatus.COMPLETED
        assert completed_private is not None
        assert completed_private.owner_id == workflow_id
        assert completed_private.workflow_id == workflow_id
        assert len(side_effect.read_text().splitlines()) == 1
        terminal_events = [event for event in completed_events if event.terminal]
        assert len(terminal_events) == 1
        assert terminal_events[0].tenant_id == tenant_id
    finally:
        asyncio.run(storage.dispose())
