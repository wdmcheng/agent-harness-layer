"""真实 Service 委派预算与 parent 聚合集成测试。"""

from __future__ import annotations

from tests.integration.test_service_delegation_contracts import (
    DelegationAggregateModel as DelegationAggregateModel,
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
    select as select,
)
from tests.integration.test_service_delegation_contracts import (
    update as update,
)
from tests.integration.test_service_delegation_contracts import (
    uuid4 as uuid4,
)


@pytest.mark.asyncio
async def test_service_finite_parent_cost_rejects_unbounded_target_before_queue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实 PostgreSQL/Redis 下，无成本 ceiling 的 target 不能被静默缩量放行。"""

    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path, source_cost_limit=10.0)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="cost-boundary-user",
        session_id=str(uuid4()),
        roles=["operator"],
        permissions=["agent.delegate"],
        auth_method="api-key",
    )

    async with isolated_database("service_delegation_unbounded_cost") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        components = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts-unbounded-cost",
        )
        try:
            parent_run_id = await _seed_parent(components, identity=identity)
            with pytest.raises(DelegationError) as captured:
                await components.delegation_service.delegate(
                    DelegationRequest(
                        parent_run_id=parent_run_id,
                        source_agent_id="examples.basic",
                        target_agent_id="examples.ticket_triage",
                        child_input={"text": "must not run"},
                        idempotency_key="unbounded-cost-key",
                        request_id="request-unbounded-cost",
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
                consumer_id="unbounded-cost-proof",
                block_milliseconds=10,
            )
        finally:
            assert isinstance(components.queue, RedisRunQueue)
            await components.queue.cleanup_namespace()
            await components.close()

    assert captured.value.code == "delegation.budget_exceeded"
    assert claims == []
    assert len(runs) == 1
    assert capacity.outstanding_reserved_event_count == 0
    assert events == []
    assert queued is None


@pytest.mark.asyncio
async def test_local_and_service_parent_aggregation_values_match(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="aggregation-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )

    local_dsn = f"sqlite+aiosqlite:///{tmp_path / 'local-aggregation.db'}"
    run_migrations(local_dsn)
    local = app_runtime.build_runtime_components(
        profile="local",
        profiles_dir=profiles,
        storage_dsn=local_dsn,
        events_path=tmp_path / "local-events.jsonl",
        artifact_root=tmp_path / "local-artifacts",
    )
    try:
        local_parent = await _seed_parent(local, identity=identity)
        local_result = await local.delegation_service.delegate(
            DelegationRequest(
                parent_run_id=local_parent,
                source_agent_id="examples.basic",
                target_agent_id="examples.ticket_triage",
                child_input={"text": "production outage"},
                idempotency_key="local-key",
                request_id="request-aggregation",
            ),
            identity=identity,
        )
    finally:
        await local.close()

    async with isolated_database("service_delegation_aggregation") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        api = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "service-artifacts",
        )
        try:
            service_parent = await _seed_parent(api, identity=identity)
            request = DelegationRequest(
                parent_run_id=service_parent,
                source_agent_id="examples.basic",
                target_agent_id="examples.ticket_triage",
                child_input={"text": "production outage"},
                idempotency_key="service-key",
                request_id="request-aggregation",
            )
            submitted = await api.delegation_service.delegate(request, identity=identity)
        finally:
            await api.close()
        worker_run_id = await runtime_worker.run_once(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "service-artifacts",
        )
        reader = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "service-artifacts",
        )
        try:
            service_result = await reader.delegation_service.delegate(request, identity=identity)
        finally:
            assert isinstance(reader.queue, RedisRunQueue)
            await reader.queue.cleanup_namespace()
            await reader.close()

    assert worker_run_id == submitted.child_run_id == service_result.child_run_id
    assert local_result.summary is not None
    assert service_result.summary is not None
    assert (
        local_result.summary.input_tokens,
        local_result.summary.output_tokens,
        local_result.summary.cost_usd,
        local_result.summary.latency_ms,
        local_result.summary.budget_status,
        local_result.summary.children[0].status,
    ) == (
        service_result.summary.input_tokens,
        service_result.summary.output_tokens,
        service_result.summary.cost_usd,
        service_result.summary.latency_ms,
        service_result.summary.budget_status,
        service_result.summary.children[0].status,
    )


@pytest.mark.asyncio
async def test_postgresql_parent_summary_rejects_aggregate_evidence_tampering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PostgreSQL 的 RUN-002 同样不得信任被篡改的 aggregate JSON。"""

    profiles = _service_profiles(tmp_path)
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="aggregate-integrity-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )
    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    async with isolated_database("service_delegation_aggregate_integrity") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        api = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "aggregate-integrity-artifacts",
        )
        try:
            parent_run_id = await _seed_parent(api, identity=identity)
            request = DelegationRequest(
                parent_run_id=parent_run_id,
                source_agent_id="examples.basic",
                target_agent_id="examples.ticket_triage",
                child_input={"text": "aggregate integrity"},
                idempotency_key="aggregate-integrity-key",
                request_id="request-aggregate-integrity",
            )
            submitted = await api.delegation_service.delegate(request, identity=identity)
        finally:
            await api.close()
        worker_run_id = await runtime_worker.run_once(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "aggregate-integrity-artifacts",
        )
        reader = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "aggregate-integrity-artifacts",
        )
        try:
            assert worker_run_id == submitted.child_run_id
            async with reader.storage.uow() as uow:
                aggregate = await uow.session.scalar(
                    select(DelegationAggregateModel).where(
                        DelegationAggregateModel.delegation_id == submitted.delegation_id
                    )
                )
                assert aggregate is not None
                summary = dict(aggregate.summary_json)
                children = [dict(child) for child in summary["children"]]
                children[0]["usage_evidence_refs"] = ["usage-forged"]
                children[0]["trace_refs"] = ["trace-forged"]
                summary.update(
                    children=children,
                    trace_refs=["trace-forged"],
                    latency_ms=999_999,
                    budget_status="exceeded",
                )
                await uow.session.execute(
                    update(DelegationAggregateModel)
                    .where(DelegationAggregateModel.id == aggregate.id)
                    .values(
                        summary_json=summary,
                        evidence_refs_json=["evidence-forged"],
                    )
                )
                await uow.commit()
            with pytest.raises(DelegationError, match="^delegation.execution_failed$"):
                await reader.delegation_service.get_parent_summary(
                    tenant_id=identity.tenant_id,
                    parent_run_id=parent_run_id,
                )
        finally:
            assert isinstance(reader.queue, RedisRunQueue)
            await reader.queue.cleanup_namespace()
            await reader.close()
