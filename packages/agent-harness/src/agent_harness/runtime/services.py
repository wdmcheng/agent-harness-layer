"""为业务 executor 装配 provider-neutral 进程内服务。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

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
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.observability import TelemetryFacade
from agent_harness.policy import PolicyEngine
from agent_harness.registry import AgentRegistry
from agent_harness.retrieval import (
    LocalSQLiteBM25RetrievalProvider,
    PostgreSQLRetrievalProvider,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.tools import ToolRegistry, WorkspacePolicy
from agent_harness.tools.cli_runtime import builtin_tools


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
) -> Mapping[str, object]:
    """构造 executor 私有依赖映射，不让进程对象穿透 DTO 或 checkpoint。

    该 composition 统一选择本地/服务检索实现，复用同一 EventBus、审计、
    artifact、共享预算与工具策略。返回值仅供已受 registry 控制的 executor
    使用；调用方不得把其中的 provider、存储或闭包序列化到公开边界。
    """

    if settings.model.provider != "fake":
        raise ValueError(
            "example execution requires the fake model provider; "
            "configure a provider adapter before selecting another provider"
        )
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
    model_router_config = ModelRouterConfig(
        default_provider="fake",
        default_model=settings.model.default_model or "fake-basic",
        timeout_seconds=settings.model.timeout_seconds,
        max_tokens_per_call=settings.budget.max_tokens_per_run,
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
    model_invocation = ModelInvocationService(
        router=ModelRouter(
            config=model_router_config,
            providers={"fake": FakeModelProvider()},
        ),
        storage=storage,
        event_bus=event_bus,
        telemetry=telemetry,
        shared_budget=shared_budget,
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
    return {
        "artifact_store": artifact_store,
        "context_assembly": ContextAssemblyService(
            storage=storage,
            artifact_store=artifact_store,
        ),
        "embedding_invocation": embedding_invocation,
        "model_invocation": model_invocation,
        "shared_budget": shared_budget,
        "retrieval_provider": retrieval,
        "telemetry": telemetry,
        "tool_registry_factory": ToolRegistryFactory(
            settings=settings,
            storage=storage,
            policy=policy,
            audit=audit,
            artifact_store=artifact_store,
            workspace_root=resolved_workspace,
        ),
        "service_root": service_root.resolve(),
    }
