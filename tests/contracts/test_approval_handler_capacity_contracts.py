"""审批 handler 单次执行与事件容量合同测试。"""

from __future__ import annotations

from tests.contracts.test_approval_execution_contracts import (
    MAX_EVENT_SEQ as MAX_EVENT_SEQ,
)
from tests.contracts.test_approval_execution_contracts import (
    Any as Any,
)
from tests.contracts.test_approval_execution_contracts import (
    ApprovalGrant as ApprovalGrant,
)
from tests.contracts.test_approval_execution_contracts import (
    ApprovalStateConflict as ApprovalStateConflict,
)
from tests.contracts.test_approval_execution_contracts import (
    EventCapacityExceeded as EventCapacityExceeded,
)
from tests.contracts.test_approval_execution_contracts import (
    Path as Path,
)
from tests.contracts.test_approval_execution_contracts import (
    RunEventCapacityModel as RunEventCapacityModel,
)
from tests.contracts.test_approval_execution_contracts import (
    RunStatus as RunStatus,
)
from tests.contracts.test_approval_execution_contracts import (
    ToolCallRequest as ToolCallRequest,
)
from tests.contracts.test_approval_execution_contracts import (
    ToolRuntimeContext as ToolRuntimeContext,
)
from tests.contracts.test_approval_execution_contracts import (
    asyncio as asyncio,
)
from tests.contracts.test_approval_execution_contracts import (
    build_approval_flow as build_approval_flow,
)
from tests.contracts.test_approval_execution_contracts import (
    hash_tool_arguments as hash_tool_arguments,
)
from tests.contracts.test_approval_execution_contracts import (
    pytest as pytest,
)
from tests.contracts.test_approval_execution_contracts import (
    update as update,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_approved_continuation_executes_once_and_seals_known_result(
    tmp_path: Path,
    fails: bool,
) -> None:
    calls = 0

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if fails:
            raise RuntimeError("deterministic tool failure")
        return {"stdout": str(arguments["command"])}

    (
        storage,
        sink,
        service,
        _orchestrator,
        identity,
        _registry,
        waiting,
    ) = await build_approval_flow(tmp_path, handler=handler)
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        resolved = await service.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
        )
        with pytest.raises(ApprovalStateConflict):
            await service.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
            )
        async with storage.uow() as uow:
            persisted = await uow.approvals.get(approval.approval_id)
            invocation = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
        events = await sink.read(run_id=waiting.run_id)
    finally:
        await storage.dispose()

    assert calls == 1
    assert persisted is not None and persisted.status == "approved"
    assert invocation is not None
    assert invocation.execution_state == ("failed" if fails else "completed")
    assert resolved.run is not None
    assert resolved.run.status == (RunStatus.FAILED if fails else RunStatus.COMPLETED)
    assert sum(event.event_type.value == "approval.resolved" for event in events) == 1
    assert sum(event.terminal for event in events) == 1


@pytest.mark.asyncio
async def test_approved_tool_reserves_capacity_during_handler_and_releases_after_result(
    tmp_path: Path,
) -> None:
    """approved tool 的三格预约覆盖 handler 副作用窗口，确定结果后完整释放。"""

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"stdout": str(arguments["command"])}

    (
        storage,
        _sink,
        _service,
        _orchestrator,
        identity,
        registry,
        waiting,
    ) = await build_approval_flow(tmp_path, handler=handler)
    execution_task: asyncio.Task[Any] | None = None
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
            lease = await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                request_id="req-tool-capacity-window",
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=approval.approval_id,
            lease_id=lease.lease_id,
            tenant_id=identity.tenant_id,
            identity_id=identity.user_id,
            agent_id=approval.agent_id,
            run_id=approval.run_id,
            action=approval.action,
            resource=approval.resource,
            arguments_hash=hash_tool_arguments({"command": "echo safe"}),
        )
        execution_task = asyncio.create_task(
            registry.call_approved(
                ToolCallRequest(
                    tool_name="shell.execute",
                    arguments={"command": "echo safe"},
                    agent_id=approval.agent_id,
                    run_id=approval.run_id,
                ),
                context=ToolRuntimeContext(
                    actor=identity,
                    agent_id=approval.agent_id,
                    run_id=approval.run_id,
                ),
                grant=grant,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        async with storage.uow() as uow:
            active = await uow.event_capacity.snapshot(waiting.run_id)
            invocation = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
        assert active.outstanding_reserved_event_count == 3
        assert invocation is not None and invocation.execution_state == "executing"
    finally:
        release.set()
        if execution_task is not None:
            await execution_task

    try:
        async with storage.uow() as uow:
            settled = await uow.event_capacity.snapshot(waiting.run_id)
        assert calls == 1
        assert settled.outstanding_reserved_event_count == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_approved_tool_capacity_exhaustion_has_zero_claim_artifact_and_handler(
    tmp_path: Path,
) -> None:
    """容量不足必须在 args artifact、tool claim 与 handler 之前失败。"""

    calls = 0

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return arguments

    (
        storage,
        _sink,
        _service,
        _orchestrator,
        identity,
        registry,
        waiting,
    ) = await build_approval_flow(tmp_path, handler=handler)
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
            lease = await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                request_id="req-tool-capacity-exhausted",
            )
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == waiting.run_id)
                .values(highest_persisted_seq=MAX_EVENT_SEQ - 1)
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=approval.approval_id,
            lease_id=lease.lease_id,
            tenant_id=identity.tenant_id,
            identity_id=identity.user_id,
            agent_id=approval.agent_id,
            run_id=approval.run_id,
            action=approval.action,
            resource=approval.resource,
            arguments_hash=hash_tool_arguments({"command": "echo safe"}),
        )

        with pytest.raises(EventCapacityExceeded):
            await registry.call_approved(
                ToolCallRequest(
                    tool_name="shell.execute",
                    arguments={"command": "echo safe"},
                    agent_id=approval.agent_id,
                    run_id=approval.run_id,
                ),
                context=ToolRuntimeContext(
                    actor=identity,
                    agent_id=approval.agent_id,
                    run_id=approval.run_id,
                ),
                grant=grant,
            )

        async with storage.uow() as uow:
            invocation = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
            capacity = await uow.event_capacity.snapshot(waiting.run_id)
        assert invocation is None
        assert capacity.outstanding_reserved_event_count == 0
        assert calls == 0
        assert list((tmp_path / "artifacts").glob("**/*.json")) == []
    finally:
        await storage.dispose()
