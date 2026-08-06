"""工具执行边界的公开 DTO。"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, Literal, cast

from pydantic import ConfigDict, Field, PrivateAttr, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.identity import IdentityContext
from agent_harness.models.structured import assert_structured_json_value, structured_digest

ToolHandler = Callable[..., Any]
ToolPreflight = Callable[..., "ToolCallResult | None"]


class ToolErrorCode(StrEnum):
    """工具调用对外暴露的稳定错误码。"""

    NOT_FOUND = "tool.not_found"
    SCHEMA_VALIDATION_FAILED = "tool.schema_validation_failed"
    POLICY_DENIED = "tool.policy_denied"
    APPROVAL_REQUIRED = "tool.approval_required"
    DISABLED = "tool.disabled"
    TIMEOUT = "tool.timeout"
    WORKSPACE_DENIED = "tool.workspace_denied"
    ALLOWLIST_DENIED = "tool.allowlist_denied"
    EXECUTION_FAILED = "tool.execution_failed"


class ToolCallRequest(HarnessDTO):
    """runtime、CLI 和 worker 进入 ToolRegistry 的统一请求形状。"""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    agent_id: str
    run_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None


class ToolRuntimeContext(HarnessDTO):
    """一次工具调用继承的身份、run 和 trace 上下文。"""

    actor: IdentityContext
    agent_id: str
    run_id: str | None = None
    request_id: str | None = None
    trace_id: str | None = None
    _approved_grant_id: str | None = PrivateAttr(default=None)

    def authorize_approved_call(self, approval_id: str) -> ToolRuntimeContext:
        """仅供 ToolRegistry 在 grant 全量校验后标记内部 approved handler。"""

        self._approved_grant_id = approval_id
        return self

    @property
    def approved_grant_id(self) -> str | None:
        """返回 private grant marker；该值不进入 DTO、trace 或 API payload。"""

        return self._approved_grant_id


class ToolError(HarnessDTO):
    """工具调用失败时的结构化错误。"""

    code: ToolErrorCode
    message: str
    field_path: str | None = None
    hint: str | None = None


class ToolCallResult(HarnessDTO):
    """工具调用结果；所有 tool output 默认按非可信输入传播。"""

    tool_name: str
    status: str
    invocation_id: str
    result: dict[str, Any] | None = None
    error: ToolError | None = None
    source_ref: str
    trust_level: str = "untrusted"
    artifact_ref: str | None = None
    truncation: dict[str, Any] = Field(default_factory=lambda: {"truncated": False})
    policy: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
    trace_id: str | None = None


class BuiltinTool(HarnessDTO):
    """内置工具描述；handler 留在进程内，不进入 payload 序列化。"""

    name: str
    action: str
    resource: str
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    input_schema_ref: str | None = None
    input_schema_version: str | None = None
    handler: ToolHandler
    preflight: ToolPreflight | None = None

    @model_validator(mode="after")
    def validate_schema_identity_pair(self) -> BuiltinTool:
        """模型目录元数据必须全有或全无；人工调用可继续使用legacy schema。"""

        if (self.input_schema_ref is None) != (self.input_schema_version is None):
            raise ValueError("tool input schema ref/version must be both set or null")
        return self


class ToolDescriptor(HarnessDTO):
    """对外可序列化的工具描述，不暴露 callable。"""

    name: str
    action: str
    resource: str
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    policy: dict[str, Any] = Field(default_factory=dict)


class ToolIntentResolutionError(RuntimeError):
    """只读解析失败的稳定、去敏错误。"""

    def __init__(
        self,
        code: Literal[
            "tool.not_found",
            "tool.allowlist_denied",
            "tool.schema_validation_failed",
            "model.tool_catalog_conflict",
        ],
    ) -> None:
        self.code = code
        super().__init__(code)


class ResolvedToolIntent(HarnessDTO):
    """Registry只读解析结果；只含数据绑定，不含handler、policy或client。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["resolved-tool-intent-v1"] = "resolved-tool-intent-v1"
    loop_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    turn_ordinal: int = Field(gt=0, strict=True)
    tool_call_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any]
    arguments_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_schema_ref: str = Field(min_length=1)
    tool_schema_version: str = Field(min_length=1)
    tool_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_usage_call_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: str = Field(min_length=1)
    resource: str = Field(min_length=1)

    @field_validator("arguments", mode="before")
    @classmethod
    def validate_arguments(cls, value: object) -> object:
        """解析结果仍只允许普通JSON object，避免后置SDK对象注入。"""

        if not isinstance(value, dict):
            raise ValueError("resolved tool arguments must be a JSON object")
        arguments = cast(dict[object, object], value)
        assert_structured_json_value(arguments)
        return arguments

    @model_validator(mode="after")
    def validate_arguments_identity(self) -> ResolvedToolIntent:
        """参数摘要必须可从解析结果逐值复算。"""

        if structured_digest(self.arguments) != self.arguments_digest:
            raise ValueError("resolved tool arguments digest mismatch")
        return self


class ToolExecutionError(RuntimeError):
    """工具实现抛出的稳定错误；Registry 会转换为 ToolCallResult。"""

    def __init__(
        self,
        code: ToolErrorCode,
        message: str,
        *,
        field_path: str | None = None,
        hint: str | None = None,
    ) -> None:
        """保存 Registry 可序列化的错误字段，避免 handler 直接拼装响应 DTO。"""

        super().__init__(message)
        self.code = code
        self.message = message
        self.field_path = field_path
        self.hint = hint


def tool_status_for_error(code: ToolErrorCode) -> str:
    """按 API-Contract 5.21 把错误码映射为稳定 status。"""

    if code == ToolErrorCode.POLICY_DENIED:
        return "denied"
    if code == ToolErrorCode.APPROVAL_REQUIRED:
        return "requires_approval"
    if code == ToolErrorCode.DISABLED:
        return "disabled"
    if code == ToolErrorCode.TIMEOUT:
        return "timeout"
    if code in {ToolErrorCode.WORKSPACE_DENIED, ToolErrorCode.ALLOWLIST_DENIED}:
        return "denied"
    return "failed"
