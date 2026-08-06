"""模型工具循环与既有shared-budget owner的联合恢复合同。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from tests.contracts.model_tool_loop_contract_helpers import (
    initial_model_tool_loop_snapshot,
)
from tests.contracts.test_model_tool_execution_claim_permit_contracts import (
    _claim,  # pyright: ignore[reportPrivateUsage]
)
from tests.contracts.test_policy_gated_model_tool_loop_public_seam_contracts import (
    FakeContextAssembly,
    FakeToolRegistry,
    ScriptedModelTurns,
    ScriptStep,
    _request,  # pyright: ignore[reportPrivateUsage]
    model_loop_limits_fixture,
    model_policy_fixture,
    tool_catalog_fixture,
)
from tests.contracts.test_shared_parent_budget_invocation_contracts import (
    CountingFakeModelProvider,
    context,
    model_request,
    model_service,
    seed_managed_root,
)

from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import ModelUsageEvidence, UsageEvidenceContext
from agent_harness.runtime import ModelToolLoopError, ModelToolLoopService
from agent_harness.storage import (
    ModelToolLoopCreate,
    SQLAlchemyStorage,
    run_migrations,
)
from agent_harness.storage.shared_budget import BudgetReservationRejected
from agent_harness.tools import (
    ModelToolExecutionClaimService,
    ModelToolExecutionNeedsReview,
    ToolExecutionPermit,
)


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


class _UnknownUsageModelTurns(ScriptedModelTurns):
    """模型结果已返回但durable usage无法可信读取的公共runtime替身。"""

    async def read_tool_loop_turn_usage(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
    ) -> ModelUsageEvidence:
        del context, usage_call_id, loop_id, turn_ordinal
        return cast(ModelUsageEvidence, object())


async def _seed_tool_loop(
    storage: SQLAlchemyStorage,
    *,
    run_id: str,
    loop_id: str,
) -> None:
    """在受管理root内创建最小active loop，供工具未知状态联合关闭。"""

    async with storage.uow() as uow:
        await uow.model_tool_loops.create(
            ModelToolLoopCreate(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                loop_id=loop_id,
                request_identity_digest="a" * 64,
                operation_identity_digest="b" * 64,
                catalog_digest="c" * 64,
                **initial_model_tool_loop_snapshot(),
                owner_lease_digest="d" * 64,
                owner_fence=1,
                owner_lease_expires_at=datetime(2031, 1, 1, tzinfo=UTC),
            )
        )
        await uow.commit()


@pytest.mark.asyncio
async def test_unknown_tool_effect_fences_same_parent_ledger_without_releasing_model_impact(
    tmp_path: Path,
) -> None:
    """工具副作用未知必须保留已结算model impact并阻止后续消费与terminal。"""

    dsn = _dsn(tmp_path / "model-tool-shared-budget-unknown.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    provider = CountingFakeModelProvider()
    service = model_service(
        storage=storage,
        sink=LocalJsonlEventSink(tmp_path / "model-events.jsonl"),
        provider=provider,
    )
    now = datetime(2030, 1, 1, tzinfo=UTC)
    loop_id = "2" * 64
    try:
        run_id = await seed_managed_root(storage, token_limit=100)
        await service.complete(
            model_request(),
            context=context(run_id),
            usage_call_id="usage-before-tool-unknown",
        )
        await _seed_tool_loop(storage, run_id=run_id, loop_id=loop_id)
        claim = _claim(
            run_id,
            lease_expires_at=now + timedelta(minutes=1),
        ).model_copy(
            update={
                "tenant_id": "tenant-a",
                "loop_id": loop_id,
                "trace_id": "trace-a",
            }
        )
        async with storage.uow() as uow:
            await uow.tool_invocations.create_model_claim(claim)
            await uow.tool_invocations.begin_model_execution(data=claim, now=now)
            before = await uow.shared_budget.get_ledger("tenant-a", run_id)
            await uow.commit()
        assert before is not None

        with pytest.raises(ModelToolExecutionNeedsReview):
            await ModelToolExecutionClaimService(storage).acquire(claim, now=now)

        async with storage.uow() as uow:
            after = await uow.shared_budget.get_ledger("tenant-a", run_id)
            terminal_allowed = await uow.shared_budget.terminal_allowed("tenant-a", run_id)
        assert after is not None
        assert (after.token_impact, after.cost_impact) == (
            before.token_impact,
            before.cost_impact,
        )
        assert after.state == "needs_review"
        assert terminal_allowed is False

        calls_before_retry = provider.calls
        with pytest.raises(BudgetReservationRejected) as rejected:
            await service.complete(
                model_request(),
                context=context(run_id),
                usage_call_id="usage-after-tool-unknown",
            )
        assert rejected.value.reason == "ledger_needs_review"
        assert provider.calls == calls_before_retry
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_known_tool_replay_has_zero_budget_delta_and_terminal_blocks_new_consumption(
    tmp_path: Path,
) -> None:
    """已知工具结果不计价，model settlement精确重放且ledger终态后不可再消费。"""

    dsn = _dsn(tmp_path / "model-tool-shared-budget-known.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    provider = CountingFakeModelProvider()
    service = model_service(
        storage=storage,
        sink=LocalJsonlEventSink(tmp_path / "known-model-events.jsonl"),
        provider=provider,
    )
    now = datetime(2030, 1, 1, tzinfo=UTC)
    loop_id = "4" * 64
    try:
        run_id = await seed_managed_root(storage, token_limit=100)
        first_model = await service.complete(
            model_request(),
            context=context(run_id),
            usage_call_id="usage-known-tool",
        )
        model_replay = await service.complete(
            model_request(),
            context=context(run_id),
            usage_call_id="usage-known-tool",
        )
        await _seed_tool_loop(storage, run_id=run_id, loop_id=loop_id)
        claim = _claim(
            run_id,
            lease_expires_at=now + timedelta(minutes=1),
        ).model_copy(
            update={
                "tenant_id": "tenant-a",
                "loop_id": loop_id,
                "tool_call_id": "5" * 64,
                "trace_id": "trace-a",
            }
        )
        claim_service = ModelToolExecutionClaimService(storage)
        permit = await claim_service.acquire(claim, now=now)
        assert isinstance(permit, ToolExecutionPermit)
        await claim_service.require_handler_permit(permit, now=now)
        await claim_service.complete(
            permit,
            result_ref="artifact://known-tool-result",
            execution_state="completed",
            status="completed",
        )
        tool_replay = await ModelToolExecutionClaimService(storage).acquire(claim, now=now)

        async with storage.uow() as uow:
            before_terminal = await uow.shared_budget.get_ledger("tenant-a", run_id)
            await uow.shared_budget.fence_terminal_if_managed("tenant-a", run_id)
            await uow.commit()
        assert first_model == model_replay
        assert provider.calls == 1
        assert not isinstance(tool_replay, ToolExecutionPermit)
        assert tool_replay.result_ref == "artifact://known-tool-result"
        assert before_terminal is not None
        assert before_terminal.state == "active"
        assert 0 < before_terminal.token_impact < before_terminal.token_limit

        with pytest.raises(BudgetReservationRejected) as rejected:
            await service.complete(
                model_request(),
                context=context(run_id),
                usage_call_id="usage-after-ledger-terminal",
            )
        assert rejected.value.reason == "ledger_needs_review"
        assert provider.calls == 1
        async with storage.uow() as uow:
            terminal = await uow.shared_budget.get_ledger("tenant-a", run_id)
        assert terminal is not None
        assert terminal.state == "terminal"
        assert (terminal.token_impact, terminal.cost_impact) == (
            before_terminal.token_impact,
            before_terminal.cost_impact,
        )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_public_loop_usage_unknown_fences_parent_before_next_turn(tmp_path: Path) -> None:
    """usage无法交叉验证时，公开loop入口须同时关闭loop与root ledger。"""

    dsn = _dsn(tmp_path / "model-tool-loop-usage-unknown.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    model_turns = _UnknownUsageModelTurns((ScriptStep("final_text"),))
    registry = FakeToolRegistry()
    service = ModelToolLoopService(
        model_turns=model_turns,
        tool_catalog_resolver=lambda _agent_id, _selection: tool_catalog_fixture(),
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=FakeContextAssembly(),
        loop_limits_resolver=lambda _agent_id: model_loop_limits_fixture(),
        agent_model_policy_resolver=lambda _agent_id: model_policy_fixture(),
        storage=storage,
    )
    try:
        run_id = await seed_managed_root(storage, token_limit=100)
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
                roles=["member"],
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )

        with pytest.raises(ModelToolLoopError) as failure:
            await bound.run(_request(), operation_key="usage-unknown")

        assert failure.value.code == "model.tool_loop_needs_review"
        assert len(model_turns.calls) == 1
        assert registry.resolve_count == registry.handler_count == 0
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger("tenant-a", run_id)
            loop = await uow.model_tool_loops.get("tenant-a", model_turns.calls[0][2])
        assert ledger is not None and ledger.state == "needs_review"
        assert loop is not None and loop.status == "needs_review"
    finally:
        await storage.dispose()
