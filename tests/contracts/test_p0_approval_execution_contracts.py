"""审批续跑、grant 绑定和 at-most-once 工具执行合同测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_harness.approvals import ApprovalService, ApprovalStateConflict
from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import (
    AgentApprovalRequest,
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    InMemoryRunQueue,
    RunOrchestrator,
    RunResult,
    RunStatus,
)
from agent_harness.storage import SQLAlchemyStorage, ToolInvocationCreate, run_migrations
from agent_harness.storage.access_repositories import ApprovalResolutionRepositoryConflict
from agent_harness.tools import (
    ApprovedToolGrantError,
    BuiltinTool,
    ToolCallRequest,
    ToolRegistry,
    ToolRuntimeContext,
    hash_tool_arguments,
)


def sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


class _ApprovedToolExecutor:
    def __init__(self, registry: ToolRegistry, arguments: dict[str, Any]) -> None:
        self.registry = registry
        self.arguments = arguments

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        del context
        return AgentExecutionResult.waiting(
            AgentApprovalRequest(
                action="shell.execute",
                resource="tool:shell",
                reason="dangerous test action",
                arguments_ref="artifact://pending-arguments",
                arguments_hash=hash_tool_arguments(self.arguments),
                continuation={"tool_name": "shell.execute"},
            )
        )

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        tool_result = await self.registry.call_approved(
            ToolCallRequest(
                tool_name="shell.execute",
                arguments=self.arguments,
                agent_id=request.agent_id,
                run_id=request.run_id,
            ),
            context=ToolRuntimeContext(
                actor=context.identity,
                agent_id=request.agent_id,
                run_id=request.run_id,
            ),
            grant=grant,
        )
        if tool_result.status != "completed":
            message = tool_result.error.message if tool_result.error else "tool failed"
            return AgentExecutionResult.failed(message)
        return AgentExecutionResult.completed({"tool_result": tool_result.result})


async def build_approval_flow(
    tmp_path: Path,
    *,
    handler: Any,
    recovery_lease_timeout_seconds: float = 300.0,
    queue: InMemoryRunQueue | None = None,
) -> tuple[
    SQLAlchemyStorage,
    LocalJsonlEventSink,
    ApprovalService,
    RunOrchestrator,
    IdentityContext,
    ToolRegistry,
    RunResult,
]:
    dsn = sqlite_dsn(tmp_path / "approval.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "approval-events.jsonl")
    artifact_store = FileArtifactStore(tmp_path / "artifacts")
    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="shell.execute",
                action="shell.execute",
                resource="tool:shell",
                input_schema={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                handler=handler,
            )
        ],
        policy=PolicyEngine(provider=YamlPolicyProvider()),
        audit=None,
        artifact_store=artifact_store,
        storage=storage,
    )
    executor = _ApprovedToolExecutor(registry, {"command": "echo safe"})
    identity = IdentityContext.local_default(session_id="approval-contract")
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
        identity=identity,
        executor_resolver=lambda _agent_id: executor,
        queue=queue,
    )
    service = ApprovalService(
        storage=storage,
        event_bus=EventBus(sink=sink),
        orchestrator=orchestrator,
        audit=AuditService(storage),
        recovery_lease_timeout_seconds=recovery_lease_timeout_seconds,
    )
    if queue is None:
        waiting = await orchestrator.start_run(agent_id="examples.dev", input={"prompt": "act"})
    else:
        submitted = await orchestrator.submit_run(
            agent_id="examples.dev",
            input={"prompt": "act"},
            identity=identity,
        )
        delivery = await queue.pickup(consumer_id="initial-service-worker")
        assert delivery is not None
        waiting = await orchestrator.execute_run(
            run_id=submitted.run_id,
            tenant_id=identity.tenant_id,
            operation_id=delivery.message.operation_id,
            owner_id="initial-service-owner",
            workflow_id="initial-service-workflow",
        )
        await queue.ack(delivery.receipt)
    return storage, sink, service, orchestrator, identity, registry, waiting


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
async def test_approve_and_deny_repository_updates_have_one_winner(tmp_path: Path) -> None:
    def passthrough(arguments: dict[str, Any]) -> dict[str, Any]:
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
        assert invalid.value.code == "approval.invalid_transition"
    finally:
        await storage.dispose()

    assert lease.approval.status == "waiting"
    assert denied.status == "denied"


@pytest.mark.asyncio
async def test_forged_grants_and_denial_never_call_handler(tmp_path: Path) -> None:
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
    calls = 0

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
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
