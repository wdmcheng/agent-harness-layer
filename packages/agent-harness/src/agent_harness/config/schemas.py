"""profile 和 agent 配置使用的 Pydantic schema。"""

from __future__ import annotations

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.identity import IdentityContext


class StorageSettings(HarnessDTO):
    """存储边界配置；local/service profile 加载只校验形状，不打开连接。"""

    kind: str
    root: str | None = None
    dsn: str | None = None


class QueueSettings(HarnessDTO):
    """队列边界配置；local 可用进程内队列，service 可声明 Redis。"""

    kind: str
    dsn: str | None = None


class ObservabilitySettings(HarnessDTO):
    """观测输出边界配置；local-jsonl 必须永远可作为 fallback。"""

    kind: str
    path: str | None = None


class PolicySettings(HarnessDTO):
    """策略 provider 和危险动作默认清单。"""

    provider: str
    path: str | None = None
    require_approval_actions: list[str] = Field(default_factory=list)
    deny_actions: list[str] = Field(default_factory=list)


class AuthSettings(HarnessDTO):
    """API 认证配置；local 默认允许无 token 使用默认 identity。"""

    provider: str = "local"
    required: bool = False
    dev_bearer_token: str | None = None


class ModelSettings(HarnessDTO):
    """模型 provider 壳配置；是否需要 API key 由 provider 边界声明。"""

    provider: str
    requires_api_key: bool = False
    default_model: str | None = None
    timeout_seconds: int = 60


class BudgetSettings(HarnessDTO):
    """运行级预算默认值，供后续 policy/model router 复用。"""

    max_tokens_per_run: int = 8192
    max_cost_usd_per_run: float | None = None


class IdentitySettings(HarnessDTO):
    """未接入认证后端时使用的默认 identity。"""

    default: IdentityContext = Field(default_factory=IdentityContext.local_default)


class ProcessSettings(HarnessDTO):
    """service profile 的进程声明，不代表加载配置时会启动进程。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int | None = None


class ServiceSettings(HarnessDTO):
    """API / worker 可拆边界的类型化占位。"""

    api_process: ProcessSettings = Field(default_factory=ProcessSettings)
    worker_process: ProcessSettings = Field(default_factory=ProcessSettings)


class AgentBudgetSettings(HarnessDTO):
    """单个 agent 可覆盖的预算片段。"""

    max_tokens_per_run: int | None = None
    max_cost_usd_per_run: float | None = None


class AgentConfig(HarnessDTO):
    """agent YAML 进入 registry 前的公共配置形状。"""

    name: str | None = None
    description: str | None = None
    budget: AgentBudgetSettings = Field(default_factory=AgentBudgetSettings)
    tool_allowlist: list[str] = Field(default_factory=list)
    eval_dataset: str | None = None
    delegation_edges: list[str] = Field(default_factory=list)


class HarnessSettings(HarnessDTO):
    """profile、agent、identity 和 service 边界的合并结果。"""

    profile: str
    storage: StorageSettings
    queue: QueueSettings
    observability: ObservabilitySettings
    auth: AuthSettings = Field(default_factory=AuthSettings)
    policy: PolicySettings
    model: ModelSettings
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    agent: AgentConfig = Field(default_factory=AgentConfig)
