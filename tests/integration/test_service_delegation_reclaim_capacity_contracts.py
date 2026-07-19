"""真实 Service 委派 reclaim 与容量拒绝集成测试。"""

from __future__ import annotations

from tests.integration.test_service_delegation_contracts import (
    MAX_EVENT_SEQ as MAX_EVENT_SEQ,
)
from tests.integration.test_service_delegation_contracts import (
    DelegationError as DelegationError,
)
from tests.integration.test_service_delegation_contracts import (
    DelegationRequest as DelegationRequest,
)
from tests.integration.test_service_delegation_contracts import (
    IdentityContext as IdentityContext,
)
from tests.integration.test_service_delegation_contracts import (
    Path as Path,
)
from tests.integration.test_service_delegation_contracts import (
    RedisRunQueue as RedisRunQueue,
)
from tests.integration.test_service_delegation_contracts import (
    _seed_parent as _seed_parent,
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
async def test_service_reclaim_executes_one_child_and_holds_unknown_budget_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证崩溃 worker 遗留消息被 reclaim 后只执行一个 child，未知用量仅保留一次复核。"""

    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    monkeypatch.setattr(runtime_worker, "RECLAIM_IDLE_SECONDS", 0)
    profiles = _service_profiles(tmp_path)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="delegation-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )

    async with isolated_database("service_delegation_reclaim") as postgres_dsn:
        # TEST_POSTGRES_DSN 是 pytest 控制变量，不属于 HarnessSettings。
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        api = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        try:
            parent_run_id = await _seed_parent(api, identity=identity)
            request = DelegationRequest(
                parent_run_id=parent_run_id,
                source_agent_id="examples.basic",
                target_agent_id="examples.ticket_triage",
                child_input={"text": "production outage"},
                idempotency_key="delegation-service-key",
                request_id="request-delegation-service",
            )
            submitted = await api.delegation_service.delegate(request, identity=identity)
            assert api.queue is not None
            abandoned = await api.queue.pickup(
                consumer_id="crashed-worker",
                block_milliseconds=10,
            )
            assert abandoned is not None
            assert abandoned.message.run_id == submitted.child_run_id
        finally:
            await api.close()

        worker_run_id = await runtime_worker.run_once(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        reader = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        try:
            replay = await reader.delegation_service.delegate(request, identity=identity)
            async with reader.storage.uow() as uow:
                claims = await uow.delegations.list_for_parent(
                    tenant_id=identity.tenant_id,
                    parent_run_id=parent_run_id,
                )
                runs = await uow.runs.list_for_tenant(identity.tenant_id)
                reservation = await uow.delegations.get_reservation(replay.delegation_id)
                capacity = await uow.event_capacity.snapshot(parent_run_id)
            parent_events = await reader.event_sink.read(run_id=parent_run_id)
            assert reader.queue is not None
            redelivery = await reader.queue.reclaim(
                consumer_id="late-worker",
                min_idle_seconds=0,
            )
        finally:
            assert isinstance(reader.queue, RedisRunQueue)
            await reader.queue.cleanup_namespace()
            await reader.close()

    children = [run for run in runs if run.parent_run_id == parent_run_id]
    assert worker_run_id == submitted.child_run_id == replay.child_run_id
    assert len(claims) == 1
    assert len(children) == 1
    assert replay.summary is not None
    assert replay.summary.budget_status == "incomplete"
    assert reservation.state == "needs_review"
    assert capacity.outstanding_reserved_event_count == 1
    assert redelivery is None
    assert [event.event_type.value for event in parent_events] == [
        "delegation.claimed",
        "delegation.child.created",
    ]
    assert [event.event_id for event in parent_events] == [
        f"delegation:{submitted.delegation_id}:claimed",
        f"delegation:{submitted.delegation_id}:child",
    ]
    assert all(
        event.run_id == parent_run_id
        and event.trace_id == "trace-delegation-service"
        and event.agent_id == "examples.basic"
        and event.record_scope == "run"
        and event.visibility == "internal"
        and event.terminal is False
        for event in parent_events
    )
    assert parent_events[0].payload == {
        "delegation_id": submitted.delegation_id,
        "source_agent_id": "examples.basic",
        "target_agent_id": "examples.ticket_triage",
        "status": "claimed",
    }
    assert parent_events[1].payload == {
        "delegation_id": submitted.delegation_id,
        "source_agent_id": "examples.basic",
        "target_agent_id": "examples.ticket_triage",
        "status": "queued",
        "child_run_id": submitted.child_run_id,
    }


@pytest.mark.asyncio
async def test_service_postgresql_capacity_exhaustion_has_zero_redis_or_child_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证 PG 事件容量不足在入队或创建 child 前失败，Redis 与持久化状态均保持干净。"""

    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="delegation-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )

    async with isolated_database("service_delegation_capacity") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        components = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        try:
            parent_run_id = await _seed_parent(components, identity=identity)
            async with components.storage.uow() as uow:
                await uow.event_capacity.reconcile_local_prefix(
                    run_id=parent_run_id,
                    highest_persisted_seq=MAX_EVENT_SEQ - 3,
                )
                await uow.commit()
            with pytest.raises(DelegationError) as captured:
                await components.delegation_service.delegate(
                    DelegationRequest(
                        parent_run_id=parent_run_id,
                        source_agent_id="examples.basic",
                        target_agent_id="examples.ticket_triage",
                        child_input={"text": "production outage"},
                        idempotency_key="capacity-key",
                    ),
                    identity=identity,
                )
            async with components.storage.uow() as uow:
                claims = await uow.delegations.list_for_parent(
                    tenant_id=identity.tenant_id,
                    parent_run_id=parent_run_id,
                )
                runs = await uow.runs.list_for_tenant(identity.tenant_id)
                capacity = await uow.event_capacity.snapshot(parent_run_id)
            events = await components.event_sink.read(run_id=parent_run_id)
            assert components.queue is not None
            queued = await components.queue.pickup(
                consumer_id="capacity-check",
                block_milliseconds=10,
            )
        finally:
            assert isinstance(components.queue, RedisRunQueue)
            await components.queue.cleanup_namespace()
            await components.close()

    assert captured.value.code == "event.sequence_exhausted"
    assert claims == []
    assert len(runs) == 1
    assert events == []
    assert queued is None
    assert capacity.highest_persisted_seq == MAX_EVENT_SEQ - 3
    assert capacity.outstanding_reserved_event_count == 0
