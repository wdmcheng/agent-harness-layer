"""profile 和 agent 配置使用的 Pydantic schema。"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator

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


class ObservabilityProviderSettings(HarnessDTO):
    """外部 observability provider 的可选配置入口。

    token 只通过 env 名称或部署 secret 引用传递，profile 不承载真实 secret。
    """

    kind: str
    enabled: bool = False
    endpoint: str | None = None
    token_env: str | None = None


def _empty_observability_providers() -> list[ObservabilityProviderSettings]:
    """为每次配置解析创建独立 provider 列表，避免默认值跨 profile 共享。"""

    return []


class ObservabilitySettings(HarnessDTO):
    """观测输出边界配置；local-jsonl 必须永远可作为 fallback。"""

    kind: str
    path: str | None = None
    providers: list[ObservabilityProviderSettings] = Field(
        default_factory=_empty_observability_providers
    )


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


class ModelCredentialSettings(HarnessDTO):
    """把 secret 与允许转发的 exact origin 绑定在同一 typed 边界。"""

    value: SecretStr = Field(min_length=1, exclude=True, repr=False)
    allowed_origins: list[str] = Field(min_length=1)


class CompletionClassifierSettings(HarnessDTO):
    """endpoint policy 明确允许的完成状态分类器 identity。"""

    ref: str
    version: str


def _empty_completion_classifiers() -> list[CompletionClassifierSettings]:
    """为每个 endpoint policy 创建独立 classifier 列表。"""

    return []


class ModelEndpointPolicySettings(HarnessDTO):
    """受信 endpoint policy；deployment 只能引用，不能自行扩充。"""

    version: str = Field(min_length=1)
    provider_kind: Literal["openai-compatible"]
    allowed_origins: list[str] = Field(min_length=1)
    completion_classifiers: list[CompletionClassifierSettings] = Field(
        default_factory=_empty_completion_classifiers
    )
    default_endpoint_catalog_ref: str | None = None
    default_endpoint_catalog_version: str | None = None

    @model_validator(mode="after")
    def validate_default_catalog_identity(self) -> ModelEndpointPolicySettings:
        """default catalog 的 ref/version 必须成对出现。"""

        if (self.default_endpoint_catalog_ref is None) != (
            self.default_endpoint_catalog_version is None
        ):
            raise ValueError("default endpoint catalog ref/version must be both set or null")
        return self


class ModelCatalogEntrySettings(HarnessDTO):
    """真实模型输入上界与价格的唯一受信目录条目。"""

    version: str = Field(min_length=1)
    provider_kind: Literal["openai-compatible"]
    model: str = Field(min_length=1)
    request_shape_ref: Literal["single-user-text-no-tools"]
    request_shape_version: Literal["v1"]
    input_bound_strategy_ref: Literal["utf8-bytes-plus-envelope"]
    input_bound_strategy_version: Literal["v1"]
    input_envelope_token_bound: int = Field(ge=0)
    cost_enabled: bool
    input_token_price_usd: Decimal | None = None
    output_token_price_usd: Decimal | None = None
    price_source_ref: str | None = None
    price_source_version: str | None = None
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("input_token_price_usd", "output_token_price_usd")
    @classmethod
    def validate_price(cls, value: Decimal | None) -> Decimal | None:
        """拒绝负数与非有限 Decimal，避免 NaN/Infinity 绕过预算。"""

        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError("model catalog prices must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_cost_identity(self) -> ModelCatalogEntrySettings:
        """价格、来源与 cost-enabled 形成全有或全无的稳定语义。"""

        values = (
            self.input_token_price_usd,
            self.output_token_price_usd,
            self.price_source_ref,
            self.price_source_version,
        )
        if self.cost_enabled and any(value is None or value == "" for value in values):
            raise ValueError("cost-enabled catalog requires prices and price source identity")
        if not self.cost_enabled and any(value is not None for value in values):
            raise ValueError("cost-disabled catalog requires null prices and source identity")
        return self


class ModelDeploymentSettings(HarnessDTO):
    """真实或 fake deployment 的冻结策略；请求只能进一步缩小。"""

    provider_kind: Literal["fake", "openai-compatible"]
    allowed_models: list[str] = Field(min_length=1)
    model_catalog_refs: dict[str, str] = Field(default_factory=dict)
    model_catalog_versions: dict[str, str] = Field(default_factory=dict)
    default_model: str
    fallback_models: list[str] = Field(default_factory=list)
    base_url: str | None = None
    endpoint_policy_ref: str | None = None
    endpoint_policy_version: str | None = None
    credential_ref: str | None = None
    completion_classifier_ref: str | None = None
    completion_classifier_version: str | None = None
    connect_timeout_ms: int = Field(default=5_000, ge=1)
    read_timeout_ms: int = Field(default=60_000, ge=1)
    total_timeout_ms: int = Field(default=60_000, ge=1)
    max_attempts: int = Field(default=1, ge=1, le=10)
    retryable_http_statuses: list[int] = Field(default_factory=lambda: list[int]())
    backoff_initial_ms: int = Field(default=0, ge=0)
    backoff_max_ms: int = Field(default=0, ge=0)
    max_retry_wait_ms: int = Field(default=0, ge=0)
    max_in_flight: int = Field(default=1, ge=1)
    queue_timeout_ms: int = Field(default=1_000, ge=1)
    max_prompt_utf8_bytes: int = Field(default=8192, ge=1)
    max_output_tokens: int = Field(default=8192, ge=1)
    max_per_attempt_token_bound: int | None = Field(default=None, ge=1)
    max_per_attempt_cost_bound: Decimal | None = None
    capabilities: list[Literal["text_completion", "text_stream"]] = Field(
        default_factory=lambda: ["text_completion"]
    )
    allow_local_http: bool = False

    @field_validator("retryable_http_statuses")
    @classmethod
    def validate_retry_statuses(cls, value: list[int]) -> list[int]:
        """只允许明确的 429/5xx，排序去重后进入冻结 route identity。"""

        if any(isinstance(item, bool) or item != 429 and not 500 <= item <= 599 for item in value):
            raise ValueError("retryable statuses must be 429 or 5xx")
        return sorted(set(value))

    @model_validator(mode="after")
    def validate_local_shape(self) -> ModelDeploymentSettings:
        """先锁定集合关系与成对 identity；目录/endpoint 逐值校验由 resolver 完成。"""

        if len(self.allowed_models) != len(set(self.allowed_models)):
            raise ValueError("allowed_models must be unique")
        allowed = set(self.allowed_models)
        if self.default_model not in allowed or not set(self.fallback_models) <= allowed:
            raise ValueError("default and fallback models must be within allowed_models")
        if (self.completion_classifier_ref is None) != (self.completion_classifier_version is None):
            raise ValueError("completion classifier ref/version must be both set or null")
        if self.retryable_http_statuses and self.completion_classifier_ref is None:
            raise ValueError("response retries require a completion classifier")
        if self.total_timeout_ms < max(self.connect_timeout_ms, self.read_timeout_ms):
            raise ValueError("total timeout must cover connect and read timeout")
        if self.backoff_max_ms < self.backoff_initial_ms:
            raise ValueError("backoff_max_ms must be >= backoff_initial_ms")
        if self.max_per_attempt_cost_bound is not None and (
            not self.max_per_attempt_cost_bound.is_finite() or self.max_per_attempt_cost_bound < 0
        ):
            raise ValueError("max_per_attempt_cost_bound must be finite and non-negative")
        return self


class ModelSettings(HarnessDTO):
    """deployment-aware 模型配置；保留旧字段仅用于 fake 配置迁移。"""

    provider: str = "fake"
    requires_api_key: bool = False
    default_model: str | None = None
    timeout_seconds: int = 60
    model_stream_chunk_utf8_bytes: int = Field(default=1024, ge=1, le=4096, strict=True)
    model_stream_sensitive_candidate_utf8_bytes: int = Field(
        default=512,
        ge=128,
        le=4096,
        strict=True,
    )
    default_deployment_id: str = "fake_default"
    deployments: dict[str, ModelDeploymentSettings] = Field(default_factory=dict)
    credentials: dict[str, ModelCredentialSettings] = Field(default_factory=dict)
    endpoint_policies: dict[str, ModelEndpointPolicySettings] = Field(default_factory=dict)
    model_catalogs: dict[str, ModelCatalogEntrySettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def provide_legacy_fake_projection(self) -> ModelSettings:
        """旧 profile 在迁移窗口内只可投影为显式 fake deployment。"""

        if not self.deployments and self.provider == "fake":
            model = self.default_model or "fake-basic"
            self.deployments = {
                "fake_default": ModelDeploymentSettings(
                    provider_kind="fake",
                    allowed_models=[model, "fake-scaffold"]
                    if model != "fake-scaffold"
                    else [model],
                    default_model=model,
                    max_prompt_utf8_bytes=8192,
                    max_output_tokens=8192,
                    max_per_attempt_token_bound=16384,
                )
            }
            self.default_deployment_id = "fake_default"
        return self


class BudgetSettings(HarnessDTO):
    """运行级预算默认值，供后续 policy/model router 复用。"""

    max_tokens_per_run: int = 8192
    max_cost_usd_per_run: float | None = None
    fingerprint_key: SecretStr = Field(min_length=1, exclude=True, repr=False)
    fingerprint_key_version: str = "budget-fingerprint-v1"


class IdentitySettings(HarnessDTO):
    """未接入认证后端时使用的默认 identity。"""

    default: IdentityContext = Field(default_factory=IdentityContext.local_default)


class ProcessSettings(HarnessDTO):
    """service profile 的进程声明，不代表加载配置时会启动进程。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int | None = None


class ApiDocsSettings(HarnessDTO):
    """OpenAPI 管理面的公开开关与静态资源部署位置。"""

    enabled: bool = True
    asset_mode: Literal["offline", "online"] = "offline"


class ServiceSettings(HarnessDTO):
    """API / worker 可拆边界的类型化占位。"""

    api_process: ProcessSettings = Field(default_factory=ProcessSettings)
    worker_process: ProcessSettings = Field(default_factory=ProcessSettings)
    api_docs: ApiDocsSettings = Field(default_factory=ApiDocsSettings)


def _empty_string_list() -> list[str]:
    """为 MCP 可变字符串配置提供独立默认容器，防止解析结果互相污染。"""

    return []


class WorkspaceToolSettings(HarnessDTO):
    """workspace 文件工具的默认安全边界。"""

    ignore_file: str = ".agentignore"
    inline_result_bytes: int = 8192


class ShellToolSettings(HarnessDTO):
    """ShellTool 默认禁用，启用后仍受 allowlist 和 timeout 约束。"""

    enabled: bool = False
    allowlist: list[str] = Field(default_factory=list)
    denylist: list[str] = Field(default_factory=list)
    env_whitelist: list[str] = Field(default_factory=list)
    timeout_seconds: int = 30
    inline_output_bytes: int = 8192


class MCPServerSettings(HarnessDTO):
    """单个 MCP server 的受控连接配置。"""

    name: str
    transport: str = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=_empty_string_list)
    url: str | None = None
    allowlist: list[str] = Field(default_factory=_empty_string_list)


class ToolSettings(HarnessDTO):
    """工具执行配置；profile 未声明时使用安全默认值。"""

    workspace: WorkspaceToolSettings = Field(default_factory=WorkspaceToolSettings)
    shell: ShellToolSettings = Field(default_factory=ShellToolSettings)
    mcp_servers: list[MCPServerSettings] = Field(default_factory=lambda: [])


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
    budget: BudgetSettings
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    agent: AgentConfig = Field(default_factory=AgentConfig)
