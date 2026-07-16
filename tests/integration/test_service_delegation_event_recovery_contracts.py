"""真实 Service 委派事件写入失败恢复集成测试。"""

from __future__ import annotations

from tests.integration.test_service_delegation_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.integration.test_service_delegation_contracts import (
    Any as Any,
)
from tests.integration.test_service_delegation_contracts import (
    CanonicalEvent as CanonicalEvent,
)
from tests.integration.test_service_delegation_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.integration.test_service_delegation_contracts import (
    IdentityContext as IdentityContext,
)
from tests.integration.test_service_delegation_contracts import (
    Path as Path,
)
from tests.integration.test_service_delegation_contracts import (
    PostgreSQLEventSink as PostgreSQLEventSink,
)
from tests.integration.test_service_delegation_contracts import (
    RedisRunQueue as RedisRunQueue,
)
from tests.integration.test_service_delegation_contracts import (
    RunStatus as RunStatus,
)
from tests.integration.test_service_delegation_contracts import (
    _DelegatingExecutor as _DelegatingExecutor,
)
from tests.integration.test_service_delegation_contracts import (
    _service_profiles as _service_profiles,
)
from tests.integration.test_service_delegation_contracts import (
    app_runtime as app_runtime,
)
from tests.integration.test_service_delegation_contracts import (
    isolated_database as isolated_database,
)
from tests.integration.test_service_delegation_contracts import (
    os as os,
)
from tests.integration.test_service_delegation_contracts import (
    pytest as pytest,
)
from tests.integration.test_service_delegation_contracts import (
    pytestmark as pytestmark,
)
from tests.integration.test_service_delegation_contracts import (
    run_migrations as run_migrations,
)
from tests.integration.test_service_delegation_contracts import (
    runtime_worker as runtime_worker,
)
from tests.integration.test_service_delegation_contracts import (
    uuid4 as uuid4,
)


@pytest.mark.asyncio
async def test_claimed_event_write_failure_recovers_child_before_parent_ack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """claim commit 后 claimed event 瞬断，不得把 parent 永久卡在 WAITING。"""

    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="claimed-recovery-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )
    parent_executor = _DelegatingExecutor()
    original_resolve = AgentRegistry.resolve_executor

    def resolve_executor(self: AgentRegistry, agent_id: str) -> Any:
        if agent_id == "examples.basic":
            return parent_executor
        return original_resolve(self, agent_id)

    failed_once = False
    original_write = PostgreSQLEventSink.write

    async def fail_first_claimed_write(
        sink: PostgreSQLEventSink,
        event: CanonicalEvent,
    ) -> CanonicalEvent:
        nonlocal failed_once
        if event.event_type == CanonicalEventType.DELEGATION_CLAIMED and not failed_once:
            failed_once = True
            raise OSError("delegation claimed sink unavailable")
        return await original_write(sink, event)

    monkeypatch.setattr(AgentRegistry, "resolve_executor", resolve_executor)
    monkeypatch.setattr(PostgreSQLEventSink, "write", fail_first_claimed_write)

    async with isolated_database("service_delegation_claimed_recovery") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        api = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "claimed-recovery-artifacts",
        )
        try:
            submitted = await api.orchestrator.submit_run(
                agent_id="examples.basic",
                input={
                    "text": "production outage",
                    "idempotency_key": "claimed-recovery-child",
                },
                idempotency_key="claimed-recovery-parent",
                identity=identity,
                request_id="request-claimed-recovery",
            )
        finally:
            await api.close()

        first_worker_run = await runtime_worker.run_once(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "claimed-recovery-artifacts",
        )
        reader = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "claimed-recovery-artifacts",
        )
        try:
            replay = await reader.orchestrator.submit_run(
                agent_id="examples.basic",
                input={
                    "text": "production outage",
                    "idempotency_key": "claimed-recovery-child",
                },
                idempotency_key="claimed-recovery-parent",
                identity=identity,
                request_id="request-claimed-recovery",
            )
            async with reader.storage.uow() as uow:
                parent = await uow.runs.get(submitted.run_id)
                checkpoint = await uow.checkpoints.get_latest(submitted.run_id)
                claims = await uow.delegations.list_for_parent(
                    tenant_id=identity.tenant_id,
                    parent_run_id=submitted.run_id,
                )
                outbox = await uow.evidence_outbox.ordered_group(
                    group_id=f"delegation:{claims[0].id}:evidence"
                )
                outbox_states = [row.state for row in outbox]
            events = await reader.event_sink.read(run_id=submitted.run_id)
            assert reader.queue is not None
            child_delivery = await reader.queue.pickup(
                consumer_id="claimed-recovery-child-check",
                block_milliseconds=10,
            )
        finally:
            assert isinstance(reader.queue, RedisRunQueue)
            await reader.queue.cleanup_namespace()
            await reader.close()

    assert failed_once is True
    assert first_worker_run == submitted.run_id == replay.run_id
    assert parent_executor.calls == 1
    assert parent is not None and parent.status == RunStatus.WAITING.value
    assert checkpoint is not None and checkpoint.state["kind"] == "delegation_terminal"
    assert len(claims) == 1 and claims[0].child_run_id is not None
    assert child_delivery is not None
    assert child_delivery.message.run_id == claims[0].child_run_id
    assert outbox_states == ["published", "published", "result_persisted"]
    assert [event.event_type.value for event in events if not event.visibility == "public"] == [
        "delegation.claimed",
        "delegation.child.created",
    ]
