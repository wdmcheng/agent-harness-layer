"""真实 Service 委派事件顺序与 parent 恢复集成测试。"""

from __future__ import annotations

from tests.integration.test_service_delegation_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.integration.test_service_delegation_contracts import (
    Any as Any,
)
from tests.integration.test_service_delegation_contracts import (
    DelegationRequest as DelegationRequest,
)
from tests.integration.test_service_delegation_contracts import (
    IdentityContext as IdentityContext,
)
from tests.integration.test_service_delegation_contracts import (
    ModelDecision as ModelDecision,
)
from tests.integration.test_service_delegation_contracts import (
    ModelResponse as ModelResponse,
)
from tests.integration.test_service_delegation_contracts import (
    Path as Path,
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
    runtime_services as runtime_services,
)
from tests.integration.test_service_delegation_contracts import (
    runtime_worker as runtime_worker,
)
from tests.integration.test_service_delegation_contracts import (
    uuid4 as uuid4,
)


@pytest.mark.asyncio
async def test_fast_service_worker_preserves_delegation_event_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实 Redis worker 在 submit 返回前完成 child 时，ordered evidence 仍严格有序。"""

    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path)
    artifact_root = tmp_path / "fast-worker-artifacts"
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="fast-worker-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )

    class _ReportedCostFakeProvider:
        provider_id = "fake"

        def complete(self, request: Any, *, model: str) -> ModelResponse:
            return ModelResponse(
                provider=self.provider_id,
                model=model,
                output_text=f"fake:{request.prompt}",
                decision=ModelDecision(action="call", estimated_tokens=1),
                token_usage={"input_tokens": 3, "output_tokens": 2},
                latency_ms=1,
                cost_usd=0.25,
                cost_status="reported",
            )

    monkeypatch.setattr(runtime_services, "FakeModelProvider", _ReportedCostFakeProvider)

    async with isolated_database("service_delegation_fast_worker") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        api = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=artifact_root,
        )
        original_orchestrator = api.orchestrator

        class _WorkerBeforeSubmitReturns:
            async def submit_run(self, **kwargs: Any) -> Any:
                child = await original_orchestrator.submit_run(**kwargs)
                worker_run_id = await runtime_worker.run_once(
                    profile="service",
                    profiles_dir=profiles,
                    storage_dsn=postgres_dsn,
                    artifact_root=artifact_root,
                )
                assert worker_run_id == child.run_id
                return child

        monkeypatch.setattr(
            api.delegation_service,
            "_orchestrator",
            _WorkerBeforeSubmitReturns(),
        )
        try:
            parent_run_id = await _seed_parent(api, identity=identity)
            result = await api.delegation_service.delegate(
                DelegationRequest(
                    parent_run_id=parent_run_id,
                    source_agent_id="examples.basic",
                    target_agent_id="examples.ticket_triage",
                    child_input={"text": "fast worker"},
                    idempotency_key="fast-worker-key",
                    request_id="request-fast-worker",
                ),
                identity=identity,
            )
            events = await api.event_sink.read(run_id=parent_run_id)
        finally:
            assert isinstance(api.queue, RedisRunQueue)
            await api.queue.cleanup_namespace()
            await api.close()

    assert result.status == "completed"
    assert [event.event_type.value for event in events] == [
        "delegation.claimed",
        "delegation.child.created",
        "delegation.completed",
    ]


@pytest.mark.asyncio
async def test_queued_parent_waits_for_delegation_then_resumes_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实 queue 中 parent executor 返回 completed 时，先冻结意图再由 child 恢复。"""

    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="queued-parent-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )
    original_resolve = AgentRegistry.resolve_executor
    parent_executor = _DelegatingExecutor()

    class _ReportedCostFakeProvider:
        provider_id = "fake"

        def complete(self, request: Any, *, model: str) -> ModelResponse:
            output = f"fake:{request.prompt}"
            return ModelResponse(
                provider=self.provider_id,
                model=model,
                output_text=output,
                decision=ModelDecision(action="call", estimated_tokens=1),
                token_usage={"input_tokens": 3, "output_tokens": 2},
                latency_ms=1,
                cost_usd=0.25,
                cost_status="reported",
            )

    def resolve_executor(self: AgentRegistry, agent_id: str) -> Any:
        if agent_id == "examples.basic":
            return parent_executor
        return original_resolve(self, agent_id)

    monkeypatch.setattr(AgentRegistry, "resolve_executor", resolve_executor)
    monkeypatch.setattr(runtime_services, "FakeModelProvider", _ReportedCostFakeProvider)

    async with isolated_database("service_delegation_parent_terminal") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        api = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "parent-terminal-artifacts",
        )
        try:
            submitted = await api.orchestrator.submit_run(
                agent_id="examples.basic",
                input={
                    "text": "production outage",
                    "idempotency_key": "queued-parent-delegation",
                },
                identity=identity,
                request_id="request-queued-parent",
            )
        finally:
            await api.close()

        first_worker_run = await runtime_worker.run_once(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "parent-terminal-artifacts",
        )
        waiting_reader = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "parent-terminal-artifacts",
        )
        try:
            async with waiting_reader.storage.uow() as uow:
                waiting_parent = await uow.runs.get(submitted.run_id)
                checkpoint = await uow.checkpoints.get_latest(submitted.run_id)
                claims = await uow.delegations.list_for_parent(
                    tenant_id=identity.tenant_id,
                    parent_run_id=submitted.run_id,
                )
                waiting_capacity = await uow.event_capacity.snapshot(submitted.run_id)
            waiting_events = await waiting_reader.event_sink.read(run_id=submitted.run_id)
        finally:
            await waiting_reader.close()

        assert first_worker_run == submitted.run_id
        assert waiting_parent is not None and waiting_parent.status == RunStatus.WAITING.value
        assert checkpoint is not None
        assert checkpoint.state["kind"] == "delegation_terminal"
        assert checkpoint.state["terminal_status"] == RunStatus.COMPLETED.value
        assert len(claims) == 1
        assert waiting_capacity.outstanding_reserved_event_count == 1
        assert all(
            event.event_type.value not in {"run.completed", "run.failed", "run.cancelled"}
            for event in waiting_events
        )

        second_worker_run = await runtime_worker.run_once(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "parent-terminal-artifacts",
        )
        final_reader = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "parent-terminal-artifacts",
        )
        try:
            async with final_reader.storage.uow() as uow:
                parent = await uow.runs.get(submitted.run_id)
                child = await uow.runs.get(claims[0].child_run_id or "")
                reservation = await uow.delegations.get_reservation(claims[0].id)
                capacity = await uow.event_capacity.snapshot(submitted.run_id)
            events = await final_reader.event_sink.read(run_id=submitted.run_id)
            assert final_reader.queue is not None
            redelivery = await final_reader.queue.reclaim(
                consumer_id="queued-parent-late-worker",
                min_idle_seconds=0,
            )
        finally:
            assert isinstance(final_reader.queue, RedisRunQueue)
            await final_reader.queue.cleanup_namespace()
            await final_reader.close()

    assert second_worker_run == claims[0].child_run_id
    assert child is not None and child.status == RunStatus.COMPLETED.value
    assert reservation.state == "settled"
    assert parent is not None and parent.status == RunStatus.COMPLETED.value
    assert parent.output == {
        "delegation_id": claims[0].id,
        "child_run_id": claims[0].child_run_id,
    }
    assert capacity.outstanding_reserved_event_count == 0
    assert sum(event.event_type.value == "run.completed" for event in events) == 1
    assert redelivery is None
