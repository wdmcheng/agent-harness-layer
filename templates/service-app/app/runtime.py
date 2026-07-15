"""service-app 的 runtime component 构造器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_harness.adapters.queue import RedisRunQueue
from agent_harness.approvals import ApprovalService
from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.auth import ApiKeyVerifier, StaticTokenVerifier, TokenVerifier
from agent_harness.config import load_settings
from agent_harness.evals import (
    AcceptanceService,
    EvalCaseFactory,
    EvalService,
    ExperimentService,
    RecordedApprovedCaseEvaluator,
    ScoreSink,
)
from agent_harness.events import EventBus, EventSink, LocalJsonlEventSink, PostgreSQLEventSink
from agent_harness.local_state import require_local_state_ready
from agent_harness.observability import TelemetryFacade
from agent_harness.policy import (
    DatabasePolicyProvider,
    InputGuardrail,
    PolicyEngine,
    YamlPolicyProvider,
)
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator, RunQueue
from agent_harness.runtime.services import build_agent_execution_services
from agent_harness.storage import (
    SQLAlchemyStorage,
    require_migration_head,
    storage_dsn_from_settings,
)


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
    eval_service: EvalService
    experiment_service: ExperimentService
    acceptance_service: AcceptanceService
    queue: RunQueue | None = None

    async def close(self) -> None:
        if self.queue is not None:
            await self.queue.close()
        await self.storage.dispose()


def build_runtime_components(
    *,
    profile: str = "local",
    profiles_dir: Path | None = None,
    storage_dsn: str | None = None,
    events_path: Path | None = None,
    workspace_root: Path | None = None,
    artifact_root: Path | None = None,
    local_state_dir: Path | None = None,
) -> RuntimeComponents:
    """从 profile 构造 API/worker 共享组件，旧 schema 必须先显式迁移。"""

    settings = load_settings(profile=profile, profiles_dir=profiles_dir)
    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    require_migration_head(resolved_dsn)
    resolved_events_path = events_path or Path(
        settings.observability.path or ".agent-harness/traces.jsonl"
    )
    service_root = (
        profiles_dir.parent.parent
        if profiles_dir is not None
        else Path.cwd() / "templates" / "service-app"
    )
    configured_artifact_root = Path(settings.storage.root or ".agent-harness/local") / "artifacts"
    resolved_artifact_root = artifact_root or (
        configured_artifact_root
        if configured_artifact_root.is_absolute()
        else service_root / configured_artifact_root
    )
    service_mode = profile == "service"
    resolved_local_state_dir = (
        local_state_dir.expanduser().resolve() if local_state_dir is not None else None
    )
    if service_mode and resolved_local_state_dir is not None:
        raise ValueError("service profile cannot use a local state bundle")
    resolved_scores_path = (
        resolved_local_state_dir / "scores.jsonl"
        if resolved_local_state_dir is not None
        else service_root / "eval-results" / "scores.jsonl"
    )
    if not service_mode:
        require_local_state_ready(
            event_paths=(resolved_events_path,),
            score_paths=(resolved_scores_path,),
            state_dir=resolved_local_state_dir,
        )
    storage = SQLAlchemyStorage.from_dsn(
        resolved_dsn,
        cross_event_loop=service_mode,
    )
    event_sink: EventSink = (
        PostgreSQLEventSink(storage)
        if service_mode
        else LocalJsonlEventSink(
            resolved_events_path,
            state_dir=resolved_local_state_dir,
        )
    )
    queue: RunQueue | None = None
    if service_mode:
        if settings.queue.kind != "redis" or settings.queue.dsn is None:
            raise ValueError("service profile requires a Redis queue DSN")
        queue = RedisRunQueue.from_dsn(settings.queue.dsn)
    artifact_store = FileArtifactStore(resolved_artifact_root)
    event_bus = EventBus(
        sink=event_sink,
        artifact_store=artifact_store,
        capacity_storage=storage if not service_mode else None,
    )
    audit = AuditService(storage=storage)
    if settings.policy.provider == "db":
        policy_provider = DatabasePolicyProvider(storage=storage)
    else:
        # YAML policy 是 local/service 模板默认入口；DB provider 只作为可替换 seam。
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
        # dev token 只映射到 profile 中的默认身份，避免 service-app 自己发明用户体系。
        auth_verifier = StaticTokenVerifier(
            {settings.auth.dev_bearer_token: settings.identity.default}
        )
    elif settings.auth.required or settings.auth.provider == "api-key":
        auth_verifier = ApiKeyVerifier(storage=storage)
    registry = AgentRegistry.load_from_directory(service_root / "agents")
    executor_services = build_agent_execution_services(
        settings=settings,
        storage=storage,
        storage_dsn=resolved_dsn,
        policy=policy_engine,
        audit=audit,
        event_sink=event_sink,
        event_bus=event_bus,
        artifact_store=artifact_store,
        service_root=service_root,
        workspace_root=workspace_root,
    )
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
        identity=settings.identity.default,
        executor_resolver=registry.resolve_executor,
        executor_services=executor_services,
        queue=queue,
    )
    input_guardrail = InputGuardrail(policy=policy_engine, audit=audit)
    approval_service = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        audit=audit,
        queue=queue,
    )
    eval_service = EvalService(
        storage=storage,
        factory=EvalCaseFactory(),
        score_sink=ScoreSink(
            local_path=resolved_scores_path,
            telemetry=TelemetryFacade(local_sink=event_sink),
            state_dir=resolved_local_state_dir,
        ),
        drafts_dir=service_root / "eval-cases" / "drafts",
        approved_dir=service_root / "eval-cases" / "approved",
        audit=audit,
    )
    experiment_service = ExperimentService(
        storage=storage,
        evaluator=RecordedApprovedCaseEvaluator(storage=storage),
    )
    acceptance_service = AcceptanceService(
        storage=storage,
        experiments=experiment_service,
        policy=policy_engine,
    )
    return RuntimeComponents(
        storage=storage,
        event_sink=event_sink,
        orchestrator=orchestrator,
        registry=registry,
        auth_verifier=auth_verifier,
        policy_engine=policy_engine,
        input_guardrail=input_guardrail,
        approval_service=approval_service,
        eval_service=eval_service,
        experiment_service=experiment_service,
        acceptance_service=acceptance_service,
        queue=queue,
    )
