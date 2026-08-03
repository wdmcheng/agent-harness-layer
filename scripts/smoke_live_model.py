"""受控真实非流式文本模型 smoke；默认只报告 hosted-unverified。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, cast

try:
    from scripts.live_model_schema_identity import live_text_output_schema_identity
except ModuleNotFoundError:  # 直接执行脚本时，`scripts/` 本身是 import root。
    from live_model_schema_identity import (  # type: ignore[no-redef]
        live_text_output_schema_identity,
    )

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.config import SettingsLoadError, load_settings
from agent_harness.config.model_endpoints import resolve_model_deployment
from agent_harness.config.secret_files import DEFAULT_SECRET_ROOT
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    BoundModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
)
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    RunOrchestrator,
    RunStatus,
)
from agent_harness.runtime.services import (
    build_agent_execution_services,
    close_agent_execution_services,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations

SCHEMA_VERSION = "model-live-smoke/v1"
AUTHORIZED_ENV = "AGENT_HARNESS_LIVE_MODEL_AUTHORIZED"
OPT_IN_ENV = "AGENT_HARNESS_LIVE_MODEL_OPT_IN"


class LiveSmokeExecutor:
    """把授权 smoke 绑定到正式 invocation facade，不把 provider 对象交给脚本。"""

    def __init__(self, request: ModelRequest) -> None:
        self._request = request
        self.run_id: str | None = None
        self.response: ModelResponse | None = None
        self.error_code: str | None = None
        self.failure_domain: Literal["provider", "runtime"] | None = None
        self.provider_called = False
        self.attempt_count = 0
        self.latency_ms: int | None = None

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """经 bound identity、policy、预算与 evidence 执行一次固定非流式请求。"""

        self.run_id = request.run_id
        invocation = cast(
            BoundModelInvocationService,
            context.require_service("model_invocation"),
        )
        try:
            self.response = await invocation.complete(
                self._request,
                operation_key="authorized-live-smoke",
            )
        except ModelProviderInvocationError as exc:
            self.error_code = exc.code
            self.failure_domain = exc.failure_domain
            self.provider_called = exc.provider_called
            self.attempt_count = exc.attempt_count
            self.latency_ms = exc.latency_ms
            return AgentExecutionResult.failed(exc.code)
        self.provider_called = True
        self.attempt_count = len(self.response.attempts)
        self.latency_ms = self.response.latency_ms
        return AgentExecutionResult.completed({"completed": True})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """live smoke 不产生审批等待；意外进入 resume 必须 fail closed。"""

        del request, context, grant
        return AgentExecutionResult.failed("live smoke does not support resume")


def _result(
    *,
    status: str,
    reason_code: str,
    provider_called: bool = False,
    deployment_id: str | None = None,
    provider_kind: str | None = None,
    model: str | None = None,
    endpoint_origin: str | None = None,
    attempt_count: int = 0,
    usage: dict[str, int] | None = None,
    latency_ms: int | None = None,
) -> dict[str, Any]:
    """只构造封闭去敏字段，禁止 prompt/response/header/URL/exception 进入证据。"""

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason_code": reason_code,
        "provider_called": provider_called,
        "deployment_id": deployment_id,
        "provider_kind": provider_kind,
        "model": model,
        "endpoint_origin": endpoint_origin,
        "attempt_count": attempt_count,
        "usage": usage,
        "latency_ms": latency_ms,
    }


async def run(
    *,
    profile: str,
    profiles_dir: Path,
    secret_root: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """只有授权、opt-in、隔离 credential 和受信 deployment 同时满足才调用一次。"""

    if os.environ.get(AUTHORIZED_ENV) != "1":
        return _result(status="hosted-unverified", reason_code="authorization_missing"), 0
    if os.environ.get(OPT_IN_ENV) != "1":
        return _result(status="hosted-unverified", reason_code="opt_in_missing"), 0
    try:
        settings = load_settings(
            profile=profile,
            profiles_dir=profiles_dir,
            secret_root=secret_root,
        )
        deployment_id = settings.model.default_deployment_id
        resolved = resolve_model_deployment(settings.model, deployment_id)
    except (SettingsLoadError, ValueError, KeyError):
        return _result(status="hosted-unverified", reason_code="typed_preflight_missing"), 0
    if (
        resolved.provider_kind != "openai-compatible"
        or resolved.credential is None
        or resolved.endpoint_origin is None
    ):
        return _result(
            status="hosted-unverified",
            reason_code="trusted_deployment_missing",
            deployment_id=deployment_id,
            provider_kind=resolved.provider_kind,
        ), 0

    request = ModelRequest(
        deployment_id=deployment_id,
        provider=resolved.provider_kind,
        prompt="Reply with OK.",
        max_output_tokens=8,
    )
    policy = AgentModelPolicy(
        deployment_id=deployment_id,
        provider=resolved.provider_kind,
        allowed_models=list(resolved.allowed_models),
        default_model=resolved.default_model,
        fallback_models=list(resolved.fallback_models),
    )
    executor = LiveSmokeExecutor(request)
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix="agent-harness-live-smoke-") as temp_root:
        temp = Path(temp_root)
        dsn = f"sqlite+aiosqlite:///{temp / 'runtime.db'}"
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
                    agent_id="system.live_model_smoke",
                    version="v1",
                    name="真实模型隔离 smoke",
                    description="只在显式授权后执行一次受控非流式文本请求",
                    input_schema_ref="live_smoke.Input",
                    output_schema_ref="live_smoke.Output",
                    output_schema_identity=live_text_output_schema_identity(
                        schema_ref="live_smoke.Output",
                        version="v1",
                    ),
                    config_ref="live-smoke:memory",
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
        )
        orchestrator = RunOrchestrator(
            storage=storage,
            event_bus=event_bus,
            identity=settings.identity.default,
            executor_resolver=lambda _agent_id: executor,
            executor_services=services,
        )
        try:
            run_result = await orchestrator.start_run(
                agent_id="system.live_model_smoke",
                input={"kind": "authorized-live-smoke"},
                request_id="authorized-live-smoke",
                trace_id="authorized-live-smoke",
            )
            provider_called = executor.provider_called
        except Exception:
            observed_response = executor.response
            if observed_response is not None:
                # provider 已返回但 terminal/budget fencing 拒绝完成时，仍必须保留
                # 已观察到的安全调用证据；只有 status 维持 fail，不能伪造零调用。
                return (
                    _result(
                        status="fail",
                        reason_code="contract_failure",
                        provider_called=True,
                        deployment_id=deployment_id,
                        provider_kind=observed_response.provider,
                        model=observed_response.model,
                        endpoint_origin=resolved.endpoint_origin,
                        attempt_count=len(observed_response.attempts),
                        usage=observed_response.token_usage or None,
                        latency_ms=observed_response.latency_ms,
                    ),
                    1,
                )
            return (
                _result(
                    status="fail",
                    reason_code="contract_failure",
                    provider_called=executor.provider_called,
                    deployment_id=deployment_id,
                    provider_kind=resolved.provider_kind,
                    model=resolved.default_model,
                    endpoint_origin=resolved.endpoint_origin,
                    attempt_count=executor.attempt_count,
                    latency_ms=(
                        executor.latency_ms
                        if executor.latency_ms is not None
                        else int((perf_counter() - started) * 1000)
                    ),
                ),
                1,
            )
        finally:
            await close_agent_execution_services(services)
            await storage.dispose()
    if run_result.status is not RunStatus.COMPLETED or executor.response is None:
        external_error = executor.failure_domain == "provider" and executor.error_code in {
            "model.provider_failed",
            "model.provider_retry_exhausted",
            "model.provider_side_effect_unknown",
        }
        return (
            _result(
                status="external-blocked" if external_error else "fail",
                reason_code=(
                    "provider_or_network_blocked" if external_error else "contract_failure"
                ),
                provider_called=provider_called,
                deployment_id=deployment_id,
                provider_kind=resolved.provider_kind,
                model=resolved.default_model,
                endpoint_origin=resolved.endpoint_origin,
                attempt_count=executor.attempt_count,
                latency_ms=(
                    executor.latency_ms
                    if executor.latency_ms is not None
                    else int((perf_counter() - started) * 1000)
                ),
            ),
            2 if external_error else 1,
        )
    response = executor.response
    return (
        _result(
            status="pass",
            reason_code="completed",
            provider_called=True,
            deployment_id=deployment_id,
            provider_kind=response.provider,
            model=response.model,
            endpoint_origin=resolved.endpoint_origin,
            attempt_count=len(response.attempts),
            usage=response.token_usage or None,
            latency_ms=response.latency_ms,
        ),
        0,
    )


def main() -> int:
    """写入单个机器 JSON，并让 hosted-unverified 保持成功退出。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="service")
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=Path("templates/service-app/configs/profiles"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/smoke/live-model/result.json"),
    )
    parser.add_argument("--secret-root", type=Path, default=DEFAULT_SECRET_ROOT)
    args = parser.parse_args()
    payload, exit_code = asyncio.run(
        run(
            profile=args.profile,
            profiles_dir=args.profiles_dir,
            secret_root=args.secret_root,
        )
    )
    rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
