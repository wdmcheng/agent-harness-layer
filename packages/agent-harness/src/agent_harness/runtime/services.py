"""为业务 executor 装配 provider-neutral 进程内服务。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.config import HarnessSettings
from agent_harness.context import ContextAssemblyService
from agent_harness.embeddings import (
    EmbeddingInvocationService,
    LocalEmbeddingProvider,
    StorageEmbeddingCache,
)
from agent_harness.events import EventBus, EventSink
from agent_harness.events.model_tool_loop import ModelToolLoopEventProducer
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelProvider,
    ModelRouter,
    ModelRouterConfig,
    StructuredSchemaResolutionError,
    ToolCatalog,
    ToolCatalogConflictError,
    ToolCatalogSelection,
    ToolCatalogSourceDescriptor,
    build_tool_catalog,
)
from agent_harness.observability import TelemetryFacade
from agent_harness.policy import PolicyEngine
from agent_harness.registry import AgentRegistry, RegistryLoadError
from agent_harness.retrieval import (
    LocalSQLiteBM25RetrievalProvider,
    PostgreSQLRetrievalProvider,
)
from agent_harness.runtime.model_tool_loop import ModelToolLoopService
from agent_harness.runtime.model_tool_loop_approval import ModelToolLoopApprovalStore
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.tools import ToolRegistry, WorkspacePolicy
from agent_harness.tools.cli_runtime import builtin_tools

if TYPE_CHECKING:
    from agent_harness.adapters.models.pydantic_ai import ControlledOpenAIClientFactory


class ToolRegistryFactory:
    """按 agent allowlist 和固定 workspace 构造隔离 ToolRegistry。"""

    def __init__(
        self,
        *,
        settings: HarnessSettings,
        storage: SQLAlchemyStorage,
        policy: PolicyEngine,
        audit: AuditService,
        artifact_store: FileArtifactStore,
        workspace_root: Path,
    ) -> None:
        """冻结 composition 提供的共享依赖与解析后的 workspace 根目录。"""

        self._settings = settings
        self._storage = storage
        self._policy = policy
        self._audit = audit
        self._artifact_store = artifact_store
        self._workspace_root = workspace_root.resolve()

    def __call__(
        self,
        *,
        allowed_tools: Sequence[str],
        requested_tool_name: str,
    ) -> ToolRegistry:
        """调用方只能缩小 agent 工具集合，不能改变 composition workspace。"""

        workspace = WorkspacePolicy(
            root=self._workspace_root,
            ignore_file=self._settings.tools.workspace.ignore_file,
        )
        return ToolRegistry(
            tools=builtin_tools(
                settings=self._settings,
                workspace_policy=workspace,
                artifact_store=self._artifact_store,
                policy=self._policy,
                requested_tool_name=requested_tool_name,
            ),
            policy=self._policy,
            audit=self._audit,
            artifact_store=self._artifact_store,
            inline_result_bytes=self._settings.tools.workspace.inline_result_bytes,
            agent_tool_allowlist=list(allowed_tools),
            enforce_agent_tool_allowlist=True,
            storage=self._storage,
        )

    def catalog_descriptors(self) -> tuple[ToolCatalogSourceDescriptor, ...]:
        """返回未按单个 Agent 过滤的 data-only 目录，供 Registry 原子预检。"""

        workspace = WorkspacePolicy(
            root=self._workspace_root,
            ignore_file=self._settings.tools.workspace.ignore_file,
        )
        return ToolRegistry(
            tools=builtin_tools(
                settings=self._settings,
                workspace_policy=workspace,
                artifact_store=self._artifact_store,
                policy=self._policy,
                requested_tool_name="",
            ),
            policy=self._policy,
            audit=self._audit,
            artifact_store=self._artifact_store,
            inline_result_bytes=self._settings.tools.workspace.inline_result_bytes,
            storage=self._storage,
        ).catalog_descriptors()


def build_registry_tool_catalog_descriptors(
    *,
    settings: HarnessSettings,
    storage: SQLAlchemyStorage,
    policy: PolicyEngine,
    audit: AuditService,
    artifact_store: FileArtifactStore,
    workspace_root: Path,
) -> tuple[ToolCatalogSourceDescriptor, ...]:
    """以与执行期相同的工具组合构造加载期受信目录快照。"""

    return ToolRegistryFactory(
        settings=settings,
        storage=storage,
        policy=policy,
        audit=audit,
        artifact_store=artifact_store,
        workspace_root=workspace_root,
    ).catalog_descriptors()


class AgentToolCatalogResolver:
    """把受信 Agent descriptor 与当前 Registry 描述投影成冻结模型工具目录。"""

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        tool_registry_factory: ToolRegistryFactory,
    ) -> None:
        """保存只读Registry与既有工具组合工厂，不持有handler或provider能力。"""

        self._registry = registry
        self._tool_registry_factory = tool_registry_factory

    def __call__(
        self,
        agent_id: str,
        selection: ToolCatalogSelection | None,
    ) -> ToolCatalog:
        """从加载期快照缩小目录，并重验当前 Registry 未发生身份漂移。"""

        catalog = self._registry.resolve_tool_catalog(agent_id, selection)
        allowed_tools = tuple(self._registry.get(agent_id).tool_policy.allowed_tools)
        current = self._tool_registry_factory(
            allowed_tools=allowed_tools,
            requested_tool_name="",
        )
        current_catalog = build_tool_catalog(
            allowed_tools=allowed_tools,
            registry_descriptors=current.catalog_descriptors(),
            selection=selection,
        )
        if current_catalog != catalog:
            raise ToolCatalogConflictError
        return catalog


async def close_agent_execution_services(services: Mapping[str, object]) -> None:
    """先于 storage dispose 幂等关闭可能已构造的真实 provider client leases。"""

    invocation = services.get("model_invocation")
    if isinstance(invocation, ModelInvocationService):
        await invocation.aclose()


def build_agent_execution_services(
    *,
    settings: HarnessSettings,
    storage: SQLAlchemyStorage,
    storage_dsn: str,
    policy: PolicyEngine,
    audit: AuditService,
    event_sink: EventSink,
    event_bus: EventBus,
    artifact_store: FileArtifactStore,
    service_root: Path,
    registry: AgentRegistry,
    workspace_root: Path | None = None,
    stream_timing_observer: Callable[[str], None] | None = None,
) -> Mapping[str, object]:
    """构造 executor 私有依赖映射，不让进程对象穿透 DTO 或 checkpoint。

    该 composition 统一选择本地/服务检索实现，复用同一 EventBus、审计、
    artifact、共享预算与工具策略。返回值仅供已受 registry 控制的 executor
    使用；调用方不得把其中的 provider、存储或闭包序列化到公开边界。
    """

    retrieval = (
        LocalSQLiteBM25RetrievalProvider(dsn=storage_dsn)
        if settings.storage.kind == "sqlite"
        else PostgreSQLRetrievalProvider(dsn=storage_dsn)
    )
    resolved_workspace = (workspace_root or service_root).resolve()
    telemetry = TelemetryFacade(
        local_sink=event_sink,
        artifact_store=artifact_store,
    )
    default_deployment = settings.model.deployments[settings.model.default_deployment_id]
    model_router_config = ModelRouterConfig(
        default_provider=default_deployment.provider_kind,
        default_model=default_deployment.default_model,
        timeout_seconds=settings.model.timeout_seconds,
        max_tokens_per_call=settings.budget.max_tokens_per_run,
        max_cost_per_call=(
            None
            if settings.budget.max_cost_usd_per_run is None
            else Decimal(str(settings.budget.max_cost_usd_per_run))
        ),
        input_token_price_usd=Decimal("0"),
        output_token_price_usd=Decimal("0"),
        price_source_ref="catalog:fake",
        price_source_version="catalog-v1",
    )
    shared_budget = SharedBudgetRuntime(
        settings=settings,
        registry=registry,
        model_config=model_router_config,
        embedding_input_token_price_usd=Decimal("0"),
        embedding_price_source_ref="catalog:local:mock-small",
        embedding_price_source_version="catalog-v1",
    )
    providers: dict[str, ModelProvider] = {"fake": FakeModelProvider()}
    client_factory: ControlledOpenAIClientFactory | None = None
    if any(
        deployment.provider_kind == "openai-compatible"
        for deployment in settings.model.deployments.values()
    ):
        # fake/local CLI 是高频离线路径，不应仅因 composition 模块加载就承担
        # 真实 provider SDK 的启动成本。只有配置确实选择该 provider 时才导入
        # adapter；公开 DTO、路由和关闭语义保持不变。
        from agent_harness.adapters.models.pydantic_ai import (
            ControlledOpenAIClientFactory,
            PydanticAIModelProvider,
        )

        client_factory = ControlledOpenAIClientFactory(model_settings=settings.model)
        providers["openai-compatible"] = PydanticAIModelProvider(
            provider_id="openai-compatible",
            client_factory=client_factory,
        )

    def resolve_output_schema(agent_id: str):
        """把 Registry 诊断翻译为模型核心唯一允许的 schema preflight 身份。"""

        try:
            return registry.resolve_output_schema(agent_id)
        except RegistryLoadError as exc:
            codes = {detail.code for detail in exc.error_details}
            code = (
                "model.structured_schema_conflict"
                if "registry.output_schema_conflict" in codes
                else "model.structured_schema_unknown"
            )
            raise StructuredSchemaResolutionError(code) from exc

    tool_registry_factory = ToolRegistryFactory(
        settings=settings,
        storage=storage,
        policy=policy,
        audit=audit,
        artifact_store=artifact_store,
        workspace_root=resolved_workspace,
    )
    tool_catalog_resolver = AgentToolCatalogResolver(
        registry=registry,
        tool_registry_factory=tool_registry_factory,
    )
    context_assembly = ContextAssemblyService(
        storage=storage,
        artifact_store=artifact_store,
    )

    model_invocation = ModelInvocationService(
        router=ModelRouter(
            config=model_router_config,
            providers=providers,
            model_settings=settings.model,
        ),
        storage=storage,
        event_bus=event_bus,
        telemetry=telemetry,
        shared_budget=shared_budget,
        agent_policy_resolver=lambda agent_id: registry.get(agent_id).model_policy,
        policy_engine=policy,
        stream_timing_observer=stream_timing_observer,
        output_schema_resolver=resolve_output_schema,
        tool_catalog_resolver=tool_catalog_resolver,
    )
    embedding_invocation = EmbeddingInvocationService(
        provider=LocalEmbeddingProvider(cache=StorageEmbeddingCache(storage)),
        storage=storage,
        event_bus=event_bus,
        telemetry=telemetry,
        shared_budget=shared_budget,
        input_token_price_usd=Decimal("0"),
        price_source_ref="catalog:local:mock-small",
        price_source_version="catalog-v1",
    )

    def resolve_tool_registry(agent_id: str, tool_name: str) -> ToolRegistry:
        """每次工具调用都重新读取 descriptor allowlist 并构造受策略保护的 Registry。"""

        allowed_tools = tuple(registry.get(agent_id).tool_policy.allowed_tools)
        return tool_registry_factory(
            allowed_tools=allowed_tools,
            requested_tool_name=tool_name,
        )

    def resolve_loop_limits(agent_id: str):
        """返回完整Agent循环maxima；缺失时不为legacy Agent合成默认值。"""

        return registry.get(agent_id).model_tool_loop

    model_tool_loop = ModelToolLoopService(
        model_turns=model_invocation,
        tool_catalog_resolver=tool_catalog_resolver,
        tool_registry_resolver=resolve_tool_registry,
        context_assembly=context_assembly,
        loop_limits_resolver=resolve_loop_limits,
        agent_model_policy_resolver=lambda agent_id: registry.get(agent_id).model_policy,
        approval_store=ModelToolLoopApprovalStore(
            storage=storage,
            artifact_store=artifact_store,
        ),
        loop_events=ModelToolLoopEventProducer(storage=storage, event_bus=event_bus),
        storage=storage,
        artifact_store=artifact_store,
    )
    return {
        "artifact_store": artifact_store,
        "context_assembly": context_assembly,
        "embedding_invocation": embedding_invocation,
        "model_invocation": model_invocation,
        "model_tool_loop": model_tool_loop,
        "shared_budget": shared_budget,
        "retrieval_provider": retrieval,
        "telemetry": telemetry,
        "tool_registry_factory": tool_registry_factory,
        "service_root": service_root.resolve(),
    }
