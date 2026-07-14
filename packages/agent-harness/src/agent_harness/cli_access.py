"""PolicyEngine 与 HITL approval 的 CLI 子命令。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agent_harness.approvals import ApprovalService
from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.cli_shared import (
    event_path,
    load_settings_or_exit,
    policy_engine,
    require_local_state_ready_or_exit,
    require_schema_or_exit,
)
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.policy import PolicyCheck
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator
from agent_harness.runtime.services import build_agent_execution_services
from agent_harness.storage import SQLAlchemyStorage, storage_dsn_from_settings

policy_app = typer.Typer(no_args_is_help=True)
approvals_app = typer.Typer(no_args_is_help=True)


def register_access_commands(root: typer.Typer) -> None:
    """把 access 相关子命令挂到根 CLI。"""

    root.add_typer(policy_app, name="policy")
    root.add_typer(approvals_app, name="approvals")


@policy_app.command("check")
def check_policy(
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
    action: Annotated[str, typer.Option("--action")] = "run.read",
    resource: Annotated[str, typer.Option("--resource")] = "run",
) -> None:
    """用当前 profile 的 PolicyEngine 检查一次动作授权。"""

    settings = load_settings_or_exit(profile, profiles_dir)
    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    require_schema_or_exit(resolved_dsn)
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    audit = AuditService(storage=storage)
    engine = policy_engine(settings, storage, audit, profiles_dir=profiles_dir)

    import asyncio

    try:
        result = asyncio.run(
            engine.evaluate(
                PolicyCheck(
                    actor=settings.identity.default,
                    action=action,
                    resource=resource,
                    context={"source": "cli"},
                )
            )
        )
    finally:
        asyncio.run(storage.dispose())

    typer.echo(f"decision: {result.decision}")
    typer.echo(f"reason: {result.reason}")


@approvals_app.command("list")
def list_approvals(
    run_id: str,
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
) -> None:
    """列出指定 run 的 HITL approvals。"""

    settings = load_settings_or_exit(profile, profiles_dir)
    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    require_schema_or_exit(resolved_dsn)
    resolved_events_path = event_path(settings, None)
    require_local_state_ready_or_exit(event_paths=(resolved_events_path,))
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    event_bus = EventBus(sink=LocalJsonlEventSink(resolved_events_path))
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
        identity=settings.identity.default,
    )
    audit = AuditService(storage=storage)
    service = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        audit=audit,
    )

    import asyncio

    async def _list() -> None:
        try:
            rows = await service.list_for_run(actor=settings.identity.default, run_id=run_id)
            columns = (
                "approval_id",
                "status",
                "action",
                "resource",
                "reason",
                "tenant_id",
                "agent_id",
                "run_id",
                "trace_id",
                "request_id",
            )
            typer.echo("\t".join(columns))
            for row in rows:
                typer.echo(
                    "\t".join(
                        str(value)
                        for value in (
                            row.approval_id,
                            row.status,
                            row.action,
                            row.resource,
                            row.reason,
                            row.tenant_id,
                            row.agent_id,
                            row.run_id,
                            row.trace_id or "",
                            row.request_id or "",
                        )
                    )
                )
        finally:
            await storage.dispose()

    asyncio.run(_list())


@approvals_app.command("approve")
def approve(
    approval_id: str,
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
    events_path: Annotated[Path | None, typer.Option("--events-path")] = None,
    agents_dir: Annotated[Path, typer.Option("--agents-dir")] = Path(
        "templates/service-app/agents"
    ),
    comment: Annotated[str | None, typer.Option("--comment")] = None,
) -> None:
    """批准一个 waiting approval 并恢复对应 run。"""

    resolve_approval(
        decision="approved",
        approval_id=approval_id,
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        events_path=events_path,
        agents_dir=agents_dir,
        comment=comment,
    )


@approvals_app.command("deny")
def deny(
    approval_id: str,
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
    events_path: Annotated[Path | None, typer.Option("--events-path")] = None,
    agents_dir: Annotated[Path, typer.Option("--agents-dir")] = Path(
        "templates/service-app/agents"
    ),
    comment: Annotated[str | None, typer.Option("--comment")] = None,
) -> None:
    """拒绝一个 waiting approval 并让对应 run 失败。"""

    resolve_approval(
        decision="denied",
        approval_id=approval_id,
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        events_path=events_path,
        agents_dir=agents_dir,
        comment=comment,
    )


def resolve_approval(
    *,
    decision: str,
    approval_id: str,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
    events_path: Path | None,
    agents_dir: Path,
    comment: str | None,
) -> None:
    """解析本地依赖后，通过 ApprovalService 完成 approve/deny。"""

    settings = load_settings_or_exit(profile, profiles_dir)
    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    require_schema_or_exit(resolved_dsn)
    resolved_events_path = event_path(settings, events_path)
    require_local_state_ready_or_exit(event_paths=(resolved_events_path,))
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    service_root = agents_dir.resolve().parent
    configured_artifact_root = Path(settings.storage.root or ".agent-harness/local") / "artifacts"
    artifact_root = (
        Path(events_path).resolve().parent / "artifacts"
        if events_path is not None
        else (
            configured_artifact_root
            if configured_artifact_root.is_absolute()
            else service_root / configured_artifact_root
        )
    )
    event_sink = LocalJsonlEventSink(resolved_events_path)
    artifact_store = FileArtifactStore(artifact_root)
    event_bus = EventBus(
        sink=event_sink,
        artifact_store=artifact_store,
    )
    audit = AuditService(storage=storage)
    policy = policy_engine(settings, storage, audit, profiles_dir=profiles_dir)
    registry = AgentRegistry.load_from_directory(agents_dir)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
        identity=settings.identity.default,
        executor_resolver=registry.resolve_executor,
        executor_services=build_agent_execution_services(
            settings=settings,
            storage=storage,
            storage_dsn=resolved_dsn,
            policy=policy,
            audit=audit,
            event_sink=event_sink,
            artifact_store=artifact_store,
            service_root=service_root,
        ),
    )
    service = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        audit=audit,
    )

    import asyncio

    async def _resolve() -> None:
        try:
            approval = await service.get_by_id(
                actor=settings.identity.default,
                approval_id=approval_id,
                audit_read=False,
            )
            await policy.require_allowed(
                PolicyCheck(
                    actor=settings.identity.default,
                    action="approval.resolve",
                    resource=f"run:{approval.run_id}:approval:{approval_id}",
                    context={"decision": decision, "source": "cli"},
                )
            )
            resolver = service.approve if decision == "approved" else service.deny
            result = await resolver(
                actor=settings.identity.default,
                run_id=approval.run_id,
                approval_id=approval_id,
                comment=comment,
            )
            typer.echo(f"approval: {result.approval.status}")
            if result.run is not None:
                typer.echo(f"run: {result.run.status.value}")
        finally:
            await storage.dispose()

    asyncio.run(_resolve())
