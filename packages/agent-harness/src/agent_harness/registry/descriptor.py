"""Agent descriptor DTO 与 public policy 摘要。"""

from __future__ import annotations

from pydantic import Field, model_validator

from agent_harness.config.schemas import ModelRouteRef
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.structured import OutputSchemaIdentity


class AgentToolPolicy(HarnessDTO):
    """public descriptor 中可暴露的工具权限摘要。"""

    allowed_tools: list[str]


class AgentModelPolicy(HarnessDTO):
    """public descriptor 中可暴露的模型路由摘要。"""

    deployment_id: str = "fake_default"
    provider: str
    allowed_models: list[str] = Field(default_factory=list)
    default_model: str
    fallback_models: list[str]
    fallback_routes: tuple[ModelRouteRef, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def validate_model_subset(self) -> AgentModelPolicy:
        """Agent 只能声明 deployment 允许集合的一个静态子集。"""

        if self.fallback_routes:
            if len(self.fallback_routes) != len(set(self.fallback_routes)):
                raise ValueError("fallback_routes must be unique")
            first = self.fallback_routes[0]
            projected_models = list(
                dict.fromkeys(
                    route.model_id
                    for route in self.fallback_routes
                    if route.deployment_id == first.deployment_id
                )
            )
            if (
                self.deployment_id != first.deployment_id
                or self.default_model != first.model_id
                or self.allowed_models != projected_models
                or self.fallback_models
            ):
                raise ValueError("legacy model fields must exactly project the first route")
            return self
        if not self.allowed_models:
            self.allowed_models = list(dict.fromkeys([self.default_model, *self.fallback_models]))
        if len(self.allowed_models) != len(set(self.allowed_models)):
            raise ValueError("allowed_models must be unique")
        allowed = set(self.allowed_models)
        if self.default_model not in allowed or not set(self.fallback_models) <= allowed:
            raise ValueError("default and fallback models must be within allowed_models")
        return self


class AgentBudget(HarnessDTO):
    """单 agent 预算摘要。"""

    max_tokens_per_run: int
    max_cost_usd_per_run: float | None


class AgentDescriptor(HarnessDTO):
    """registry 对 API、CLI 和 runtime 暴露的 agent 描述。"""

    agent_id: str
    version: str
    name: str
    description: str
    input_schema_ref: str
    output_schema_ref: str
    output_schema_identity: OutputSchemaIdentity
    config_ref: str
    tool_policy: AgentToolPolicy
    model_policy: AgentModelPolicy
    budget: AgentBudget
    eval_dataset: str | None
    delegation_targets: list[str]
