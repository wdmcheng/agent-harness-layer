"""service-app 的 runtime component 构造器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_harness.approvals import ApprovalService
from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.auth import ApiKeyVerifier, StaticTokenVerifier, TokenVerifier
from agent_harness.config import load_settings
from agent_harness.events import EventBus, EventSink, LocalJsonlEventSink
from agent_harness.policy import (
    DatabasePolicyProvider,
    InputGuardrail,
    PolicyEngine,
    YamlPolicyProvider,
)
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage, run_migrations, storage_dsn_from_settings


@dataclass(slots=True)
class RuntimeComponents:
    """API 和 worker 共用的一组 runtime seam。"""

    storage: SQLAlchemyStorage
    event_sink: EventSink
    orchestrator: RunOrchestrator
    registry: AgentRegistry
    auth_verifier: TokenVerifier | None
    policy_engine: PolicyEngine
    input_guardrail: InputGuardrail
    approval_service: ApprovalService

    async def close(self) -> None:
        await self.storage.dispose()


def build_runtime_components(
    *,
    profile: str = "local",
    profiles_dir: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
) -> RuntimeComponents:
    """从 profile 构造 API/worker 共享的 runtime 组件。

    这里允许执行 migration，因为调用方显式启动的是 service/app runtime，而不是
    单纯 import 配置模块。测试可直接注入 orchestrator/event_sink 跳过真实依赖。
    """

    settings = load_settings(profile=profile, profiles_dir=profiles_dir)
    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    run_migrations(resolved_dsn)

    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    resolved_events_path = events_path or Path(
        settings.observability.path or ".agent-harness/traces.jsonl"
    )
    artifact_root = Path(settings.storage.root or ".agent-harness/local") / "artifacts"
    service_root = (
        profiles_dir.parent.parent
        if profiles_dir is not None
        else Path.cwd() / "templates" / "service-app"
    )
    event_sink = LocalJsonlEventSink(resolved_events_path)
    event_bus = EventBus(
        sink=event_sink,
        artifact_store=FileArtifactStore(artifact_root),
    )
    audit = AuditService(storage=storage)
    if settings.policy.provider == "db":
        policy_provider = DatabasePolicyProvider(storage=storage)
    else:
        policy_path = None
        if settings.policy.path is not None:
            configured_path = Path(settings.policy.path)
            policy_path = (
                configured_path if configured_path.is_absolute() else service_root / configured_path
            )
        policy_provider = (
            YamlPolicyProvider.from_path(
                policy_path,
                fallback_require_approval_actions=settings.policy.require_approval_actions,
                fallback_deny_actions=settings.policy.deny_actions,
            )
            if policy_path is not None
            else YamlPolicyProvider(
                require_approval_actions=settings.policy.require_approval_actions,
                deny_actions=settings.policy.deny_actions,
            )
        )
    policy_engine = PolicyEngine(provider=policy_provider, audit=audit)
    auth_verifier: TokenVerifier | None = None
    if settings.auth.dev_bearer_token is not None:
        auth_verifier = StaticTokenVerifier(
            {settings.auth.dev_bearer_token: settings.identity.default}
        )
    elif settings.auth.required or settings.auth.provider == "api-key":
        auth_verifier = ApiKeyVerifier(storage=storage)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
        identity=settings.identity.default,
    )
    input_guardrail = InputGuardrail(policy=policy_engine, audit=audit)
    approval_service = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        audit=audit,
    )
    return RuntimeComponents(
        storage=storage,
        event_sink=event_sink,
        orchestrator=orchestrator,
        registry=AgentRegistry.load_from_directory(service_root / "agents"),
        auth_verifier=auth_verifier,
        policy_engine=policy_engine,
        input_guardrail=input_guardrail,
        approval_service=approval_service,
    )
