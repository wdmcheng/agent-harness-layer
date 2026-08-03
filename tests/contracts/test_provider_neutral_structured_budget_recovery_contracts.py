"""结构化预算崩溃恢复与owner围栏合同。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.contracts.agent_delegation_service_identity_test_support import (
    _identity as delegation_identity,
)
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _build_service as build_delegation_service,
)
from tests.contracts.provider_neutral_structured_output_test_support import (
    StructuredCrashAfterSend,
    StructuredCrashProvider,
)
from tests.contracts.test_provider_neutral_structured_budget_contracts import (
    BudgetOutput,
    budget_identity,
    budget_request,
)
from tests.contracts.test_shared_parent_budget_invocation_contracts import (
    TestIdentityRuntime,
    context,
    resolve_trace,
    seed_managed_root,
)

from agent_harness.delegation import DelegationRequest
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    ModelInvocationService,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
    compile_output_schema,
    stable_usage_call_id,
)
from agent_harness.runtime import RunStatus
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
)


@pytest.mark.asyncio
async def test_direct_started_crash_keeps_full_reservation_and_fences_owner_ledger(
    tmp_path: Path,
) -> None:
    """Direct send 后崩溃恢复为 unknown，并保留完整 reservation 与 owner fence。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured-direct-crash.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "structured-direct-crash-events.jsonl")
    schema = compile_output_schema(BudgetOutput, schema_ref="fixture.Output", version="v1")
    provider = StructuredCrashProvider(schema)
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
                    default_provider="fake",
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
        with pytest.raises(StructuredCrashAfterSend):
            await bound.complete_structured(
                budget_request(),
                operation_key="structured-direct-crash",
                repair_limit=1,
            )
        assert await service.recover_pending(run_id=run_id) == 1
        usage_call_id = stable_usage_call_id(
            context=context(run_id),
            operation_key="structured-direct-crash",
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
                    claim.actual_tokens,
                    claim.token_impact,
                    claim.reserved_tokens,
                )
            )
            ledger_facts = None if ledger is None else (ledger.state, ledger.token_impact)
        assert claim_facts is not None and ledger_facts is not None
        assert claim_facts[0] == "needs_review"
        assert claim_facts[1] is None
        assert claim_facts[2] == claim_facts[3]
        assert ledger_facts == ("needs_review", claim_facts[3])
        assert provider.sends == [(0, 1)]
    finally:
        if service is not None:
            await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_allocation_started_crash_keeps_full_reservation_and_fences_owner_ledger(
    tmp_path: Path,
) -> None:
    """Delegated send 后崩溃不得释放 allocation 或让 parent ledger 继续消费。"""

    storage, delegation_service, _runtime, parent_run_id, sink = await build_delegation_service(
        tmp_path,
        mode="service",
        child_status=RunStatus.RUNNING,
        source_cost_limit=None,
        root_token_limit=5_000,
        target_token_limit=5_000,
    )
    schema = compile_output_schema(BudgetOutput, schema_ref="fixture.Output", version="v1")
    provider = StructuredCrashProvider(schema)
    service: ModelInvocationService | None = None
    try:
        delegated = await delegation_service.delegate(
            DelegationRequest(
                parent_run_id=parent_run_id,
                source_agent_id="agent-source",
                target_agent_id="agent-target",
                child_input={"prompt": "structured crash child"},
                idempotency_key="structured-allocation-crash",
                request_id="request-a",
            ),
            identity=delegation_identity(),
        )
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(
                    default_provider="fake",
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
        with pytest.raises(StructuredCrashAfterSend):
            await bound.complete_structured(
                budget_request(),
                operation_key="structured-child-crash",
            )
        assert await service.recover_pending(run_id=delegated.child_run_id) == 1
        usage_call_id = stable_usage_call_id(
            context=child_context,
            operation_key="structured-child-crash",
        )
        async with storage.uow() as uow:
            allocation = await uow.session.scalar(
                select(DelegationBudgetAllocationModel).where(
                    DelegationBudgetAllocationModel.usage_call_id == usage_call_id
                )
            )
            top_claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.delegation_id == delegated.delegation_id
                )
            )
            ledger = await uow.shared_budget.get_ledger("tenant-a", parent_run_id)
            allocation_facts = (
                None
                if allocation is None
                else (
                    allocation.state,
                    allocation.actual_tokens,
                    allocation.token_impact,
                    allocation.reserved_tokens,
                )
            )
            top_claim_state = None if top_claim is None else top_claim.state
            ledger_state = None if ledger is None else ledger.state
        assert allocation_facts is not None
        assert allocation_facts[0] == "needs_review"
        assert allocation_facts[1] is None
        assert allocation_facts[2] == allocation_facts[3]
        assert top_claim_state == "needs_review"
        assert ledger_state == "needs_review"
        assert provider.sends == [(0, 1)]
    finally:
        if service is not None:
            await service.aclose()
        await storage.dispose()
