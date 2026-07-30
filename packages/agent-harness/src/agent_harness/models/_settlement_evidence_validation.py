"""耐久 settlement evidence 的冻结 route、attempt 与 charge 校验。"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Literal, cast
from urllib.parse import urlsplit

from pydantic import field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.providers import ModelAttemptEvidence
from agent_harness.models.usage import ModelUsageEvidence, UsageInvocationReplayError

_ATTEMPT_FIELDS = {
    "attempt",
    "outcome",
    "side_effect_state",
    "completion_observed",
    "http_status",
    "retry_after_ms",
    "input_tokens",
    "output_tokens",
    "cost_usd",
    "cost_status",
    "budget_charge_tokens",
    "budget_charge_cost_usd",
    "latency_ms",
    "error_code",
}
_BUDGET_CHARGE_FIELDS = {
    "charged_tokens",
    "charged_cost_usd",
    "charge_status",
    "unresolved_attempts",
}


class _RetryEvidence(HarnessDTO):
    """冻结 route 内允许恢复解释的封闭 retry 参数。"""

    retryable_http_statuses: list[int]
    max_attempts: int
    max_wait_ms: int
    backoff_initial_ms: int
    backoff_max_ms: int

    @field_validator(
        "max_attempts",
        "max_wait_ms",
        "backoff_initial_ms",
        "backoff_max_ms",
        mode="before",
    )
    @classmethod
    def validate_integer(cls, value: object) -> object:
        """拒绝 bool/coercion；恢复只能消费原始非负整数。"""

        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("retry values must be non-negative integers")
        return value

    @field_validator("retryable_http_statuses", mode="before")
    @classmethod
    def validate_statuses(cls, value: object) -> object:
        """状态码必须排序去重，并且只允许冻结契约中的 429/5xx。"""

        if not isinstance(value, list) or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or item != 429
            and not 500 <= item <= 599
            for item in cast(list[object], value)
        ):
            raise ValueError("retry statuses must be 429 or 5xx integers")
        statuses = cast(list[int], value)
        if statuses != sorted(set(statuses)):
            raise ValueError("retry statuses must be unique and sorted")
        return statuses

    @model_validator(mode="after")
    def validate_policy(self) -> _RetryEvidence:
        """等待与 backoff 不能自相矛盾。"""

        if self.max_attempts < 1 or self.backoff_max_ms < self.backoff_initial_ms:
            raise ValueError("retry policy bounds are invalid")
        return self


class _BulkheadEvidence(HarnessDTO):
    """冻结 route 内允许恢复解释的封闭 bulkhead 参数。"""

    scope: Literal["process_deployment"]
    max_in_flight: int
    queue_timeout_ms: int

    @field_validator("max_in_flight", "queue_timeout_ms", mode="before")
    @classmethod
    def validate_positive_integer(cls, value: object) -> object:
        """并发和排队上界必须保持严格正整数。"""

        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("bulkhead values must be positive integers")
        return value


class _RouteEvidence(HarnessDTO):
    """5.29 公开 route 投影的完整 typed 形状，不接受私有 URL 或额外字段。"""

    snapshot_schema_version: Literal["budget-tree-v2"]
    deployment_id: str
    provider_kind: Literal["openai-compatible"]
    provider: Literal["openai-compatible"]
    model: str
    capability: Literal["text_completion", "text_stream"]
    endpoint_origin: str
    endpoint_policy_ref: str
    endpoint_policy_version: str
    endpoint_policy_digest: str
    completion_classifier_ref: str | None
    completion_classifier_version: str | None
    credential_ref: str
    model_catalog_ref: str
    model_catalog_version: str
    model_catalog_digest: str
    request_shape_ref: Literal["single-user-text-no-tools"]
    request_shape_version: Literal["v1"]
    input_bound_strategy_ref: Literal["utf8-bytes-plus-envelope"]
    input_bound_strategy_version: Literal["v1"]
    input_envelope_token_bound: int
    input_token_price_usd: Decimal | None
    output_token_price_usd: Decimal | None
    price_source_ref: str | None
    price_source_version: str | None
    prompt_utf8_bytes: int
    trusted_input_token_bound: int
    output_token_cap: int
    per_attempt_token_bound: int
    per_attempt_cost_bound: Decimal | None
    max_attempts: int
    reserved_token_bound: int
    reserved_cost_bound: Decimal | None
    connect_timeout_ms: int
    read_timeout_ms: int
    total_timeout_ms: int
    retry_policy: _RetryEvidence
    bulkhead_policy: _BulkheadEvidence

    @field_validator(
        "deployment_id",
        "model",
        "endpoint_origin",
        "endpoint_policy_ref",
        "endpoint_policy_version",
        "credential_ref",
        "model_catalog_ref",
        "model_catalog_version",
    )
    @classmethod
    def validate_non_empty_identity(cls, value: str) -> str:
        """恢复所需的 route identity 不允许空白占位。"""

        if not value.strip():
            raise ValueError("route identity must not be blank")
        return value

    @field_validator("endpoint_policy_digest", "model_catalog_digest")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        """冻结目录 identity 只接受小写 SHA-256。"""

        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("route digest must be lowercase SHA-256")
        return value

    @field_validator(
        "input_envelope_token_bound",
        "prompt_utf8_bytes",
        "trusted_input_token_bound",
        "output_token_cap",
        "per_attempt_token_bound",
        "max_attempts",
        "reserved_token_bound",
        "connect_timeout_ms",
        "read_timeout_ms",
        "total_timeout_ms",
        mode="before",
    )
    @classmethod
    def validate_route_integer(cls, value: object) -> object:
        """冻结上界拒绝 bool 与 Pydantic 数字 coercion。"""

        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("route bounds must be non-negative integers")
        return value

    @field_validator(
        "input_token_price_usd",
        "output_token_price_usd",
        "per_attempt_cost_bound",
        "reserved_cost_bound",
        mode="before",
    )
    @classmethod
    def validate_route_decimal(cls, value: object) -> object:
        """公开价格和 cost 上界只能是有限非负 number 或 null。"""

        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            raise ValueError("route cost values must be numeric or null")
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite() or decimal_value < 0:
            raise ValueError("route cost values must be finite and non-negative")
        return decimal_value

    @model_validator(mode="after")
    def validate_route_invariants(self) -> _RouteEvidence:
        """重算冻结公式与 identity 配对，禁止脏 route 缩小 reservation。"""

        parsed_origin = urlsplit(self.endpoint_origin)
        if (
            parsed_origin.scheme not in {"http", "https"}
            or not parsed_origin.hostname
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in {"", "/"}
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise ValueError("endpoint_origin must be a canonical HTTP(S) origin")
        if (self.completion_classifier_ref is None) != (self.completion_classifier_version is None):
            raise ValueError("completion classifier identity must be paired")
        if self.completion_classifier_ref is not None and (
            self.completion_classifier_ref != "trusted_response_header_not_started"
            or self.completion_classifier_version != "v1"
        ):
            raise ValueError("completion classifier identity is unsupported")
        if self.completion_classifier_ref is None and self.retry_policy.retryable_http_statuses:
            raise ValueError("response retries require a completion classifier")
        if self.trusted_input_token_bound != (
            self.prompt_utf8_bytes + self.input_envelope_token_bound
        ):
            raise ValueError("trusted input bound formula mismatch")
        if self.output_token_cap < 1 or self.max_attempts < 1:
            raise ValueError("output and attempt bounds must be positive")
        if self.per_attempt_token_bound != (self.trusted_input_token_bound + self.output_token_cap):
            raise ValueError("per-attempt token formula mismatch")
        if self.reserved_token_bound != self.per_attempt_token_bound * self.max_attempts:
            raise ValueError("reserved token formula mismatch")
        if min(self.connect_timeout_ms, self.read_timeout_ms, self.total_timeout_ms) < 1:
            raise ValueError("timeouts must be positive")
        if self.total_timeout_ms < max(self.connect_timeout_ms, self.read_timeout_ms):
            raise ValueError("total timeout must cover connect and read timeout")
        if self.retry_policy.max_attempts != self.max_attempts:
            raise ValueError("retry max_attempts must match route")

        cost_values = (
            self.input_token_price_usd,
            self.output_token_price_usd,
            self.per_attempt_cost_bound,
            self.reserved_cost_bound,
            self.price_source_ref,
            self.price_source_version,
        )
        cost_enabled = any(value is not None for value in cost_values)
        if cost_enabled and any(value is None or value == "" for value in cost_values):
            raise ValueError("cost-enabled route requires complete price identity")
        if cost_enabled:
            expected_attempt_cost = Decimal(self.trusted_input_token_bound) * cast(
                Decimal, self.input_token_price_usd
            ) + Decimal(self.output_token_cap) * cast(Decimal, self.output_token_price_usd)
            if self.per_attempt_cost_bound != expected_attempt_cost:
                raise ValueError("per-attempt cost formula mismatch")
            if self.reserved_cost_bound != expected_attempt_cost * self.max_attempts:
                raise ValueError("reserved cost formula mismatch")
        return self


class _BudgetChargeEvidence(HarnessDTO):
    """5.29 的封闭调用级 charge 形状；私有 ledger impact 不在此暴露。"""

    charged_tokens: int | None
    charged_cost_usd: float | None
    charge_status: Literal["actual", "unknown"]
    unresolved_attempts: list[int]

    @field_validator("charged_tokens", mode="before")
    @classmethod
    def validate_tokens(cls, value: object) -> object:
        """公开 token charge 只能是非 bool 非负整数或 null。"""

        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("charged_tokens must be a non-negative integer or null")
        return value

    @field_validator("charged_cost_usd", mode="before")
    @classmethod
    def validate_cost(cls, value: object) -> object:
        """公开 cost charge 只能是有限非负 number 或 null。"""

        if value is None:
            return value
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("charged_cost_usd must be finite and non-negative or null")
        return value

    @field_validator("unresolved_attempts", mode="before")
    @classmethod
    def validate_unresolved_attempts(cls, value: object) -> object:
        """未决 ordinal 必须是去重、升序的正整数列表。"""

        if not isinstance(value, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 1
            for item in cast(list[object], value)
        ):
            raise ValueError("unresolved_attempts must contain positive integers")
        unresolved = cast(list[int], value)
        if unresolved != sorted(set(unresolved)):
            raise ValueError("unresolved_attempts must be unique and sorted")
        return unresolved

    @model_validator(mode="after")
    def validate_status_shape(self) -> _BudgetChargeEvidence:
        """actual 与 unknown 使用互斥形状，避免未知消耗伪装成已结算值。"""

        if self.charge_status == "actual":
            if self.unresolved_attempts or self.charged_tokens is None:
                raise ValueError("actual charge requires tokens and no unresolved attempts")
        elif (
            not self.unresolved_attempts
            or self.charged_tokens is not None
            or self.charged_cost_usd is not None
        ):
            raise ValueError("unknown charge requires unresolved attempts and null totals")
        return self


def _replay_error(state: str) -> UsageInvocationReplayError:
    """把任何不可信嵌套值统一收敛为稳定恢复错误。"""

    return UsageInvocationReplayError(state)


def validate_settlement_evidence_nesting(
    evidence: ModelUsageEvidence,
    *,
    started: ModelUsageEvidence,
    state: str,
) -> int:
    """验证 5.29 嵌套证据并返回可信 attempt 数量。

    fake、零 provider 拒绝与历史 evidence 可以同时省略嵌套字段；只要任一字段出现，
    就必须连同 typed route 按封闭形状、冻结公式和逐 attempt charge 规则完整对账。
    """

    decision = evidence.decision
    raw_attempts = decision.get("attempts")
    raw_budget_charge = decision.get("budget_charge")
    started_route = started.decision.get("route")
    provider_called = decision.get("provider_called")
    controlled_real = evidence.provider == "openai-compatible" and (
        provider_called is True or isinstance(started_route, dict)
    )
    if raw_attempts is None and raw_budget_charge is None:
        if controlled_real:
            raise _replay_error(state)
        return int(decision.get("provider_called") is True)
    if not isinstance(raw_attempts, list) or not isinstance(raw_budget_charge, dict):
        raise _replay_error(state)

    raw_route = decision.get("route")
    if not isinstance(raw_route, dict):
        raise _replay_error(state)
    if not isinstance(started_route, dict) or raw_route != started_route:
        # final route 的公开投影不足以独立复算 endpoint policy digest；durable
        # started identity 是 provider 副作用前冻结的锚点，恢复时必须逐字段一致。
        raise _replay_error(state)

    attempts: list[ModelAttemptEvidence] = []
    try:
        route = _RouteEvidence.model_validate(cast(dict[object, object], raw_route))
        if route.provider != evidence.provider or route.model != evidence.model:
            raise _replay_error(state)
        for expected, raw_attempt in enumerate(cast(list[object], raw_attempts), start=1):
            if not isinstance(raw_attempt, dict):
                raise _replay_error(state)
            attempt_payload = cast(dict[object, object], raw_attempt)
            if set(attempt_payload) != _ATTEMPT_FIELDS:
                raise _replay_error(state)
            attempt = ModelAttemptEvidence.model_validate(attempt_payload)
            if attempt.attempt != expected:
                raise _replay_error(state)
            attempts.append(attempt)
        budget_payload = cast(dict[object, object], raw_budget_charge)
        if set(budget_payload) != _BUDGET_CHARGE_FIELDS:
            raise _replay_error(state)
        budget_charge = _BudgetChargeEvidence.model_validate(budget_payload)
    except (ValueError, TypeError):
        raise _replay_error(state) from None

    if controlled_real and (
        not isinstance(provider_called, bool) or provider_called and not attempts
    ):
        raise _replay_error(state)

    cost_enabled = route.input_token_price_usd is not None
    unresolved: set[int] = set()
    charged_tokens = 0
    charged_cost = 0.0
    aggregate_input = 0
    aggregate_output = 0
    aggregate_cost = 0.0
    aggregate_cost_status = "reported"

    for attempt in attempts:
        if attempt.error_code is not None and not attempt.error_code.strip():
            raise _replay_error(state)
        if attempt.cost_usd is None:
            if attempt.cost_status != "unavailable":
                raise _replay_error(state)
        elif attempt.cost_status == "unavailable":
            raise _replay_error(state)

        if attempt.side_effect_state == "not_started":
            if attempt.outcome == "completed" or any(
                value is not None
                for value in (attempt.input_tokens, attempt.output_tokens, attempt.cost_usd)
            ):
                raise _replay_error(state)
            if attempt.budget_charge_tokens != 0 or attempt.budget_charge_cost_usd not in {
                None,
                0,
            }:
                raise _replay_error(state)
        elif attempt.side_effect_state == "started":
            if attempt.outcome == "completed" and attempt.completion_observed is not True:
                raise _replay_error(state)
            token_known = attempt.input_tokens is not None and attempt.output_tokens is not None
            if (attempt.input_tokens is None) != (attempt.output_tokens is None):
                raise _replay_error(state)
            expected_tokens = (
                cast(int, attempt.input_tokens) + cast(int, attempt.output_tokens)
                if token_known
                else None
            )
            expected_cost = attempt.cost_usd if cost_enabled else None
            if (
                attempt.budget_charge_tokens != expected_tokens
                or attempt.budget_charge_cost_usd != expected_cost
            ):
                raise _replay_error(state)
            if token_known:
                aggregate_input += cast(int, attempt.input_tokens)
                aggregate_output += cast(int, attempt.output_tokens)
                charged_tokens += cast(int, expected_tokens)
            if cost_enabled and attempt.cost_usd is not None:
                aggregate_cost += attempt.cost_usd
                charged_cost += attempt.cost_usd
                if attempt.cost_status == "estimated":
                    aggregate_cost_status = "estimated"
            if not token_known or (cost_enabled and attempt.cost_usd is None):
                unresolved.add(attempt.attempt)
        else:
            if (
                attempt.budget_charge_tokens is not None
                or attempt.budget_charge_cost_usd is not None
            ):
                raise _replay_error(state)
            unresolved.add(attempt.attempt)

    if charged_tokens > route.reserved_token_bound:
        unresolved.update(attempt.attempt for attempt in attempts)
    if cost_enabled and Decimal(str(charged_cost)) > cast(Decimal, route.reserved_cost_bound):
        unresolved.update(attempt.attempt for attempt in attempts)

    expected_unresolved = sorted(unresolved)
    if expected_unresolved:
        if (
            budget_charge.charge_status != "unknown"
            or budget_charge.unresolved_attempts != expected_unresolved
        ):
            raise _replay_error(state)
        if (
            any(
                value is not None
                for value in (evidence.input_tokens, evidence.output_tokens, evidence.cost_usd)
            )
            or evidence.cost_status != "unavailable"
        ):
            raise _replay_error(state)
    else:
        expected_cost = charged_cost if cost_enabled else None
        if (
            budget_charge.charge_status != "actual"
            or budget_charge.unresolved_attempts
            or budget_charge.charged_tokens != charged_tokens
            or budget_charge.charged_cost_usd != expected_cost
        ):
            raise _replay_error(state)
        provider_called = decision.get("provider_called") is True
        if evidence.input_tokens != (aggregate_input if provider_called else None):
            raise _replay_error(state)
        if evidence.output_tokens != (aggregate_output if provider_called else None):
            raise _replay_error(state)
        expected_evidence_cost = aggregate_cost if provider_called and cost_enabled else None
        expected_cost_status = (
            aggregate_cost_status if expected_evidence_cost is not None else "unavailable"
        )
        if (
            evidence.cost_usd != expected_evidence_cost
            or evidence.cost_status != expected_cost_status
        ):
            raise _replay_error(state)
    return len(attempts)
