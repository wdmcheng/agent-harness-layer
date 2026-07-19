"""审批并发解决、伪造授权与 claim 状态合同测试。"""

from __future__ import annotations

from tests.contracts.test_approval_execution_contracts import (
    Any as Any,
)
from tests.contracts.test_approval_execution_contracts import (
    ApprovalGrant as ApprovalGrant,
)
from tests.contracts.test_approval_execution_contracts import (
    ApprovalResolutionRepositoryConflict as ApprovalResolutionRepositoryConflict,
)
from tests.contracts.test_approval_execution_contracts import (
    ApprovalStateConflict as ApprovalStateConflict,
)
from tests.contracts.test_approval_execution_contracts import (
    ApprovedToolGrantError as ApprovedToolGrantError,
)
from tests.contracts.test_approval_execution_contracts import (
    Path as Path,
)
from tests.contracts.test_approval_execution_contracts import (
    ToolCallRequest as ToolCallRequest,
)
from tests.contracts.test_approval_execution_contracts import (
    ToolInvocationCreate as ToolInvocationCreate,
)
from tests.contracts.test_approval_execution_contracts import (
    ToolRuntimeContext as ToolRuntimeContext,
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


@pytest.mark.asyncio
async def test_approve_and_deny_repository_updates_have_one_winner(tmp_path: Path) -> None:
    """审批和拒绝竞争同一记录时 repository 必须只允许一个状态转换胜出，后续操作走恢复语义。"""

    def passthrough(arguments: dict[str, Any]) -> dict[str, Any]:
        """返回原参数的无副作用工具替身，使本用例只验证审批状态竞争。"""

        return arguments

    (
        storage,
        _sink,
        service,
        orchestrator,
        identity,
        _registry,
        waiting,
    ) = await build_approval_flow(tmp_path, handler=passthrough)
    try:
        async with storage.uow() as uow:
            first = (await uow.approvals.list_by_run(waiting.run_id))[0]
            lease = await uow.approvals.claim_resolution(
                approval_id=first.approval_id,
                run_id=first.run_id,
                tenant_id=first.tenant_id,
                request_id="req-first-resolution-lease",
            )
            await uow.commit()
        with pytest.raises(ApprovalResolutionRepositoryConflict) as in_progress:
            async with storage.uow() as uow:
                await uow.approvals.deny_waiting(
                    approval_id=first.approval_id,
                    run_id=first.run_id,
                    tenant_id=first.tenant_id,
                    resolved_by=identity.user_id,
                    request_id="req-losing-deny",
                )
        assert in_progress.value.code == "approval.resolution_in_progress"
        recovered = await service.recover_claimed(
            actor=identity,
            run_id=first.run_id,
            approval_id=first.approval_id,
        )
        assert recovered.approval.status == "approved"

        second_waiting = await orchestrator.start_run(
            agent_id="examples.dev",
            input={"prompt": "deny first"},
        )
        async with storage.uow() as uow:
            second = (await uow.approvals.list_by_run(second_waiting.run_id))[0]
            denied = await uow.approvals.deny_waiting(
                approval_id=second.approval_id,
                run_id=second.run_id,
                tenant_id=second.tenant_id,
                resolved_by=identity.user_id,
                request_id="req-winning-deny",
            )
            await uow.commit()
        with pytest.raises(ApprovalResolutionRepositoryConflict) as invalid:
            async with storage.uow() as uow:
                await uow.approvals.claim_resolution(
                    approval_id=second.approval_id,
                    run_id=second.run_id,
                    tenant_id=second.tenant_id,
                    request_id="req-invalid-resolution-lease",
                )
        assert invalid.value.code == "approval.resolution_in_progress"
    finally:
        await storage.dispose()

    assert lease.approval.status == "waiting"
    assert denied.status == "waiting"


@pytest.mark.asyncio
async def test_forged_grants_and_denial_never_call_handler(tmp_path: Path) -> None:
    """任一 grant 绑定字段被伪造或审批被拒绝时，工具 handler 都不得获得执行机会。"""

    calls = 0

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        """计数并返回参数；计数必须保持零，作为权限验证先于副作用的强断言。"""

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
                request_id="req-approved-continuation-lease",
            )
            await uow.commit()
        base = ApprovalGrant(
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
        request = ToolCallRequest(
            tool_name="shell.execute",
            arguments={"command": "echo safe"},
            agent_id=approval.agent_id,
            run_id=approval.run_id,
        )
        context = ToolRuntimeContext(
            actor=identity,
            agent_id=approval.agent_id,
            run_id=approval.run_id,
        )
        mutations: dict[str, str] = {
            "approval_id": "forged-approval",
            "lease_id": "forged-lease",
            "tenant_id": "other-tenant",
            "identity_id": "other-user",
            "agent_id": "other-agent",
            "run_id": "other-run",
            "action": "file.delete",
            "resource": "tool:other",
            "arguments_hash": hash_tool_arguments({"command": "different"}),
        }
        for field, value in mutations.items():
            with pytest.raises(ApprovedToolGrantError):
                await registry.call_approved(
                    request,
                    context=context,
                    grant=base.model_copy(update={field: value}),
                )
    finally:
        await storage.dispose()

    deny_root = tmp_path / "deny"
    deny_root.mkdir()
    (
        storage,
        _sink,
        service,
        _orchestrator,
        identity,
        _registry,
        waiting,
    ) = await build_approval_flow(deny_root, handler=handler)
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        result = await service.deny(
            actor=identity,
            run_id=approval.run_id,
            approval_id=approval.approval_id,
        )
    finally:
        await storage.dispose()

    assert result.approval.status == "denied"
    assert calls == 0


@pytest.mark.asyncio
async def test_existing_executing_claim_keeps_public_waiting_and_needs_review(
    tmp_path: Path,
) -> None:
    """发现既有执行中工具 claim 时，公开审批保持等待并转人工复核，不能贸然再次执行。"""

    calls = 0

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        """若状态保护失效会递增计数；正确路径应在 handler 前阻断，始终保持零次调用。"""

        nonlocal calls
        calls += 1
        return arguments

    (
        storage,
        _sink,
        service,
        _orchestrator,
        identity,
        _registry,
        waiting,
    ) = await build_approval_flow(tmp_path, handler=handler)
    try:
        async with storage.uow() as uow:
            approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
            await uow.tool_invocations.create(
                ToolInvocationCreate(
                    tenant_id=identity.tenant_id,
                    agent_id=approval.agent_id,
                    run_id=approval.run_id,
                    tool_name="shell.execute",
                    args_ref="artifact://pending",
                    status="executing",
                    approval_id=approval.approval_id,
                    arguments_hash=hash_tool_arguments({"command": "echo safe"}),
                    execution_state="executing",
                )
            )
            await uow.commit()
        with pytest.raises(ApprovalStateConflict) as exc_info:
            await service.approve(
                actor=identity,
                run_id=approval.run_id,
                approval_id=approval.approval_id,
            )
        async with storage.uow() as uow:
            public = await uow.approvals.get(approval.approval_id)
            private = await uow.approvals.get_resolution(approval.approval_id)
    finally:
        await storage.dispose()

    assert exc_info.value.code == "approval.execution_needs_review"
    assert calls == 0
    assert public is not None and public.status == "waiting"
    assert private is not None and private.state == "needs_review"
