"""模型路由的冻结 DTO 与窄 Agent policy protocol。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import ConfigDict, Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.providers import ModelDecision


class AgentModelPolicyLike(Protocol):
    """Router 只消费 Agent policy 的窄字段，避免反向依赖 registry/runtime。"""

    deployment_id: str
    provider: str
    allowed_models: list[str]
    default_model: str
    fallback_models: list[str]


class FrozenAgentModelPolicy(HarnessDTO):
    """从 durable 子快照解析的最小 Agent 模型策略。"""

    deployment_id: str
    provider: str
    allowed_models: list[str]
    default_model: str
    fallback_models: list[str]
    deployment_fallback_models: list[str] | None = None


class ModelRouteError(ValueError):
    """在 provider 副作用前返回的稳定路由错误。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class ModelRouterConfig(HarnessDTO):
    """兼容 fake 路由与旧预算快照的运行时配置。"""

    default_provider: str = "fake"
    default_model: str
    fallback_models: list[str] = Field(default_factory=list)
    timeout_seconds: int = 60
    max_tokens_per_call: int | None = None
    max_cost_per_call: Decimal | None = None
    input_token_price_usd: Decimal | None = None
    output_token_price_usd: Decimal | None = None
    price_source_ref: str | None = None
    price_source_version: str | None = None
    route_price_source_refs: dict[str, str] = Field(default_factory=dict)
    route_price_source_versions: dict[str, str] = Field(default_factory=dict)
    route_input_token_prices_usd: dict[str, Decimal] = Field(default_factory=dict)
    route_output_token_prices_usd: dict[str, Decimal] = Field(default_factory=dict)
    route_max_tokens_per_call: dict[str, int] = Field(default_factory=dict)
    route_max_cost_per_call: dict[str, Decimal] = Field(default_factory=dict)


class ModelRetryPolicy(HarnessDTO):
    """route plan 内冻结的版本化 retry 执行参数。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    retryable_http_statuses: tuple[int, ...] = ()
    max_attempts: int = Field(default=1, ge=1)
    max_wait_ms: int = Field(default=0, ge=0)
    backoff_initial_ms: int = Field(default=0, ge=0)
    backoff_max_ms: int = Field(default=0, ge=0)


class ModelBulkheadPolicy(HarnessDTO):
    """route plan 内冻结的 process-local 并发与排队边界。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    scope: Literal["process_deployment"] = "process_deployment"
    max_in_flight: int = Field(default=1, ge=1)
    queue_timeout_ms: int = Field(default=1000, ge=1)


class ModelRoutePlan(HarnessDTO):
    """provider 副作用前冻结的路由、安全 identity 与调用级上界。"""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        frozen=True,
        arbitrary_types_allowed=False,
    )

    deployment_id: str = "fake_default"
    provider_kind: str = "fake"
    provider: str
    allowed_models: tuple[str, ...] = ()
    model: str
    capability: str = "text_completion"
    decision: ModelDecision
    approval_kind: Literal["soft_budget"] | None = None
    canonical_base_url: str | None = Field(default=None, exclude=True, repr=False)
    endpoint_origin: str | None = None
    endpoint_policy_ref: str | None = None
    endpoint_policy_version: str | None = None
    endpoint_policy_digest: str | None = None
    completion_classifier_ref: str | None = None
    completion_classifier_version: str | None = None
    credential_ref: str | None = None
    model_catalog_ref: str | None = None
    model_catalog_version: str | None = None
    model_catalog_digest: str | None = None
    request_shape_ref: str | None = None
    request_shape_version: str | None = None
    input_bound_strategy_ref: str | None = None
    input_bound_strategy_version: str | None = None
    input_envelope_token_bound: int = 0
    prompt_utf8_bytes: int = 0
    trusted_input_token_bound: int = 0
    output_token_cap: int = 0
    per_attempt_token_bound: int = 0
    per_attempt_cost_bound: Decimal | None = None
    max_attempts: int = 1
    reserved_token_bound: int = 0
    reserved_cost_bound: Decimal | None = None
    input_token_price_usd: Decimal | None = None
    output_token_price_usd: Decimal | None = None
    price_source_ref: str | None = None
    price_source_version: str | None = None
    connect_timeout_ms: int = 1
    read_timeout_ms: int = 1
    total_timeout_ms: int = 1
    retry_policy: ModelRetryPolicy = Field(default_factory=ModelRetryPolicy)
    bulkhead_policy: ModelBulkheadPolicy = Field(default_factory=ModelBulkheadPolicy)
    snapshot_schema_version: str = "budget-tree-v1"
    trusted_token_bound: int
    trusted_cost_bound: Decimal | None


class FrozenModelRouteSnapshot(HarnessDTO):
    """v2 子快照中重建真实 route 所需的完整、非敏感静态输入。"""

    usage_kind: Literal["model"]
    deployment_id: str
    provider: str
    model: str
    canonical_base_url: str
    endpoint_origin: str
    endpoint_policy_ref: str
    endpoint_policy_version: str
    endpoint_policy_digest: str
    completion_classifier_ref: str | None = None
    completion_classifier_version: str | None = None
    credential_ref: str
    capabilities: tuple[str, ...]
    model_catalog_ref: str
    model_catalog_version: str
    model_catalog_digest: str
    request_shape_ref: str
    request_shape_version: str
    input_bound_strategy_ref: str
    input_bound_strategy_version: str
    input_envelope_token_bound: int
    cost_enabled: bool
    input_token_price_usd: Decimal | None = None
    output_token_price_usd: Decimal | None = None
    price_source_ref: str | None = None
    price_source_version: str | None = None
    max_prompt_utf8_bytes: int
    max_output_tokens: int
    max_per_attempt_token_bound: int
    max_per_attempt_cost_bound: Decimal | None = None
    soft_max_tokens_per_call: int
    max_attempts: int
    connect_timeout_ms: int
    read_timeout_ms: int
    total_timeout_ms: int
    retry_policy: dict[str, Any]
    bulkhead_policy: dict[str, Any]
