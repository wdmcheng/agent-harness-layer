"""真实模型增量 smoke 的授权门禁、composition 与资源收口。"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from scripts.live_model_stream_contract import (
    AUTHORIZED_ENV,
    STREAM_OPT_IN_ENV,
    SmokeResult,
    classify_incomplete_run,
    make_result,
)
from scripts.live_model_stream_probe import (
    LiveStreamSmokeExecutor,
    StreamTimingRecorder,
    measure_existing_sse_first_frame,
    service_app_factory,
)

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.config import SettingsLoadError, load_settings
from agent_harness.config.model_endpoints import (
    ResolvedModelDeployment,
    resolve_model_deployment,
)
from agent_harness.config.schemas import HarnessSettings
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import ModelRequest
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime import RunOrchestrator, RunStatus
from agent_harness.runtime.services import (
    build_agent_execution_services,
    close_agent_execution_services,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations


def _classify_local_state(
    *,
    executor: LiveStreamSmokeExecutor | None,
    recorder: StreamTimingRecorder,
    existing_event_first_frame_ms: int | None,
) -> tuple[SmokeResult, int]:
    """把 setup、runtime、probe 或 cleanup 异常压缩为同一安全本地失败证据。"""

    return classify_incomplete_run(
        response_observed=executor is not None and executor.response is not None,
        error=executor.error if executor is not None else None,
        existing_event_first_frame_ms=existing_event_first_frame_ms,
        provider_first_delta_ms=recorder.provider_first_delta_ms,
        committed_first_delta_ms=recorder.committed_first_delta_ms,
        client_first_delta_ms=recorder.client_first_delta_ms,
    )


def _classify_local_failure(
    *,
    executor: LiveStreamSmokeExecutor | None,
    recorder: StreamTimingRecorder,
    existing_event_first_frame_ms: int | None,
) -> tuple[SmokeResult, int]:
    """本地编排失败强制归入合同失败，同时保留已经观察到的 provider 事实。"""

    error = executor.error if executor is not None else None
    provider_called = bool(
        (executor is not None and executor.response is not None)
        or (error is not None and error.provider_called)
        or recorder.provider_first_delta_ms is not None
        or recorder.committed_first_delta_ms is not None
        or recorder.client_first_delta_ms is not None
    )
    return make_result(
        status="failed",
        provider_called=provider_called,
        existing_event_first_frame_ms=existing_event_first_frame_ms,
        provider_first_delta_ms=recorder.provider_first_delta_ms,
        committed_first_delta_ms=recorder.committed_first_delta_ms,
        client_first_delta_ms=recorder.client_first_delta_ms,
        reason_code="contract_failure",
    ), 1


async def _close_runtime(
    *,
    services: Mapping[str, object] | None,
    storage: SQLAlchemyStorage | None,
) -> None:
    """分别尝试关闭 composition 与 storage，避免前者失败跳过数据库释放。"""

    try:
        if services is not None:
            await close_agent_execution_services(services)
    finally:
        if storage is not None:
            await storage.dispose()


async def _run_authorized(
    *,
    profile: str,
    profiles_dir: Path,
    settings: HarnessSettings,
    resolved: ResolvedModelDeployment,
) -> tuple[SmokeResult, int]:
    """执行唯一受控调用；任一本地异常都经安全状态机形成 artifact。"""

    deployment_id = settings.model.default_deployment_id
    request = ModelRequest(
        deployment_id=deployment_id,
        provider=resolved.provider_kind,
        capability="text_stream",
        prompt="Reply with OK.",
        max_output_tokens=8,
    )
    policy = AgentModelPolicy(
        deployment_id=deployment_id,
        provider=resolved.provider_kind,
        allowed_models=list(resolved.allowed_models),
        default_model=resolved.default_model,
        fallback_models=[],
    )
    recorder = StreamTimingRecorder()
    executor: LiveStreamSmokeExecutor | None = None
    existing_event_first_frame_ms: int | None = None
    orchestration_failed = False
    try:
        with tempfile.TemporaryDirectory(prefix="agent-harness-live-stream-smoke-") as temp_root:
            temp = Path(temp_root)
            dsn = f"sqlite+aiosqlite:///{temp / 'runtime.db'}"
            storage: SQLAlchemyStorage | None = None
            services: Mapping[str, object] | None = None
            try:
                run_migrations(dsn)
                storage = SQLAlchemyStorage.from_dsn(dsn)
                sink = LocalJsonlEventSink(temp / "events.jsonl")
                artifact_store = FileArtifactStore(temp / "artifacts")
                event_bus = EventBus(
                    sink=sink,
                    artifact_store=artifact_store,
                    capacity_storage=storage,
                )
                audit = AuditService(storage)
                policy_engine = PolicyEngine(provider=YamlPolicyProvider(), audit=audit)
                registry = AgentRegistry(
                    [
                        AgentDescriptor(
                            agent_id="system.live_model_stream_smoke",
                            version="v1",
                            name="真实模型增量 smoke",
                            description="只在双重授权后执行一次受控普通文本流",
                            input_schema_ref="live_stream_smoke.Input",
                            output_schema_ref="live_stream_smoke.Output",
                            config_ref="live-stream-smoke:memory",
                            tool_policy=AgentToolPolicy(allowed_tools=[]),
                            model_policy=policy,
                            budget=AgentBudget(
                                max_tokens_per_run=settings.budget.max_tokens_per_run,
                                max_cost_usd_per_run=settings.budget.max_cost_usd_per_run,
                            ),
                            eval_dataset=None,
                            delegation_targets=[],
                        )
                    ]
                )
                executor = LiveStreamSmokeExecutor(
                    request=request,
                    sink=sink,
                    recorder=recorder,
                )
                services = build_agent_execution_services(
                    settings=settings,
                    storage=storage,
                    storage_dsn=dsn,
                    policy=policy_engine,
                    audit=audit,
                    event_sink=sink,
                    event_bus=event_bus,
                    artifact_store=artifact_store,
                    service_root=profiles_dir.parent.parent,
                    workspace_root=temp,
                    registry=registry,
                    stream_timing_observer=recorder.observe,
                )
                orchestrator = RunOrchestrator(
                    storage=storage,
                    event_bus=event_bus,
                    identity=settings.identity.default,
                    executor_resolver=lambda _agent_id: executor,
                    executor_services=services,
                )
                try:
                    result = await orchestrator.start_run(
                        agent_id="system.live_model_stream_smoke",
                        input={"kind": "authorized-live-stream-smoke"},
                        request_id="authorized-live-stream-smoke",
                        trace_id="authorized-live-stream-smoke",
                    )
                except Exception:
                    orchestration_failed = True
                    result = None
                if result is not None:
                    create_app = service_app_factory(profiles_dir.parent.parent)
                    app = create_app(
                        orchestrator=orchestrator,
                        event_sink=sink,
                        profile=profile,
                        profiles_dir=profiles_dir,
                    )
                    existing_event_first_frame_ms = await measure_existing_sse_first_frame(
                        app,
                        run_id=result.run_id,
                    )
            finally:
                await _close_runtime(services=services, storage=storage)

        if orchestration_failed:
            return _classify_local_failure(
                executor=executor,
                recorder=recorder,
                existing_event_first_frame_ms=existing_event_first_frame_ms,
            )
        if result is None or result.status is not RunStatus.COMPLETED or executor.response is None:
            return _classify_local_state(
                executor=executor,
                recorder=recorder,
                existing_event_first_frame_ms=existing_event_first_frame_ms,
            )
        return make_result(
            status="passed",
            provider_called=True,
            existing_event_first_frame_ms=existing_event_first_frame_ms,
            provider_first_delta_ms=recorder.provider_first_delta_ms,
            committed_first_delta_ms=recorder.committed_first_delta_ms,
            client_first_delta_ms=recorder.client_first_delta_ms,
            reason_code=None,
        ), 0
    except Exception:
        return _classify_local_failure(
            executor=executor,
            recorder=recorder,
            existing_event_first_frame_ms=existing_event_first_frame_ms,
        )


async def run(
    *,
    profile: str,
    profiles_dir: Path,
    secret_root: Path | None = None,
) -> tuple[SmokeResult, int]:
    """只有授权、独立 stream opt-in、隔离 credential 与可信 endpoint 齐全才调用。"""

    if os.environ.get(AUTHORIZED_ENV) != "1":
        return make_result(
            status="hosted-unverified",
            provider_called=False,
            reason_code="authorization_missing",
        ), 0
    if os.environ.get(STREAM_OPT_IN_ENV) != "1":
        return make_result(
            status="hosted-unverified",
            provider_called=False,
            reason_code="stream_opt_in_missing",
        ), 0
    try:
        settings = load_settings(
            profile=profile,
            profiles_dir=profiles_dir,
            secret_root=secret_root,
        )
        deployment_id = settings.model.default_deployment_id
        resolved = resolve_model_deployment(settings.model, deployment_id)
    except SettingsLoadError:
        return make_result(
            status="hosted-unverified",
            provider_called=False,
            reason_code="credential_missing",
        ), 0
    except (ValueError, KeyError):
        return make_result(
            status="hosted-unverified",
            provider_called=False,
            reason_code="endpoint_untrusted",
        ), 0
    except Exception:
        return make_result(
            status="failed",
            provider_called=False,
            reason_code="contract_failure",
        ), 1
    if resolved.credential is None:
        return make_result(
            status="hosted-unverified",
            provider_called=False,
            reason_code="credential_missing",
        ), 0
    if resolved.provider_kind != "openai-compatible" or resolved.endpoint_origin is None:
        return make_result(
            status="hosted-unverified",
            provider_called=False,
            reason_code="endpoint_untrusted",
        ), 0
    return await _run_authorized(
        profile=profile,
        profiles_dir=profiles_dir,
        settings=settings,
        resolved=resolved,
    )


__all__ = ["run"]
