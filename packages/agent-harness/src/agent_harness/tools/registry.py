"""ToolRegistry：统一工具发现、策略检查、错误码和输出元数据。"""

from __future__ import annotations

import inspect
from typing import cast
from uuid import uuid4

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.runtime.executor import ApprovalGrant
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.tools.approved_execution import ApprovedToolExecutor
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
    ToolDescriptor,
    ToolErrorCode,
    ToolExecutionError,
    ToolRuntimeContext,
    tool_status_for_error,
)


class ToolRegistry:
    """工具执行的进程内注册表，不让调用方直接碰具体 adapter。"""

    def __init__(
        self,
        *,
        tools: list[BuiltinTool],
        policy: PolicyEngine,
        audit: AuditService | None,
        artifact_store: FileArtifactStore,
        inline_result_bytes: int = 8192,
        agent_tool_allowlist: list[str] | None = None,
        enforce_agent_tool_allowlist: bool = False,
        storage: SQLAlchemyStorage | None = None,
    ) -> None:
        """注册工具及其安全协作者，保留可选持久化 seam 给审批后执行路径。

        allowlist 只有在显式启用时才限制工具发现和调用，避免历史 local profile 因
        空配置失去工具；真正的授权仍由 policy engine 在每次调用时判定。
        """

        self._tools = {tool.name: tool for tool in tools}
        self._policy = policy
        self._audit = audit
        self._artifact_store = artifact_store
        self._inline_result_bytes = inline_result_bytes
        self._agent_tool_allowlist = set(agent_tool_allowlist or [])
        self._enforce_agent_tool_allowlist = enforce_agent_tool_allowlist
        self._storage = storage

    def list_tools(self) -> list[ToolDescriptor]:
        """返回按名称排序的工具描述，供 CLI 和 runtime allowlist 使用。"""

        return [
            ToolDescriptor(
                name=tool.name,
                action=tool.action,
                resource=tool.resource,
                input_schema=tool.input_schema,
            )
            for tool in (self._tools[name] for name in sorted(self._tools))
            if self._is_agent_tool_allowed(tool.name)
        ]

    async def call(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
    ) -> ToolCallResult:
        """执行一次未批准工具调用，并统一处理校验、策略、脱敏、审计与大结果外置。

        各个拒绝分支也写审计记录，保证调用尝试可追溯。工具 handler 的预检、策略
        与异常映射均发生在实际副作用前；已是 ``ToolCallResult`` 的适配器结果仍会
        经过脱敏，防止 provider 绕过公共输出边界。
        """

        invocation_id = str(uuid4())
        result_source_ref = source_ref(request.tool_name, invocation_id, context.run_id)
        tool = self._tools.get(request.tool_name)
        if tool is None:
            result = error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.NOT_FOUND,
                f"tool not found: {request.tool_name}",
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result
        if not self._is_agent_tool_allowed(request.tool_name):
            result = error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.POLICY_DENIED,
                f"tool is not allowlisted for agent: {request.tool_name}",
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result

        validation_error = validate_arguments(tool.input_schema, request.arguments)
        if validation_error is not None:
            result = error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.SCHEMA_VALIDATION_FAILED,
                validation_error.message,
                field_path=validation_error.field_path,
                hint=validation_error.hint,
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result

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

        policy = await self._policy.evaluate(
            PolicyCheck(
                actor=context.actor,
                action=tool.action,
                resource=tool.resource,
                context={
                    "tool_name": request.tool_name,
                    "agent_id": request.agent_id,
                    "run_id": context.run_id,
                    "tenant_id": context.actor.tenant_id,
                    "user_id": context.actor.user_id,
                    "request_id": context.request_id or request.request_id,
                    "trace_id": context.trace_id or request.trace_id,
                },
            )
        )
        policy_payload = policy.to_payload()
        if policy.decision == GuardrailDecisionStatus.DENY.value:
            result = error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.POLICY_DENIED,
                policy.reason,
                policy=policy_payload,
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result
        if policy.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
            result = error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.APPROVAL_REQUIRED,
                policy.reason,
                policy=policy_payload,
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result

        try:
            raw_result = invoke_handler(tool.handler, request, context)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
            if isinstance(raw_result, ToolCallResult):
                raw_result = redact_tool_result(raw_result)
                result_status = raw_result.status
                await self._record_audit(
                    context,
                    request.tool_name,
                    raw_result.invocation_id,
                    result_status,
                )
                return raw_result.model_copy(update={"policy": raw_result.policy or policy_payload})
        except ToolExecutionError as exc:
            await self._record_audit(
                context,
                request.tool_name,
                invocation_id,
                tool_status_for_error(exc.code),
            )
            return error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                exc.code,
                exc.message,
                field_path=exc.field_path,
                hint=exc.hint,
                policy=policy_payload,
            )
        except Exception as exc:  # noqa: BLE001 - adapter 异常必须转换为稳定错误码
            await self._record_audit(context, request.tool_name, invocation_id, "failed")
            return error_result(
                request,
                context,
                invocation_id,
                result_source_ref,
                ToolErrorCode.EXECUTION_FAILED,
                str(redact_secrets(str(exc))),
                policy=policy_payload,
            )

        result, artifact_ref, truncation = guarded_tool_payload(
            tool_name=request.tool_name,
            invocation_id=invocation_id,
            payload=raw_result,
            artifact_store=self._artifact_store,
            inline_bytes=self._inline_result_bytes,
        )
        await self._record_audit(context, request.tool_name, invocation_id, "completed")
        return ToolCallResult(
            tool_name=request.tool_name,
            status="completed",
            invocation_id=invocation_id,
            result=result,
            source_ref=result_source_ref,
            artifact_ref=artifact_ref,
            truncation=truncation,
            policy=policy_payload,
            request_id=context.request_id or request.request_id,
            trace_id=context.trace_id or request.trace_id,
        )

    async def call_approved(
        self,
        request: ToolCallRequest,
        *,
        context: ToolRuntimeContext,
        grant: ApprovalGrant,
    ) -> ToolCallResult:
        """在持久化 at-most-once claim 后执行一次 approved action。"""

        executor = ApprovedToolExecutor(
            tools=self._tools,
            storage=self._storage,
            artifact_store=self._artifact_store,
            audit=self._audit,
            inline_result_bytes=self._inline_result_bytes,
            agent_tool_allowlist=self._agent_tool_allowlist,
            enforce_agent_tool_allowlist=self._enforce_agent_tool_allowlist,
        )
        return await executor.execute(
            request,
            context=context,
            grant=grant,
        )

    async def _record_audit(
        self,
        context: ToolRuntimeContext,
        tool_name: str,
        invocation_id: str,
        status: str,
    ) -> None:
        """在审计服务存在时记录最小化调用元数据；审计关闭不阻塞工具主流程。"""

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
        """按显式开关应用 agent allowlist，关闭时维持兼容的全量工具可见性。"""

        if not self._enforce_agent_tool_allowlist:
            return True
        return tool_name in self._agent_tool_allowlist
