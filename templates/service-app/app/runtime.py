"""service-app 的 runtime component 构造器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_harness.artifacts import FileArtifactStore
from agent_harness.config import load_settings
from agent_harness.events import EventBus, EventSink, LocalJsonlEventSink
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
    return RuntimeComponents(
        storage=storage,
        event_sink=event_sink,
        orchestrator=RunOrchestrator(
            storage=storage,
            event_bus=event_bus,
            identity=settings.identity.default,
        ),
        registry=AgentRegistry.load_from_directory(service_root / "agents"),
    )
