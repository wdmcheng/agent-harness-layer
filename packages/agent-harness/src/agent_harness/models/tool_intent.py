"""Provider-neutral 工具意图与模型单轮结果 DTO。"""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.providers import ModelAttemptEvidence, ModelResponse
from agent_harness.models.structured import assert_structured_json_value, structured_digest
from agent_harness.models.tool_catalog import (
    ToolCatalog as ToolCatalog,
)
from agent_harness.models.tool_catalog import (
    ToolCatalogConflictError as ToolCatalogConflictError,
)
from agent_harness.models.tool_catalog import ToolCatalogEntry as ToolCatalogEntry
from agent_harness.models.tool_catalog import ToolCatalogSelection as ToolCatalogSelection
from agent_harness.models.tool_catalog import (
    ToolCatalogSourceDescriptor as ToolCatalogSourceDescriptor,
)
from agent_harness.models.tool_catalog import ToolIntentRequestIdentity as ToolIntentRequestIdentity
from agent_harness.models.tool_catalog import build_tool_catalog as build_tool_catalog
from agent_harness.models.tool_catalog import (
    provider_tool_catalog_bytes as provider_tool_catalog_bytes,
)
from agent_harness.models.tool_catalog import (
    provider_tool_catalog_digest as provider_tool_catalog_digest,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ToolIntentValidationError(RuntimeError):
    """Candidate 无法绑定冻结上下文时使用的稳定关闭失败。"""

    code = "model.tool_intent_invalid"

    def __init__(self) -> None:
        """不携带 arguments、provider 原文或 SDK 异常，避免错误面泄密。"""

        super().__init__(self.code)
        self.attempts: list[ModelAttemptEvidence] = []


class ProviderToolIntentCandidate(HarnessDTO):
    """Adapter 单次请求返回的未验证工具意图与唯一 usage 事实。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["provider-tool-intent-candidate-v1"] = (
        "provider-tool-intent-candidate-v1"
    )
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any]
    tool_schema_ref: str = Field(min_length=1)
    tool_schema_version: str = Field(min_length=1)
    tool_schema_digest: str = Field(pattern=_SHA256_PATTERN)
    attempts: list[ModelAttemptEvidence]

    @field_validator("arguments", mode="before")
    @classmethod
    def validate_arguments(cls, value: object) -> object:
        """只接受普通 JSON object，不允许 adapter 字符串化 SDK 对象。"""

        if not isinstance(value, dict):
            raise ValueError("tool intent candidate arguments must be a JSON object")
        arguments = cast(dict[object, object], value)
        assert_structured_json_value(arguments)
        return arguments

    @model_validator(mode="after")
    def validate_single_attempt(self) -> ProviderToolIntentCandidate:
        """合法 proposal 只能对应一个已完成且已计量的 provider send。"""

        if len(self.attempts) != 1 or self.attempts[0].attempt != 1:
            raise ValueError("tool intent candidate requires exactly one local attempt")
        attempt = self.attempts[0]
        if (
            attempt.side_effect_state != "started"
            or attempt.outcome != "completed"
            or attempt.completion_observed is not True
            or attempt.error_code is not None
        ):
            raise ValueError("tool intent candidate requires one completed send attempt")
        return self

    @staticmethod
    def validated_snapshot(value: object) -> ProviderToolIntentCandidate | None:
        """深拷贝并重验 exact candidate，拒绝 duck type、子类和后置篡改。"""

        if type(value) is not ProviderToolIntentCandidate:
            return None
        try:
            payload = ProviderToolIntentCandidate.model_dump(value, mode="python")
            snapshot = ProviderToolIntentCandidate.model_validate(payload)
        except (AttributeError, TypeError, ValueError):
            return None
        return snapshot.model_copy(deep=True)


class ToolIntent(HarnessDTO):
    """核心验证后冻结的工具调用意图；不携带任何可执行对象。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["tool-intent-v1"] = "tool-intent-v1"
    loop_id: str = Field(pattern=_SHA256_PATTERN)
    turn_ordinal: int = Field(gt=0, strict=True)
    tool_call_id: str = Field(pattern=_SHA256_PATTERN)
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any]
    arguments_digest: str = Field(pattern=_SHA256_PATTERN)
    tool_schema_ref: str = Field(min_length=1)
    tool_schema_version: str = Field(min_length=1)
    tool_schema_digest: str = Field(pattern=_SHA256_PATTERN)
    model_usage_call_id: str = Field(pattern=_SHA256_PATTERN)
    catalog_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("arguments", mode="before")
    @classmethod
    def validate_arguments(cls, value: object) -> object:
        """只允许普通 JSON object，阻止 SDK、callable 与非有限数越界。"""

        if not isinstance(value, dict):
            raise ValueError("tool intent arguments must be a JSON object")
        arguments = cast(dict[object, object], value)
        assert_structured_json_value(arguments)
        return arguments

    @model_validator(mode="after")
    def validate_arguments_identity(self) -> ToolIntent:
        """拒绝 arguments 与摘要分离，避免下游只校验其中一份。"""

        if structured_digest(self.arguments) != self.arguments_digest:
            raise ValueError("tool intent arguments digest does not match arguments")
        return self


class ToolIntentReplaySeed(HarnessDTO):
    """私有settlement快照；恢复不再读取reload后的Registry或model catalog。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["tool-intent-replay-seed-v1"] = "tool-intent-replay-seed-v1"
    usage_call_id: str = Field(pattern=_SHA256_PATTERN)
    loop_id: str = Field(pattern=_SHA256_PATTERN)
    turn_ordinal: int = Field(gt=0, strict=True)
    bound_operation_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    operation_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    tool_catalog: ToolCatalog
    request_identity: ToolIntentRequestIdentity
    provider_tool_catalog_json: str

    @model_validator(mode="after")
    def validate_frozen_catalog(self) -> ToolIntentReplaySeed:
        """完整schema bytes、授权摘要与route request identity必须逐值一致。"""

        expected = provider_tool_catalog_bytes(self.tool_catalog).decode("utf-8")
        if (
            self.provider_tool_catalog_json != expected
            or self.request_identity.tool_catalog_digest != self.tool_catalog.catalog_digest
            or self.request_identity.tool_catalog_utf8_bytes != len(expected.encode("utf-8"))
            or self.operation_identity_digest
            != tool_intent_operation_identity_digest(
                usage_call_id=self.usage_call_id,
                bound_operation_identity_digest=self.bound_operation_identity_digest,
                loop_id=self.loop_id,
                turn_ordinal=self.turn_ordinal,
                tool_request_identity_digest=self.request_identity.digest,
            )
        ):
            raise ValueError("tool intent replay seed catalog identity mismatch")
        return self

    def to_payload(self) -> dict[str, Any]:
        """显式保留被公共ToolCatalog投影排除的严格input schema body。"""

        return {
            "schema_version": self.schema_version,
            "usage_call_id": self.usage_call_id,
            "loop_id": self.loop_id,
            "turn_ordinal": self.turn_ordinal,
            "bound_operation_identity_digest": self.bound_operation_identity_digest,
            "operation_identity_digest": self.operation_identity_digest,
            "tool_catalog": {
                "schema_version": self.tool_catalog.schema_version,
                "catalog_digest": self.tool_catalog.catalog_digest,
                "tools": [
                    {
                        **item.model_dump(mode="json"),
                        "input_schema": item.input_schema.model_dump(mode="json"),
                    }
                    for item in self.tool_catalog.tools
                ],
            },
            "request_identity": self.request_identity.to_payload(),
            "provider_tool_catalog_json": self.provider_tool_catalog_json,
        }


def normalize_provider_tool_intent(
    candidate: object,
    *,
    expected_provider: str,
    expected_model: str,
    expected_tool_name: str,
    expected_tool_schema_ref: str,
    expected_tool_schema_version: str,
    expected_tool_schema_digest: str,
    loop_id: str,
    turn_ordinal: int,
    model_usage_call_id: str,
    catalog_digest: str,
) -> ToolIntent:
    """把未验证 candidate 绑定到核心冻结的 loop、turn 与 catalog 身份。

    调用方必须来自 bound runtime；adapter 只能贡献 proposal 内容与单次 usage
    事实。Provider/model 只用于校验，不进入 tool call identity，因此不同
    provider 对同一受信轮次的等价 proposal 会产生相同核心身份。
    """

    snapshot = ProviderToolIntentCandidate.validated_snapshot(candidate)
    if snapshot is None or (
        snapshot.provider != expected_provider
        or snapshot.model != expected_model
        or snapshot.tool_name != expected_tool_name
        or snapshot.tool_schema_ref != expected_tool_schema_ref
        or snapshot.tool_schema_version != expected_tool_schema_version
        or snapshot.tool_schema_digest != expected_tool_schema_digest
    ):
        raise ToolIntentValidationError

    arguments_digest = structured_digest(snapshot.arguments)
    tool_call_id = structured_digest(
        {
            "schema_version": "tool-call-identity-v1",
            "loop_id": loop_id,
            "turn_ordinal": turn_ordinal,
            "tool_name": snapshot.tool_name,
            "arguments_digest": arguments_digest,
            "tool_schema_ref": snapshot.tool_schema_ref,
            "tool_schema_version": snapshot.tool_schema_version,
            "tool_schema_digest": snapshot.tool_schema_digest,
            "model_usage_call_id": model_usage_call_id,
            "catalog_digest": catalog_digest,
        }
    )
    try:
        return ToolIntent(
            loop_id=loop_id,
            turn_ordinal=turn_ordinal,
            tool_call_id=tool_call_id,
            tool_name=snapshot.tool_name,
            arguments=snapshot.arguments,
            arguments_digest=arguments_digest,
            tool_schema_ref=snapshot.tool_schema_ref,
            tool_schema_version=snapshot.tool_schema_version,
            tool_schema_digest=snapshot.tool_schema_digest,
            model_usage_call_id=model_usage_call_id,
            catalog_digest=catalog_digest,
        )
    except (TypeError, ValueError) as exc:
        raise ToolIntentValidationError from exc


def tool_loop_identity_digest(
    *,
    tenant_id: str,
    run_id: str,
    agent_id: str,
    request_id: str | None,
    trace_id: str,
    operation_key: str,
) -> str:
    """由 bound runtime 字段派生首轮 loop identity，业务请求不能自报。"""

    return structured_digest(
        {
            "schema_version": "tool-loop-identity-v1",
            "tenant_id": tenant_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "operation_key": operation_key,
        }
    )


def tool_intent_operation_identity_digest(
    *,
    usage_call_id: str,
    bound_operation_identity_digest: str,
    loop_id: str,
    turn_ordinal: int,
    tool_request_identity_digest: str,
) -> str:
    """把bound operation与usage、loop、turn、route/catalog identity收敛为单一摘要。"""

    return structured_digest(
        {
            "schema_version": "tool-intent-operation-identity-v1",
            "usage_call_id": usage_call_id,
            "bound_operation_identity_digest": bound_operation_identity_digest,
            "loop_id": loop_id,
            "turn_ordinal": turn_ordinal,
            "tool_request_identity_digest": tool_request_identity_digest,
        }
    )


class FinalTextTurnResult(HarnessDTO):
    """普通文本协议的唯一成功分支，逐值复用既有响应。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    kind: Literal["final_text"] = "final_text"
    response: ModelResponse

    @model_validator(mode="after")
    def reject_structured_payload(self) -> FinalTextTurnResult:
        """带 structured result 的响应不得冒充普通文本终态。"""

        if self.response.structured_output is not None:
            raise ValueError("final text turn cannot contain structured output")
        return self


class FinalStructuredTurnResult(HarnessDTO):
    """结构化协议的唯一成功分支，保留 MOD-005 完整响应。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    kind: Literal["final_structured"] = "final_structured"
    response: ModelResponse

    @model_validator(mode="after")
    def require_structured_payload(self) -> FinalStructuredTurnResult:
        """结构化终态必须包含已经由 MOD-005 核心验证的成功结果。"""

        if self.response.structured_output is None:
            raise ValueError("final structured turn requires structured output")
        return self


class ToolIntentTurnResult(HarnessDTO):
    """只向后续受控循环交付意图，不在模型边界执行工具。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    kind: Literal["tool_intent"] = "tool_intent"
    intent: ToolIntent


type ModelTurnResult = Annotated[
    FinalTextTurnResult | FinalStructuredTurnResult | ToolIntentTurnResult,
    Field(discriminator="kind"),
]


__all__ = [
    "FinalStructuredTurnResult",
    "FinalTextTurnResult",
    "ModelTurnResult",
    "ProviderToolIntentCandidate",
    "ToolCatalog",
    "ToolCatalogConflictError",
    "ToolCatalogEntry",
    "ToolCatalogSelection",
    "ToolCatalogSourceDescriptor",
    "ToolIntent",
    "ToolIntentReplaySeed",
    "ToolIntentValidationError",
    "ToolIntentTurnResult",
    "normalize_provider_tool_intent",
    "build_tool_catalog",
    "provider_tool_catalog_bytes",
    "provider_tool_catalog_digest",
    "tool_loop_identity_digest",
    "tool_intent_operation_identity_digest",
]
