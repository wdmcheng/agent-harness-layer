"""Pydantic settings schemas for profiles and agent configuration."""

from __future__ import annotations

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.identity import IdentityContext


class StorageSettings(HarnessDTO):
    kind: str
    root: str | None = None
    dsn: str | None = None


class QueueSettings(HarnessDTO):
    kind: str
    dsn: str | None = None


class ObservabilitySettings(HarnessDTO):
    kind: str
    path: str | None = None


class PolicySettings(HarnessDTO):
    provider: str
    require_approval_actions: list[str] = Field(default_factory=list)


class ModelSettings(HarnessDTO):
    provider: str
    requires_api_key: bool = False
    default_model: str | None = None
    timeout_seconds: int = 60


class BudgetSettings(HarnessDTO):
    max_tokens_per_run: int = 8192
    max_cost_usd_per_run: float | None = None


class IdentitySettings(HarnessDTO):
    default: IdentityContext = Field(default_factory=IdentityContext.local_default)


class ProcessSettings(HarnessDTO):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int | None = None


class ServiceSettings(HarnessDTO):
    api_process: ProcessSettings = Field(default_factory=ProcessSettings)
    worker_process: ProcessSettings = Field(default_factory=ProcessSettings)


class AgentBudgetSettings(HarnessDTO):
    max_tokens_per_run: int | None = None
    max_cost_usd_per_run: float | None = None


class AgentConfig(HarnessDTO):
    name: str | None = None
    description: str | None = None
    budget: AgentBudgetSettings = Field(default_factory=AgentBudgetSettings)
    tool_allowlist: list[str] = Field(default_factory=list)
    eval_dataset: str | None = None
    delegation_edges: list[str] = Field(default_factory=list)


class HarnessSettings(HarnessDTO):
    profile: str
    storage: StorageSettings
    queue: QueueSettings
    observability: ObservabilitySettings
    policy: PolicySettings
    model: ModelSettings
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    agent: AgentConfig = Field(default_factory=AgentConfig)
