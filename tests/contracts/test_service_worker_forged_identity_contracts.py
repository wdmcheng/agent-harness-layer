"""Service worker 伪造身份拒绝合同测试。"""

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
    Any as Any,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    ApprovalService as ApprovalService,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    ApprovalStateConflict as ApprovalStateConflict,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    FakeContractExecutor as FakeContractExecutor,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    InvalidRunTransition as InvalidRunTransition,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    Path as Path,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    RunCreate as RunCreate,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    RunStatus as RunStatus,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    SessionCreate as SessionCreate,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    SimpleNamespace as SimpleNamespace,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    build_execute_message as build_execute_message,
)
from tests.contracts.test_split_runtime_execution_contracts import (
    cast as cast,
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


@pytest.mark.parametrize("entrypoint", ["reconcile", "execute", "terminal_evidence"])
@pytest.mark.asyncio
async def test_service_worker_rejects_forged_execution_identity_before_side_effects(
    tmp_path: Path, entrypoint: str
) -> None:
    """身份快照 tenant 被篡改时，reconcile 与 execute 都必须在副作用前失败。"""

    class RecordingExecutor(FakeContractExecutor):
        """记录 executor 是否被调用的替身；身份门禁失效时才会产生该副作用。"""

        def __init__(self) -> None:
            """初始化调用计数。"""

            self.calls = 0

        async def run(
            self,
            request: AgentExecutionRequest,
            context: AgentExecutionContext,
        ) -> AgentExecutionResult:
            """记录意外执行并返回完成结果，使断言能清楚指出门禁被绕过。"""

            del request, context
            self.calls += 1
            return AgentExecutionResult.completed({"unexpected": True})

    dsn = sqlite_dsn(tmp_path / f"forged-identity-{entrypoint}.db")
    events_path = tmp_path / f"forged-identity-{entrypoint}.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    executor = RecordingExecutor()
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=LocalJsonlEventSink(events_path)),
        executor_resolver=lambda _agent_id: executor,
    )
    try:
        async with storage.uow() as uow:
            # 直接植入 tenant 不一致的持久化身份快照，覆盖 worker 三个入口的共同门禁。
            await uow.tenants.ensure("tenant-real")
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id="tenant-real",
                    user_id="submitter",
                    agent_id="fake-agent",
                )
            )
            run = await uow.runs.create_queued(
                RunCreate(
                    tenant_id="tenant-real",
                    session_id=session.id,
                    agent_id="fake-agent",
                    idempotency_key="forged-key",
                    trace_id="trace-forged",
                ),
                execution_context={
                    "identity": {
                        "tenant_id": "tenant-forged",
                        "user_id": "forged-user",
                        "session_id": "forged-session",
                        "roles": [],
                        "permissions": [],
                        "auth_method": "api-key",
                    },
                    "request_id": "request-forged",
                    "trace_id": "trace-forged",
                },
                operation_id="run:pending:execute",
                request_id="request-forged",
                effective_idempotency_key="forged-key",
            )
            private = await uow.runs.get_execution(run.id)
            assert private is not None
            if entrypoint in {"execute", "terminal_evidence"}:
                await uow.runs.mark_queued(
                    run_id=run.id,
                    operation_id=private.operation_id,
                    message_id="forged-message",
                )
            if entrypoint == "terminal_evidence":
                await uow.runs.set_status(
                    run.id,
                    RunStatus.FAILED.value,
                    error={"message": "persisted before evidence"},
                )
            await uow.commit()
        message = build_execute_message(
            request_id="request-forged",
            tenant_id="tenant-real",
            run_id=run.id,
            idempotency_key="forged-key",
        )

        # 三个入口都必须先验证快照，不能先认领队列、调用 executor 或写终态 evidence。
        with pytest.raises(InvalidRunTransition, match="tenant mismatch"):
            if entrypoint == "reconcile":
                await orchestrator.reconcile_queued_run(
                    message=message,
                    message_id="forged-message",
                )
            elif entrypoint == "execute":
                await orchestrator.execute_run(
                    run_id=run.id,
                    tenant_id="tenant-real",
                    operation_id=message.operation_id,
                    owner_id="forged-owner",
                    workflow_id="forged-workflow",
                )
            else:
                await orchestrator.fail_queued_run(
                    run_id=run.id,
                    tenant_id="tenant-real",
                    reason="dbos.error",
                )

        async with storage.uow() as uow:
            persisted = await uow.runs.get(run.id)
            persisted_private = await uow.runs.get_execution(run.id)
        events = await LocalJsonlEventSink(events_path).read(run_id=run.id)
    finally:
        await storage.dispose()

    assert persisted is not None
    assert persisted_private is not None
    expected_status = RunStatus.FAILED if entrypoint == "terminal_evidence" else RunStatus.CREATED
    assert persisted.status == expected_status
    assert persisted_private.owner_id is None
    expected_enqueue_state = "enqueue_pending" if entrypoint == "reconcile" else "queued"
    assert persisted_private.enqueue_state == expected_enqueue_state
    assert executor.calls == 0
    assert events == []


@pytest.mark.asyncio
async def test_approval_worker_rejects_forged_run_identity_tenant() -> None:
    """approval continuation 必须把 run 身份快照与 resolution 权威 tenant 对账。"""

    state = SimpleNamespace(
        tenant_id="tenant-real",
        run_id="run-1",
        operation_id="operation-1",
        lease_id="lease-1",
        resolution_state="execution_owned",
        reviewer_id="reviewer-1",
    )
    record = SimpleNamespace(tenant_id="tenant-real", run_id="run-1")
    run = SimpleNamespace(tenant_id="tenant-real")
    run_state = SimpleNamespace(
        tenant_id="tenant-real",
        execution_context={
            "identity": {
                "tenant_id": "tenant-forged",
                "user_id": "submitter",
                "session_id": "forged-session",
                "roles": [],
                "permissions": [],
                "auth_method": "api-key",
            }
        },
    )

    class ApprovalRepository:
        """提供审批状态和记录的只读仓储替身。"""

        async def get_resolution_queue_state(self, _approval_id: str) -> object:
            """返回已被 worker 认领的 resolution 队列状态。"""

            return state

        async def get(self, _approval_id: str) -> object:
            """返回审批记录，供服务核对运行归属。"""

            return record

    class RunRepository:
        """提供运行公开记录和私有执行身份快照的仓储替身。"""

        async def get(self, _run_id: str) -> object:
            """返回可信运行 tenant 记录。"""

            return run

        async def get_execution(self, _run_id: str) -> object:
            """返回被伪造 tenant 的执行身份快照。"""

            return run_state

    class UnitOfWork:
        """暴露审批与运行仓储的异步上下文替身，不执行真实事务。"""

        approvals = ApprovalRepository()
        runs = RunRepository()

        async def __aenter__(self) -> UnitOfWork:
            """进入当前固定的只读夹具上下文。"""

            return self

        async def __aexit__(self, *_args: object) -> None:
            """无副作用退出；测试关注服务在读取后立即拒绝。"""

            return None

    class Storage:
        """为审批服务返回固定 UoW 的最小存储替身。"""

        def uow(self) -> UnitOfWork:
            """创建独立但内容相同的 UoW，符合服务的调用形状。"""

            return UnitOfWork()

    class Orchestrator:
        """仅实现审批服务构造所需绑定 seam 的编排器替身。"""

        def bind_approval_service(self, _service: ApprovalService) -> None:
            """接受绑定但不保存服务，避免测试引入循环依赖。"""

            return None

    service = ApprovalService(
        storage=cast(Any, Storage()),
        event_bus=cast(Any, SimpleNamespace()),
        orchestrator=cast(Any, Orchestrator()),
    )
    with pytest.raises(ApprovalStateConflict, match="tenant mismatch"):
        await service.execute_queued_approval(
            approval_id="approval-1",
            tenant_id="tenant-real",
            run_id="run-1",
            operation_id="operation-1",
            lease_id="lease-1",
        )
