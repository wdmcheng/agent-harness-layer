"""审批后工具执行的 grant 校验、跨进程仲裁与结果持久化。"""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import cast
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.runtime.executor import (
    AgentExecutionLeaseLost,
    AgentExecutionUncertain,
    ApprovalGrant,
)
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage, ToolInvocationCreate
from agent_harness.tools.execution_support import (
    error_result,
    invoke_handler,
    redact_tool_result,
    source_ref,
    validate_arguments,
)
from agent_harness.tools.output_guard import guarded_tool_payload
from agent_harness.tools.types import (
    BuiltinTool,
    ToolCallRequest,
    ToolCallResult,
    ToolErrorCode,
    ToolExecutionError,
    ToolRuntimeContext,
)


class ApprovedToolGrantError(RuntimeError):
    """ApprovalGrant 与待执行 tool request 不匹配。"""


class ApprovedToolLeaseLost(ApprovedToolGrantError, AgentExecutionLeaseLost):
    """持久化 lease 已被新所有者接管，旧 grant 必须停止执行。"""


class ApprovedToolExecutionUncertain(AgentExecutionUncertain):
    """持久化 executing claim 没有确定性 result artifact。"""


def hash_tool_arguments(arguments: dict[str, object]) -> str:
    """返回 checkpoint/grant 绑定使用的 canonical SHA-256。"""

    serialized = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).hexdigest()


class ApprovedToolExecutor:
    """审批后工具的 at-most-once 执行器，与普通 policy 评估路径隔离。"""

    def __init__(
        self,
        *,
        tools: dict[str, BuiltinTool],
        storage: SQLAlchemyStorage | None,
        artifact_store: FileArtifactStore,
        audit: AuditService | None,
        inline_result_bytes: int,
        agent_tool_allowlist: set[str],
        enforce_agent_tool_allowlist: bool,
    ) -> None:
        self._tools = tools
        self._storage = storage
        self._artifact_store = artifact_store
        self._audit = audit
        self._inline_result_bytes = inline_result_bytes
        self._agent_tool_allowlist = agent_tool_allowlist
        self._enforce_agent_tool_allowlist = enforce_agent_tool_allowlist

    async def execute(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
        grant: ApprovalGrant,
    ) -> ToolCallResult:
        """校验持久化 lease，并在唯一 claim 后至多执行一次 approved action。"""

        if self._storage is None:
            raise RuntimeError("approved tool execution requires storage")
        tool = self._tools.get(request.tool_name)
        if tool is None:
            raise ApprovedToolGrantError("approval grant references an unknown tool")
        validate_approval_grant(grant, request, context, tool)
        async with self._storage.uow() as uow:
            lease = await uow.approvals.get_resolution(grant.approval_id)
        if lease is None:
            raise ApprovedToolGrantError("approval grant does not match its persisted lease")
        if lease.lease_id != grant.lease_id:
            raise ApprovedToolLeaseLost(
                f"approval lease was fenced by another owner: {grant.approval_id}"
            )
        approval = lease.approval
        persisted = {
            "tenant_id": approval.tenant_id,
            "identity_id": str(approval.metadata.get("identity_id") or approval.requested_by),
            "agent_id": approval.agent_id,
            "run_id": approval.run_id,
            "action": approval.action,
            "resource": approval.resource,
            "arguments_hash": str(approval.metadata.get("arguments_hash") or ""),
        }
        grant_fields = {
            "tenant_id": grant.tenant_id,
            "identity_id": grant.identity_id,
            "agent_id": grant.agent_id,
            "run_id": grant.run_id,
            "action": grant.action,
            "resource": grant.resource,
            "arguments_hash": grant.arguments_hash,
        }
        mismatch = next(
            (field for field, value in persisted.items() if grant_fields[field] != value),
            None,
        )
        if mismatch is not None:
            raise ApprovedToolGrantError(f"approval grant persistence mismatch: {mismatch}")
        async with self._storage.uow() as uow:
            existing = await uow.tool_invocations.get_by_approval_id(grant.approval_id)
        if lease.state not in {"claimed", "recovery_pending"}:
            if (
                existing
                and existing.result_ref
                and existing.execution_state in {"completed", "failed"}
            ):
                payload = self._artifact_store.read_json(existing.result_ref)
                return ToolCallResult.model_validate(payload)
            if lease.state == "needs_review":
                raise ApprovedToolExecutionUncertain(
                    f"approved tool execution needs review: {grant.approval_id}"
                )
            raise ApprovedToolGrantError("approval grant lease is no longer executable")

        arguments_hash = hash_tool_arguments(request.arguments)
        args_ref = self._artifact_store.write_json({"arguments": request.arguments}).ref
        created = False
        try:
            async with self._storage.uow() as uow:
                fenced = await uow.approvals.fence_resolution_lease(
                    approval_id=grant.approval_id,
                    run_id=grant.run_id,
                    tenant_id=grant.tenant_id,
                    lease_id=grant.lease_id,
                )
                if not fenced:
                    raise ApprovedToolLeaseLost(
                        f"approval lease is no longer active: {grant.approval_id}"
                    )
                await uow.tool_invocations.create(
                    ToolInvocationCreate(
                        tenant_id=context.actor.tenant_id,
                        agent_id=context.agent_id,
                        run_id=context.run_id,
                        tool_name=request.tool_name,
                        args_ref=args_ref,
                        status="executing",
                        approval_id=grant.approval_id,
                        arguments_hash=arguments_hash,
                        execution_state="executing",
                        trace_id=context.trace_id or request.trace_id,
                        request_id=context.request_id or request.request_id,
                        metadata={"lease_id": grant.lease_id},
                    )
                )
                await uow.commit()
                created = True
        except IntegrityError:
            # unique approval_id 是跨进程仲裁点；冲突后只允许读取确定性结果。
            created = False

        if not created:
            if existing is None:
                async with self._storage.uow() as uow:
                    existing = await uow.tool_invocations.get_by_approval_id(grant.approval_id)
            if existing is None:
                raise ApprovedToolExecutionUncertain(
                    "approved tool claim collided but cannot be read"
                )
            if existing.result_ref and existing.execution_state in {"completed", "failed"}:
                payload = self._artifact_store.read_json(existing.result_ref)
                return ToolCallResult.model_validate(payload)
            raise ApprovedToolExecutionUncertain(
                f"approved tool execution needs review: {grant.approval_id}"
            )

        approved_context = context.model_copy(deep=True).authorize_approved_call(grant.approval_id)
        result = await self._call_handler(request, context=approved_context, tool=tool)
        result_ref = self._artifact_store.write_json(result.to_payload()).ref
        execution_state = "completed" if result.status == "completed" else "failed"
        async with self._storage.uow() as uow:
            await uow.tool_invocations.finish_approved_claim(
                approval_id=grant.approval_id,
                result_ref=result_ref,
                execution_state=execution_state,
                status=result.status,
            )
            await uow.commit()
        return result

    async def _call_handler(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
        tool: BuiltinTool,
    ) -> ToolCallResult:
        """校验输入并执行 handler，不重新进入已通过的 approval policy gate。"""

        invocation_id = str(uuid4())
        result_source_ref = source_ref(request.tool_name, invocation_id, context.run_id)
        if not self._is_agent_tool_allowed(request.tool_name):
            return error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.ALLOWLIST_DENIED,
                f"tool is not allowlisted for agent: {request.tool_name}",
            )
        validation_error = validate_arguments(tool.input_schema, request.arguments)
        if validation_error is not None:
            return error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.SCHEMA_VALIDATION_FAILED,
                validation_error.message,
                field_path=validation_error.field_path,
                hint=validation_error.hint,
            )
        if tool.preflight is not None:
            try:
                preflight_result = invoke_handler(tool.preflight, request, context)
                if inspect.isawaitable(preflight_result):
                    preflight_result = await preflight_result
            except ToolExecutionError as exc:
                preflight_result = error_result(
                    request,
                    context,
                    invocation_id,
                    result_source_ref,
                    exc.code,
                    exc.message,
                    field_path=exc.field_path,
                    hint=exc.hint,
                )
            if preflight_result is not None:
                result = cast(ToolCallResult, preflight_result)
                await self._record_audit(
                    context,
                    request.tool_name,
                    result.invocation_id,
                    result.status,
                )
                return result
        try:
            raw_result = invoke_handler(tool.handler, request, context)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
            if isinstance(raw_result, ToolCallResult):
                result = raw_result
            else:
                payload, artifact_ref, truncation = guarded_tool_payload(
                    tool_name=request.tool_name,
                    invocation_id=invocation_id,
                    payload=raw_result,
                    artifact_store=self._artifact_store,
                    inline_bytes=self._inline_result_bytes,
                )
                result = ToolCallResult(
                    tool_name=request.tool_name,
                    status="completed",
                    invocation_id=invocation_id,
                    result=payload,
                    source_ref=result_source_ref,
                    artifact_ref=artifact_ref,
                    truncation=truncation,
                    request_id=context.request_id or request.request_id,
                    trace_id=context.trace_id or request.trace_id,
                )
        except ToolExecutionError as exc:
            result = error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                exc.code,
                exc.message,
                field_path=exc.field_path,
                hint=exc.hint,
            )
        except Exception as exc:  # noqa: BLE001 - persist deterministic redacted failure
            result = error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.EXECUTION_FAILED,
                str(redact_secrets(str(exc))),
            )
        result = redact_tool_result(result)
        await self._record_audit(context, request.tool_name, result.invocation_id, result.status)
        return result

    async def _record_audit(
        self,
        context: ToolRuntimeContext,
        tool_name: str,
        invocation_id: str,
        status: str,
    ) -> None:
        if self._audit is None:
            return
        await self._audit.record(
            actor=context.actor,
            action="tool.invocation",
            resource=f"tool:{tool_name}",
            payload={
                "tool_name": tool_name,
                "invocation_id": invocation_id,
                "status": status,
                "run_id": context.run_id,
                "tenant_id": context.actor.tenant_id,
                "user_id": context.actor.user_id,
                "agent_id": context.agent_id,
                "request_id": context.request_id,
                "trace_id": context.trace_id,
            },
        )

    def _is_agent_tool_allowed(self, tool_name: str) -> bool:
        if not self._enforce_agent_tool_allowlist:
            return True
        return tool_name in self._agent_tool_allowlist


def validate_approval_grant(
    grant: ApprovalGrant,
    request: ToolCallRequest,
    context: ToolRuntimeContext,
    tool: BuiltinTool,
) -> None:
    """校验 grant 是否绑定当前 identity、run、tool 和参数。"""

    expected = {
        "tenant_id": context.actor.tenant_id,
        "identity_id": context.actor.user_id,
        "agent_id": request.agent_id,
        "run_id": request.run_id,
        "action": tool.action,
        "resource": tool.resource,
        "arguments_hash": hash_tool_arguments(request.arguments),
    }
    actual = {
        "tenant_id": grant.tenant_id,
        "identity_id": grant.identity_id,
        "agent_id": grant.agent_id,
        "run_id": grant.run_id,
        "action": grant.action,
        "resource": grant.resource,
        "arguments_hash": grant.arguments_hash,
    }
    mismatch = next((field for field, value in expected.items() if actual[field] != value), None)
    if mismatch is not None:
        raise ApprovedToolGrantError(f"approval grant mismatch: {mismatch}")
