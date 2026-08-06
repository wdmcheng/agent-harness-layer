"""Tool-intent shared budget replay 与 hard rejection 结算合同。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.test_controlled_real_model_budget_snapshot_contracts import _registry
from tests.contracts.test_controlled_real_model_config_contracts import PROFILES
from tests.contracts.test_tool_intent_model_catalog_config_contracts import (
    _router_and_policy,
    _tool_catalog,
    tool_intent_override,
)
from tests.contracts.test_tool_intent_usage_settlement_contracts import (
    _run_id,
    _ToolIntentProvider,
)

from agent_harness.config import load_settings
from agent_harness.events import EventBus
from agent_harness.events.sinks.local_jsonl import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelInvocationService,
    ModelRequest,
    stable_usage_call_id,
)
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.registry import AgentRegistry
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.shared_budget import BudgetReservationRejected


@pytest.mark.asyncio
async def test_shared_budget_exact_replay_uses_durable_catalog_not_current_resolver(
    tmp_path: Path,
) -> None:
    """结果已耐久后即使current Registry漂移，也必须先重放且不触碰resolver/provider。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'shared-tool-intent.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await _run_id(storage, agent_id="agent-real")
    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=tool_intent_override(),
    )
    registry = _registry()
    shared_budget = SharedBudgetRuntime(settings=settings, registry=registry)
    ledger = shared_budget.ledger_create(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-real",
    )
    async with storage.uow() as uow:
        await uow.shared_budget.create_ledger(ledger)
        await uow.commit()
    provider = _ToolIntentProvider()
    router, policy = _router_and_policy()
    cast(Any, router)._providers["openai-compatible"] = provider
    sink = LocalJsonlEventSink(tmp_path / "shared-events.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    resolver_calls = 0

    def resolve_catalog(_agent_id: str, _selection: object):
        nonlocal resolver_calls
        resolver_calls += 1
        if resolver_calls > 1:
            raise AssertionError("current Registry must not be read during exact replay")
        return _tool_catalog()

    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        shared_budget=shared_budget,
        agent_policy_resolver=lambda _: policy,
        tool_catalog_resolver=resolve_catalog,
    )
    bound = service.bind_execution(
        identity=IdentityContext(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
        ),
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-real",
        request_id="request-a",
        trace_id="trace-a",
    )
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        prompt="find weather",
        capability="tool_intent",
        max_output_tokens=8,
    )
    try:
        first = await bound.complete_tool_intent(request, operation_key="turn-1")
        replay = await bound.complete_tool_intent(request, operation_key="turn-1")
        assert replay == first
        assert resolver_calls == 1
        assert provider.prepare_count == provider.send_count == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_tool_intent_hard_budget_rejection_keeps_zero_usage_claim_and_provider(
    tmp_path: Path,
) -> None:
    """联合 reservation 超过 hard budget 时不得留下本轮 claim 或创建 provider。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'tool-intent-budget-reject.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    run_id = await _run_id(storage, agent_id="agent-real")
    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=tool_intent_override(),
    )
    descriptor = _registry().get("agent-real")
    descriptor.budget.max_tokens_per_run = 100
    registry = AgentRegistry([descriptor])
    shared_budget = SharedBudgetRuntime(settings=settings, registry=registry)
    ledger = shared_budget.ledger_create(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-real",
    )
    async with storage.uow() as uow:
        await uow.shared_budget.create_ledger(ledger)
        await uow.commit()
    provider = _ToolIntentProvider()
    router, policy = _router_and_policy()
    cast(Any, router)._providers["openai-compatible"] = provider
    sink = LocalJsonlEventSink(tmp_path / "tool-intent-budget-reject-events.jsonl")

    async def resolve_trace(**_: object) -> str:
        return "trace-a"

    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        shared_budget=shared_budget,
        agent_policy_resolver=lambda _: policy,
        tool_catalog_resolver=lambda _agent_id, _selection: _tool_catalog(),
    )
    bound = service.bind_execution(
        identity=IdentityContext(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
        ),
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-real",
        request_id="request-a",
        trace_id="trace-a",
    )
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        prompt="find weather",
        capability="tool_intent",
        max_output_tokens=8,
    )
    try:
        with pytest.raises(BudgetReservationRejected):
            await bound.complete_tool_intent(request, operation_key="turn-1")
        async with storage.uow() as uow:
            with pytest.raises(LookupError):
                await uow.evidence_outbox.get_usage(
                    tenant_id="tenant-a",
                    usage_call_id=stable_usage_call_id(
                        context=UsageEvidenceContext(
                            tenant_id="tenant-a",
                            run_id=run_id,
                            agent_id="agent-real",
                            request_id="request-a",
                            trace_id="trace-a",
                        ),
                        operation_key="turn-1",
                    ),
                )
        assert provider.prepare_count == provider.send_count == 0
    finally:
        await storage.dispose()
