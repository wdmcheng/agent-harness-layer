"""Agent Harness 的命令行入口。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agent_harness.artifacts import FileArtifactStore
from agent_harness.config import SettingsLoadError, load_settings
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.registry import AgentRegistry, RegistryLoadError
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage, run_migrations, storage_dsn_from_settings
from agent_harness.storage.diagnostics import (
    eval_directory_status,
    migration_revision,
    observability_status,
    redis_status,
)

app = typer.Typer(no_args_is_help=True)
agents_app = typer.Typer(no_args_is_help=True)
app.add_typer(agents_app, name="agents")


@app.callback()
def cli_root() -> None:
    """开发者和维护者共用的本地命令集合。"""


@app.command()
def doctor(
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
) -> None:
    """校验 profile 配置，并按 profile 类型报告本地或 service 依赖状态。"""

    try:
        settings = load_settings(profile=profile, profiles_dir=profiles_dir)
    except SettingsLoadError as exc:
        for error in exc.errors:
            field = f" field={error.field_path}" if error.field_path else ""
            hint = f" hint={error.hint}" if error.hint else ""
            typer.echo(f"{error.code}:{field} {error.message}{hint}", err=True)
        raise typer.Exit(1) from exc

    key_status = "api key required" if settings.model.requires_api_key else "api key not required"
    typer.echo(f"profile: {settings.profile}")
    typer.echo(f"storage: {settings.storage.kind}")
    typer.echo(f"queue: {settings.queue.kind}")
    typer.echo(f"policy: {settings.policy.provider}")
    typer.echo(
        f"identity: {settings.identity.default.tenant_id}/{settings.identity.default.user_id}"
    )
    typer.echo(f"model: {settings.model.provider} ({key_status})")
    revision = migration_revision(settings, storage_dsn=storage_dsn)
    typer.echo(f"migration: {revision or 'not initialized'}")
    redis_ok, redis_message = redis_status(settings)
    typer.echo(f"redis: {redis_message}")
    observability_ok, observability_message = observability_status(settings)
    typer.echo(f"observability: {settings.observability.kind}")
    typer.echo(f"observability sink: {observability_message}")
    eval_ok, eval_message, eval_directory = eval_directory_status(profiles_dir)
    typer.echo(f"eval directory: {eval_directory} ({eval_message})")

    if settings.profile == "service" and (
        revision is None or not redis_ok or not observability_ok or not eval_ok
    ):
        raise typer.Exit(1)


@app.command()
def run(
    agent_id: str,
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
    events_path: Annotated[Path | None, typer.Option("--events-path")] = None,
    agents_dir: Annotated[Path, typer.Option("--agents-dir")] = Path(
        "templates/service-app/agents"
    ),
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
) -> None:
    """运行内置 fake agent，验证 runtime、storage 和 event seam。"""

    try:
        settings = load_settings(profile=profile, profiles_dir=profiles_dir)
    except SettingsLoadError as exc:
        for error in exc.errors:
            field = f" field={error.field_path}" if error.field_path else ""
            hint = f" hint={error.hint}" if error.hint else ""
            typer.echo(f"{error.code}:{field} {error.message}{hint}", err=True)
        raise typer.Exit(1) from exc

    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    try:
        AgentRegistry.load_from_directory(agents_dir).get(agent_id)
    except RegistryLoadError as exc:
        for error in exc.error_details:
            field = f" field={error.field_path}" if error.field_path else ""
            hint = f" hint={error.hint}" if error.hint else ""
            typer.echo(f"{error.code}:{field} {error.message}{hint}", err=True)
        raise typer.Exit(1) from exc

    run_migrations(resolved_dsn)
    resolved_events_path = events_path
    if resolved_events_path is None:
        resolved_events_path = Path(settings.observability.path or ".agent-harness/traces.jsonl")
    artifact_root = Path(settings.storage.root or ".agent-harness/local") / "artifacts"
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    orchestrator = RunOrchestrator(
        storage=storage,
        # CLI 也带 artifact store；否则本地命令路径会绕过“大 payload 只留
        # payload_ref”的事件规则。
        event_bus=EventBus(
            sink=LocalJsonlEventSink(resolved_events_path),
            artifact_store=FileArtifactStore(artifact_root),
        ),
        identity=settings.identity.default,
    )

    import asyncio

    try:
        result = asyncio.run(
            orchestrator.start_run(
                agent_id=agent_id,
                input={"source": "cli"},
                idempotency_key=idempotency_key,
            )
        )
    finally:
        asyncio.run(storage.dispose())

    typer.echo(f"run_id: {result.run_id}")
    typer.echo(f"status: {result.status.value}")
    typer.echo(f"terminal_event: {result.terminal_event}")


@agents_app.command("list")
def list_agents(
    agents_dir: Annotated[Path, typer.Option("--agents-dir")] = Path(
        "templates/service-app/agents"
    ),
) -> None:
    """列出 registry 中已配置的 agent public descriptor。"""

    try:
        registry = AgentRegistry.load_from_directory(agents_dir)
    except RegistryLoadError as exc:
        for error in exc.error_details:
            field = f" field={error.field_path}" if error.field_path else ""
            hint = f" hint={error.hint}" if error.hint else ""
            typer.echo(f"{error.code}:{field} {error.message}{hint}", err=True)
        raise typer.Exit(1) from exc

    for descriptor in registry.list_agents():
        typer.echo(f"{descriptor.agent_id}\t{descriptor.name}\t{descriptor.model_policy.provider}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
