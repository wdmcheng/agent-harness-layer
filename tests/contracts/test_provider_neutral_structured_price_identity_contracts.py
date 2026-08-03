"""结构化价格值与 catalog 来源身份的公开 seam 合同。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from tests.contracts.test_provider_neutral_structured_budget_contracts import (
    BudgetOutput,
    budget_identity,
    budget_request,
)
from tests.contracts.test_shared_parent_budget_invocation_contracts import (
    TestIdentityRuntime,
    resolve_trace,
    seed_managed_root,
)

from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    FakeStructuredScript,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    compile_output_schema,
)
from agent_harness.models._router_current import AgentModelPolicyLike
from agent_harness.models.router import ModelRoutePlan
from agent_harness.storage import SQLAlchemyStorage, run_migrations


class _IncompletePriceIdentityRouter(ModelRouter):
    """只畸变冻结价格来源，用公开 invocation seam 验证内部路由错误不外泄。"""

    def plan(
        self,
        request: ModelRequest,
        *,
        config: ModelRouterConfig | None = None,
        agent_policy: AgentModelPolicyLike | None = None,
    ) -> ModelRoutePlan:
        """保留正常路由行为，仅模拟持久化快照缺失一个来源版本。"""

        plan = super().plan(request, config=config, agent_policy=agent_policy)
        return plan.model_copy(update={"price_source_version": None})


@pytest.mark.asyncio
async def test_cost_disabled_structured_route_keeps_catalog_identity_and_completes(
    tmp_path: Path,
) -> None:
    """Cost 关闭时价格值保持 null，但 catalog 来源身份仍可耐久追踪。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured-cost-disabled.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "structured-cost-disabled-events.jsonl")
    schema = compile_output_schema(BudgetOutput, schema_ref="fixture.Output", version="v1")
    provider = FakeModelProvider(
        structured_script=FakeStructuredScript(candidates=({"answer": "ok"},))
    )
    service: ModelInvocationService | None = None
    try:
        run_id = await seed_managed_root(
            storage,
            token_limit=5_000,
            soft_token_limit=5_000,
            cost_limit=None,
            model_input_price=None,
            model_output_price=None,
        )
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_model="fake-basic",
                    max_tokens_per_call=5_000,
                ),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
            shared_budget=TestIdentityRuntime(),
            output_schema_resolver=lambda _agent_id: schema,
        )
        response = await service.bind_execution(
            identity=budget_identity(),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        ).complete_structured(
            budget_request(),
            operation_key="structured-cost-disabled",
        )

        assert response.structured_output is not None
        assert response.structured_output.value == {"answer": "ok"}
        assert provider.structured_send_count == 1
    finally:
        if service is not None:
            await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_incomplete_price_identity_is_mapped_at_public_structured_seam(
    tmp_path: Path,
) -> None:
    """冻结价格身份畸形须返回稳定公开错误，且不得创建 provider 请求。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured-price-identity.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "structured-price-identity-events.jsonl")
    schema = compile_output_schema(BudgetOutput, schema_ref="fixture.Output", version="v1")
    provider = FakeModelProvider(
        structured_script=FakeStructuredScript(candidates=({"answer": "unused"},))
    )
    service: ModelInvocationService | None = None
    try:
        run_id = await seed_managed_root(
            storage,
            token_limit=5_000,
            soft_token_limit=5_000,
        )
        service = ModelInvocationService(
            router=_IncompletePriceIdentityRouter(
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
            identity=budget_identity(),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )

        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                budget_request(),
                operation_key="structured-price-identity",
            )

        assert failure.value.code == "budget.reservation_rejected"
        assert failure.value.provider_called is False
        assert failure.value.attempt_count == 0
        assert provider.structured_send_count == 0
        assert provider.structured_close_count == 0
    finally:
        if service is not None:
            await service.aclose()
        await storage.dispose()
