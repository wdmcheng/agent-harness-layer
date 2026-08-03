"""结构化调用的冻结route、attempt与summary evidence DTO。"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, cast
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models._settlement_evidence_models import (
    BulkheadSettlementEvidence,
    RetrySettlementEvidence,
)
from agent_harness.models.providers import StructuredModelAttemptEvidence
from agent_harness.models.structured import StructuredOutputAttemptEvidence
from agent_harness.models.usage import StructuredUsageSummary, StructuredUsageValidationIssue


class StructuredSettlementRouteEvidence(HarnessDTO):
    """单route结构化调用的provider-neutral冻结reservation证据。"""

    snapshot_schema_version: Literal["budget-tree-v1", "budget-tree-v2"]
    deployment_id: str
    provider_kind: str
    provider: str
    model: str
    capability: Literal["structured_output"]
    endpoint_origin: str | None
    endpoint_policy_ref: str | None
    endpoint_policy_version: str | None
    endpoint_policy_digest: str | None
    completion_classifier_ref: str | None
    completion_classifier_version: str | None
    credential_ref: str | None
    model_catalog_ref: str | None
    model_catalog_version: str | None
    model_catalog_digest: str | None
    request_shape_ref: str | None
    request_shape_version: str | None
    input_bound_strategy_ref: str | None
    input_bound_strategy_version: str | None
    input_envelope_token_bound: int = Field(ge=0, strict=True)
    input_token_price_usd: Decimal | None
    output_token_price_usd: Decimal | None
    price_source_ref: str | None
    price_source_version: str | None
    prompt_utf8_bytes: int = Field(ge=0, strict=True)
    trusted_input_token_bound: int = Field(ge=0, strict=True)
    output_token_cap: int = Field(ge=1, strict=True)
    per_attempt_token_bound: int = Field(ge=1, strict=True)
    per_attempt_cost_bound: Decimal | None
    max_attempts: int = Field(ge=1, strict=True)
    reserved_token_bound: int = Field(ge=1, strict=True)
    reserved_cost_bound: Decimal | None
    connect_timeout_ms: int = Field(ge=1, strict=True)
    read_timeout_ms: int = Field(ge=1, strict=True)
    total_timeout_ms: int = Field(ge=1, strict=True)
    retry_policy: RetrySettlementEvidence
    bulkhead_policy: BulkheadSettlementEvidence
    repair_limit: int = Field(ge=0, le=2, strict=True)
    provider_request_limit: int = Field(ge=1, strict=True)

    @field_validator("deployment_id", "provider_kind", "provider", "model")
    @classmethod
    def validate_non_empty_route_identity(cls, value: str) -> str:
        """Structured route 的基础身份不能使用空白占位。"""

        if not value.strip():
            raise ValueError("structured route identity must not be blank")
        return value

    @field_validator("endpoint_policy_digest", "model_catalog_digest")
    @classmethod
    def validate_optional_route_digest(cls, value: str | None) -> str | None:
        """受控 route digest 必须是小写 SHA-256；legacy null 保持兼容。"""

        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("structured route digest must be lowercase SHA-256")
        return value

    @field_validator(
        "input_token_price_usd",
        "output_token_price_usd",
        "per_attempt_cost_bound",
        "reserved_cost_bound",
        mode="before",
    )
    @classmethod
    def validate_cost(cls, value: object) -> object:
        """Structured route 的价格与上界只能是有限非负值或 null。"""

        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            raise ValueError("structured route cost must be numeric or null")
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite() or decimal_value < 0:
            raise ValueError("structured route cost must be finite and non-negative")
        return decimal_value

    @model_validator(mode="after")
    def validate_joint_reservation(self) -> StructuredSettlementRouteEvidence:
        """重算 transport×repair 联合 reservation 与受控 route 身份。"""

        expected_requests = self.max_attempts * (1 + self.repair_limit)
        if self.provider_request_limit != expected_requests:
            raise ValueError("structured provider request limit formula mismatch")
        if self.retry_policy.max_attempts != self.max_attempts:
            raise ValueError("structured retry max_attempts must match route")
        if self.total_timeout_ms < max(self.connect_timeout_ms, self.read_timeout_ms):
            raise ValueError("structured total timeout must cover connect and read timeout")
        if self.snapshot_schema_version == "budget-tree-v2":
            controlled_identity = (
                self.endpoint_origin,
                self.endpoint_policy_ref,
                self.endpoint_policy_version,
                self.endpoint_policy_digest,
                self.credential_ref,
                self.model_catalog_ref,
                self.model_catalog_version,
                self.model_catalog_digest,
                self.request_shape_ref,
                self.request_shape_version,
                self.input_bound_strategy_ref,
                self.input_bound_strategy_version,
            )
            if self.provider_kind != "openai-compatible" or self.provider != "openai-compatible":
                raise ValueError("budget-tree-v2 structured route must be controlled real")
            if any(value is None or not value.strip() for value in controlled_identity):
                raise ValueError("controlled structured route identity is incomplete")
            parsed_origin = urlsplit(cast(str, self.endpoint_origin))
            if (
                parsed_origin.scheme not in {"http", "https"}
                or not parsed_origin.hostname
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise ValueError("structured endpoint_origin must be a canonical HTTP(S) origin")
            if (
                self.request_shape_ref != "single-user-text-no-tools"
                or self.request_shape_version != "v1"
                or self.input_bound_strategy_ref != "utf8-bytes-plus-envelope"
                or self.input_bound_strategy_version != "v1"
            ):
                raise ValueError("controlled structured request identity is unsupported")
        elif self.provider_kind == "openai-compatible":
            raise ValueError("controlled structured route requires budget-tree-v2")
        if (self.completion_classifier_ref is None) != (self.completion_classifier_version is None):
            raise ValueError("structured completion classifier identity must be paired")
        if self.completion_classifier_ref is not None and (
            self.completion_classifier_ref != "trusted_response_header_not_started"
            or self.completion_classifier_version != "v1"
        ):
            raise ValueError("structured completion classifier identity is unsupported")
        if self.completion_classifier_ref is None and self.retry_policy.retryable_http_statuses:
            raise ValueError("structured response retries require a completion classifier")
        expected_input = (
            self.prompt_utf8_bytes
            if self.snapshot_schema_version == "budget-tree-v1"
            else self.prompt_utf8_bytes + self.input_envelope_token_bound
        )
        if self.snapshot_schema_version == "budget-tree-v1" and self.input_envelope_token_bound:
            raise ValueError("legacy structured route cannot claim an input envelope")
        if self.trusted_input_token_bound != expected_input:
            raise ValueError("structured trusted input formula mismatch")
        if self.per_attempt_token_bound != (self.trusted_input_token_bound + self.output_token_cap):
            raise ValueError("structured per-attempt token formula mismatch")
        if self.reserved_token_bound != self.per_attempt_token_bound * expected_requests:
            raise ValueError("structured token reservation formula mismatch")
        cost_values = (
            self.input_token_price_usd,
            self.output_token_price_usd,
            self.per_attempt_cost_bound,
            self.reserved_cost_bound,
        )
        cost_enabled = any(value is not None for value in cost_values)
        price_source = (self.price_source_ref, self.price_source_version)
        price_source_enabled = any(value is not None for value in price_source)
        if (
            cost_enabled
            and any(value is None for value in cost_values)
            or price_source_enabled
            and any(value is None or not value.strip() for value in price_source)
            or cost_enabled
            and not price_source_enabled
        ):
            raise ValueError("cost-enabled structured route requires complete price identity")
        if cost_enabled:
            expected_attempt_cost = Decimal(self.trusted_input_token_bound) * cast(
                Decimal, self.input_token_price_usd
            ) + Decimal(self.output_token_cap) * cast(Decimal, self.output_token_price_usd)
            if self.per_attempt_cost_bound != expected_attempt_cost:
                raise ValueError("structured per-attempt cost formula mismatch")
            if self.reserved_cost_bound != expected_attempt_cost * expected_requests:
                raise ValueError("structured cost reservation formula mismatch")
        return self


StructuredSettlementValidationIssue = StructuredUsageValidationIssue
StructuredSettlementSummary = StructuredUsageSummary


class StructuredSettlementAttemptEvidence(StructuredModelAttemptEvidence):
    """恢复边界解析的 structured attempt exact subtype。"""

    @model_validator(mode="after")
    def validate_cleanup_and_request_fact(self) -> StructuredSettlementAttemptEvidence:
        """Prepare proof 与 send 后 cleanup 事实形成互斥分类。"""

        detail: StructuredOutputAttemptEvidence = self.structured_output
        if detail.not_started_proof is not None:
            proof = detail.not_started_proof
            if self.side_effect_state != "not_started" or (
                proof.kind == "client_prepare_not_started"
                and detail.cleanup_status != "not_applicable"
            ):
                raise ValueError("structured prepare proof facts mismatch")
        elif detail.cleanup_status == "not_applicable":
            raise ValueError("sent structured attempt requires cleanup observation")
        return self
