"""Service 提交与 worker 共享运行身份合同测试。"""

from __future__ import annotations

from tests.contracts.test_split_runtime_execution_contracts import (
    AgentExecutionContext as AgentExecutionContext,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    AgentExecutionRequest as AgentExecutionRequest,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    AgentExecutionResult as AgentExecutionResult,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    ApprovalService as ApprovalService,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    FakeContractExecutor as FakeContractExecutor,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    IdentityContext as IdentityContext,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    InMemoryRunQueue as InMemoryRunQueue,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    Path as Path,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    RunStatus as RunStatus,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    pytest as pytest,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    sqlite_dsn as sqlite_dsn,
)


@pytest.mark.asyncio
async def test_service_submit_and_worker_execute_share_run_and_identity(tmp_path: Path) -> None:
    """service API只排队；worker从持久化 context执行同一 run。"""

    class RecordingExecutor(FakeContractExecutor):
        """记录 worker 接收到的请求和身份上下文，验证跨进程传递没有丢失。"""

        calls: list[tuple[AgentExecutionRequest, AgentExecutionContext]]

        def __init__(self) -> None:
            """初始化空调用记录，避免父类夹具状态影响本合同的精确断言。"""

            self.calls = []

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """保存 worker 输入后返回确定性完成结果，不引入外部执行副作用。"""

            self.calls.append((request, context))
            return AgentExecutionResult.completed({"source_ref": request.input["source_ref"]})

    db_path = tmp_path / "service-runtime.db"
    events_path = tmp_path / "service-events.jsonl"
    run_migrations(sqlite_dsn(db_path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(db_path))
    queue = InMemoryRunQueue()
    executor = RecordingExecutor()
    identity = IdentityContext(
        tenant_id="tenant-service",
        user_id="user-service",
        session_id="session-service",
        roles=["operator"],
        permissions=["runs:execute"],
        auth_method="api-key",
    )
    event_bus = EventBus(sink=LocalJsonlEventSink(events_path))
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
        queue=queue,
        executor_resolver=lambda _agent_id: executor,
    )
    approval_service = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        queue=queue,
    )
    try:
        submitted = await orchestrator.submit_run(
            agent_id="fake-agent",
            input={"source_ref": "source://service", "trust_level": "trusted"},
            idempotency_key="client-key",
            identity=identity,
            request_id="req-service",
            trace_id="trace-service",
        )
        assert submitted.status == RunStatus.CREATED
        assert executor.calls == []

        delivery = await queue.pickup(consumer_id="worker-a")
        assert delivery is not None
        completed = await orchestrator.execute_run(
            run_id=delivery.message.run_id,
            tenant_id=delivery.message.tenant_id,
            operation_id=delivery.message.operation_id,
            owner_id="owner-service",
            workflow_id="workflow-service",
        )
        replay = await orchestrator.execute_run(
            run_id=delivery.message.run_id,
            tenant_id=delivery.message.tenant_id,
            operation_id=delivery.message.operation_id,
            owner_id="owner-service",
            workflow_id="workflow-service",
        )
        waiting_submit = await orchestrator.submit_run(
            agent_id="fake-agent",
            input={"source_ref": "source://guardrail", "trust_level": "untrusted"},
            checkpoint_state={
                "reason": "guardrail approval",
                "policy": {"decision": "require_approval"},
            },
            identity=identity,
            request_id="req-guardrail",
            trace_id="trace-guardrail",
        )
        waiting_delivery = await queue.pickup(consumer_id="worker-a")
        assert waiting_delivery is not None
        waiting = await orchestrator.execute_run(
            run_id=waiting_delivery.message.run_id,
            tenant_id=waiting_delivery.message.tenant_id,
            operation_id=waiting_delivery.message.operation_id,
            owner_id="owner-waiting",
            workflow_id="workflow-waiting",
        )
        async with storage.uow() as uow:
            guardrail_approvals = await uow.approvals.list_by_run(waiting_submit.run_id)
        reviewer = IdentityContext(
            tenant_id=identity.tenant_id,
            user_id="guardrail-reviewer",
            session_id="guardrail-review-session",
            roles=["reviewer"],
            permissions=["*"],
            auth_method="api-key",
        )
        guardrail_approval = guardrail_approvals[0]
        await approval_service.approve(
            actor=reviewer,
            run_id=waiting_submit.run_id,
            approval_id=guardrail_approval.approval_id,
            request_id="req-guardrail-approve",
            comment="approved guardrail",
        )
        approval_delivery = await queue.pickup(consumer_id="worker-approval")
        assert approval_delivery is not None
        assert approval_delivery.message.kind == "resume_approval"
        async with storage.uow() as uow:
            assert await uow.approvals.claim_resolution_execution(
                approval_id=guardrail_approval.approval_id,
                tenant_id=approval_delivery.message.tenant_id,
                run_id=approval_delivery.message.run_id,
                lease_id=approval_delivery.message.resolution_lease_id or "",
                operation_id=approval_delivery.message.operation_id,
                request_id=approval_delivery.message.request_id,
                message_id=approval_delivery.receipt.message_id,
                workflow_owner_id="guardrail-approval-owner",
                workflow_id="guardrail-approval-workflow",
            )
            await uow.commit()
        guardrail_resolved = await approval_service.execute_queued_approval(
            approval_id=guardrail_approval.approval_id,
            tenant_id=approval_delivery.message.tenant_id,
            run_id=approval_delivery.message.run_id,
            operation_id=approval_delivery.message.operation_id,
            lease_id=approval_delivery.message.resolution_lease_id or "",
        )
        events = await LocalJsonlEventSink(events_path).read(run_id=submitted.run_id)
        guardrail_events = await LocalJsonlEventSink(events_path).read(run_id=waiting_submit.run_id)
    finally:
        await storage.dispose()

    assert completed.status == RunStatus.COMPLETED
    assert replay.status == RunStatus.COMPLETED
    assert waiting_submit.status == RunStatus.CREATED
    assert waiting.status == RunStatus.WAITING
    assert len(guardrail_approvals) == 1
    assert guardrail_approvals[0].action == "input.prompt_injection"
    assert guardrail_resolved.run is not None
    assert guardrail_resolved.run.status == RunStatus.COMPLETED
    correlated_types = {
        CanonicalEventType.CHECKPOINT_CREATED,
        CanonicalEventType.RUN_RESUMED,
        CanonicalEventType.RUN_COMPLETED,
    }
    correlated = [event for event in guardrail_events if event.event_type in correlated_types]
    assert len(correlated) == 3
    expected_request_ids = {
        CanonicalEventType.CHECKPOINT_CREATED: "req-guardrail",
        CanonicalEventType.RUN_RESUMED: "req-guardrail-approve",
        CanonicalEventType.RUN_COMPLETED: "req-guardrail-approve",
    }
    for event in correlated:
        assert event.request_id == expected_request_ids[event.event_type]
        assert event.trace_id == "trace-guardrail"
        assert event.payload is not None
        assert event.payload["source_ref"] == "source://guardrail"
        assert event.payload["trust_level"] == "untrusted"
    assert len(executor.calls) == 1
    request, context = executor.calls[0]
    assert request.run_id == submitted.run_id
    assert request.input["trust_level"] == "trusted"
    assert context.identity == identity
    assert context.request_id == "req-service"
    assert context.trace_id == "trace-service"
    assert [event.event_type.value for event in events] == [
        "run.queued",
        "run.started",
        "run.completed",
    ]
    assert events[-1].request_id == "req-service"
    assert events[-1].trace_id == "trace-service"
    assert events[-1].payload is not None
    assert events[-1].payload["source_ref"] == "source://service"
    assert events[-1].payload["trust_level"] == "trusted"
