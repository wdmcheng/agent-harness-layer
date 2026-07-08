"""ToolRegistry：统一工具发现、策略检查、错误码和输出元数据。"""

from __future__ import annotations

import inspect
from typing import Any, cast
from uuid import uuid4

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.security.redaction import redact_secrets
from agent_harness.tools.output_guard import guarded_tool_payload
from agent_harness.tools.types import (
    BuiltinTool,
    ToolCallRequest,
    ToolCallResult,
    ToolDescriptor,
    ToolError,
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
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._policy = policy
        self._audit = audit
        self._artifact_store = artifact_store
        self._inline_result_bytes = inline_result_bytes
        self._agent_tool_allowlist = set(agent_tool_allowlist or [])
        self._enforce_agent_tool_allowlist = enforce_agent_tool_allowlist

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
        invocation_id = str(uuid4())
        source_ref = _source_ref(request.tool_name, invocation_id, context.run_id)
        tool = self._tools.get(request.tool_name)
        if tool is None:
            result = _error_result(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.NOT_FOUND,
                f"tool not found: {request.tool_name}",
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result
        if not self._is_agent_tool_allowed(request.tool_name):
            result = _error_result(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.POLICY_DENIED,
                f"tool is not allowlisted for agent: {request.tool_name}",
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result

        validation_error = _validate_arguments(tool.input_schema, request.arguments)
        if validation_error is not None:
            result = _error_result(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.SCHEMA_VALIDATION_FAILED,
                validation_error.message,
                field_path=validation_error.field_path,
                hint=validation_error.hint,
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result

        if tool.preflight is not None:
            try:
                preflight_result = _invoke_handler(tool.preflight, request, context)
                if inspect.isawaitable(preflight_result):
                    preflight_result = await preflight_result
            except ToolExecutionError as exc:
                preflight_result = _error_result(
                    request,
                    context,
                    invocation_id,
                    source_ref,
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
            result = _error_result(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.POLICY_DENIED,
                policy.reason,
                policy=policy_payload,
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result
        if policy.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
            result = _error_result(
                request,
                context,
                invocation_id,
                source_ref,
                ToolErrorCode.APPROVAL_REQUIRED,
                policy.reason,
                policy=policy_payload,
            )
            await self._record_audit(context, request.tool_name, invocation_id, result.status)
            return result

        try:
            raw_result = _invoke_handler(tool.handler, request, context)
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
            if isinstance(raw_result, ToolCallResult):
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
            return _error_result(
                request,
                context,
                invocation_id,
                source_ref,
                exc.code,
                exc.message,
                field_path=exc.field_path,
                hint=exc.hint,
                policy=policy_payload,
            )
        except Exception as exc:  # noqa: BLE001 - adapter 异常必须转换为稳定错误码
            await self._record_audit(context, request.tool_name, invocation_id, "failed")
            return _error_result(
                request,
                context,
                invocation_id,
                source_ref,
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
            source_ref=source_ref,
            artifact_ref=artifact_ref,
            truncation=truncation,
            policy=policy_payload,
            request_id=context.request_id or request.request_id,
            trace_id=context.trace_id or request.trace_id,
        )

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


class _ValidationError:
    def __init__(self, message: str, *, field_path: str | None, hint: str | None = None) -> None:
        self.message = message
        self.field_path = field_path
        self.hint = hint


def _validate_arguments(
    schema: dict[str, Any], arguments: dict[str, Any]
) -> _ValidationError | None:
    if schema.get("type", "object") != "object":
        return _ValidationError("tool input schema must be an object", field_path=None)

    required = schema.get("required", [])
    if isinstance(required, list):
        required_fields = cast(list[object], required)
        for field in required_fields:
            if isinstance(field, str) and field not in arguments:
                return _ValidationError(
                    f"missing required argument: {field}",
                    field_path=field,
                    hint="provide all required tool arguments",
                )

    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return None
    property_specs = cast(dict[object, object], properties)
    for field, spec in property_specs.items():
        if field not in arguments or not isinstance(field, str) or not isinstance(spec, dict):
            continue
        typed_spec = cast(dict[str, Any], spec)
        expected_type = typed_spec.get("type")
        if expected_type is None:
            continue
        if not _matches_json_schema_type(arguments[field], expected_type):
            return _ValidationError(
                f"argument {field} must be {expected_type}",
                field_path=field,
                hint="match the tool input schema",
            )
    return None


def _matches_json_schema_type(value: Any, expected_type: object) -> bool:
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    return True


def _error_result(
    request: ToolCallRequest,
    context: ToolRuntimeContext,
    invocation_id: str,
    source_ref: str,
    code: ToolErrorCode,
    message: str,
    *,
    field_path: str | None = None,
    hint: str | None = None,
    policy: dict[str, Any] | None = None,
) -> ToolCallResult:
    return ToolCallResult(
        tool_name=request.tool_name,
        status=tool_status_for_error(code),
        invocation_id=invocation_id,
        error=ToolError(code=code, message=message, field_path=field_path, hint=hint),
        source_ref=source_ref,
        policy=policy or {},
        request_id=context.request_id or request.request_id,
        trace_id=context.trace_id or request.trace_id,
    )


def _source_ref(tool_name: str, invocation_id: str, run_id: str | None) -> str:
    run_part = run_id or "adhoc"
    return f"tool://{tool_name}/{run_part}/{invocation_id}"


def _invoke_handler(handler: Any, request: ToolCallRequest, context: ToolRuntimeContext) -> Any:
    """兼容纯参数 handler 和需要完整 request/context 的内置工具。"""

    signature = inspect.signature(handler)
    if "context" in signature.parameters:
        return handler(request, context=context)
    return handler(request.arguments)
