"""受控真实多 deployment failover smoke；默认零网络并输出 hosted-unverified。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

try:
    from scripts.live_model_failover_contract import (
        preflight_result,
        validate_preflight_routes,
        validate_result,
        validate_result_against_evidence,
    )
    from scripts.live_model_failover_evidence import (
        candidate_payload as _candidate_payload,
    )
    from scripts.live_model_failover_evidence import (
        load_durable_failover_evidence,
    )
    from scripts.smoke_live_model import LiveSmokeExecutor
except ModuleNotFoundError:  # 直接执行脚本时，`scripts/` 本身是 import root。
    from live_model_failover_contract import (  # type: ignore[no-redef]
        preflight_result,
        validate_preflight_routes,
        validate_result,
        validate_result_against_evidence,
    )
    from live_model_failover_evidence import (  # type: ignore[no-redef]
        candidate_payload as _candidate_payload,
    )
    from live_model_failover_evidence import (
        load_durable_failover_evidence,
    )
    from smoke_live_model import LiveSmokeExecutor  # type: ignore[no-redef]

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.config import ModelRouteRef, SettingsLoadError, load_settings
from agent_harness.config.model_endpoints import resolve_model_deployment
from agent_harness.config.secret_files import DEFAULT_SECRET_ROOT
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    ModelRequest,
    UsageEvidenceContext,
    stable_usage_call_id,
)
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

AUTHORIZED_ENV = "AGENT_HARNESS_LIVE_MODEL_AUTHORIZED"
FAILOVER_OPT_IN_ENV = "AGENT_HARNESS_LIVE_MODEL_FAILOVER_OPT_IN"
CREDENTIAL_PAIR_ENV = "AGENT_HARNESS_LIVE_MODEL_FAILOVER_CREDENTIAL_PAIR_PRESENT"
DEPLOYMENT_PAIR_ENV = "AGENT_HARNESS_LIVE_MODEL_FAILOVER_DEPLOYMENT_PAIR_VALID"
NOT_STARTED_FIXTURE_ENV = "AGENT_HARNESS_LIVE_MODEL_FAILOVER_NOT_STARTED_FIXTURE"
DEPLOYMENTS_ENV = "AGENT_HARNESS_LIVE_MODEL_FAILOVER_DEPLOYMENTS"


def run_preflight() -> tuple[dict[str, object], int]:
    """只判断显式前置标志；不打开 credential、不构造 client、不访问网络。"""

    values = {
        "authorized": os.environ.get(AUTHORIZED_ENV) == "1",
        "failover_opt_in": os.environ.get(FAILOVER_OPT_IN_ENV) == "1",
        "credential_pair_present": os.environ.get(CREDENTIAL_PAIR_ENV) == "1",
        "deployment_pair_valid": os.environ.get(DEPLOYMENT_PAIR_ENV) == "1",
        "not_started_fixture_present": os.environ.get(NOT_STARTED_FIXTURE_ENV) == "1",
    }
    try:
        return preflight_result(**values), 0
    except ValueError:
        # 完整 live 前置必须由受保护环境中的正式 producer 注入耐久证据；本地 CLI
        # 不猜 endpoint、credential 或 fixture，也不会以伪造结果冒充真实调用。
        return validate_result(
            {
                "schema_version": "model-failover-live-smoke/v1",
                "status": "failed",
                "provider_called": False,
                "attempt_count": 0,
                "chain_id": None,
                "selected_ordinal": None,
                "candidates": [],
                "usage": None,
                "reason_code": "contract_failure",
            }
        ), 1


def classify_frozen_run_failure(
    *,
    chain_id: str,
    selected_ordinal: int | None,
    candidates: list[dict[str, object]],
    provider_called: bool,
    attempt_count: int,
    error_code: str | None,
    failure_domain: str | None,
) -> tuple[dict[str, object], int]:
    """按显式失败域分类冻结链结果，并原样保留耐久 identity 与调用事实。"""

    external_reason = {
        "model.provider_side_effect_unknown": "provider_result_unknown",
        "model.provider_retry_exhausted": "provider_rejected",
        "model.provider_failed": "provider_rejected",
    }.get(error_code or "")
    is_external = (
        failure_domain == "provider" and external_reason is not None and selected_ordinal is None
    )
    return validate_result(
        {
            "schema_version": "model-failover-live-smoke/v1",
            "status": "external-blocked" if is_external else "failed",
            "provider_called": provider_called,
            "attempt_count": attempt_count,
            "chain_id": chain_id,
            "selected_ordinal": None if is_external else selected_ordinal,
            "candidates": candidates,
            "usage": None,
            "reason_code": external_reason if is_external else "contract_failure",
        }
    ), (2 if is_external else 1)


async def run_authorized(
    *,
    profile: str,
    profiles_dir: Path,
    secret_root: Path,
) -> tuple[dict[str, object], int]:
    """经正式 composition 执行恰好两候选；任何前置漂移都在 client 构造前关闭。"""

    deployment_ids = [item.strip() for item in os.environ.get(DEPLOYMENTS_ENV, "").split(",")]
    if len(deployment_ids) != 2 or any(not item for item in deployment_ids):
        return validate_result(
            {
                "schema_version": "model-failover-live-smoke/v1",
                "status": "failed",
                "provider_called": False,
                "attempt_count": 0,
                "chain_id": None,
                "selected_ordinal": None,
                "candidates": [],
                "usage": None,
                "reason_code": "contract_failure",
            }
        ), 1
    try:
        settings = load_settings(
            profile=profile,
            profiles_dir=profiles_dir,
            secret_root=secret_root,
        )
        resolved = [resolve_model_deployment(settings.model, item) for item in deployment_ids]
        validate_preflight_routes(
            [
                {
                    "deployment_id": item.deployment_id,
                    "provider_kind": item.provider_kind,
                    "credential_ref": item.credential_ref,
                    "endpoint_origin": item.endpoint_origin,
                    "max_attempts": settings.model.deployments[item.deployment_id].max_attempts,
                }
                for item in resolved
            ]
        )
    except (SettingsLoadError, KeyError, TypeError, ValueError):
        return preflight_result(
            authorized=True,
            failover_opt_in=True,
            credential_pair_present=True,
            deployment_pair_valid=False,
            not_started_fixture_present=True,
        ), 0

    first, second = resolved
    routes = tuple(
        ModelRouteRef(deployment_id=item.deployment_id, model_id=item.default_model)
        for item in resolved
    )
    agent_policy = AgentModelPolicy(
        deployment_id=first.deployment_id,
        provider=first.provider_kind,
        allowed_models=[first.default_model],
        default_model=first.default_model,
        fallback_models=[],
        fallback_routes=routes,
    )
    executor = LiveSmokeExecutor(
        ModelRequest(
            prompt="Reply with OK.",
            max_output_tokens=min(
                8,
                settings.model.deployments[first.deployment_id].max_output_tokens,
                settings.model.deployments[second.deployment_id].max_output_tokens,
            ),
        )
    )
    with tempfile.TemporaryDirectory(prefix="agent-harness-live-failover-") as root:
        temp = Path(root)
        dsn = f"sqlite+aiosqlite:///{temp / 'runtime.db'}"
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        sink = LocalJsonlEventSink(temp / "events.jsonl")
        artifacts = FileArtifactStore(temp / "artifacts")
        event_bus = EventBus(sink=sink, artifact_store=artifacts, capacity_storage=storage)
        audit = AuditService(storage)
        policy = PolicyEngine(provider=YamlPolicyProvider(), audit=audit)
        registry = AgentRegistry(
            [
                AgentDescriptor(
                    agent_id="system.live_model_failover_smoke",
                    version="v1",
                    name="真实模型 failover smoke",
                    description="只在完整授权前置下验证受信未开始切换",
                    input_schema_ref="live_failover.Input",
                    output_schema_ref="live_failover.Output",
                    config_ref="live-failover:memory",
                    tool_policy=AgentToolPolicy(allowed_tools=[]),
                    model_policy=agent_policy,
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
            policy=policy,
            audit=audit,
            event_sink=sink,
            event_bus=event_bus,
            artifact_store=artifacts,
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
        state = None
        try:
            run_result = await orchestrator.start_run(
                agent_id="system.live_model_failover_smoke",
                input={"kind": "authorized-live-failover-smoke"},
                request_id="authorized-live-failover-smoke",
                trace_id="authorized-live-failover-smoke",
            )
            context = UsageEvidenceContext(
                tenant_id=settings.identity.default.tenant_id,
                run_id=run_result.run_id,
                agent_id="system.live_model_failover_smoke",
                request_id="authorized-live-failover-smoke",
                trace_id="authorized-live-failover-smoke",
            )
            usage_call_id = stable_usage_call_id(
                context=context,
                operation_key="authorized-live-smoke",
            )
            async with storage.uow() as uow:
                state = await uow.shared_budget.get_model_route_chain_state(
                    tenant_id=context.tenant_id,
                    run_id=run_result.run_id,
                    usage_call_id=usage_call_id,
                )
            if state is None:
                raise ValueError("live failover did not persist route-chain state")
            candidates = [
                _candidate_payload(
                    item,
                    attempt_count=sum(
                        attempt.candidate_ordinal == item.ordinal
                        for attempt in state.attempt_lifecycle
                    ),
                )
                for item in state.candidates
            ]
            response = executor.response
            if run_result.status is RunStatus.COMPLETED and response is not None:
                durable_state, durable = await load_durable_failover_evidence(
                    storage=storage,
                    tenant_id=context.tenant_id,
                    run_id=run_result.run_id,
                    usage_call_id=usage_call_id,
                )
                if durable_state != state:
                    raise ValueError("live failover durable state changed during artifact assembly")
                usage = {
                    "input_tokens": int(response.token_usage.get("input_tokens", 0)),
                    "output_tokens": int(response.token_usage.get("output_tokens", 0)),
                    "cost_usd": response.cost_usd,
                    "cost_status": response.cost_status,
                }
                payload: dict[str, object] = {
                    "schema_version": "model-failover-live-smoke/v1",
                    "status": "passed",
                    "provider_called": True,
                    "attempt_count": len(state.attempt_lifecycle),
                    "chain_id": state.chain_id,
                    "selected_ordinal": state.selected_ordinal,
                    "candidates": candidates,
                    "usage": usage,
                    "reason_code": None,
                }
                return validate_result_against_evidence(payload, durable), 0
            return classify_frozen_run_failure(
                chain_id=state.chain_id,
                selected_ordinal=state.selected_ordinal,
                candidates=candidates,
                provider_called=any(
                    item.request_sent or item.http_response_observed
                    for item in state.attempt_lifecycle
                ),
                attempt_count=len(state.attempt_lifecycle),
                error_code=executor.error_code,
                failure_domain=executor.failure_domain,
            )
        except Exception:
            if state is None and executor.run_id is not None:
                # `start_run()` 可能在 executor 已提交 provider/usage 事实后才因 terminal
                # 发布失败。这里只用 executor 绑定的正式 run identity 回读，不能用响应猜链。
                context = UsageEvidenceContext(
                    tenant_id=settings.identity.default.tenant_id,
                    run_id=executor.run_id,
                    agent_id="system.live_model_failover_smoke",
                    request_id="authorized-live-failover-smoke",
                    trace_id="authorized-live-failover-smoke",
                )
                usage_call_id = stable_usage_call_id(
                    context=context,
                    operation_key="authorized-live-smoke",
                )
                async with storage.uow() as uow:
                    state = await uow.shared_budget.get_model_route_chain_state(
                        tenant_id=context.tenant_id,
                        run_id=context.run_id,
                        usage_call_id=usage_call_id,
                    )
            if state is not None:
                candidates = [
                    _candidate_payload(
                        item,
                        attempt_count=sum(
                            attempt.candidate_ordinal == item.ordinal
                            for attempt in state.attempt_lifecycle
                        ),
                    )
                    for item in state.candidates
                ]
                return validate_result(
                    {
                        "schema_version": "model-failover-live-smoke/v1",
                        "status": "failed",
                        "provider_called": any(
                            item.request_sent or item.http_response_observed
                            for item in state.attempt_lifecycle
                        ),
                        "attempt_count": len(state.attempt_lifecycle),
                        "chain_id": state.chain_id,
                        "selected_ordinal": state.selected_ordinal,
                        "candidates": candidates,
                        "usage": None,
                        "reason_code": "contract_failure",
                    }
                ), 1
            return validate_result(
                {
                    "schema_version": "model-failover-live-smoke/v1",
                    "status": "failed",
                    "provider_called": False,
                    "attempt_count": 0,
                    "chain_id": None,
                    "selected_ordinal": None,
                    "candidates": [],
                    "usage": None,
                    "reason_code": "contract_failure",
                }
            ), 1
        finally:
            await close_agent_execution_services(services)
            await storage.dispose()


def main() -> int:
    """写入单个去敏 artifact；前置不足以成功退出映射 CI skipped。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".artifacts/smoke/live-model-failover/result.json"),
    )
    parser.add_argument("--profile", default="service")
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=Path("templates/service-app/configs/profiles"),
    )
    parser.add_argument("--secret-root", type=Path, default=DEFAULT_SECRET_ROOT)
    args = parser.parse_args()
    payload, exit_code = run_preflight()
    if payload["status"] == "failed" and all(
        os.environ.get(name) == "1"
        for name in (
            AUTHORIZED_ENV,
            FAILOVER_OPT_IN_ENV,
            CREDENTIAL_PAIR_ENV,
            DEPLOYMENT_PAIR_ENV,
            NOT_STARTED_FIXTURE_ENV,
        )
    ):
        payload, exit_code = asyncio.run(
            run_authorized(
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
