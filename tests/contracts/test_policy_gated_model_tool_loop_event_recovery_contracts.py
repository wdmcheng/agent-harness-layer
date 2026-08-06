"""模型工具循环耐久事件 exact 恢复与未知版本关闭合同。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.test_policy_gated_model_tool_loop_event_contracts import (
    _event_loop_fixture,
)

from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.events.model_tool_loop import (
    ModelToolLoopEventProducer,
    ModelToolLoopEventRecoveryError,
)
from agent_harness.models.tool_intent import tool_loop_identity_digest
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "operation_key", "expected_event"),
    [
        ("tool", "automatic-tool-event-recovery", CanonicalEventType.TOOL_CALL_COMPLETED),
        (
            "context",
            "automatic-context-event-recovery",
            CanonicalEventType.CONTEXT_ASSEMBLY_COMPLETED,
        ),
    ],
)
async def test_public_run_recovery_republishes_exact_event_and_completes_unique_loop(
    tmp_path: Path,
    failure_mode: str,
    operation_key: str,
    expected_event: CanonicalEventType,
) -> None:
    """生产bound重放必须自动补投exact event，再完成原loop且不重做已知副作用。"""

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
        fail_final_publish=failure_mode == "tool",
        fail_context_final_publish=failure_mode == "context",
    )
    loop_id = tool_loop_identity_digest(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
        operation_key=operation_key,
    )
    try:
        with pytest.raises(RuntimeError, match="publish is unknown"):
            await bound.run(request, operation_key=operation_key)

        response = await bound.run(request, operation_key=operation_key)

        assert response.output_text == "done"
        assert provider.send_count == 2
        assert handler_count() == 1
        events = await sink.read(run_id=run_id)
        assert [event.event_type for event in events].count(expected_event) == 1
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.get("tenant-a", loop_id)
            pending_loop_events = [
                item
                for item in await uow.evidence_outbox.pending(run_id=run_id)
                if item.group_id is not None
                and item.group_id.startswith("model-tool-loop:")
                and item.state == "result_persisted"
            ]
        assert loop is not None and loop.status == "completed"
        assert pending_loop_events == []
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_pending_tool_event_recovery_republishes_exact_envelope_without_reexecution(
    tmp_path: Path,
) -> None:
    """重启只补投耐久event intent，不得把outbox当成重新执行handler的许可。"""

    (
        storage,
        _failed_sink,
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
            await bound.run(request, operation_key="event-recovery")
        async with storage.uow() as uow:
            pending = [
                (item.group_id, item.event_id)
                for item in await uow.evidence_outbox.pending(run_id=run_id)
                if item.operation_kind == EvidenceOperationKind.TOOL_INVOCATION.value
                and item.state == "result_persisted"
            ]
        assert len(pending) == 1
        group_id, final_event_id = pending[0]
        assert group_id is not None

        async def resolve_trace(**_: object) -> str:
            return "trace-a"

        recovered_sink = LocalJsonlEventSink(
            tmp_path / "events.jsonl",
            run_trace_resolver=resolve_trace,
        )
        recovered = ModelToolLoopEventProducer(
            storage=storage,
            event_bus=EventBus(
                sink=recovered_sink,
                capacity_storage=storage,
            ),
        )
        recovered_count = await recovered.recover_group(group_id=group_id)

        events = await recovered_sink.read(run_id=run_id)
        assert recovered_count == 1
        assert provider.send_count == 1
        assert handler_count() == 1
        assert [
            event.event_id
            for event in events
            if event.event_type == CanonicalEventType.TOOL_CALL_COMPLETED
        ] == [final_event_id]
        async with storage.uow() as uow:
            group_states = [
                item.state for item in await uow.evidence_outbox.ordered_group(group_id=group_id)
            ]
        assert group_states == ["published", "published"]
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_pending_context_event_recovery_preserves_tool_and_model_counts(
    tmp_path: Path,
) -> None:
    """Context final补投只读取耐久intent，不重跑工具、组装或下一模型轮。"""

    (
        storage,
        _failed_sink,
        provider,
        bound,
        request,
        run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path, fail_context_final_publish=True)
    try:
        with pytest.raises(RuntimeError, match="context final publish is unknown"):
            await bound.run(request, operation_key="context-event-recovery")
        async with storage.uow() as uow:
            pending = [
                (item.group_id, item.event_id)
                for item in await uow.evidence_outbox.pending(run_id=run_id)
                if item.operation_kind == EvidenceOperationKind.CONTEXT_ASSEMBLY.value
                and item.state == "result_persisted"
            ]
        assert len(pending) == 1
        group_id, final_event_id = pending[0]
        assert group_id is not None

        async def resolve_trace(**_: object) -> str:
            return "trace-a"

        recovered_sink = LocalJsonlEventSink(
            tmp_path / "events.jsonl",
            run_trace_resolver=resolve_trace,
        )
        recovered = ModelToolLoopEventProducer(
            storage=storage,
            event_bus=EventBus(sink=recovered_sink, capacity_storage=storage),
        )
        assert await recovered.recover_group(group_id=group_id) == 1

        events = await recovered_sink.read(run_id=run_id)
        assert provider.send_count == 1
        assert handler_count() == 1
        assert [
            event.event_id
            for event in events
            if event.event_type == CanonicalEventType.CONTEXT_ASSEMBLY_COMPLETED
        ] == [final_event_id]
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_unknown_event_version_fails_closed_and_keeps_outbox_fence(tmp_path: Path) -> None:
    """未知event版本不得发布、跳过或触发业务重放，并留下稳定人工处置坐标。"""

    (
        storage,
        failed_sink,
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
            await bound.run(request, operation_key="event-version-unknown")
        async with storage.uow() as uow:
            pending = [
                item
                for item in await uow.evidence_outbox.pending(run_id=run_id)
                if item.operation_kind == EvidenceOperationKind.TOOL_INVOCATION.value
                and item.state == "result_persisted"
            ]
            assert len(pending) == 1
            row = pending[0]
            assert row.group_id is not None and row.result_json is not None
            raw = dict(row.result_json)
            raw["event_version"] = "2.0"
            correlation = cast(dict[str, Any], raw["payload"])["correlation"]
            assert isinstance(correlation, dict)
            loop_id = cast(dict[str, Any], correlation)["loop_id"]
            row.result_json = raw
            group_id = row.group_id
            final_event_id = row.event_id
            await uow.commit()

        async def resolve_trace(**_: object) -> str:
            return "trace-a"

        recovered = ModelToolLoopEventProducer(
            storage=storage,
            event_bus=EventBus(
                sink=LocalJsonlEventSink(
                    tmp_path / "events.jsonl",
                    run_trace_resolver=resolve_trace,
                ),
                capacity_storage=storage,
            ),
        )
        with pytest.raises(ModelToolLoopEventRecoveryError) as failure:
            await recovered.recover_group(group_id=group_id)

        assert failure.value.code == "model.tool_loop_needs_review"
        assert provider.send_count == 1
        assert handler_count() == 1
        assert not any(
            event.event_id == final_event_id for event in await failed_sink.read(run_id=run_id)
        )
        async with storage.uow() as uow:
            persisted = [
                item
                for item in await uow.evidence_outbox.ordered_group(group_id=group_id)
                if item.event_id == final_event_id
            ][0]
            loop = await uow.model_tool_loops.get("tenant-a", cast(str, loop_id))
            persisted_state = persisted.state
            persisted_error = persisted.error_code
        assert persisted_state == "result_persisted"
        assert persisted_error == "event_version_unknown"
        assert loop is not None and loop.status == "needs_review"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_public_run_recovery_fences_unknown_event_version_without_reexecution(
    tmp_path: Path,
) -> None:
    """生产bound自动恢复遇到未知版本时必须关闭失败，不能重调模型或工具。"""

    (
        storage,
        _failed_sink,
        provider,
        bound,
        request,
        run_id,
        handler_count,
        _registry,
        _loop_events,
        _model_turns,
    ) = await _event_loop_fixture(tmp_path, fail_final_publish=True)
    operation_key = "automatic-event-version-unknown"
    loop_id = tool_loop_identity_digest(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
        operation_key=operation_key,
    )
    try:
        with pytest.raises(RuntimeError, match="publish is unknown"):
            await bound.run(request, operation_key=operation_key)
        async with storage.uow() as uow:
            pending = [
                item
                for item in await uow.evidence_outbox.pending(run_id=run_id)
                if item.group_id is not None
                and item.group_id.startswith("model-tool-loop:")
                and item.state == "result_persisted"
            ]
            assert len(pending) == 1 and pending[0].result_json is not None
            pending[0].result_json = {**pending[0].result_json, "event_version": "2.0"}
            await uow.commit()

        with pytest.raises(RuntimeError, match="model.tool_loop_needs_review"):
            await bound.run(request, operation_key=operation_key)

        assert provider.send_count == 1
        assert handler_count() == 1
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.get("tenant-a", loop_id)
        assert loop is not None and loop.status == "needs_review"
    finally:
        await storage.dispose()
