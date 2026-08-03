"""结构化 delegated allocation 预约与实际结算合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from tests.contracts.agent_delegation_service_identity_test_support import (
    _identity as delegation_identity,
)
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _build_service as build_delegation_service,
)
from tests.contracts.test_provider_neutral_structured_budget_contracts import (
    BudgetOutput,
    budget_identity,
    budget_request,
)
from tests.contracts.test_shared_parent_budget_invocation_contracts import TestIdentityRuntime

from agent_harness.delegation import DelegationRequest
from agent_harness.events import EventBus
from agent_harness.models import (
    FakeModelProvider,
    FakeStructuredScript,
    ModelInvocationService,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
    compile_output_schema,
    stable_usage_call_id,
)
from agent_harness.runtime import RunStatus
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
)


@pytest.mark.asyncio
async def test_delegated_structured_uses_allocation_not_direct_claim(tmp_path: Path) -> None:
    """Child structured 调用只结算 relation-scoped allocation，不建 direct claim。"""

    storage, delegation_service, _runtime, parent_run_id, sink = await build_delegation_service(
        tmp_path,
        mode="service",
        child_status=RunStatus.RUNNING,
        source_cost_limit=None,
        root_token_limit=5_000,
        target_token_limit=5_000,
    )
    schema = compile_output_schema(BudgetOutput, schema_ref="fixture.Output", version="v1")
    provider = FakeModelProvider(
        structured_script=FakeStructuredScript(candidates=({"answer": "delegated"},))
    )
    service: ModelInvocationService | None = None
    try:
        delegated = await delegation_service.delegate(
            DelegationRequest(
                parent_run_id=parent_run_id,
                source_agent_id="agent-source",
                target_agent_id="agent-target",
                child_input={"prompt": "structured child"},
                idempotency_key="structured-allocation",
                request_id="request-a",
            ),
            identity=delegation_identity(),
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
            event_bus=EventBus(sink=sink),
            shared_budget=TestIdentityRuntime(),
            output_schema_resolver=lambda _agent_id: schema,
        )
        bound = service.bind_execution(
            identity=budget_identity(),
            tenant_id="tenant-a",
            run_id=delegated.child_run_id,
            agent_id="agent-target",
            request_id="request-a",
            trace_id="trace-parent",
        )
        child_context = UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=delegated.child_run_id,
            agent_id="agent-target",
            request_id="request-a",
            trace_id="trace-parent",
        )
        response = await bound.complete_structured(
            budget_request(),
            operation_key="structured-child",
        )
        usage_call_id = stable_usage_call_id(
            context=child_context,
            operation_key="structured-child",
        )
        async with storage.uow() as uow:
            direct = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == usage_call_id
                )
            )
            allocation = await uow.session.scalar(
                select(DelegationBudgetAllocationModel).where(
                    DelegationBudgetAllocationModel.usage_call_id == usage_call_id
                )
            )
            allocation_facts = (
                None
                if allocation is None
                else (
                    allocation.state,
                    allocation.reserved_tokens,
                    allocation.actual_tokens,
                )
            )
        assert response.structured_output is not None
        assert direct is None
        assert allocation_facts is not None
        assert allocation_facts[0] == "settled"
        assert allocation_facts[2] is not None and allocation_facts[2] > 0
        assert allocation_facts[1] is not None
        assert allocation_facts[1] >= allocation_facts[2]
    finally:
        if service is not None:
            await service.aclose()
        await storage.dispose()
