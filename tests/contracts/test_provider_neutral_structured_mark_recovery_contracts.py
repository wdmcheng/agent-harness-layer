"""结构化 durable mark 提交确认未知的恢复与预算围栏合同。"""

from __future__ import annotations

import asyncio
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
    FakeModelProvider,
    FakeStructuredScript,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    OutputSchemaDefinition,
    PreparedStructuredModelCall,
    StructuredProviderCandidate,
    UsageEvidenceContext,
    compile_output_schema,
    stable_usage_call_id,
)
from agent_harness.models._settlement_contracts import DurableMarkStateUnknown
from agent_harness.runtime import RunStatus
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_models import RunEvidenceOutboxModel
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
)


class _CancelAfterDurableStructuredMarkService(ModelInvocationService):
    """真实 mark 提交后注入确认未知，验证 send 边界仍保持零请求。"""

    async def _mark_side_effect_started(self, **kwargs: object) -> None:
        """先执行生产 mark 事务，再模拟 commit ack 返回窗口的取消。"""

        await super()._mark_side_effect_started(**kwargs)  # type: ignore[arg-type]
        raise DurableMarkStateUnknown


class _CloseFailingPreparedStructuredCall:
    """在保留真实 Fake cleanup 计数后注入 prepared close 失败。"""

    def __init__(self, inner: PreparedStructuredModelCall) -> None:
        self._inner = inner

    async def send_structured(
        self,
        *,
        provider_prompt: str,
        repair_ordinal: int,
        transport_ordinal: int,
    ) -> StructuredProviderCandidate:
        """保持公开 prepared 协议；本夹具只改变 cleanup 结果。"""

        return await self._inner.send_structured(
            provider_prompt=provider_prompt,
            repair_ordinal=repair_ordinal,
            transport_ordinal=transport_ordinal,
        )

    async def aclose(self) -> None:
        """先完成底层 cleanup，再模拟调用方无法确认的失败响应。"""

        await self._inner.aclose()
        raise RuntimeError("fixture structured cleanup failure")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cancel_window",
    ["before_commit", "after_commit", "after_commit_close_failed"],
)
async def test_shared_budget_mark_commit_ack_unknown_keeps_reservation_and_needs_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_window: str,
) -> None:
    """mark 提交确认未知虽能证明零请求，仍须保留预约并围栏账本。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured-mark-cancel.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "structured-mark-cancel-events.jsonl")
    schema = compile_output_schema(BudgetOutput, schema_ref="fixture.Output", version="v1")
    provider = FakeModelProvider(
        structured_script=FakeStructuredScript(candidates=({"answer": "unused"},))
    )
    service: ModelInvocationService | None = None
    mark_started = asyncio.Event()
    mark_gate = asyncio.Event()
    try:
        run_id = await seed_managed_root(
            storage,
            token_limit=5_000,
            soft_token_limit=5_000,
        )
        async with storage.uow() as uow:
            repository_type = type(uow.shared_budget)

        async def blocked_mark(_repository: object, **_kwargs: object) -> None:
            """在真实生产 mark 事务的 repository 调用处暂停，再由调用方取消。"""

            mark_started.set()
            await mark_gate.wait()

        if cancel_window == "before_commit":
            monkeypatch.setattr(repository_type, "mark_direct_started", blocked_mark)
        if cancel_window == "after_commit_close_failed":
            original_prepare = provider.prepare_structured

            async def prepare_with_close_failure(
                request: ModelRequest,
                *,
                plan: object,
                schema: OutputSchemaDefinition,
            ) -> PreparedStructuredModelCall:
                """只在公开 provider seam 包装 prepared cleanup 故障。"""

                prepared = await original_prepare(request, plan=plan, schema=schema)
                return _CloseFailingPreparedStructuredCall(prepared)

            monkeypatch.setattr(provider, "prepare_structured", prepare_with_close_failure)
        service_type = (
            ModelInvocationService
            if cancel_window == "before_commit"
            else _CancelAfterDurableStructuredMarkService
        )
        service = service_type(
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
            identity=budget_identity(),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )

        async def invoke() -> None:
            """逐值断言三个 mark 失败窗口共享同一公开终态。"""

            with pytest.raises(ModelProviderInvocationError) as failure:
                await bound.complete_structured(
                    budget_request(),
                    operation_key="structured-mark-cancel",
                )
            assert failure.value.code == "model.provider_side_effect_unknown"
            assert failure.value.provider_called is False
            assert failure.value.attempt_count == 1

        if cancel_window == "before_commit":
            task = asyncio.create_task(invoke())
            await mark_started.wait()
            task.cancel()
            await task
        else:
            await invoke()
        assert provider.structured_send_count == 0
        assert provider.structured_close_count == 1
        usage_call_id = stable_usage_call_id(
            context=context(run_id),
            operation_key="structured-mark-cancel",
        )
        async with storage.uow() as uow:
            claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.usage_call_id == usage_call_id
                )
            )
            evidence = await uow.session.scalar(
                select(RunEvidenceOutboxModel).where(
                    RunEvidenceOutboxModel.usage_call_id == usage_call_id
                )
            )
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
            evidence_result = None if evidence is None else evidence.result_json
        assert claim_facts is not None
        assert claim_facts[0] == "needs_review"
        assert claim_facts[1] is None
        assert claim_facts[2] == claim_facts[3]
        assert evidence_result is not None
        final_evidence = evidence_result["evidence"]
        assert final_evidence["decision"]["provider_called"] is False
        structured_summary = final_evidence["decision"]["structured_output"]
        assert structured_summary["status"] == "needs_review"
        assert structured_summary["provider_request_count"] == 0
        structured_attempt = final_evidence["decision"]["attempts"][0]["structured_output"]
        assert structured_attempt["cleanup_status"] == (
            "failed" if cancel_window == "after_commit_close_failed" else "completed"
        )
    finally:
        mark_gate.set()
        if service is not None:
            await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_allocation_mark_commit_ack_unknown_fences_parent_budget(
    tmp_path: Path,
) -> None:
    """Delegated mark 提交确认未知须保留 allocation 并围栏 parent owner。"""

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
        structured_script=FakeStructuredScript(candidates=({"answer": "unused"},))
    )
    service: ModelInvocationService | None = None
    try:
        delegated = await delegation_service.delegate(
            DelegationRequest(
                parent_run_id=parent_run_id,
                source_agent_id="agent-source",
                target_agent_id="agent-target",
                child_input={"prompt": "structured mark unknown child"},
                idempotency_key="structured-allocation-mark-unknown",
                request_id="request-a",
            ),
            identity=delegation_identity(),
        )
        service = _CancelAfterDurableStructuredMarkService(
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
        child_context = UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=delegated.child_run_id,
            agent_id="agent-target",
            request_id="request-a",
            trace_id="trace-parent",
        )
        bound = service.bind_execution(
            identity=budget_identity(),
            tenant_id="tenant-a",
            run_id=delegated.child_run_id,
            agent_id="agent-target",
            request_id="request-a",
            trace_id="trace-parent",
        )

        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                budget_request(),
                operation_key="structured-child-mark-unknown",
            )
        assert failure.value.code == "model.provider_side_effect_unknown"
        assert failure.value.provider_called is False
        assert provider.structured_send_count == 0

        usage_call_id = stable_usage_call_id(
            context=child_context,
            operation_key="structured-child-mark-unknown",
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
    finally:
        if service is not None:
            await service.aclose()
        await storage.dispose()
