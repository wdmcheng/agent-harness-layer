"""真实调用 retry/deadline、动态预算与 side-effect unknown 合同。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from tests.contracts.controlled_real_model_runtime_composition_test_support import ResultDouble
from tests.contracts.provider_neutral_structured_output_test_support import (
    fixture_output_schema_identity,
)
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)
from tests.contracts.test_model_usage_invocation_contracts import usage_run

from agent_harness.adapters.models.pydantic_ai import (
    ControlledOpenAIClientFactory,
    PydanticAIModelProvider,
)
from agent_harness.config import HarnessSettings, ModelSettings, load_settings
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    ModelInvocationService,
    ModelRequest,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
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
from agent_harness.storage.shared_budget import BudgetReservationRejected
from agent_harness.storage.shared_budget_models import BudgetOperationClaimModel


class SequenceAgent:
    """按顺序返回异常或结果，精确暴露 adapter attempt call count。"""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def run(
        self, prompt: str, *, model_settings: object
    ) -> ResultDouble | ResultWithoutUsageDouble:
        del prompt, model_settings
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, (ResultDouble, ResultWithoutUsageDouble))
        return outcome


class ResultWithoutUsageDouble:
    """模拟 provider 已完成并返回文本，但没有报告任何 usage。"""

    output = "adapter-result-without-usage"


def retry_settings(*, classifier: bool) -> HarnessSettings:
    """构造带可选可信 completion classifier 的完整 typed settings。"""

    overrides = real_model_override()
    deployment = overrides["model"]["deployments"]["real_primary"]  # type: ignore[index]
    policy = overrides["model"]["endpoint_policies"]["real_primary_endpoint"]  # type: ignore[index]
    # Linux 全量合同运行时需覆盖首次 Pydantic AI/SDK 冷启动；具体 read timeout
    # 仍保持 2000ms，避免把测试预算误当成 provider 超时语义。
    deployment["total_timeout_ms"] = 10_000  # type: ignore[index]
    deployment["max_attempts"] = 2  # type: ignore[index]
    deployment["max_retry_wait_ms"] = 100  # type: ignore[index]
    if classifier:
        deployment["retryable_http_statuses"] = [429, 503]  # type: ignore[index]
        deployment["completion_classifier_ref"] = "trusted_response_header_not_started"  # type: ignore[index]
        deployment["completion_classifier_version"] = "v1"  # type: ignore[index]
        policy["completion_classifiers"] = [  # type: ignore[index]
            {"ref": "trusted_response_header_not_started", "version": "v1"}
        ]
    return load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)


def retry_route(*, classifier: bool) -> tuple[ModelSettings, ModelRequest, AgentModelPolicy]:
    settings = retry_settings(classifier=classifier)
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        prompt="retry fixture",
        max_output_tokens=9,
    )
    agent_policy = AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )
    return settings.model, request, agent_policy


async def managed_real_invocation(
    tmp_path: Path,
    *,
    classifier: bool,
    outcomes: list[object],
    database_name: str,
    transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    service_type: type[ModelInvocationService] = ModelInvocationService,
) -> tuple[
    SQLAlchemyStorage,
    LocalJsonlEventSink,
    ModelInvocationService,
    ModelRequest,
    str,
    int,
    SequenceAgent,
    ControlledOpenAIClientFactory | None,
    PydanticAIModelProvider,
    ModelRoutePlan,
]:
    """从 typed settings 建 v2 root ledger，并返回真实 invocation 公共 seam。"""

    settings = retry_settings(classifier=classifier)
    _model_settings, request, policy = retry_route(classifier=classifier)
    registry = AgentRegistry(
        [
            AgentDescriptor(
                agent_id="agent-a",
                version="v1",
                name="真实 retry 预算合同",
                description="只使用离线 provider double",
                input_schema_ref="fixture.Input",
                output_schema_ref="fixture.Output",
                output_schema_identity=fixture_output_schema_identity(),
                config_ref="fixture/config.yaml",
                tool_policy=AgentToolPolicy(allowed_tools=[]),
                model_policy=policy,
                budget=AgentBudget(max_tokens_per_run=4096, max_cost_usd_per_run=1.0),
                eval_dataset=None,
                delegation_targets=[],
            )
        ]
    )
    shared_budget = SharedBudgetRuntime(settings=settings, registry=registry)
    dsn = f"sqlite+aiosqlite:///{tmp_path / database_name}"
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

    sink = LocalJsonlEventSink(tmp_path / f"{database_name}.events.jsonl")

    async def resolve_trace(**_: object) -> str:
        """让 usage event 与固定测试 run 使用同一 trace。"""

        return "trace-a"

    agent = SequenceAgent(outcomes)
    client_factory: ControlledOpenAIClientFactory | None = None
    if transport_factory is None:
        provider = PydanticAIModelProvider(
            provider_id="openai-compatible",
            agent_factory=lambda _plan: agent,
        )
    else:
        client_factory = ControlledOpenAIClientFactory(
            model_settings=settings.model,
            transport_factory=transport_factory,
        )
        provider = PydanticAIModelProvider(
            provider_id="openai-compatible",
            client_factory=client_factory,
        )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    plan = router.plan(request, agent_policy=policy)
    service = service_type(
        router=router,
        storage=storage,
        event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        shared_budget=shared_budget,
        agent_policy_resolver=lambda _agent_id: policy,
    )
    return (
        storage,
        sink,
        service,
        request,
        run_id,
        plan.reserved_token_bound,
        agent,
        client_factory,
        provider,
        plan,
    )


async def assert_unresolved_real_settlement(
    *,
    storage: SQLAlchemyStorage,
    run_id: str,
    usage_call_id: str,
    reserved_tokens: int,
    expected_attempt_count: int,
) -> None:
    """从公开 final evidence 与 durable ledger 两侧证明 reservation 未被错误退款。"""

    async with storage.uow() as uow:
        ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
        claim_rows = list(
            await uow.session.scalars(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == usage_call_id
                )
            )
        )
        outbox = await uow.evidence_outbox.get_usage(
            tenant_id="tenant-a",
            usage_call_id=usage_call_id,
        )
        terminal_allowed = await uow.shared_budget.terminal_allowed("tenant-a", run_id)
        assert ledger is not None
        assert len(claim_rows) == 1
        claim = claim_rows[0]
        ledger_state = ledger.state
        ledger_token_impact = ledger.token_impact
        claim_state = claim.state
        claim_side_effect_state = claim.side_effect_state
        claim_reserved_tokens = claim.reserved_tokens
        claim_actual_tokens = claim.actual_tokens
        claim_token_impact = claim.token_impact
        outbox_state = outbox.state
        outbox_result = outbox.result_json
        with pytest.raises(BudgetReservationRejected) as terminal_error:
            await uow.shared_budget.fence_terminal("tenant-a", run_id)

    assert terminal_error.value.reason == "ledger_needs_review"
    assert ledger_state == "needs_review"
    assert ledger_token_impact == reserved_tokens
    assert terminal_allowed is False
    assert claim_state == "needs_review"
    assert claim_side_effect_state == "result_committed"
    assert claim_reserved_tokens == reserved_tokens
    assert claim_actual_tokens is None
    assert claim_token_impact == reserved_tokens
    assert outbox_state == "published"
    assert outbox_result is not None
    evidence = outbox_result["evidence"]
    decision = evidence["decision"]
    assert len(decision["attempts"]) == expected_attempt_count
    assert decision["budget_charge"] == {
        "charged_tokens": None,
        "charged_cost_usd": None,
        "charge_status": "unknown",
        "unresolved_attempts": [1],
    }
    assert evidence["input_tokens"] is None
    assert evidence["output_tokens"] is None
    assert evidence["cost_usd"] is None
