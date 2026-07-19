"""真实 Service 委派 owner 边界与绑定执行器集成测试。"""

from __future__ import annotations

from tests.integration.test_service_delegation_contracts import (
    AgentDelegationModel as AgentDelegationModel,
)
from tests.integration.test_service_delegation_contracts import (
    AgentDelegationModule as AgentDelegationModule,
)
from tests.integration.test_service_delegation_contracts import (
    AgentExecutionRequest as AgentExecutionRequest,
)
from tests.integration.test_service_delegation_contracts import (
    AgentRunModel as AgentRunModel,
)
from tests.integration.test_service_delegation_contracts import (
    DelegationBudgetReservationModel as DelegationBudgetReservationModel,
)
from tests.integration.test_service_delegation_contracts import (
    DelegationError as DelegationError,
)
from tests.integration.test_service_delegation_contracts import (
    DelegationExecutionResult as DelegationExecutionResult,
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
    SessionModel as SessionModel,
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
    asyncio as asyncio,
)
from tests.integration.test_service_delegation_contracts import (
    build_execution_context as build_execution_context,
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
    select as select,
)
from tests.integration.test_service_delegation_contracts import (
    uuid4 as uuid4,
)


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
    """验证绑定执行器传递可信身份，并与并发委派竞争同一父级预算。

    该集成用例同时固定幂等重放、子运行身份继承、Redis 投递和预算拒绝，
    防止 service seam 因多进程装配而绕开直接委派路径的所有权约束。
    """
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
