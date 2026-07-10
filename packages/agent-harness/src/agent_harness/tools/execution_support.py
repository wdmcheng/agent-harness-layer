"""工具 handler 执行、输入校验和稳定错误结果的共享支撑。"""

from __future__ import annotations

import inspect
from typing import Any, cast

from agent_harness.security.redaction import redact_secrets
from agent_harness.tools.types import (
    ToolCallRequest,
    ToolCallResult,
    ToolError,
    ToolErrorCode,
    ToolRuntimeContext,
    tool_status_for_error,
)


class ArgumentValidationError:
    """JSON Schema 子集校验失败的进程内结果。"""

    def __init__(self, message: str, *, field_path: str | None, hint: str | None = None) -> None:
        self.message = message
        self.field_path = field_path
        self.hint = hint


def validate_arguments(
    schema: dict[str, Any], arguments: dict[str, Any]
) -> ArgumentValidationError | None:
    """校验内置工具当前承诺支持的 JSON Schema 子集。"""

    if schema.get("type", "object") != "object":
        return ArgumentValidationError("tool input schema must be an object", field_path=None)

    required = schema.get("required", [])
    if isinstance(required, list):
        required_fields = cast(list[object], required)
        for field in required_fields:
            if isinstance(field, str) and field not in arguments:
                return ArgumentValidationError(
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
        if not matches_json_schema_type(arguments[field], expected_type):
            return ArgumentValidationError(
                f"argument {field} must be {expected_type}",
                field_path=field,
                hint="match the tool input schema",
            )
    return None


def matches_json_schema_type(value: Any, expected_type: object) -> bool:
    """判断运行时值是否匹配工具输入 schema 的基础类型。"""

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


def error_result(
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
    """把工具边界错误转换为稳定、可关联的结果 DTO。"""

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


def source_ref(tool_name: str, invocation_id: str, run_id: str | None) -> str:
    """生成一次工具调用的稳定来源引用。"""

    run_part = run_id or "adhoc"
    return f"tool://{tool_name}/{run_part}/{invocation_id}"


def invoke_handler(handler: Any, request: ToolCallRequest, context: ToolRuntimeContext) -> Any:
    """兼容纯参数 handler 和需要完整 request/context 的内置工具。"""

    signature = inspect.signature(handler)
    if "context" in signature.parameters:
        return handler(request, context=context)
    return handler(request.arguments)


def redact_tool_result(result: ToolCallResult) -> ToolCallResult:
    """在返回、持久化和 trace 前统一清理 handler 结果中的 secret。"""

    return ToolCallResult.model_validate(redact_secrets(result.to_payload()))
