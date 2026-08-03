"""结构化 direct reservation、实际结算与硬预算拒绝合同。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from tests.contracts.test_shared_parent_budget_invocation_contracts import (
    TestIdentityRuntime,
    context,
    resolve_trace,
    seed_managed_root,
)

from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    FakeStructuredScript,
    ModelInvocationService,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    compile_output_schema,
    stable_usage_call_id,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.shared_budget import BudgetReservationRejected
from agent_harness.storage.shared_budget_models import BudgetOperationClaimModel


class _Output(BaseModel):
    """预算合同使用的最小严格 structured 输出。"""

    model_config = ConfigDict(extra="forbid")

    answer: str


def _request() -> ModelRequest:
    """返回不携带可信预算字段的业务请求。"""

    return ModelRequest(
        provider="fake",
        model="fake-basic",
        prompt="return an answer",
        max_output_tokens=8,
    )


def _identity() -> IdentityContext:
    """绑定现有 shared-budget fixture 的调用身份。"""

    return IdentityContext(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )


# 崩溃恢复测试分文件后只通过这些稳定夹具名复用输入，避免依赖私有实现名。
BudgetOutput = _Output
budget_identity = _identity
budget_request = _request


@pytest.mark.asyncio
async def test_direct_structured_reserves_all_repair_requests_then_replaces_with_actual(
    tmp_path: Path,
) -> None:
    """Direct claim 先占 transport×repair 上界，完成后只保留两次真实 usage。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured-direct-budget.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "structured-direct-budget-events.jsonl")
    schema = compile_output_schema(_Output, schema_ref="fixture.Output", version="v1")
    provider = FakeModelProvider(
        structured_script=FakeStructuredScript(
            candidates=({"wrong": 1}, {"answer": "ok"}),
        )
    )
    service: ModelInvocationService | None = None
    try:
        run_id = await seed_managed_root(
            storage,
            token_limit=5_000,
            soft_token_limit=5_000,
        )
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_model="fake-basic",
                    max_tokens_per_call=5_000,
                    input_token_price_usd=Decimal("0"),
                    output_token_price_usd=Decimal("0"),
                    price_source_ref="catalog:fake",
                    price_source_version="catalog-v1",
                ),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
            output_schema_resolver=lambda _agent_id: schema,
        )
        bound = service.bind_execution(
            identity=_identity(),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        response = await bound.complete_structured(
            _request(),
            operation_key="structured-direct",
            repair_limit=1,
        )
        usage_call_id = stable_usage_call_id(
            context=context(run_id),
            operation_key="structured-direct",
        )
        actual_tokens = sum(
            (attempt.input_tokens or 0) + (attempt.output_tokens or 0)
            for attempt in response.attempts
        )
        async with storage.uow() as uow:
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == usage_call_id
                )
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            claim_facts = (
                None
                if claim is None
                else (
                    claim.state,
                    claim.reserved_tokens,
                    claim.actual_tokens,
                    claim.token_impact,
                )
            )
        assert response.structured_output is not None
        assert response.structured_output.provider_request_count == 2
        assert claim_facts is not None
        assert claim_facts[0] == "settled"
        assert claim_facts[1] > actual_tokens
        assert claim_facts[2] == actual_tokens
        assert claim_facts[3] == actual_tokens
        assert ledger is not None and ledger.token_impact == actual_tokens
    finally:
        if service is not None:
            await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_insufficient_direct_budget_rejects_before_provider_send(tmp_path: Path) -> None:
    """完整 structured prompt 上界超过 root hard budget 时零 provider 调用。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured-budget-rejected.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "structured-budget-rejected-events.jsonl")
    schema = compile_output_schema(_Output, schema_ref="fixture.Output", version="v1")
    provider = FakeModelProvider(
        structured_script=FakeStructuredScript(candidates=({"answer": "unused"},))
    )
    service: ModelInvocationService | None = None
    try:
        run_id = await seed_managed_root(
            storage,
            token_limit=100,
            soft_token_limit=5_000,
        )
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic", max_tokens_per_call=5_000),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
            output_schema_resolver=lambda _agent_id: schema,
        )
        bound = service.bind_execution(
            identity=_identity(),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        with pytest.raises(BudgetReservationRejected) as rejected:
            await bound.complete_structured(
                _request(),
                operation_key="structured-rejected",
                repair_limit=1,
            )
        assert rejected.value.reason == "hard_limit_ineligible"
        assert provider.structured_send_count == 0
        assert provider.structured_close_count == 0
    finally:
        if service is not None:
            await service.aclose()
        await storage.dispose()
