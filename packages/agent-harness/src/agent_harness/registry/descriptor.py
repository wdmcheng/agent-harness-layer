"""Agent descriptor DTO 与 public policy 摘要。"""

from __future__ import annotations

from agent_harness.contracts.dto import HarnessDTO


class AgentToolPolicy(HarnessDTO):
    """public descriptor 中可暴露的工具权限摘要。"""

    allowed_tools: list[str]


class AgentModelPolicy(HarnessDTO):
    """public descriptor 中可暴露的模型路由摘要。"""

    provider: str
    default_model: str
    fallback_models: list[str]


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
    config_ref: str
    tool_policy: AgentToolPolicy
    model_policy: AgentModelPolicy
    budget: AgentBudget
    eval_dataset: str | None
    delegation_targets: list[str]
