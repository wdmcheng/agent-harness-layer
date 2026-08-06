"""批准后模型工具的 claim、容量与事件 identity 原子提交合同。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from tests.contracts.model_tool_loop_contract_helpers import initial_model_tool_loop_snapshot
from tests.contracts.run_trace_revision_hardening_postgresql_helpers import postgres_database
from tests.contracts.test_approval_execution_contracts import build_approval_flow

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events import EventBus
from agent_harness.events.model_tool_loop import (
    ModelToolLoopEventProducer,
    ModelToolLoopEventStep,
)
from agent_harness.models import ToolCatalog, ToolIntent, build_tool_catalog, structured_digest
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import ApprovalGrant
from agent_harness.storage import ModelToolLoopCreate, SQLAlchemyStorage
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.evidence_repositories import MAX_EVENT_SEQ, EventCapacityExceeded
from agent_harness.storage.models import RunEventCapacityModel
from agent_harness.storage.tool_repositories import ToolInvocationRecord
from agent_harness.tools import (
    BuiltinTool,
    ModelToolExecutionClaimActive,
    ModelToolExecutionClaimService,
    ResolvedToolIntent,
    ToolCallRequest,
    ToolCallResult,
    ToolRegistry,
    ToolRuntimeContext,
    hash_tool_arguments,
)
from agent_harness.tools.execution_support import ApprovedModelToolExecution


@dataclass(frozen=True)
class _ApprovedModelFixture:
    """公开 ``call_approved`` 所需的真实 SQLite、approval 与事件协作者。"""

    storage: SQLAlchemyStorage
    registry: ToolRegistry
    events: ModelToolLoopEventProducer
    sink: Any
    grant: ApprovalGrant
    context: ToolRuntimeContext
    intent: ToolIntent
    resolved: ResolvedToolIntent
    catalog: ToolCatalog
    run_id: str
    approval_id: str
    handler_count: list[int]


async def _approved_model_fixture(
    tmp_path: Path,
    *,
    storage_dsn: str | None = None,
    handler_failure: bool = False,
    result_guard_failure: bool = False,
) -> _ApprovedModelFixture:
    """在指定真实存储上构造带 tool-intent identity 的批准后调用。"""

    handler_count = [0]

    def handler(arguments: dict[str, Any]) -> dict[str, object]:
        """只记录唯一业务副作用，并返回可经公共结果守卫处理的值。"""

        handler_count[0] += 1
        if handler_failure:
            raise RuntimeError("handler outcome became unknown after side effect")
        if result_guard_failure:
            return {"stdout": object()}
        return {"stdout": str(arguments["command"])}

    (
        storage,
        sink,
        _service,
        _orchestrator,
        identity,
        _legacy_registry,
        waiting,
    ) = await build_approval_flow(
        tmp_path,
        handler=handler,
        storage_dsn=storage_dsn,
    )
    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="shell.execute",
                action="shell.execute",
                resource="tool:shell",
                input_schema={
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
                input_schema_ref="shell-execute-input",
                input_schema_version="v1",
                handler=handler,
            )
        ],
        policy=PolicyEngine(provider=YamlPolicyProvider()),
        audit=None,
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        storage=storage,
    )
    catalog = build_tool_catalog(
        allowed_tools=("shell.execute",),
        registry_descriptors=registry.catalog_descriptors(),
        selection=None,
    )
    entry = catalog.tools[0]
    arguments: dict[str, object] = {"command": "echo safe"}
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
    resolved = registry.resolve_intent(intent, catalog=catalog)
    async with storage.uow() as uow:
        approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
        lease = await uow.approvals.claim_resolution(
            approval_id=approval.approval_id,
            run_id=approval.run_id,
            tenant_id=approval.tenant_id,
            request_id="approved-model-atomicity",
        )
        run = await uow.runs.get(waiting.run_id)
        assert run is not None
        await uow.model_tool_loops.create(
            ModelToolLoopCreate(
                tenant_id=approval.tenant_id,
                run_id=waiting.run_id,
                agent_id=approval.agent_id,
                loop_id=intent.loop_id,
                request_identity_digest="d" * 64,
                operation_identity_digest="e" * 64,
                catalog_digest=catalog.catalog_digest,
                **initial_model_tool_loop_snapshot(),
                owner_lease_digest="f" * 64,
                owner_fence=1,
                owner_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )
        await uow.commit()
    context = ToolRuntimeContext(
        actor=identity,
        agent_id=approval.agent_id,
        run_id=waiting.run_id,
        request_id="request-approved-model",
        trace_id=run.trace_id,
    )
    return _ApprovedModelFixture(
        storage=storage,
        registry=registry,
        events=ModelToolLoopEventProducer(storage=storage, event_bus=EventBus(sink=sink)),
        sink=sink,
        grant=ApprovalGrant(
            approval_id=approval.approval_id,
            lease_id=lease.lease_id,
            tenant_id=identity.tenant_id,
            identity_id=identity.user_id,
            session_id=identity.session_id,
            agent_id=approval.agent_id,
            run_id=approval.run_id,
            action=approval.action,
            resource=approval.resource,
            arguments_hash=hash_tool_arguments(arguments),
        ),
        context=context,
        intent=intent,
        resolved=resolved,
        catalog=catalog,
        run_id=waiting.run_id,
        approval_id=approval.approval_id,
        handler_count=handler_count,
    )


async def _call_approved(fixture: _ApprovedModelFixture) -> object:
    """经真实 Registry 公开入口执行一次带 canonical event 的批准后模型工具。"""

    return await fixture.registry.call_approved(
        fixture.resolved,
        context=fixture.context,
        grant=fixture.grant,
        intent=fixture.intent,
        catalog=fixture.catalog,
        events=fixture.events,
    )


async def _call_unapproved(fixture: _ApprovedModelFixture) -> ToolCallResult:
    """经真实 Registry 未批准公共入口执行同一模型工具identity。"""

    return await fixture.registry.call(
        fixture.resolved,
        context=fixture.context,
        intent=fixture.intent,
        catalog=fixture.catalog,
        events=fixture.events,
    )


@pytest.mark.asyncio
async def test_approved_model_claim_and_events_share_first_owner_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """批准后首次可见 owner commit 同时包含 claim 与两项稳定 event identity。"""

    fixture = await _approved_model_fixture(tmp_path)
    approved_context = fixture.context.model_copy(deep=True).authorize_approved_call(
        fixture.approval_id
    )
    step = await fixture.events.prepare_tool_claim(
        context=approved_context,
        intent=fixture.intent,
    )
    original_commit = SQLAlchemyUnitOfWork.commit
    owner_snapshots: list[tuple[str | None, tuple[str, ...]]] = []

    async def observe_owner_commit(uow: SQLAlchemyUnitOfWork) -> None:
        claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
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
    try:
        await _call_approved(fixture)
        assert fixture.handler_count == [1]
        assert owner_snapshots
        assert owner_snapshots[0] == ("claimed", ("reserved", "reserved"))
        assert all(
            claim_state is not None and len(states) == 2 for claim_state, states in owner_snapshots
        )
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["capacity", "claim"])
async def test_approved_model_preclaim_failure_leaves_no_claim_or_event_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """容量失败或claim拒绝都不得留下孤立claim、outbox或handler副作用。"""

    fixture = await _approved_model_fixture(tmp_path)
    if failure == "capacity":
        async with fixture.storage.uow() as uow:
            await uow.session.execute(
                update(RunEventCapacityModel)
                .where(RunEventCapacityModel.run_id == fixture.run_id)
                .values(highest_persisted_seq=MAX_EVENT_SEQ - 1)
            )
            await uow.commit()
        expected_error: type[Exception] = EventCapacityExceeded
    else:

        async def reject_claim(*_args: object, **_kwargs: object) -> object:
            raise ModelToolExecutionClaimActive

        monkeypatch.setattr(ModelToolExecutionClaimService, "acquire", reject_claim)
        expected_error = ModelToolExecutionClaimActive

    try:
        with pytest.raises(expected_error):
            await _call_approved(fixture)
        async with fixture.storage.uow() as uow:
            assert (
                await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id) is None
            )
            assert await uow.evidence_outbox.pending(run_id=fixture.run_id) == []
            assert not await uow.evidence_outbox.blocks_model_loop_terminal(
                run_id=fixture.run_id,
                in_flight_approval_ids=(),
            )
        assert fixture.handler_count == [0]
    finally:
        await fixture.storage.dispose()


async def _assert_concurrent_approved_calls_have_one_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    storage_dsn: str | None,
) -> None:
    """从公开入口同时放行两个worker，并观察唯一owner及exact replay。"""

    fixture = await _approved_model_fixture(tmp_path, storage_dsn=storage_dsn)
    original_execute_model_claim = ApprovedModelToolExecution.execute
    original_reserve = fixture.events.reserve_tool_in_owner_uow
    arrivals = 0
    arrival_lock = asyncio.Lock()
    both_ready = asyncio.Event()
    reservation_calls = 0

    async def synchronize_execution_entry(
        executor: ApprovedModelToolExecution,
        *,
        request: ToolCallRequest,
        context: ToolRuntimeContext,
        grant: ApprovalGrant,
        tool: BuiltinTool,
        existing: ToolInvocationRecord | None,
        events: ModelToolLoopEventProducer | None,
        intent: ToolIntent,
        resolved: ResolvedToolIntent,
    ) -> ToolCallResult:
        """只同步两个公共approved执行入口，不替代生产锁或事务逻辑。"""

        nonlocal arrivals
        async with arrival_lock:
            arrivals += 1
            if arrivals == 2:
                both_ready.set()
        await asyncio.wait_for(both_ready.wait(), timeout=5)
        return await original_execute_model_claim(
            executor,
            request=request,
            context=context,
            grant=grant,
            tool=tool,
            existing=existing,
            events=events,
            intent=intent,
            resolved=resolved,
        )

    async def count_owner_reservation(
        *,
        step: ModelToolLoopEventStep,
        uow: SQLAlchemyUnitOfWork,
    ) -> None:
        """记录实际执行owner准备回调的次数，再委托真实事件仓储写入。"""

        nonlocal reservation_calls
        reservation_calls += 1
        await original_reserve(step=step, uow=uow)

    monkeypatch.setattr(
        ApprovedModelToolExecution,
        "execute",
        synchronize_execution_entry,
    )
    monkeypatch.setattr(fixture.events, "reserve_tool_in_owner_uow", count_owner_reservation)
    approved_context = fixture.context.model_copy(deep=True).authorize_approved_call(
        fixture.approval_id
    )
    step = await fixture.events.prepare_tool_claim(
        context=approved_context,
        intent=fixture.intent,
    )
    try:
        results = await asyncio.wait_for(
            asyncio.gather(_call_approved(fixture), _call_approved(fixture)),
            timeout=15,
        )
        assert arrivals == 2
        assert fixture.handler_count == [1]
        assert reservation_calls == 1
        assert results[0] == results[1]
        async with fixture.storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
            group = await uow.evidence_outbox.ordered_group(group_id=step.group_id)
        assert claim is not None and claim.execution_state == "completed"
        assert len(group) == 2
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_sqlite_concurrent_approved_calls_prepare_only_the_actual_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite 两worker竞争只允许赢家准备owner证据并执行一次handler。"""

    await _assert_concurrent_approved_calls_have_one_owner(
        tmp_path,
        monkeypatch,
        storage_dsn=None,
    )


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实PostgreSQL approved并发合同需要本地测试DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_concurrent_approved_calls_prepare_only_the_actual_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL advisory owner锁让并发输家读取exact结果而非撞outbox。"""

    async with postgres_database("agent_harness_approved_tool_owner") as (dsn, _engine):
        await _assert_concurrent_approved_calls_have_one_owner(
            tmp_path,
            monkeypatch,
            storage_dsn=dsn,
        )


async def _assert_concurrent_unapproved_calls_have_one_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    storage_dsn: str | None,
) -> None:
    """同时放行两个普通公共调用，要求输家等待并exact replay。"""

    fixture = await _approved_model_fixture(tmp_path, storage_dsn=storage_dsn)
    original_call = ToolRegistry.call
    original_reserve = fixture.events.reserve_tool_in_owner_uow
    arrivals = 0
    arrival_lock = asyncio.Lock()
    both_ready = asyncio.Event()
    reservation_calls = 0

    async def synchronize_registry_entry(
        registry: ToolRegistry,
        request: ToolCallRequest | ResolvedToolIntent,
        *,
        context: ToolRuntimeContext,
        intent: ToolIntent | None = None,
        catalog: ToolCatalog | None = None,
        events: ModelToolLoopEventProducer | None = None,
    ) -> ToolCallResult:
        """只同步两个真实Registry入口，不替代生产Policy、锁或执行逻辑。"""

        nonlocal arrivals
        async with arrival_lock:
            arrivals += 1
            if arrivals == 2:
                both_ready.set()
        await asyncio.wait_for(both_ready.wait(), timeout=5)
        return await original_call(
            registry,
            request,
            context=context,
            intent=intent,
            catalog=catalog,
            events=events,
        )

    async def count_owner_reservation(
        *,
        step: ModelToolLoopEventStep,
        uow: SQLAlchemyUnitOfWork,
    ) -> None:
        """记录普通路径实际owner准备次数，并委托真实事件仓储。"""

        nonlocal reservation_calls
        reservation_calls += 1
        await original_reserve(step=step, uow=uow)

    monkeypatch.setattr(ToolRegistry, "call", synchronize_registry_entry)
    monkeypatch.setattr(fixture.events, "reserve_tool_in_owner_uow", count_owner_reservation)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(_call_unapproved(fixture), _call_unapproved(fixture)),
            timeout=15,
        )
        assert arrivals == 2
        assert fixture.handler_count == [1]
        assert reservation_calls == 1
        assert results[0] == results[1]
        async with fixture.storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
        assert claim is not None
        assert claim.execution_state == "completed"
        assert claim.result_ref is not None
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_sqlite_concurrent_unapproved_calls_wait_and_replay_exact_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite普通双worker只执行一次handler并向输家返回exact结果。"""

    await _assert_concurrent_unapproved_calls_have_one_owner(
        tmp_path,
        monkeypatch,
        storage_dsn=None,
    )


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实PostgreSQL普通并发合同需要本地测试DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_concurrent_unapproved_calls_wait_and_replay_exact_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL普通双worker通过advisory execution锁收敛为唯一结果。"""

    async with postgres_database("agent_harness_unapproved_tool_owner") as (dsn, _engine):
        await _assert_concurrent_unapproved_calls_have_one_owner(
            tmp_path,
            monkeypatch,
            storage_dsn=dsn,
        )
