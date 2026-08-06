"""模型工具循环五项hard bounds与exact缩权合同。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.contracts.test_policy_gated_model_tool_loop_public_seam_contracts import (
    FakeContextAssembly,
    FakeToolRegistry,
    ScriptedModelTurns,
    ScriptStep,
    model_policy_fixture,
    tool_catalog_fixture,
    tool_intent_request_fixture,
)

from agent_harness.identity import IdentityContext
from agent_harness.models import ModelRequest, ModelUsageEvidence, UsageEvidenceContext
from agent_harness.registry import AgentModelToolLoop
from agent_harness.runtime import (
    BoundModelToolLoopService,
    ModelToolLoopError,
    ModelToolLoopLimitOverrides,
    ModelToolLoopService,
    build_execution_context,
)
from agent_harness.storage import ModelToolLoopCumulativeUsage, ModelToolLoopFrozenBounds
from agent_harness.tools import ResolvedToolIntent, ToolCallResult


def _maxima() -> AgentModelToolLoop:
    """提供足够小且完整的测试Agent maxima。"""

    return AgentModelToolLoop(
        max_turns=4,
        max_total_tokens=20,
        max_total_cost_usd=2.0,
        max_tool_output_bytes=128,
        max_duration_seconds=10,
    )


def _inherit_all() -> ModelToolLoopLimitOverrides:
    """显式null与入口缺省等价，五字段仍必须全部出现。"""

    return ModelToolLoopLimitOverrides(
        max_turns=None,
        max_total_tokens=None,
        max_total_cost_usd=None,
        max_tool_output_bytes=None,
        max_duration_seconds=None,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "max_turns": None,
            "max_total_tokens": None,
            "max_total_cost_usd": None,
            "max_tool_output_bytes": None,
        },
        {
            "max_turns": None,
            "max_total_tokens": None,
            "max_total_cost_usd": None,
            "max_tool_output_bytes": None,
            "max_duration_seconds": None,
            "deadline_at": 1,
        },
        {
            "max_turns": None,
            "max_total_tokens": None,
            "max_total_cost_usd": None,
            "max_tool_output_bytes": None,
            "max_duration_seconds": None,
            "loop_started_at": "2026-08-04T00:00:00Z",
        },
        {
            "max_turns": True,
            "max_total_tokens": None,
            "max_total_cost_usd": None,
            "max_tool_output_bytes": None,
            "max_duration_seconds": None,
        },
        {
            "max_turns": None,
            "max_total_tokens": 0,
            "max_total_cost_usd": None,
            "max_tool_output_bytes": None,
            "max_duration_seconds": None,
        },
        {
            "max_turns": None,
            "max_total_tokens": None,
            "max_total_cost_usd": True,
            "max_tool_output_bytes": None,
            "max_duration_seconds": None,
        },
        {
            "max_turns": None,
            "max_total_tokens": None,
            "max_total_cost_usd": float("nan"),
            "max_tool_output_bytes": None,
            "max_duration_seconds": None,
        },
        {
            "max_turns": None,
            "max_total_tokens": None,
            "max_total_cost_usd": float("inf"),
            "max_tool_output_bytes": None,
            "max_duration_seconds": None,
        },
        {
            "max_turns": None,
            "max_total_tokens": None,
            "max_total_cost_usd": -0.1,
            "max_tool_output_bytes": None,
            "max_duration_seconds": None,
        },
    ],
)
def test_limit_overrides_are_exact_and_reject_invalid_values(payload: dict[str, Any]) -> None:
    """缺失、额外、bool、非有限或越界值不能被Pydantic隐式修复。"""

    with pytest.raises(ValidationError):
        ModelToolLoopLimitOverrides.model_validate(payload)


def test_limit_overrides_accept_null_inheritance_and_individual_shrinking() -> None:
    """合法DTO保留五项exact nullable字段。"""

    assert _inherit_all().model_dump(mode="json") == {
        "max_turns": None,
        "max_total_tokens": None,
        "max_total_cost_usd": None,
        "max_tool_output_bytes": None,
        "max_duration_seconds": None,
    }
    narrowed = ModelToolLoopLimitOverrides(
        max_turns=2,
        max_total_tokens=10,
        max_total_cost_usd=1.0,
        max_tool_output_bytes=64,
        max_duration_seconds=5,
    )
    assert narrowed.max_turns == 2


def test_public_and_durable_cost_dtos_reject_bool_before_numeric_coercion() -> None:
    """public maxima与耐久余额都不能把JSON bool静默解释成一美元。"""

    with pytest.raises(ValidationError):
        AgentModelToolLoop.model_validate(
            {
                "max_turns": 4,
                "max_total_tokens": 20,
                "max_total_cost_usd": True,
                "max_tool_output_bytes": 128,
                "max_duration_seconds": 10,
            }
        )
    with pytest.raises(ValidationError):
        ModelToolLoopFrozenBounds.model_validate(
            {
                "max_turns": 4,
                "max_total_tokens": 20,
                "max_total_cost_usd": True,
                "max_tool_output_bytes": 128,
                "max_duration_seconds": 10,
                "loop_started_at": "2026-08-04T00:00:00Z",
                "deadline_at": "2026-08-04T00:00:10Z",
            }
        )
    with pytest.raises(ValidationError):
        ModelToolLoopCumulativeUsage.model_validate(
            {
                "turns_completed": 1,
                "total_tokens_used": 2,
                "total_cost_usd": True,
            }
        )


class _MeteredModelTurns(ScriptedModelTurns):
    """每轮返回固定durable usage，独立于provider实现。"""

    def __init__(
        self,
        *,
        tokens: int,
        cost_usd: float | None,
        clock: _TrustedClock | None = None,
        advance_after_turn_seconds: int = 0,
    ) -> None:
        super().__init__((ScriptStep("tool_intent"), ScriptStep("final_text")))
        self.tokens = tokens
        self.cost_usd = cost_usd
        self.clock = clock
        self.advance_after_turn_seconds = advance_after_turn_seconds

    async def complete_tool_loop_turn(
        self,
        request: ModelRequest,
        **kwargs: object,
    ) -> object:
        """可在provider结算后推进受信时钟，验证边界后的短路。"""

        result = await super().complete_tool_loop_turn(request, **kwargs)
        if self.clock is not None:
            self.clock.advance(self.advance_after_turn_seconds)
        return result

    async def read_tool_loop_turn_usage(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
    ) -> ModelUsageEvidence:
        del usage_call_id, loop_id, turn_ordinal
        return ModelUsageEvidence(
            usage_kind="model",
            tenant_id=context.tenant_id,
            provider="fake",
            model="fake-tool-model",
            input_tokens=self.tokens,
            output_tokens=0,
            cost_usd=self.cost_usd,
            cost_status="unavailable" if self.cost_usd is None else "reported",
            latency_ms=1,
            decision={},
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )


class _ReservationAwareModelTurns(_MeteredModelTurns):
    """记录每轮进入model runtime前的loop剩余预约上界。"""

    def __init__(self) -> None:
        super().__init__(tokens=1, cost_usd=0.1)
        self.loop_reservations: list[tuple[int, float | None]] = []

    async def complete_tool_loop_turn(
        self,
        request: ModelRequest,
        **kwargs: object,
    ) -> object:
        token_bound = kwargs["loop_token_bound"]
        cost_bound = kwargs["loop_cost_bound"]
        assert type(token_bound) is int
        assert cost_bound is None or type(cost_bound) is float
        self.loop_reservations.append((token_bound, cost_bound))
        return await super().complete_tool_loop_turn(request, **kwargs)


class _TrustedClock:
    """只由composition注入的可推进UTC时钟。"""

    def __init__(self) -> None:
        self.now = datetime(2026, 8, 4, tzinfo=UTC)
        self.monotonic_now = 0.0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)
        self.monotonic_now += seconds

    def monotonic(self) -> float:
        """返回与wall clock独立的单进程递增时间。"""

        return self.monotonic_now


class _RollbackClock(_TrustedClock):
    """模拟wall clock回拨，但进程内monotonic仍持续前进。"""

    def advance(self, seconds: int) -> None:
        self.now -= timedelta(seconds=seconds)
        self.monotonic_now += seconds


def _bound_limits_loop(
    *,
    model: _MeteredModelTurns,
    clock: _TrustedClock,
    registry: FakeToolRegistry | None = None,
) -> tuple[BoundModelToolLoopService, FakeToolRegistry]:
    """绑定完整Agent maxima、受信时钟和副作用计数Registry。"""

    registry = registry or FakeToolRegistry()
    service = ModelToolLoopService(
        model_turns=model,
        tool_catalog_resolver=lambda _agent_id, _selection: tool_catalog_fixture(),
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=FakeContextAssembly(),
        loop_limits_resolver=lambda _agent_id: _maxima(),
        agent_model_policy_resolver=lambda _agent_id: model_policy_fixture(),
        trusted_clock=clock,
        monotonic_clock=clock.monotonic,
    )
    execution = build_execution_context(
        identity=IdentityContext.local_default(session_id="loop-limits"),
        services={"model_tool_loop": service},
        agent_id="agent-a",
        run_id="run-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    bound = execution.require_service("model_tool_loop")
    assert isinstance(bound, BoundModelToolLoopService)
    return bound, registry


@pytest.mark.asyncio
async def test_expanding_or_untyped_limits_fail_before_model_registry_or_tool() -> None:
    """任意扩大值和raw dict在全部副作用前关闭。"""

    model = _MeteredModelTurns(tokens=1, cost_usd=0.1)
    bound, registry = _bound_limits_loop(model=model, clock=_TrustedClock())
    expanded = ModelToolLoopLimitOverrides(
        max_turns=5,
        max_total_tokens=None,
        max_total_cost_usd=None,
        max_tool_output_bytes=None,
        max_duration_seconds=None,
    )
    for limits in (expanded, {"max_turns": 1}):
        with pytest.raises(ModelToolLoopError) as failure:
            await bound.run(
                tool_intent_request_fixture(),
                operation_key=f"invalid-{type(limits).__name__}",
                limits=cast(Any, limits),
            )
        assert failure.value.code == "model.tool_loop_limit_invalid"
    assert model.calls == []
    assert registry.resolve_count == registry.handler_count == 0


@pytest.mark.asyncio
async def test_token_or_cost_exhaustion_stops_before_tool_or_next_model() -> None:
    """模型实际usage命中循环余额后，不再产生工具或下一模型副作用。"""

    model = _MeteredModelTurns(tokens=6, cost_usd=0.6)
    bound, registry = _bound_limits_loop(model=model, clock=_TrustedClock())
    limits = ModelToolLoopLimitOverrides(
        max_turns=None,
        max_total_tokens=5,
        max_total_cost_usd=0.5,
        max_tool_output_bytes=None,
        max_duration_seconds=None,
    )
    with pytest.raises(ModelToolLoopError) as failure:
        await bound.run(
            tool_intent_request_fixture(),
            operation_key="usage-exhausted",
            limits=limits,
        )

    assert failure.value.code == "model.tool_loop_limit_exceeded"
    assert len(model.calls) == 1
    assert registry.resolve_count == registry.handler_count == 0


@pytest.mark.asyncio
async def test_each_model_reservation_receives_monotonically_decreasing_loop_balance() -> None:
    """每轮route/root预约还必须被同一冻结loop token/cost余额夹住。"""

    model = _ReservationAwareModelTurns()
    bound, registry = _bound_limits_loop(model=model, clock=_TrustedClock())
    limits = ModelToolLoopLimitOverrides(
        max_turns=None,
        max_total_tokens=5,
        max_total_cost_usd=0.5,
        max_tool_output_bytes=None,
        max_duration_seconds=None,
    )

    response = await bound.run(
        tool_intent_request_fixture(),
        operation_key="reservation-balance",
        limits=limits,
    )

    assert response.output_text == "done"
    assert model.loop_reservations == [
        (5, pytest.approx(0.5)),
        (4, pytest.approx(0.4)),
    ]
    assert registry.handler_count == 1


@pytest.mark.asyncio
async def test_trusted_deadline_is_not_caller_controlled_and_stops_before_model() -> None:
    """deadline只从composition时钟推导，首轮耗尽后不进入Registry或工具。"""

    clock = _TrustedClock()
    model = _MeteredModelTurns(
        tokens=1,
        cost_usd=0.1,
        clock=clock,
        advance_after_turn_seconds=2,
    )
    bound, registry = _bound_limits_loop(model=model, clock=clock)
    limits = ModelToolLoopLimitOverrides(
        max_turns=None,
        max_total_tokens=None,
        max_total_cost_usd=None,
        max_tool_output_bytes=None,
        max_duration_seconds=1,
    )
    with pytest.raises(ModelToolLoopError) as failure:
        await bound.run(
            tool_intent_request_fixture(),
            operation_key="trusted-deadline",
            limits=limits,
        )
    assert failure.value.code == "model.tool_loop_limit_exceeded"
    assert len(model.calls) == 1
    assert registry.resolve_count == registry.handler_count == 0


@pytest.mark.asyncio
async def test_monotonic_deadline_blocks_wall_clock_rollback_before_tool() -> None:
    """wall clock回拨不能延长同一进程内已冻结的工具授权窗口。"""

    clock = _RollbackClock()
    model = _MeteredModelTurns(
        tokens=1,
        cost_usd=0.1,
        clock=clock,
        advance_after_turn_seconds=2,
    )
    bound, registry = _bound_limits_loop(model=model, clock=clock)
    limits = ModelToolLoopLimitOverrides(
        max_turns=None,
        max_total_tokens=None,
        max_total_cost_usd=None,
        max_tool_output_bytes=None,
        max_duration_seconds=1,
    )

    with pytest.raises(ModelToolLoopError) as failure:
        await bound.run(
            tool_intent_request_fixture(),
            operation_key="wall-clock-rollback",
            limits=limits,
        )

    assert failure.value.code == "model.tool_loop_limit_exceeded"
    assert len(model.calls) == 1
    assert registry.resolve_count == registry.handler_count == 0


@pytest.mark.asyncio
async def test_turn_limit_stops_before_registry_and_tool() -> None:
    """最后允许回合若仍返回intent，不产生无后续模型可消费的工具副作用。"""

    model = _MeteredModelTurns(tokens=1, cost_usd=0.1)
    bound, registry = _bound_limits_loop(model=model, clock=_TrustedClock())
    limits = ModelToolLoopLimitOverrides(
        max_turns=1,
        max_total_tokens=None,
        max_total_cost_usd=None,
        max_tool_output_bytes=None,
        max_duration_seconds=None,
    )
    with pytest.raises(ModelToolLoopError) as failure:
        await bound.run(
            tool_intent_request_fixture(),
            operation_key="turn-limit",
            limits=limits,
        )
    assert failure.value.code == "model.tool_loop_limit_exceeded"
    assert len(model.calls) == 1
    assert registry.resolve_count == registry.handler_count == 0


class _LargeResultRegistry(FakeToolRegistry):
    """handler完成后返回超过冻结inline上限的已守卫结果。"""

    async def call(self, request: ResolvedToolIntent, **_: object) -> ToolCallResult:
        self.handler_count += 1
        return ToolCallResult(
            tool_name=request.tool_name,
            status="completed",
            invocation_id="large-result",
            result={"value": "x" * 128},
            source_ref=f"tool://{request.tool_call_id}",
            trust_level="untrusted",
            truncation={"truncated": False, "prompt_injection_signals": []},
        )


@pytest.mark.asyncio
async def test_output_byte_limit_stops_before_context_or_next_model() -> None:
    """守卫结果仍超限时允许既有handler结果闭合，但不得回注或再调模型。"""

    model = _MeteredModelTurns(tokens=1, cost_usd=0.1)
    registry = _LargeResultRegistry()
    bound, _ = _bound_limits_loop(
        model=model,
        clock=_TrustedClock(),
        registry=registry,
    )
    limits = ModelToolLoopLimitOverrides(
        max_turns=None,
        max_total_tokens=None,
        max_total_cost_usd=None,
        max_tool_output_bytes=32,
        max_duration_seconds=None,
    )
    with pytest.raises(ModelToolLoopError) as failure:
        await bound.run(
            tool_intent_request_fixture(),
            operation_key="output-limit",
            limits=limits,
        )
    assert failure.value.code == "model.tool_loop_limit_exceeded"
    assert len(model.calls) == 1
    assert registry.handler_count == 1
