"""规范事件类型、容量与 terminal 不变量合同测试。"""

from __future__ import annotations

from tests.contracts.test_canonical_events_artifacts_contracts import (
    Any as Any,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    CanonicalEvent as CanonicalEvent,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    ContractRunTraceResolver as ContractRunTraceResolver,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    Path as Path,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    TerminalEventError as TerminalEventError,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    ValidationError as ValidationError,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    asyncio as asyncio,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    cast as cast,
)
from tests.contracts.test_canonical_events_artifacts_contracts import (
    pytest as pytest,
)


def test_canonical_event_scope_is_typed_and_trace_is_conditionally_required() -> None:
    """non-run 可保留 nullable trace；run 与未知 scope 在 DTO 边界失败。"""

    non_run = CanonicalEvent(
        tenant_id="telemetry",
        run_id="telemetry",
        event_type=CanonicalEventType.ARTIFACT_CREATED,
        seq=1,
        trace_id=None,
        record_scope="non_run",
    )
    assert non_run.trace_id is None
    assert "trace_id" not in non_run.to_payload()
    missing_trace = CanonicalEvent(
        tenant_id="telemetry",
        run_id="telemetry",
        event_type=CanonicalEventType.ARTIFACT_CREATED,
        seq=2,
        record_scope="non_run",
    )
    assert missing_trace.trace_id is None

    with pytest.raises(ValidationError):
        CanonicalEvent(
            tenant_id="tenant-a",
            run_id="run-a",
            event_type=CanonicalEventType.RUN_STARTED,
            seq=1,
            trace_id=None,
        )
    with pytest.raises(ValidationError):
        CanonicalEvent(
            tenant_id="tenant-a",
            run_id="run-a",
            event_type=CanonicalEventType.RUN_STARTED,
            seq=1,
            trace_id="trace-a",
            record_scope=cast(Any, "other"),
        )

    schema = CanonicalEvent.model_json_schema()
    assert schema["properties"]["record_scope"]["enum"] == ["run", "non_run"]
    assert any("if" in branch and "then" in branch for branch in schema["allOf"])


@pytest.mark.asyncio
async def test_local_direct_sink_rejects_untyped_scope_without_side_effect(
    tmp_path: Path,
) -> None:
    """DTO 被刻意绕过时，local 持久化 seam 仍拒绝第三种 scope。"""

    path = tmp_path / "typed-scope.jsonl"
    sink = LocalJsonlEventSink(path)
    non_run = CanonicalEvent(
        event_id="non-run-null-trace",
        tenant_id="telemetry",
        run_id="telemetry",
        event_type=CanonicalEventType.ARTIFACT_CREATED,
        seq=0,
        trace_id=None,
        record_scope="non_run",
    )
    persisted = await sink.write(non_run)
    assert persisted.trace_id is None

    invalid_path = tmp_path / "invalid-scope.jsonl"
    invalid_sink = LocalJsonlEventSink(invalid_path)
    invalid = non_run.model_copy(update={"event_id": "invalid-scope"})
    object.__setattr__(invalid, "record_scope", "other")
    with pytest.raises(ValueError, match="record_scope must be run or non_run"):
        await invalid_sink.write(invalid)
    assert not invalid_path.exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_event_bus_coordinates_each_event_loop_without_cross_loop_lock_binding() -> None:
    """同一 service EventBus 经 DBOS loop 和 worker loop 连续竞争时都可发布。"""

    class YieldingSink:
        def __init__(self) -> None:
            self.events: list[CanonicalEvent] = []

        async def write(self, event: CanonicalEvent) -> CanonicalEvent:
            await asyncio.sleep(0)
            self.events.append(event)
            return event

        async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
            return [
                event for event in self.events if event.run_id == run_id and event.seq > after_seq
            ]

        async def latest_seq(self, run_id: str) -> int:
            return max(
                (event.seq for event in self.events if event.run_id == run_id),
                default=0,
            )

        async def has_terminal(self, run_id: str) -> bool:
            return any(event.run_id == run_id and event.terminal for event in self.events)

    sink = YieldingSink()
    bus = EventBus(
        sink=sink,
        run_trace_resolver=ContractRunTraceResolver({("tenant-loop", "run-loop"): "trace-loop"}),
    )

    async def burst(prefix: str) -> None:
        await asyncio.gather(
            *(
                bus.publish(
                    tenant_id="tenant-loop",
                    run_id="run-loop",
                    event_type=CanonicalEventType.RUN_STARTED,
                    event_id=f"{prefix}-{index}",
                    trace_id="trace-loop",
                )
                for index in range(2)
            )
        )

    asyncio.run(burst("dbos"))
    asyncio.run(burst("worker"))

    assert [event.seq for event in sink.events] == [1, 2, 3, 4]


def test_canonical_event_type_catalog_contains_product_spec_events() -> None:
    # Product-Spec 的事件 catalog 是 adapter 互通的枚举契约。这里锁完整清单，
    # 防止只测 happy path 的 run/guardrail 事件时漏掉 eval 或 compaction 事件。
    expected = {
        "run.queued",
        "run.started",
        "run.resumed",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "model.request.started",
        "model.output.delta",
        "model.output.completed",
        "model.structured.delta",
        "model.structured.completed",
        "model.usage.updated",
        "input.guardrail.checked",
        "input.guardrail.blocked",
        "reasoning.delta",
        "tool.call.args_delta",
        "tool.call.started",
        "tool.call.completed",
        "tool.call.failed",
        "retrieval.query.started",
        "retrieval.query.completed",
        "context.assembly.started",
        "context.assembly.completed",
        "policy.decision",
        "approval.required",
        "approval.resolved",
        "delegation.claimed",
        "delegation.child.created",
        "delegation.completed",
        "delegation.failed",
        "checkpoint.created",
        "context.compaction.started",
        "context.compaction.completed",
        "eval.case.drafted",
        "eval.case.approved",
        "eval.run.started",
        "eval.run.completed",
        "eval.score.recorded",
        "artifact.created",
    }

    assert expected == {event_type.value for event_type in CanonicalEventType}


@pytest.mark.asyncio
async def test_event_bus_assigns_seq_and_rejects_second_terminal(tmp_path: Path) -> None:
    # EventBus 是 runtime 的写入 seam：调用方不手动分配 seq，也不能绕过 terminal 规则。
    # 这里用 local jsonl sink 证明断线续读语义；不声明它已经支持多进程并发队列。
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    bus = EventBus(
        sink=sink,
        run_trace_resolver=ContractRunTraceResolver({("default", "run-1"): "trace-1"}),
    )

    first = await bus.publish(
        tenant_id="default",
        run_id="run-1",
        user_id="user-1",
        agent_id="fake-agent",
        parent_run_id="parent-run",
        event_type=CanonicalEventType.RUN_STARTED,
        payload={"step": "start"},
        raw_event_ref="provider://raw/run-1/1",
        trace_id="trace-1",
        span_id="span-1",
    )
    terminal = await bus.publish(
        tenant_id="default",
        run_id="run-1",
        agent_id="fake-agent",
        event_type=CanonicalEventType.RUN_COMPLETED,
        payload={"status": "completed"},
        terminal=True,
        visibility="public",
        trace_id="trace-1",
    )

    # Envelope 字段是外部 adapter 之间的稳定协议；这里锁字段名，避免实现
    # 悄悄退回 provider-specific id 或缺少 trace/resume 所需的关联字段。
    assert first.event_id
    assert first.event_version == "1.0"
    assert first.user_id == "user-1"
    assert first.parent_run_id == "parent-run"
    assert first.raw_event_ref == "provider://raw/run-1/1"
    assert first.span_id == "span-1"
    assert first.seq == 1
    assert terminal.seq == 2
    assert terminal.terminal is True
    assert [event.seq for event in await sink.read(run_id="run-1", after_seq=1)] == [2]

    with pytest.raises(TerminalEventError):
        await bus.publish(
            tenant_id="default",
            run_id="run-1",
            agent_id="fake-agent",
            event_type=CanonicalEventType.RUN_FAILED,
            payload={"status": "failed"},
            terminal=True,
            visibility="public",
            trace_id="trace-1",
        )
