"""模型路由的冻结 DTO 与窄 Agent policy protocol。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import ConfigDict, Field, field_validator, model_validator

from agent_harness.config.schemas import ModelRouteRef
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models._router_identity import canonical_decimal, model_route_digest
from agent_harness.models.providers import ModelDecision


class AgentModelPolicyLike(Protocol):
    """Router 只消费 Agent policy 的窄字段，避免反向依赖 registry/runtime。"""

    deployment_id: str
    provider: str
    allowed_models: list[str]
    default_model: str
    fallback_models: list[str]
    fallback_routes: tuple[ModelRouteRef, ...]


class FrozenAgentModelPolicy(HarnessDTO):
    """从 durable 子快照解析的最小 Agent 模型策略。"""

    deployment_id: str
    provider: str
    allowed_models: list[str]
    default_model: str
    fallback_models: list[str]
    deployment_fallback_models: list[str] | None = None
    fallback_routes: tuple[ModelRouteRef, ...] = Field(default=(), max_length=8)


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
    cross_provider_failover_http_statuses: tuple[int, ...] = ()
    bulkhead_policy: ModelBulkheadPolicy = Field(default_factory=ModelBulkheadPolicy)
    snapshot_schema_version: str = "budget-tree-v1"
    trusted_token_bound: int
    trusted_cost_bound: Decimal | None


class ModelRouteCandidate(HarnessDTO):
    """公开脱敏 identity 与私有完整 route plan 的不可变组合。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    ordinal: int = Field(ge=1, le=8, strict=True)
    deployment_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    route_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    endpoint_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    retry_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bulkhead_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_ref: str | None
    model_catalog_ref: str = Field(min_length=1)
    model_catalog_version: str = Field(min_length=1)
    reserved_token_bound: int = Field(ge=0, strict=True)
    reserved_cost_bound: Decimal | None = Field(ge=0)
    static_ineligible_cause: (
        Literal["capability", "catalog", "input_bound", "hard_budget"] | None
    ) = Field(default=None, exclude=True, repr=False)
    route: ModelRoutePlan = Field(exclude=True, repr=False)

    @field_validator("reserved_cost_bound")
    @classmethod
    def validate_cost_bound(cls, value: Decimal | None) -> Decimal | None:
        """拒绝非有限成本；Pydantic 的数值比较不能可靠拦截 NaN。"""

        if value is not None and not value.is_finite():
            raise ValueError("reserved cost bound must be finite")
        return value


class ModelRouteAgentPolicyIdentity(HarnessDTO):
    """chain identity 中请求缩权前的完整 Agent 最大授权与 legacy 投影。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    deployment_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    allowed_models: tuple[str, ...] = Field(min_length=1)
    default_model: str = Field(min_length=1)
    fallback_models: tuple[str, ...]
    fallback_routes: tuple[ModelRouteRef, ...] = Field(min_length=1, max_length=8)


class ModelRouteRequestBounds(HarnessDTO):
    """chain id 绑定的输入字节与输出上界，不保存 prompt 原文。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    prompt_utf8_bytes: int = Field(ge=0, strict=True)
    max_output_tokens: int = Field(ge=1, strict=True)


class ModelRouteChainPlan(HarnessDTO):
    """在任何预算或 provider 副作用前冻结的有序 route chain。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["model-route-chain-v1"] = "model-route-chain-v1"
    chain_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability: Literal["text_completion", "text_stream"]
    candidate_count: int = Field(ge=1, le=8, strict=True)
    agent_model_policy: ModelRouteAgentPolicyIdentity
    request_bounds: ModelRouteRequestBounds
    candidates: tuple[ModelRouteCandidate, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_chain_shape(self) -> ModelRouteChainPlan:
        """候选数量与连续 ordinal 必须逐值一致。"""

        if self.candidate_count != len(self.candidates):
            raise ValueError("route candidate count does not match candidates")
        if [item.ordinal for item in self.candidates] != list(range(1, self.candidate_count + 1)):
            raise ValueError("route candidate ordinals must be continuous")
        if any(item.route.capability != self.capability for item in self.candidates):
            raise ValueError("route candidate capability mismatch")
        preimage: dict[str, object] = {
            "schema_version": "model-route-chain-id-v1",
            "capability": self.capability,
            "candidate_count": self.candidate_count,
            "agent_model_policy": self.agent_model_policy.model_dump(mode="json"),
            "request_bounds": self.request_bounds.model_dump(mode="json"),
            "candidates": [
                {
                    **item.model_dump(mode="json", exclude={"route", "reserved_cost_bound"}),
                    "reserved_cost_bound": (
                        None
                        if item.reserved_cost_bound is None
                        else canonical_decimal(item.reserved_cost_bound)
                    ),
                }
                for item in self.candidates
            ],
        }
        if model_route_digest(preimage) != self.chain_id:
            raise ValueError("route chain id does not match canonical identity")
        return self


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
    cross_provider_failover_http_statuses: tuple[int, ...] = ()
    bulkhead_policy: dict[str, Any]
