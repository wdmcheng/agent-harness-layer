"""真实 PostgreSQL/Redis worker reclaim 下的 delegation 唯一执行合同。"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.adapters.queue import RedisRunQueue
from agent_harness.delegation import (
    AgentDelegateInput,
    AgentDelegationModule,
    DelegationRequest,
)
from agent_harness.delegation.service import DelegationError, DelegationExecutionResult
from agent_harness.events import CanonicalEvent, CanonicalEventType, PostgreSQLEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import ModelDecision, ModelResponse
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import AgentExecutionResult
from agent_harness.runtime import services as runtime_services
from agent_harness.runtime.executor import (
    AgentExecutionContext,
    AgentExecutionRequest,
    build_execution_context,
)
from agent_harness.runtime.state import RunStatus
from agent_harness.storage import RunCreate, SessionCreate, run_migrations
from agent_harness.storage.delegation_models import (
    AgentDelegationModel,
    DelegationAggregateModel,
    DelegationBudgetReservationModel,
)
from agent_harness.storage.event_capacity_repositories import MAX_EVENT_SEQ
from agent_harness.storage.models import AgentRunModel, SessionModel
from app import runtime as app_runtime
from app.workers import runtime_worker

pytestmark = pytest.mark.skipif(
    not (os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN") and os.environ.get("REDIS_TEST_DSN")),
    reason="service delegation 合同需要真实 PostgreSQL 与 Redis。",
)


def _service_profiles(
    tmp_path: Path,
    *,
    source_token_limit: int | None = None,
    source_cost_limit: float | None = None,
) -> Path:
    source = Path(__file__).resolve().parents[2] / "templates" / "service-app"
    target = tmp_path / "service-app"
    shutil.copytree(source, target)
    config = target / "agents" / "examples" / "basic" / "config.yaml"
    content = (
        config.read_text(encoding="utf-8")
        .replace(
            "delegation_edges: []",
            "delegation_edges:\n  - examples.ticket_triage",
        )
        .replace(
            "max_tokens_per_run: 8192",
            f"max_tokens_per_run: {source_token_limit or 8192}",
        )
    )
    if source_cost_limit is not None:
        content = content.replace(
            "max_cost_usd_per_run: null",
            f"max_cost_usd_per_run: {source_cost_limit}",
        )
    config.write_text(content, encoding="utf-8")
    return target / "configs" / "profiles"


class _DelegatingExecutor:
    """以真实 execution context 取得绑定 module，业务输入不携带 identity。"""

    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        self.calls += 1
        module = cast(Any, context.require_service("agent.delegate"))
        result = await module.delegate(
            AgentDelegateInput(
                target_agent_id="examples.ticket_triage",
                child_input={"text": str(request.input["text"])},
                idempotency_key=str(request.input["idempotency_key"]),
            )
        )
        return AgentExecutionResult.completed(
            {
                "delegation_id": result.delegation_id,
                "child_run_id": result.child_run_id,
            }
        )


async def _seed_parent(
    components: app_runtime.RuntimeComponents,
    *,
    identity: IdentityContext,
) -> str:
    async with components.storage.uow() as uow:
        await uow.tenants.ensure(identity.tenant_id)
        session = await uow.sessions.ensure(
            SessionCreate(
                session_id=identity.session_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                agent_id="examples.basic",
            )
        )
        parent = await uow.runs.create(
            RunCreate(
                tenant_id=identity.tenant_id,
                session_id=session.id,
                agent_id="examples.basic",
                trace_id="trace-delegation-service",
            )
        )
        await uow.runs.set_status(parent.id, "running")
        await uow.commit()
        return parent.id


@pytest.mark.asyncio
async def test_service_reclaim_executes_one_child_and_holds_unknown_budget_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_update",
    [
        {"session_id": "session-forged"},
        {"user_id": "user-forged"},
    ],
    ids=["different-session", "different-user"],
)
async def test_service_parent_ownership_denies_before_postgresql_or_redis_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    identity_update: dict[str, str],
) -> None:
    """service application seam 必须在 PostgreSQL claim 与 Redis enqueue 前拒绝越权。"""

    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="delegation-owner",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )

    async with isolated_database("service_delegation_ownership") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        components = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "ownership-artifacts",
        )
        try:
            parent_run_id = await _seed_parent(components, identity=identity)
            with pytest.raises(DelegationError) as captured:
                await components.delegation_service.delegate(
                    DelegationRequest(
                        parent_run_id=parent_run_id,
                        source_agent_id="examples.basic",
                        target_agent_id="examples.ticket_triage",
                        child_input={"text": "forged ownership"},
                        idempotency_key="ownership-key",
                    ),
                    identity=identity.model_copy(update=identity_update),
                )
            async with components.storage.uow() as uow:
                claims = await uow.delegations.list_for_parent(
                    tenant_id=identity.tenant_id,
                    parent_run_id=parent_run_id,
                )
                reservations = list(
                    await uow.session.scalars(select(DelegationBudgetReservationModel))
                )
                runs = await uow.runs.list_for_tenant(identity.tenant_id)
            events = await components.event_sink.read(run_id=parent_run_id)
            assert components.queue is not None
            queued = await components.queue.pickup(
                consumer_id="ownership-check",
                block_milliseconds=10,
            )
        finally:
            assert isinstance(components.queue, RedisRunQueue)
            await components.queue.cleanup_namespace()
            await components.close()

    assert captured.value.code == "delegation.policy_denied"
    assert claims == []
    assert reservations == []
    assert [run.id for run in runs] == [parent_run_id]
    assert events == []
    assert queued is None


@pytest.mark.asyncio
async def test_service_bound_executor_competes_budget_and_replays_trusted_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path, source_token_limit=2048)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="trusted-delegation-user",
        session_id=str(uuid4()),
        roles=["operator"],
        permissions=["agent.delegate"],
        auth_method="api-key",
    )

    async with isolated_database("service_delegation_bound_budget") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        first = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts-first",
        )
        second = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts-second",
        )
        try:
            parent_run_id = await _seed_parent(first, identity=identity)
            context = build_execution_context(
                identity=identity,
                services={
                    "agent.delegate": AgentDelegationModule(first.delegation_service),
                },
                agent_id="examples.basic",
                run_id=parent_run_id,
                request_id="request-bound",
                trace_id="trace-delegation-service",
            )
            executor = _DelegatingExecutor()
            executor_result = await executor.run(
                AgentExecutionRequest(
                    agent_id="examples.basic",
                    run_id=parent_run_id,
                    input={
                        "text": "production outage",
                        "idempotency_key": "bound-key",
                    },
                ),
                context,
            )
            assert executor_result.output is not None
            bound_delegation_id = str(executor_result.output["delegation_id"])
            replay = await first.delegation_service.delegate(
                DelegationRequest(
                    parent_run_id=parent_run_id,
                    source_agent_id="examples.basic",
                    target_agent_id="examples.ticket_triage",
                    child_input={"text": "production outage"},
                    idempotency_key="bound-key",
                    request_id="request-bound",
                ),
                identity=identity,
            )
            competing = await asyncio.gather(
                second.delegation_service.delegate(
                    DelegationRequest(
                        parent_run_id=parent_run_id,
                        source_agent_id="examples.basic",
                        target_agent_id="examples.ticket_triage",
                        child_input={"text": "billing outage"},
                        idempotency_key="other-key-a",
                        request_id="request-other-a",
                    ),
                    identity=identity,
                ),
                first.delegation_service.delegate(
                    DelegationRequest(
                        parent_run_id=parent_run_id,
                        source_agent_id="examples.basic",
                        target_agent_id="examples.ticket_triage",
                        child_input={"text": "shipping outage"},
                        idempotency_key="other-key-b",
                        request_id="request-other-b",
                    ),
                    identity=identity,
                ),
                return_exceptions=True,
            )
            competing_success = [
                item for item in competing if isinstance(item, DelegationExecutionResult)
            ]
            competing_failures = [item for item in competing if isinstance(item, DelegationError)]
            async with first.storage.uow() as uow:
                claims = await uow.delegations.list_for_parent(
                    tenant_id=identity.tenant_id,
                    parent_run_id=parent_run_id,
                )
                reservations = [await uow.delegations.get_reservation(claim.id) for claim in claims]
                bound_model = await uow.session.scalar(
                    select(AgentDelegationModel).where(
                        AgentDelegationModel.id == bound_delegation_id
                    )
                )
                assert bound_model is not None and bound_model.child_run_id is not None
                child = await uow.session.get(AgentRunModel, bound_model.child_run_id)
                assert child is not None
                child_session = await uow.session.get(SessionModel, child.session_id)
                bound_identity = dict(bound_model.identity_json)
                child_identity = (
                    None
                    if child_session is None
                    else (
                        child_session.tenant_id,
                        child_session.user_id,
                        child_session.id,
                    )
                )
            assert first.queue is not None
            queued = [
                await first.queue.pickup(consumer_id="budget-proof", block_milliseconds=10),
                await first.queue.pickup(consumer_id="budget-proof", block_milliseconds=10),
            ]
        finally:
            assert isinstance(first.queue, RedisRunQueue)
            await first.queue.cleanup_namespace()
            await first.close()
            await second.close()

    assert replay.delegation_id == bound_delegation_id
    assert replay.child_run_id == executor_result.output["child_run_id"]
    assert len(competing_success) == 1
    assert competing_success[0].delegation_id != bound_delegation_id
    assert len(competing_failures) == 1
    assert competing_failures[0].code == "delegation.budget_exceeded"
    assert len(claims) == 2
    assert [item.reserved_tokens for item in reservations] == [1024, 1024]
    assert sum(item.reserved_tokens for item in reservations) == 2048
    assert bound_identity == identity.to_payload()
    assert child_identity == (identity.tenant_id, identity.user_id, identity.session_id)
    assert {delivery.message.run_id for delivery in queued if delivery is not None} == {
        claim.child_run_id for claim in claims
    }


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
