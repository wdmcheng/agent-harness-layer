"""模型工具循环容量、claim 与终态发布围栏合同。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.contracts.model_tool_loop_contract_helpers import initial_model_tool_loop_snapshot
from tests.contracts.test_policy_gated_model_tool_loop_event_contracts import (
    _event_loop_fixture,
)
from tests.contracts.test_tool_intent_model_catalog_config_contracts import _tool_catalog

from agent_harness.events import CanonicalEventType, EventBus
from agent_harness.events.model_tool_loop import (
    ModelToolLoopEventProducer,
)
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ToolIntent,
    structured_digest,
)
from agent_harness.runtime import (
    ModelToolLoopError,
)
from agent_harness.storage import ModelToolLoopCreate
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EvidenceOperationKind,
)
from agent_harness.tools import (
    ModelToolExecutionClaimActive,
    ModelToolExecutionClaimService,
    ModelToolExecutionNeedsReview,
    ResolvedToolIntent,
    ToolRuntimeContext,
)


class _NoReconcileEventBus(EventBus):
    """容量边界测试跳过本地文件前缀同步，只聚焦 typed reservation 失败。"""

    async def reconcile_local_capacity(self, *, run_id: str) -> None:
        """测试已直接设置可信数据库高水位，无需构造数十亿条 JSONL。"""

        del run_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deny", "handler_failure"),
    [
        (True, None),
        (False, "runtime"),
    ],
)
async def test_policy_deny_or_handler_unknown_fences_later_loop_events(
    tmp_path: Path,
    deny: bool,
    handler_failure: str | None,
) -> None:
    """deny保持零工具事件；handler未知只保留started且不伪造失败终态。"""

    (
        storage,
        sink,
        provider,
        bound,
        request,
        run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(
        tmp_path,
        deny=deny,
        handler_failure=handler_failure,
    )
    try:
        with pytest.raises(ModelToolLoopError) as failure:
            await bound.run(
                request,
                operation_key=f"event-negative-{deny}-{handler_failure}",
            )
        events = await sink.read(run_id=run_id)
        tool_events = [event for event in events if event.event_type.value.startswith("tool.call.")]

        assert provider.send_count == 1
        assert handler_count() == (0 if deny else 1)
        assert not any(event.event_type.value.startswith("context.assembly.") for event in events)
        if deny:
            assert tool_events == []
        else:
            assert failure.value.code == "model.tool_loop_needs_review"
            assert [event.event_type for event in tool_events] == [
                CanonicalEventType.TOOL_CALL_STARTED
            ]
    finally:
        await storage.dispose()


def _resolved_intent() -> tuple[ToolIntent, ResolvedToolIntent]:
    """构造与冻结catalog逐值一致的稳定工具事件身份。"""

    catalog = _tool_catalog()
    entry = catalog.tools[0]
    arguments = {"q": "weather"}
    intent = ToolIntent(
        loop_id="a" * 64,
        turn_ordinal=1,
        tool_call_id="b" * 64,
        tool_name=entry.name,
        arguments=arguments,
        arguments_digest=structured_digest(arguments),
        tool_schema_ref=entry.input_schema_ref,
        tool_schema_version=entry.input_schema_version,
        tool_schema_digest=entry.input_schema_digest,
        model_usage_call_id="c" * 64,
        catalog_digest=catalog.catalog_digest,
    )
    return intent, ResolvedToolIntent(
        loop_id=intent.loop_id,
        turn_ordinal=intent.turn_ordinal,
        tool_call_id=intent.tool_call_id,
        tool_name=intent.tool_name,
        arguments=intent.arguments,
        arguments_digest=intent.arguments_digest,
        tool_schema_ref=intent.tool_schema_ref,
        tool_schema_version=intent.tool_schema_version,
        tool_schema_digest=intent.tool_schema_digest,
        model_usage_call_id=intent.model_usage_call_id,
        catalog_digest=intent.catalog_digest,
        action=entry.action,
        resource=entry.resource,
    )


@pytest.mark.asyncio
async def test_tool_capacity_exhaustion_precedes_claim_handler_and_event(tmp_path: Path) -> None:
    """工具事件最大预约失败时不得创建execution claim、调用handler或留下部分事件。"""

    (
        storage,
        sink,
        _provider,
        _bound,
        _request,
        run_id,
        handler_count,
        registry,
        loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path)
    intent, resolved = _resolved_intent()
    try:
        async with storage.uow() as uow:
            await uow.model_tool_loops.create(
                ModelToolLoopCreate(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    loop_id=intent.loop_id,
                    request_identity_digest="a" * 64,
                    operation_identity_digest="b" * 64,
                    catalog_digest=intent.catalog_digest,
                    **initial_model_tool_loop_snapshot(),
                    owner_lease_digest="c" * 64,
                    owner_fence=1,
                    owner_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                )
            )
            await uow.event_capacity.reconcile_local_prefix(
                run_id=run_id,
                highest_persisted_seq=MAX_EVENT_SEQ - 2,
            )
            await uow.commit()
        loop_events = ModelToolLoopEventProducer(
            storage=storage,
            event_bus=_NoReconcileEventBus(sink=sink),
        )
        with pytest.raises(Exception) as failure:
            await registry.call(
                resolved,
                context=ToolRuntimeContext(
                    actor=IdentityContext(
                        tenant_id="tenant-a",
                        user_id="user-a",
                        session_id="session-a",
                        roles=["member"],
                    ),
                    agent_id="agent-a",
                    run_id=run_id,
                    request_id="request-a",
                    trace_id="trace-a",
                ),
                intent=intent,
                catalog=_tool_catalog(),
                events=loop_events,
            )
        assert getattr(failure.value, "code", None) == "event.sequence_exhausted"
        assert handler_count() == 0
        assert await sink.read(run_id=run_id) == []
        async with storage.uow() as uow:
            assert await uow.tool_invocations.get_by_tool_call_id(intent.tool_call_id) is None
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_error",
    [ModelToolExecutionClaimActive, ModelToolExecutionNeedsReview],
)
async def test_tool_claim_rejection_precedes_started_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claim_error: type[Exception],
) -> None:
    """claim未授权handler时不得发布事件或留下无owner的容量/outbox预约。"""

    (
        storage,
        sink,
        _provider,
        _bound,
        _request,
        run_id,
        handler_count,
        registry,
        loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path)
    intent, resolved = _resolved_intent()

    async def reject_claim(*_args: object, **_kwargs: object) -> object:
        raise claim_error

    monkeypatch.setattr(ModelToolExecutionClaimService, "acquire", reject_claim)
    try:
        async with storage.uow() as uow:
            await uow.model_tool_loops.create(
                ModelToolLoopCreate(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    loop_id=intent.loop_id,
                    request_identity_digest="a" * 64,
                    operation_identity_digest="b" * 64,
                    catalog_digest=intent.catalog_digest,
                    **initial_model_tool_loop_snapshot(),
                    owner_lease_digest="c" * 64,
                    owner_fence=1,
                    owner_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                )
            )
            await uow.commit()

        with pytest.raises(claim_error):
            await registry.call(
                resolved,
                context=ToolRuntimeContext(
                    actor=IdentityContext(
                        tenant_id="tenant-a",
                        user_id="user-a",
                        session_id="session-a",
                        roles=["member"],
                    ),
                    agent_id="agent-a",
                    run_id=run_id,
                    request_id="request-a",
                    trace_id="trace-a",
                ),
                intent=intent,
                catalog=_tool_catalog(),
                events=loop_events,
            )

        assert handler_count() == 0
        assert not any(
            event.event_type
            in {
                CanonicalEventType.TOOL_CALL_STARTED,
                CanonicalEventType.TOOL_CALL_COMPLETED,
                CanonicalEventType.TOOL_CALL_FAILED,
            }
            for event in await sink.read(run_id=run_id)
        )
        async with storage.uow() as uow:
            assert await uow.tool_invocations.get_by_tool_call_id(intent.tool_call_id) is None
            assert await uow.evidence_outbox.pending(run_id=run_id) == []
            assert not await uow.evidence_outbox.blocks_model_loop_terminal(
                run_id=run_id,
                in_flight_approval_ids=(),
            )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_tool_claim_and_event_reservation_share_first_owner_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次可见 owner 提交必须同时包含 claim 与完整工具事件预约组。"""

    (
        storage,
        _sink,
        _provider,
        _bound,
        _request,
        run_id,
        handler_count,
        registry,
        loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path)
    intent, resolved = _resolved_intent()
    context = ToolRuntimeContext(
        actor=IdentityContext(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
            roles=["member"],
        ),
        agent_id="agent-a",
        run_id=run_id,
        request_id="request-a",
        trace_id="trace-a",
    )
    try:
        async with storage.uow() as uow:
            await uow.model_tool_loops.create(
                ModelToolLoopCreate(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    loop_id=intent.loop_id,
                    request_identity_digest="a" * 64,
                    operation_identity_digest="b" * 64,
                    catalog_digest=intent.catalog_digest,
                    **initial_model_tool_loop_snapshot(),
                    owner_lease_digest="c" * 64,
                    owner_fence=1,
                    owner_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                )
            )
            await uow.commit()
        step = await loop_events.prepare_tool_claim(context=context, intent=intent)
        original_commit = SQLAlchemyUnitOfWork.commit
        owner_snapshots: list[tuple[str | None, tuple[str, ...]]] = []

        async def observe_owner_commit(uow: SQLAlchemyUnitOfWork) -> None:
            claim = await uow.tool_invocations.get_by_tool_call_id(intent.tool_call_id)
            group = await uow.evidence_outbox.ordered_group(group_id=step.group_id)
            if claim is not None or group:
                owner_snapshots.append(
                    (
                        None if claim is None else claim.execution_state,
                        tuple(item.state for item in group),
                    )
                )
            await original_commit(uow)

        monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", observe_owner_commit)
        await registry.call(
            resolved,
            context=context,
            intent=intent,
            catalog=_tool_catalog(),
            events=loop_events,
        )

        assert handler_count() == 1
        assert owner_snapshots
        assert owner_snapshots[0] == ("claimed", ("reserved", "reserved"))
        assert all(
            claim_state is not None and len(states) == 2 for claim_state, states in owner_snapshots
        )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_unknown_tool_final_publish_blocks_context_and_next_model(tmp_path: Path) -> None:
    """结果已落盘但final event未知时保持outbox围栏，禁止Context和下一模型轮。"""

    (
        storage,
        sink,
        provider,
        bound,
        request,
        run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path, fail_final_publish=True)
    try:
        with pytest.raises(RuntimeError, match="publish is unknown"):
            await bound.run(request, operation_key="event-publish-unknown")
        events = await sink.read(run_id=run_id)
        async with storage.uow() as uow:
            pending = [
                (item.operation_kind, item.state)
                for item in await uow.evidence_outbox.pending(run_id=run_id)
            ]

        assert provider.send_count == 1
        assert handler_count() == 1
        assert [
            event.event_type for event in events if event.event_type.value.startswith("tool.")
        ] == [CanonicalEventType.TOOL_CALL_STARTED]
        assert not any(event.event_type.value.startswith("context.assembly.") for event in events)
        assert any(
            operation_kind == EvidenceOperationKind.TOOL_INVOCATION.value
            and state == "result_persisted"
            for operation_kind, state in pending
        )
    finally:
        await storage.dispose()
