"""Provider-neutral structured output 的 schema、canonical 与验证核心。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO


class StructuredSchemaResolutionError(Exception):
    """在 composition 边界保留 schema unknown/conflict 的 provider-neutral 失败身份。"""

    def __init__(
        self,
        code: Literal[
            "model.structured_schema_unknown",
            "model.structured_schema_conflict",
        ],
    ) -> None:
        """只接受冻结的 preflight 错误码，避免 Registry 异常类型进入模型核心。"""

        self.code = code
        super().__init__(code)


_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_VALIDATION_CODE_VOCABULARY = (
    "constraint_violation",
    "extra_field",
    "json_invalid",
    "missing_required",
    "schema_invalid",
    "type_mismatch",
    "validation_issue_overflow",
    "value_not_allowed",
)


def assert_structured_json_value(value: object, *, path: str = "$") -> None:
    """递归拒绝 SDK/Python 专属值，避免 serializer 悄悄字符串化对象。"""

    if value is None or isinstance(value, str | bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    if isinstance(value, list):
        for index, item in enumerate(cast(list[object], value)):
            assert_structured_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise ValueError(f"non-string JSON object key at {path}")
            assert_structured_json_value(item, path=f"{path}.{key}")
        return
    if isinstance(value, Decimal):
        raise ValueError(f"Decimal is not a structured JSON value at {path}")
    raise ValueError(f"unsupported structured JSON value at {path}")


def canonical_structured_json(value: object) -> str:
    """按 ``structured-canonical-json-v1`` 生成唯一 UTF-8 JSON 文本。"""

    assert_structured_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def structured_digest(value: object) -> str:
    """对 canonical structured JSON 计算小写 SHA-256。"""

    return hashlib.sha256(canonical_structured_json(value).encode("utf-8")).hexdigest()


def structured_operation_identity_digest(
    *,
    tenant_id: str,
    run_id: str,
    agent_id: str,
    request_id: str | None,
    trace_id: str | None,
    operation_key: str,
) -> str:
    """计算 exact operation identity；调用方不得持久化瞬时 operation key。"""

    return structured_digest(
        {
            "schema_version": "structured-output-operation-v1",
            "tenant_id": tenant_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "operation_key": operation_key,
        }
    )


class OutputSchemaIdentity(HarnessDTO):
    """版本化业务输出 schema 的公开稳定身份。"""

    schema_version: Literal["output-schema-identity-v1"] = "output-schema-identity-v1"
    schema_ref: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(pattern=_SHA256_PATTERN)


class OutputSchemaDefinition(HarnessDTO):
    """Registry 持有的 provider-neutral 严格 schema 定义。"""

    identity: OutputSchemaIdentity
    schema_definition: dict[str, Any] = Field(alias="schema", serialization_alias="schema")
    canonical_schema: str

    @model_validator(mode="after")
    def validate_canonical_identity(self) -> OutputSchemaDefinition:
        """防止 schema、canonical bytes 与 identity digest 三者漂移。"""

        canonical = canonical_structured_json(self.schema_definition)
        if canonical != self.canonical_schema:
            raise ValueError("canonical schema does not match schema definition")
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if digest != self.identity.digest:
            raise ValueError("schema identity digest does not match canonical schema")
        return self


class StructuredOutputRequest(HarnessDTO):
    """核心 structured 调用消费的 schema identity 与有限 repair 策略。"""

    schema_identity: OutputSchemaIdentity = Field(
        alias="schema",
        serialization_alias="schema",
    )
    repair_limit: int = Field(ge=0, le=2, strict=True)

    def to_payload(self) -> dict[str, Any]:
        """按公共语义名输出 schema identity，禁止 definition 进入调用身份。"""

        return self.model_dump(mode="json", by_alias=True)


class StructuredOutputNotStartedProof(HarnessDTO):
    """核心在 send 前构造、可逐字节复算的零请求证明。"""

    schema_version: Literal["structured-output-not-started-proof-v1"] = (
        "structured-output-not-started-proof-v1"
    )
    kind: Literal["client_prepare_not_started", "cancelled_before_send"]
    usage_call_id: str = Field(pattern=_SHA256_PATTERN)
    operation_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    route_digest: str = Field(pattern=_SHA256_PATTERN)
    schema_identity: OutputSchemaIdentity
    prompt_digest: str = Field(pattern=_SHA256_PATTERN)
    attempt: int = Field(ge=1, strict=True)
    repair_ordinal: int = Field(ge=0, le=2, strict=True)
    transport_ordinal: int = Field(ge=1, strict=True)
    digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_digest(self) -> StructuredOutputNotStartedProof:
        """拒绝形状正确但 identity 或 ordinal 已被篡改的 proof。"""

        payload = self.model_dump(mode="json")
        payload.pop("digest")
        if structured_digest(payload) != self.digest:
            raise ValueError("structured not-started proof digest mismatch")
        return self


def structured_not_started_proof(
    *,
    kind: Literal["client_prepare_not_started", "cancelled_before_send"],
    usage_call_id: str,
    operation_identity_digest: str,
    route_digest: str,
    schema_identity: OutputSchemaIdentity,
    prompt_digest: str,
    attempt: int,
    repair_ordinal: int,
    transport_ordinal: int,
) -> StructuredOutputNotStartedProof:
    """仅供 invocation 核心从自身 send 前控制流事实生成 proof。"""

    payload: dict[str, object] = {
        "schema_version": "structured-output-not-started-proof-v1",
        "kind": kind,
        "usage_call_id": usage_call_id,
        "operation_identity_digest": operation_identity_digest,
        "route_digest": route_digest,
        "schema_identity": schema_identity.model_dump(mode="json"),
        "prompt_digest": prompt_digest,
        "attempt": attempt,
        "repair_ordinal": repair_ordinal,
        "transport_ordinal": transport_ordinal,
    }
    payload["digest"] = structured_digest(payload)
    return StructuredOutputNotStartedProof.model_validate(payload)


class StructuredOutputAttemptEvidence(HarnessDTO):
    """Structured controller attempt 的 exact 嵌套判别详情。"""

    schema_version: Literal["structured-output-attempt-v1"] = "structured-output-attempt-v1"
    schema_identity: OutputSchemaIdentity
    phase: Literal["initial", "repair"]
    repair_ordinal: int = Field(ge=0, le=2, strict=True)
    transport_ordinal: int = Field(ge=1, strict=True)
    prompt_digest: str = Field(pattern=_SHA256_PATTERN)
    repair_trigger_codes: tuple[str, ...]
    validation_codes: tuple[str, ...] | None
    not_started_proof: StructuredOutputNotStartedProof | None
    cleanup_status: Literal["not_applicable", "completed", "failed", "unknown"]

    @model_validator(mode="after")
    def validate_attempt_detail(self) -> StructuredOutputAttemptEvidence:
        """锁定 phase、trigger、validation code 与 proof/cleanup 的局部联合体。"""

        if (self.phase == "initial") != (self.repair_ordinal == 0):
            raise ValueError("structured attempt phase/repair ordinal mismatch")
        trigger_codes = tuple(sorted(set(self.repair_trigger_codes)))
        if self.repair_trigger_codes != trigger_codes or any(
            code not in _VALIDATION_CODE_VOCABULARY for code in trigger_codes
        ):
            raise ValueError("structured repair trigger codes are unsupported or unsorted")
        if self.phase == "initial" and trigger_codes:
            raise ValueError("initial structured attempt cannot have repair trigger codes")
        if self.phase == "repair" and not trigger_codes:
            raise ValueError("repair structured attempt requires trigger codes")
        if self.validation_codes is not None:
            validation_codes = tuple(sorted(set(self.validation_codes)))
            if self.validation_codes != validation_codes or any(
                code not in _VALIDATION_CODE_VOCABULARY for code in validation_codes
            ):
                raise ValueError("structured validation codes are unsupported or unsorted")
        if self.not_started_proof is not None:
            proof = self.not_started_proof
            if (
                (
                    proof.kind == "client_prepare_not_started"
                    and self.cleanup_status != "not_applicable"
                )
                or proof.schema_identity != self.schema_identity
                or proof.prompt_digest != self.prompt_digest
                or proof.repair_ordinal != self.repair_ordinal
                or proof.transport_ordinal != self.transport_ordinal
            ):
                raise ValueError("structured not-started proof/detail mismatch")
        elif self.cleanup_status == "not_applicable":
            raise ValueError("not-applicable cleanup requires a not-started proof")
        return self


def _empty_validation_issues() -> list[dict[str, str]]:
    """为每个验证结果创建独立的去敏问题集合。"""

    return []


class StructuredValidationResult(HarnessDTO):
    """核心 validator 的去敏结果；永不保存原始无效候选。"""

    status: Literal["valid", "invalid", "extra_fields"]
    value: dict[str, Any] | None = None
    issues: list[dict[str, str]] = Field(default_factory=_empty_validation_issues)

    @model_validator(mode="after")
    def validate_union(self) -> StructuredValidationResult:
        """只有 valid 可以携带 canonical value，失败只保留稳定 issues。"""

        if (self.status == "valid") != (self.value is not None):
            raise ValueError("structured validation value/status mismatch")
        if self.status == "valid" and self.issues:
            raise ValueError("valid structured value cannot contain validation issues")
        if self.status != "valid" and not self.issues:
            raise ValueError("invalid structured value requires validation issues")
        return self


class StructuredOutputReplayIdentity(HarnessDTO):
    """结构化结果的完整耐久 replay 预映像。"""

    schema_version: Literal["structured-output-replay-v1"] = "structured-output-replay-v1"
    tenant_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    request_id: str | None
    trace_id: str | None
    usage_call_id: str = Field(pattern=_SHA256_PATTERN)
    operation_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    prompt_digest: str = Field(pattern=_SHA256_PATTERN)
    deployment_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    route_digest: str = Field(pattern=_SHA256_PATTERN)
    schema_identity: OutputSchemaIdentity
    transport_attempt_limit: int = Field(ge=1, strict=True)
    repair_limit: int = Field(ge=0, le=2, strict=True)
    repair_count: int | None = Field(ge=0, le=2, strict=True)
    provider_request_count: int | None = Field(ge=0, strict=True)
    final_status: Literal[
        "valid", "invalid", "extra_fields", "repair_exhausted", "failed", "needs_review"
    ]
    value_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("request_id", "trace_id")
    @classmethod
    def validate_nullable_identity(cls, value: str | None) -> str | None:
        """Nullable replay身份只接受非空字符串，禁止空串形成第二种null语义。"""

        if value == "":
            raise ValueError("structured replay identity must be non-empty or null")
        return value

    @model_validator(mode="after")
    def validate_terminal_union(self) -> StructuredOutputReplayIdentity:
        """逐值锁定已知计数、联合上限与终态 nullable 规则。"""

        limit = self.transport_attempt_limit * (1 + self.repair_limit)
        if self.repair_count is not None and self.repair_count > self.repair_limit:
            raise ValueError("repair count exceeds repair limit")
        if self.provider_request_count is not None and self.provider_request_count > limit:
            raise ValueError("provider request count exceeds joint attempt limit")
        if self.final_status != "needs_review" and (
            self.repair_count is None or self.provider_request_count is None
        ):
            raise ValueError("determinate structured terminal requires exact counts")
        if self.final_status == "valid":
            if self.value_digest is None or not self.provider_request_count:
                raise ValueError("valid structured replay requires value and provider request")
        elif self.value_digest is not None:
            raise ValueError("non-valid structured replay cannot contain value digest")
        if self.final_status in {"invalid", "extra_fields", "repair_exhausted"} and not (
            self.provider_request_count and self.provider_request_count > 0
        ):
            raise ValueError("schema failure requires a provider request")
        return self

    @property
    def digest(self) -> str:
        """返回完整 exact 预映像的 canonical SHA-256。"""

        return structured_digest(self.model_dump(mode="json"))


class StructuredOutputResult(HarnessDTO):
    """成功返回给业务和耐久 response 的 provider-neutral DTO。"""

    schema_version: Literal["structured-output-result-v1"] = "structured-output-result-v1"
    schema_identity: OutputSchemaIdentity
    status: Literal["valid"] = "valid"
    value: dict[str, Any]
    repair_count: int = Field(ge=0, le=2, strict=True)
    provider_request_count: int = Field(ge=1, strict=True)
    replay_identity: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("value", mode="before")
    @classmethod
    def validate_value(cls, value: object) -> object:
        """成功值也必须保持普通 JSON object，不能携带 SDK/Pydantic 实例。"""

        if not isinstance(value, dict):
            raise ValueError("structured result value must be a JSON object")
        mapping = cast(dict[object, object], value)
        assert_structured_json_value(mapping)
        return mapping


def structured_provider_prompt(
    *,
    business_prompt: str,
    schema: OutputSchemaDefinition,
    repair_ordinal: int,
    validation_codes: Sequence[str] = (),
) -> str:
    """构造 exact ``structured-provider-prompt-v1``，不回传 invalid 原文或 path。"""

    if isinstance(repair_ordinal, bool) or not 0 <= repair_ordinal <= 2:
        raise ValueError("repair ordinal must be an integer between zero and two")
    codes = sorted(set(validation_codes))
    if any(code not in _VALIDATION_CODE_VOCABULARY for code in codes):
        raise ValueError("validation code is outside the frozen vocabulary")
    if repair_ordinal == 0 and codes:
        raise ValueError("initial structured prompt cannot contain validation codes")
    if repair_ordinal > 0 and not codes:
        raise ValueError("repair structured prompt requires validation codes")
    return canonical_structured_json(
        {
            "schema_version": "structured-provider-prompt-v1",
            "phase": "initial" if repair_ordinal == 0 else "repair",
            "repair_ordinal": repair_ordinal,
            "business_prompt": business_prompt,
            "schema_identity": schema.identity.model_dump(mode="json"),
            "schema": schema.schema_definition,
            "validation_codes": codes,
        }
    )


def maximum_structured_validation_codes() -> tuple[str, ...]:
    """供 planning 构造最坏 repair prompt 的冻结 code 词汇表。"""

    return _VALIDATION_CODE_VOCABULARY


# 保留既有公共导入面；实现位于窄schema模块，延迟到DTO定义完成后加载以避免循环。
from agent_harness.models.structured_schema import (  # noqa: E402
    compile_output_schema as compile_output_schema,
)
from agent_harness.models.structured_schema import (  # noqa: E402
    compile_output_schema_definition as compile_output_schema_definition,
)
from agent_harness.models.structured_schema import (  # noqa: E402
    validate_structured_candidate as validate_structured_candidate,
)
