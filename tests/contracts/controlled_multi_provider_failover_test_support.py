"""受控多供应商回退 completion/streaming 公共 seam 的离线多候选测试支撑。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from tests.contracts.controlled_multi_provider_failover_settings_test_support import (
    ROUTE_A,
    ROUTE_B,
    ROUTE_C,
    chain_settings,
    downstream_chain_policy,
    three_deployment_override,
)
from tests.contracts.test_model_usage_invocation_contracts import usage_run

from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    BoundModelInvocationService,
    ModelAttemptEvidence,
    ModelDecision,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
    ModelStreamCloseResult,
    ModelStreamDelta,
    ModelStreamUsage,
    UsageEvidenceContext,
    stable_usage_call_id,
)
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage import SQLAlchemyStorage, run_migrations

__all__ = [
    "ROUTE_A",
    "ROUTE_B",
    "ROUTE_C",
    "chain_settings",
    "downstream_chain_policy",
    "three_deployment_override",
]


class ScriptedCandidateFailure(RuntimeError):
    """provider double 暴露的封闭事实；不携带 raw body、header 或 secret。"""

    def __init__(
        self,
        reason: Literal[
            "client_not_started",
            "trusted_business_not_started",
            "ambiguous_timeout",
        ],
    ) -> None:
        super().__init__(reason)
        self.code = "model.provider_retry_exhausted"
        self.not_started_reason = (
            reason if reason in {"client_not_started", "trusted_business_not_started"} else None
        )
        self.side_effect_state = "not_started" if reason == "client_not_started" else "started"
        self.request_sent = reason != "client_not_started"
        self.http_response_observed = reason == "trusted_business_not_started"
        self.http_status = 429 if reason == "trusted_business_not_started" else None
        self.response_identity_observed = False
        self.usage_observed = False
        self.text_observed = False
        self.delta_observed = False
        self.completion_observed = False if reason == "trusted_business_not_started" else None
        self.classifier_ref = (
            "trusted_response_header_not_started"
            if reason == "trusted_business_not_started"
            else None
        )
        self.classifier_version = "v1" if self.classifier_ref is not None else None


class SimulatedProcessCrash(BaseException):
    """测试专用硬崩溃信号；绕过普通异常结算以复现恢复窗口。"""


class _PreparedCandidateCall:
    """惰性 send seam；prepare 只记录 client/permit 取得，不触发 provider。"""

    def __init__(self, provider: ScriptedFailoverProvider, plan: ModelRoutePlan) -> None:
        self._provider = provider
        self._plan = plan
        self._cancelled_outcome: str | None = None

    async def send(self) -> ModelResponse:
        """按候选脚本执行唯一 send，并返回 provider-neutral DTO。"""

        outcome = self._provider.scripts[self._plan.deployment_id][0]
        if outcome.startswith("cancelled_on_send"):
            self._cancelled_outcome = self._provider.scripts[self._plan.deployment_id].pop(0)
            raise asyncio.CancelledError
        return self._provider.send(self._plan)

    async def aclose(self) -> None:
        """测试 double 不持有进程级资源。"""

        if self._plan.deployment_id in self._provider.cleanup_failures:
            self._provider.cleanup_failures.remove(self._plan.deployment_id)
            self._provider.trace.append(f"close:{self._plan.deployment_id}")
            raise RuntimeError("scripted prepared call close failed")
        if self._cancelled_outcome is None:
            return
        self._provider.trace.append(f"close:{self._plan.deployment_id}")
        if self._cancelled_outcome == "cancelled_on_send_close_failure":
            raise RuntimeError("scripted prepared call close failed")


class _PreparedCandidateStream:
    """惰性流 double；第一次迭代才产生 provider 观察或 delta。"""

    def __init__(self, provider: ScriptedFailoverProvider, plan: ModelRoutePlan) -> None:
        self._provider = provider
        self._plan = plan
        self._iterated = False
        self._completed = False
        self._cancelled_outcome: str | None = None

    def __aiter__(self) -> _PreparedCandidateStream:
        return self

    async def __anext__(self) -> ModelStreamDelta:
        if self._completed:
            raise StopAsyncIteration
        if self._iterated:
            self._completed = True
            raise StopAsyncIteration
        outcome = self._provider.scripts[self._plan.deployment_id][0]
        if outcome.startswith("cancelled_on_iterate"):
            self._cancelled_outcome = self._provider.scripts[self._plan.deployment_id].pop(0)
            raise asyncio.CancelledError
        self._iterated = True
        self._provider.trace.append(f"iterate:{self._plan.deployment_id}")
        outcome = self._provider.scripts[self._plan.deployment_id].pop(0)
        if outcome == "crash_after_send":
            raise SimulatedProcessCrash("stream iterate started before proof or settlement")
        if outcome == "ambiguous_timeout_cleanup_failure":
            self._provider.cleanup_failures.add(self._plan.deployment_id)
            raise ScriptedCandidateFailure("ambiguous_timeout")
        if outcome in {"trusted_business_not_started", "ambiguous_timeout"}:
            raise ScriptedCandidateFailure(cast(Any, outcome))
        if outcome != "completed":
            raise AssertionError(f"unknown scripted stream outcome: {outcome}")
        return ModelStreamDelta(text=f"delta:{self._plan.deployment_id}")

    async def result(self) -> ModelResponse:
        """自然耗尽后返回与实际产出候选绑定的唯一完成结果。"""

        if not self._iterated:
            raise RuntimeError("stream result requested before iteration")
        return self._provider.completed_response(self._plan).model_copy(
            update={"output_text": f"delta:{self._plan.deployment_id}"}
        )

    async def aclose(self) -> ModelStreamCloseResult:
        """按是否观察 delta 返回封闭关闭事实，不制造第二次 provider 调用。"""

        if self._plan.deployment_id in self._provider.cleanup_failures:
            self._provider.cleanup_failures.remove(self._plan.deployment_id)
            self._provider.trace.append(f"close_stream:{self._plan.deployment_id}")
            raise RuntimeError("scripted prepared stream close failed")
        if self._cancelled_outcome is not None:
            self._provider.trace.append(f"close_stream:{self._plan.deployment_id}")
            if self._cancelled_outcome == "cancelled_on_iterate_close_failure":
                raise RuntimeError("scripted prepared stream close failed")
            if self._cancelled_outcome == "cancelled_on_iterate_not_started":
                return ModelStreamCloseResult(state="not_started")
            if self._cancelled_outcome == "cancelled_on_iterate_stopped_null":
                return ModelStreamCloseResult(state="stopped")
            if self._cancelled_outcome == "cancelled_on_iterate_unknown_null":
                return ModelStreamCloseResult(state="unknown")
            finality = (
                "complete"
                if self._cancelled_outcome == "cancelled_on_iterate_stopped_complete"
                else "partial"
            )
            state = (
                "unknown"
                if self._cancelled_outcome == "cancelled_on_iterate_unknown_partial"
                else "stopped"
            )
            return ModelStreamCloseResult(
                state=state,
                usage=ModelStreamUsage(
                    finality=finality,
                    input_tokens=1,
                    output_tokens=1,
                    cost_usd=0.000002,
                    cost_status="reported",
                    latency_ms=1,
                ),
            )
        if not self._iterated:
            return ModelStreamCloseResult(state="not_started")
        return ModelStreamCloseResult(
            state="stopped",
            usage=ModelStreamUsage(
                finality="complete" if self._completed else "partial",
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.000002,
                cost_status="reported",
                latency_ms=1,
            ),
        )


class ScriptedFailoverProvider:
    """按 deployment 脚本化 prepare/send，用调用轨迹证明只推进一次。"""

    provider_id = "openai-compatible"

    def __init__(
        self,
        scripts: dict[str, list[str]],
        *,
        prepare_delays_seconds: dict[str, float] | None = None,
    ) -> None:
        self.scripts = {key: list(value) for key, value in scripts.items()}
        self.prepare_delays_seconds = dict(prepare_delays_seconds or {})
        self.trace: list[str] = []
        self.cleanup_failures: set[str] = set()

    async def _wait_before_prepare(self, deployment_id: str) -> None:
        """只延迟本地 prepare，用于区分候选 deadline 与 provider 调用事实。"""

        delay = self.prepare_delays_seconds.get(deployment_id, 0.0)
        if delay > 0:
            await asyncio.sleep(delay)

    async def prepare(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> _PreparedCandidateCall:
        """client-not-started 在 prepare 边界关闭；其他结果延迟到 send。"""

        del request
        self.trace.append(f"prepare:{plan.deployment_id}")
        await self._wait_before_prepare(plan.deployment_id)
        outcome = self.scripts[plan.deployment_id][0]
        if outcome == "cancelled_before_send":
            self.scripts[plan.deployment_id].pop(0)
            raise asyncio.CancelledError
        if outcome == "client_not_started":
            self.scripts[plan.deployment_id].pop(0)
            raise ScriptedCandidateFailure("client_not_started")
        if outcome == "crash_before_send":
            self.scripts[plan.deployment_id].pop(0)
            raise SimulatedProcessCrash("attempt started committed before send")
        return _PreparedCandidateCall(self, plan)

    def completed_response(self, plan: ModelRoutePlan) -> ModelResponse:
        """构造与当前候选绑定的确定性完成 DTO。"""

        return ModelResponse(
            provider=plan.provider,
            model=plan.model,
            output_text=f"completed:{plan.deployment_id}",
            decision=ModelDecision(action="call", estimated_tokens=2),
            token_usage={"input_tokens": 1, "output_tokens": 1},
            latency_ms=1,
            cost_usd=0.000002,
            cost_status="reported",
            attempts=[
                ModelAttemptEvidence(
                    attempt=1,
                    side_effect_state="started",
                    outcome="completed",
                    completion_observed=True,
                    input_tokens=1,
                    output_tokens=1,
                    cost_usd=0.000002,
                    cost_status="reported",
                    budget_charge_tokens=2,
                    budget_charge_cost_usd=0.000002,
                    latency_ms=1,
                )
            ],
        )

    def send(self, plan: ModelRoutePlan) -> ModelResponse:
        """返回完成结果或带封闭观察事实的受控失败。"""

        self.trace.append(f"send:{plan.deployment_id}")
        outcome = self.scripts[plan.deployment_id].pop(0)
        if outcome == "crash_after_send":
            raise SimulatedProcessCrash("send observed before proof or settlement")
        if outcome == "ambiguous_timeout_cleanup_failure":
            self.cleanup_failures.add(plan.deployment_id)
            raise ScriptedCandidateFailure("ambiguous_timeout")
        if outcome in {"trusted_business_not_started", "ambiguous_timeout"}:
            raise ScriptedCandidateFailure(cast(Any, outcome))
        if outcome != "completed":
            raise AssertionError(f"unknown scripted outcome: {outcome}")
        return self.completed_response(plan)

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        """兼容直接 provider seam，同时仍复用惰性 prepare/send 轨迹。"""

        prepared = await self.prepare(request, plan=plan)
        return await prepared.send()

    async def prepare_stream(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
    ) -> _PreparedCandidateStream:
        """prepare 阶段不拉取 delta；client-not-started 在此确定关闭。"""

        del request
        self.trace.append(f"prepare_stream:{plan.deployment_id}")
        await self._wait_before_prepare(plan.deployment_id)
        outcome = self.scripts[plan.deployment_id][0]
        if outcome == "cancelled_before_send":
            self.scripts[plan.deployment_id].pop(0)
            raise asyncio.CancelledError
        if outcome == "client_not_started":
            self.scripts[plan.deployment_id].pop(0)
            raise ScriptedCandidateFailure("client_not_started")
        if outcome == "crash_before_send":
            self.scripts[plan.deployment_id].pop(0)
            raise SimulatedProcessCrash("stream attempt started committed before iterate")
        return _PreparedCandidateStream(self, plan)


class UnexpectedFakeProvider:
    """真实 chain 不得隐式落入的 fake sentinel。"""

    provider_id = "fake"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        """任何调用都立即暴露真实链被偷偷追加 fake 的越界。"""

        del request, plan
        self.calls += 1
        raise AssertionError("real route chain must not fall through to fake")


@dataclass
class BoundFailoverFixture:
    """一次 bound invocation 所需的受信身份、存储与可观察 double。"""

    bound: BoundModelInvocationService
    storage: SQLAlchemyStorage
    sink: LocalJsonlEventSink
    provider: Any
    fake_provider: UnexpectedFakeProvider
    run_id: str
    usage_call_id: str
    operation_key: str
    candidate_token_bounds: tuple[int, ...]
    agent_policy: AgentModelPolicy


async def bound_failover_invocation(
    tmp_path: Path,
    *,
    scripts: dict[str, list[str]],
    storage_dsn: str | None = None,
    route_count: Literal[2, 3] = 3,
    policy_engine: Any | None = None,
    provider_override: Any | None = None,
    soft_token_limits: dict[str, int] | None = None,
    first_capabilities: list[Literal["text_completion", "text_stream"]] | None = None,
    first_max_output_tokens: int | None = None,
    max_attempts_by_deployment: dict[str, int] | None = None,
    total_timeout_ms_by_deployment: dict[str, int] | None = None,
    prepare_delays_seconds: dict[str, float] | None = None,
) -> BoundFailoverFixture:
    """装配真实 UoW/ledger 和公共 bound seam，全程只使用离线 provider double。"""

    settings = chain_settings(route_count=route_count)
    first_deployment = settings.model.deployments[ROUTE_A["deployment_id"]]
    if first_capabilities is not None:
        first_deployment.capabilities = first_capabilities
    if first_max_output_tokens is not None:
        first_deployment.max_output_tokens = first_max_output_tokens
        catalog_ref = first_deployment.model_catalog_refs[ROUTE_A["model_id"]]
        catalog = settings.model.model_catalogs[catalog_ref]
        first_deployment.max_per_attempt_token_bound = (
            first_deployment.max_prompt_utf8_bytes
            + catalog.input_envelope_token_bound
            + first_max_output_tokens
        )
        if catalog.cost_enabled:
            assert catalog.input_token_price_usd is not None
            assert catalog.output_token_price_usd is not None
            first_deployment.max_per_attempt_cost_bound = (
                Decimal(first_deployment.max_prompt_utf8_bytes + catalog.input_envelope_token_bound)
                * catalog.input_token_price_usd
                + Decimal(first_max_output_tokens) * catalog.output_token_price_usd
            )
    for deployment_id, outcomes in scripts.items():
        deployment = settings.model.deployments[deployment_id]
        if total_timeout_ms_by_deployment is not None:
            deployment.total_timeout_ms = total_timeout_ms_by_deployment.get(
                deployment_id,
                deployment.total_timeout_ms,
            )
        deployment.max_attempts = (max_attempts_by_deployment or {}).get(
            deployment_id,
            len(outcomes),
        )
        deployment.retryable_http_statuses = [429] if len(outcomes) > 1 else []
        deployment.cross_provider_failover_http_statuses = [429, 503]
    policy = downstream_chain_policy(route_count=route_count)
    registry = AgentRegistry(
        [
            AgentDescriptor(
                agent_id="agent-a",
                version="v1",
                name="controlled multi-provider failover fixture",
                description="离线多候选公共 seam",
                input_schema_ref="fixture.Input",
                output_schema_ref="fixture.Output",
                config_ref="fixture/config.yaml",
                tool_policy=AgentToolPolicy(allowed_tools=[]),
                model_policy=policy,
                budget=AgentBudget(max_tokens_per_run=4096, max_cost_usd_per_run=1.0),
                eval_dataset=None,
                delegation_targets=[],
            )
        ]
    )
    router_config = ModelRouterConfig(
        default_provider="openai-compatible",
        default_model="fixture-text-1",
        route_max_tokens_per_call=soft_token_limits or {},
    )
    shared_budget = SharedBudgetRuntime(
        settings=settings,
        registry=registry,
        model_config=router_config,
    )
    dsn = storage_dsn or f"sqlite+aiosqlite:///{tmp_path / 'failover.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await usage_run(storage)
    async with storage.uow() as uow:
        await uow.shared_budget.create_ledger(
            shared_budget.ledger_create(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
            )
        )
        await uow.commit()

    sink = LocalJsonlEventSink(tmp_path / "failover.events.jsonl")

    async def resolve_trace(**_: object) -> str:
        """测试 run 的 canonical trace 固定为同一受信 identity。"""

        return "trace-a"

    provider = provider_override or ScriptedFailoverProvider(
        scripts,
        prepare_delays_seconds=prepare_delays_seconds,
    )
    fake_provider = UnexpectedFakeProvider()
    router = ModelRouter(
        config=router_config,
        providers={"openai-compatible": provider, "fake": fake_provider},
        model_settings=settings.model,
    )
    route_plan = router.plan_chain(
        ModelRequest(
            prompt="hello",
            max_output_tokens=8,
            route_refs=policy.fallback_routes,
        ),
        agent_policy=policy,
    )
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(
            sink=sink,
            run_trace_resolver=resolve_trace,
            capacity_storage=storage,
        ),
        shared_budget=shared_budget,
        agent_policy_resolver=lambda _agent_id: policy,
        policy_engine=policy_engine,
    )
    operation_key = "primary-model-call"
    context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    bound = service.bind_execution(
        identity=IdentityContext.local_default(),
        tenant_id=context.tenant_id,
        run_id=context.run_id,
        agent_id=context.agent_id,
        request_id=context.request_id,
        trace_id=context.trace_id or "trace-a",
    )
    return BoundFailoverFixture(
        bound=bound,
        storage=storage,
        sink=sink,
        provider=provider,
        fake_provider=fake_provider,
        run_id=run_id,
        usage_call_id=stable_usage_call_id(context=context, operation_key=operation_key),
        operation_key=operation_key,
        candidate_token_bounds=tuple(
            candidate.reserved_token_bound for candidate in route_plan.candidates
        ),
        agent_policy=policy,
    )
