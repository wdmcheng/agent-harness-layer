"""Agent Harness 的命令行入口。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast
from uuid import uuid4

import typer

from agent_harness import cli_local_state, cli_shared
from agent_harness.approvals import ApprovalService
from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.cli_access import register_access_commands
from agent_harness.cli_eval_commands import register_eval_commands
from agent_harness.cli_eval_experiment import register_eval_experiment_commands
from agent_harness.cli_events import register_event_commands
from agent_harness.delegation import AgentDelegationModule, DelegationService
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.policy import InputGuardrail, PolicyCheck, PolicyDeniedError
from agent_harness.registry import AgentRegistry, RegistryLoadError
from agent_harness.runtime import RunOrchestrator, RunTraceError
from agent_harness.runtime._continuation_context import RunInputProvenance
from agent_harness.runtime.services import (
    build_agent_execution_services,
    build_registry_tool_catalog_descriptors,
    close_agent_execution_services,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.scaffold import ScaffoldError, scaffold_agent_package
from agent_harness.storage import SQLAlchemyStorage, storage_dsn_from_settings
from agent_harness.storage import diagnostics as storage_diagnostics
from agent_harness.tools import ToolCallRequest, ToolRuntimeContext, WorkspacePolicy
from agent_harness.tools.cli_runtime import call_and_record_tool, visible_tool_descriptors

app = typer.Typer(no_args_is_help=True)
agents_app = typer.Typer(no_args_is_help=True)
tools_app = typer.Typer(no_args_is_help=True)
eval_app = typer.Typer(no_args_is_help=True)
eval_experiment_app = typer.Typer(no_args_is_help=True)
scaffold_app = typer.Typer(no_args_is_help=True)
app.add_typer(agents_app, name="agents")
app.add_typer(tools_app, name="tools")
app.add_typer(eval_app, name="eval")
eval_app.add_typer(eval_experiment_app, name="experiment")
app.add_typer(scaffold_app, name="scaffold")
register_access_commands(app)
register_event_commands(app)
register_eval_commands(eval_app)
register_eval_experiment_commands(eval_experiment_app)
cli_local_state.register_local_state_commands(app)

# 保留原根模块导入 seam；实现与注册归属 cli_local_state。
migrate_local_state_command = cli_local_state.migrate_local_state_command


@app.callback()
def cli_root() -> None:
    """开发者和维护者共用的本地命令集合。"""


def _exit_registry_error(exc: RegistryLoadError) -> NoReturn:
    """把 registry 诊断稳定投影到 CLI，并以统一退出码结束当前命令。"""

    for error in exc.error_details:
        field = f" field={error.field_path}" if error.field_path else ""
        hint = f" hint={error.hint}" if error.hint else ""
        typer.echo(f"{error.code}:{field} {error.message}{hint}", err=True)
    raise typer.Exit(1) from exc


@scaffold_app.command("agent")
def scaffold_agent(
    agent_id: str,
    agents_dir: Annotated[Path | None, typer.Option("--agents-dir")] = None,
) -> None:
    """原子生成一个离线、安全、可由当前 registry 加载的 Agent package。"""

    try:
        result = scaffold_agent_package(agent_id, agents_dir=agents_dir)
    except ScaffoldError as exc:
        hint = f" hint={exc.hint}" if exc.hint else ""
        typer.echo(f"{exc.code}: {exc.message}{hint}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"created: {result.relative_path}")


@app.command()
def doctor(
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
) -> None:
    """校验 profile 配置，并按 profile 类型报告本地或 service 依赖状态。"""

    settings = cli_shared.load_settings_or_exit(profile, profiles_dir)

    key_status = "api key required" if settings.model.requires_api_key else "api key not required"
    typer.echo(f"profile: {settings.profile}")
    typer.echo(f"storage: {settings.storage.kind}")
    typer.echo(f"queue: {settings.queue.kind}")
    typer.echo(f"policy: {settings.policy.provider}")
    typer.echo(
        f"identity: {settings.identity.default.tenant_id}/{settings.identity.default.user_id}"
    )
    typer.echo(f"model: {settings.model.provider} ({key_status})")
    revision = storage_diagnostics.migration_revision(settings, storage_dsn=storage_dsn)
    typer.echo(f"migration: {revision or 'not initialized'}")
    redis_ok, redis_message = storage_diagnostics.redis_status(settings)
    typer.echo(f"redis: {redis_message}")
    observability_ok, observability_message = storage_diagnostics.observability_status(settings)
    typer.echo(f"observability: {settings.observability.kind}")
    typer.echo(f"observability sink: {observability_message}")
    for provider_status in storage_diagnostics.observability_provider_statuses(settings):
        typer.echo(f"observability provider: {provider_status}")
    eval_ok, eval_message, eval_directory = storage_diagnostics.eval_directory_status(profiles_dir)
    typer.echo(f"eval directory: {eval_directory} ({eval_message})")
    for extension_status in storage_diagnostics.retrieval_extension_statuses(
        settings, storage_dsn=storage_dsn
    ):
        typer.echo(
            "retrieval extension "
            f"{storage_diagnostics.format_retrieval_extension_status(extension_status)}"
        )

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
    trace_id: Annotated[str | None, typer.Option("--trace-id")] = None,
    prompt: Annotated[str | None, typer.Option("--prompt")] = None,
) -> None:
    """通过 registry executor 运行 Agent，并保留 runtime、storage 与 event evidence。"""

    settings = cli_shared.load_settings_or_exit(profile, profiles_dir)
    try:
        # 未知目标必须在 storage、artifact 恢复或动态 import 之前拒绝；这个探针只读
        # registry YAML，完整 registry 验证仍在后续 composition 中执行。
        AgentRegistry.require_declared_agent(agents_dir, agent_id)
    except RegistryLoadError as exc:
        _exit_registry_error(exc)

    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    resolved_events_path = cli_shared.event_path(settings, events_path)
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
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    audit = AuditService(storage=storage)
    policy = cli_shared.policy_engine(settings, storage, audit, profiles_dir=profiles_dir)
    event_sink = LocalJsonlEventSink(resolved_events_path)
    artifact_store = FileArtifactStore(artifact_root)
    event_bus = EventBus(
        sink=event_sink,
        artifact_store=artifact_store,
        capacity_storage=storage,
    )
    registry: AgentRegistry | None = None
    registry_error: RegistryLoadError | None = None
    try:
        registry = AgentRegistry.load_from_directory(
            agents_dir,
            model_settings=settings.model,
            tool_catalog_descriptors=build_registry_tool_catalog_descriptors(
                settings=settings,
                storage=storage,
                policy=policy,
                audit=audit,
                artifact_store=artifact_store,
                workspace_root=service_root,
            ),
        )
        registry.get(agent_id)
    except RegistryLoadError as exc:
        registry_error = exc
    input_payload: dict[str, Any] = {}
    if prompt is not None:
        input_payload["prompt"] = prompt
    provenance = RunInputProvenance(source="cli")

    async def _run():
        """在唯一事件循环内校验启动条件、执行并关闭全部异步资源。"""

        try:
            if registry_error is not None:
                raise registry_error
            if registry is None:  # pragma: no cover - 上述装载分支已穷尽
                raise RuntimeError("agent registry initialization did not complete")
            # Registry 是纯配置边界，先拒绝未知 Agent；确认目标存在后才允许
            # storage/local-state 门禁或 runtime composition 产生运行时副作用。
            cli_shared.require_schema_or_exit(resolved_dsn)
            cli_shared.require_local_state_ready_or_exit(event_paths=(resolved_events_path,))
            executor_services = build_agent_execution_services(
                settings=settings,
                storage=storage,
                storage_dsn=resolved_dsn,
                policy=policy,
                audit=audit,
                event_sink=event_sink,
                event_bus=event_bus,
                artifact_store=artifact_store,
                service_root=service_root,
                registry=registry,
            )
            try:
                orchestrator = RunOrchestrator(
                    storage=storage,
                    # CLI 也带 artifact store；否则本地命令路径会绕过“大 payload 只留
                    # payload_ref”的事件规则。
                    event_bus=event_bus,
                    identity=settings.identity.default,
                    executor_resolver=registry.resolve_executor,
                    executor_services=executor_services,
                )
                delegation_service = DelegationService(
                    storage=storage,
                    registry=registry,
                    policy=policy,
                    event_bus=event_bus,
                    orchestrator=orchestrator,
                    shared_budget=cast(SharedBudgetRuntime, executor_services["shared_budget"]),
                    mode="local",
                )
                orchestrator.bind_execution_service(
                    "agent.delegate",
                    AgentDelegationModule(delegation_service),
                )
                approval_service = ApprovalService(
                    storage=storage,
                    event_bus=event_bus,
                    orchestrator=orchestrator,
                    audit=audit,
                )
                preflight_trace = await orchestrator.prepare_trace(
                    agent_id=agent_id,
                    idempotency_key=idempotency_key,
                    identity=settings.identity.default,
                    trace_id=trace_id,
                )
                async with orchestrator.coordinate_run_submission(
                    agent_id=agent_id,
                    idempotency_key=idempotency_key,
                    trace_id=preflight_trace,
                    identity=settings.identity.default,
                ):
                    canonical_trace = await orchestrator.prepare_trace(
                        agent_id=agent_id,
                        idempotency_key=idempotency_key,
                        identity=settings.identity.default,
                        trace_id=preflight_trace,
                    )
                    checkpoint_state = None
                    decision = None
                    if not canonical_trace.replays_existing:
                        guardrail = InputGuardrail(policy=policy, audit=audit)
                        decision = await guardrail.check(
                            actor=settings.identity.default,
                            agent_id=agent_id,
                            input=input_payload,
                            provenance=provenance,
                        )
                        if decision.decision == "deny":
                            raise PolicyDeniedError(decision.reason)
                        if decision.decision == "require_approval":
                            checkpoint_state = {
                                "reason": decision.reason,
                                "policy": decision.to_payload(),
                            }
                    run_result = await orchestrator._start_run_with_provenance(  # pyright: ignore[reportPrivateUsage]
                        agent_id=agent_id,
                        input=input_payload,
                        idempotency_key=idempotency_key,
                        checkpoint_state=checkpoint_state,
                        identity=settings.identity.default,
                        trace_id=canonical_trace,
                        provenance=provenance,
                        pre_run_events=(
                            [(CanonicalEventType.INPUT_GUARDRAIL_CHECKED, decision.to_payload())]
                            if decision is not None
                            else None
                        ),
                    )
                    if (
                        decision is not None
                        and checkpoint_state is not None
                        and run_result.resume_token is not None
                    ):
                        await approval_service.require_approval(
                            actor=settings.identity.default,
                            run_id=run_result.run_id,
                            agent_id=agent_id,
                            action="input.prompt_injection",
                            resource=f"agent:{agent_id}:input",
                            reason=decision.reason,
                            resume_token=run_result.resume_token,
                        )
                    return run_result
            finally:
                await close_agent_execution_services(executor_services)
        finally:
            await storage.dispose()

    try:
        result = asyncio.run(_run())
    except RegistryLoadError as exc:
        _exit_registry_error(exc)
    except PolicyDeniedError as exc:
        typer.echo(f"policy.denied: {exc}", err=True)
        raise typer.Exit(1) from exc
    except RunTraceError as exc:
        typer.echo(f"{exc.code}: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"run_id: {result.run_id}")
    typer.echo(f"status: {result.status.value}")
    typer.echo(f"terminal_event: {result.terminal_event}")


@agents_app.command("list")
def list_agents(
    agents_dir: Annotated[Path, typer.Option("--agents-dir")] = Path(
        "templates/service-app/agents"
    ),
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
) -> None:
    """按当前身份和策略列出 registry 中可见的 agent public descriptor。"""

    settings = cli_shared.load_settings_or_exit(profile, profiles_dir)
    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    cli_shared.require_schema_or_exit(resolved_dsn)
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    audit = AuditService(storage=storage)
    policy = cli_shared.policy_engine(settings, storage, audit, profiles_dir=profiles_dir)
    service_root = agents_dir.resolve().parent
    configured_artifact_root = Path(settings.storage.root or ".agent-harness/local") / "artifacts"
    artifact_root = (
        configured_artifact_root
        if configured_artifact_root.is_absolute()
        else service_root / configured_artifact_root
    )
    try:
        registry = AgentRegistry.load_from_directory(
            agents_dir,
            model_settings=settings.model,
            tool_catalog_descriptors=build_registry_tool_catalog_descriptors(
                settings=settings,
                storage=storage,
                policy=policy,
                audit=audit,
                artifact_store=FileArtifactStore(artifact_root),
                workspace_root=service_root,
            ),
        )
    except RegistryLoadError as exc:
        asyncio.run(storage.dispose())
        _exit_registry_error(exc)

    async def _check_visibility() -> None:
        """在同步 Typer 命令中桥接异步策略检查，避免直接绕过授权门面。"""

        await policy.require_allowed(
            PolicyCheck(
                actor=settings.identity.default,
                action="agents.list",
                resource="agents",
                context={"source": "cli"},
            )
        )

    try:
        asyncio.run(_check_visibility())
    except PolicyDeniedError as exc:
        typer.echo(f"policy.denied: {exc}", err=True)
        raise typer.Exit(1) from exc
    finally:
        asyncio.run(storage.dispose())

    for descriptor in registry.list_agents():
        typer.echo(f"{descriptor.agent_id}\t{descriptor.name}\t{descriptor.model_policy.provider}")


@tools_app.command("list")
def list_tools(
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
) -> None:
    """列出本地可用工具；这不是 HTTP tools route。"""

    settings = cli_shared.load_settings_or_exit(profile, profiles_dir)
    workspace_policy = WorkspacePolicy(
        root=workspace,
        ignore_file=settings.tools.workspace.ignore_file,
    )
    artifact_root = Path(settings.storage.root or ".agent-harness/local") / "artifacts"
    descriptors = asyncio.run(
        visible_tool_descriptors(
            settings=settings,
            workspace_policy=workspace_policy,
            artifact_store=FileArtifactStore(artifact_root),
            actor=settings.identity.default,
            profiles_dir=profiles_dir,
        )
    )
    for descriptor in descriptors:
        typer.echo(json.dumps(descriptor.to_payload(), ensure_ascii=False))


@tools_app.command("call")
def call_tool(
    tool_name: str,
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    workspace: Annotated[Path, typer.Option("--workspace")] = Path("."),
    arguments: Annotated[str, typer.Option("--arguments")] = "{}",
    agent_id: Annotated[str, typer.Option("--agent-id")] = "cli.tool",
) -> None:
    """通过 CLI 调用内置工具，输出 ToolCallResult JSON。"""

    settings = cli_shared.load_settings_or_exit(profile, profiles_dir)
    try:
        loaded_arguments: Any = json.loads(arguments)
    except json.JSONDecodeError as exc:
        typer.echo(f"tool.schema_validation_failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not isinstance(loaded_arguments, dict):
        typer.echo("tool.schema_validation_failed: arguments must be a JSON object", err=True)
        raise typer.Exit(1)
    parsed_arguments = cast(dict[str, Any], loaded_arguments)

    workspace_policy = WorkspacePolicy(
        root=workspace,
        ignore_file=settings.tools.workspace.ignore_file,
    )
    artifact_root = Path(settings.storage.root or ".agent-harness/local") / "artifacts"
    artifacts = FileArtifactStore(artifact_root)
    request_id = f"req_{uuid4()}"
    trace_id = f"trace_{uuid4()}"
    context = ToolRuntimeContext(
        actor=settings.identity.default,
        agent_id=agent_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    request = ToolCallRequest(
        tool_name=tool_name,
        arguments=parsed_arguments,
        agent_id=agent_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    result = asyncio.run(
        call_and_record_tool(
            request,
            context=context,
            workspace_policy=workspace_policy,
            artifact_store=artifacts,
            settings=settings,
            profiles_dir=profiles_dir,
        )
    )
    typer.echo(json.dumps(result.to_payload(), ensure_ascii=False))
    if result.status != "completed":
        raise typer.Exit(1)


def main() -> None:
    """执行 Typer 根应用。"""

    app()


if __name__ == "__main__":
    main()
