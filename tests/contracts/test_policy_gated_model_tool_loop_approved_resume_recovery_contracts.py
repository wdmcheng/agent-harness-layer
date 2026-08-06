"""审批后模型工具循环的恢复、未知副作用与终态事件合同。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn
from tests.contracts.test_policy_gated_model_tool_loop_sqlite_resume_contracts import (
    _build_runtime,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.approvals import ApprovalStateConflict
from agent_harness.events import CanonicalEventType, LocalJsonlEventSink
from agent_harness.events.model_tool_loop import ModelToolLoopEventPublishPending
from agent_harness.identity import IdentityContext
from agent_harness.models import ToolCatalog, ToolCatalogSourceDescriptor, build_tool_catalog
from agent_harness.runtime import RunStatus
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


@pytest.mark.asyncio
async def test_sqlite_approved_exact_replay_recovers_pending_final_event(
    tmp_path: Path,
) -> None:
    """审批工具终态发布未知后，公开恢复补投原event并完成唯一loop。"""

    dsn = sqlite_dsn(tmp_path / "model-tool-loop-approved-event-recovery.db")
    run_migrations(dsn)
    await assert_database_approved_exact_replay_recovers_pending_final_event(
        dsn=dsn,
        tmp_path=tmp_path,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_failure", ["runtime", "timeout", "cancelled"])
async def test_approved_handler_unknown_fences_public_loop(
    tmp_path: Path,
    handler_failure: str,
) -> None:
    """批准后handler已产生副作用再异常时，claim、loop与预算必须共同围栏。"""

    dsn = sqlite_dsn(tmp_path / "model-tool-loop-approved-failure.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    identity = IdentityContext.local_default(session_id="model-tool-loop-approved-failure")
    handler_effects: list[dict[str, Any]] = []
    approvals, orchestrator, provider = _build_runtime(
        storage=storage,
        tmp_path=tmp_path,
        identity=identity,
        final_text=False,
        handler_effects=handler_effects,
        handler_failure=handler_failure,
    )
    try:
        waiting = await orchestrator.start_run(
            agent_id="agent-a",
            input={"prompt": "use search"},
        )
        records = await approvals.list_for_run(actor=identity, run_id=waiting.run_id)
        assert waiting.status == RunStatus.WAITING
        assert len(records) == 1
        continuation = cast(dict[str, Any], records[0].metadata["continuation"])
        if handler_failure == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await approvals.approve(
                    actor=identity,
                    run_id=waiting.run_id,
                    approval_id=records[0].approval_id,
                )
        else:
            with pytest.raises(ApprovalStateConflict) as failure:
                await approvals.approve(
                    actor=identity,
                    run_id=waiting.run_id,
                    approval_id=records[0].approval_id,
                )
            assert failure.value.code == "approval.execution_needs_review"
        assert provider.send_count == 1
        assert handler_effects == [{"q": "weather"}]
        async with storage.uow() as uow:
            loop = await uow.model_tool_loops.get("default", continuation["loop_id"])
            tool_claim = await uow.tool_invocations.get_by_approval_id(records[0].approval_id)
            resolution = await uow.approvals.get_resolution(records[0].approval_id)
        assert tool_claim is not None and tool_claim.execution_state == "needs_review"
        assert tool_claim.result_ref is None
        assert loop is not None and loop.status == "needs_review"
        assert loop.error_ref is not None and loop.error_ref.startswith(
            "model-tool-execution-review:"
        )
        assert resolution is not None and resolution.state == "needs_review"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_approval_resume_registry_drift_writes_validation_before_tool_effects(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """审批等待后Registry撤权时，恢复只留脱敏摘要且不创建工具claim。"""

    dsn = sqlite_dsn(tmp_path / "approval-registry-drift.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    identity = IdentityContext.local_default(session_id="approval-registry-drift")
    handler_effects: list[dict[str, Any]] = []
    preflight_effects: list[dict[str, Any]] = []
    registries: list[Any] = []
    approvals, orchestrator, provider = _build_runtime(
        storage=storage,
        tmp_path=tmp_path,
        identity=identity,
        final_text=False,
        handler_effects=handler_effects,
        registry_sink=registries,
        preflight_effects=preflight_effects,
    )
    waiting = await orchestrator.start_run(
        agent_id="agent-a",
        input={"prompt": "use search"},
    )
    records = await approvals.list_for_run(actor=identity, run_id=waiting.run_id)
    assert waiting.status == RunStatus.WAITING
    assert len(records) == 1 and len(registries) == 1
    async with storage.uow() as uow:
        audits_before = await uow.audit_logs.list_for_tenant(identity.tenant_id)
    policy_before = len([record for record in audits_before if record.action == "policy.decision"])
    registries[0]._agent_tool_allowlist.clear()  # pyright: ignore[reportPrivateUsage]
    caplog.set_level("WARNING", logger="agent_harness.tools.registry.validation")

    try:
        resolved = await approvals.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=records[0].approval_id,
        )
        assert resolved.run is not None and resolved.run.status == RunStatus.FAILED
        assert handler_effects == []
        assert preflight_effects == []
        assert provider.send_count == 1
        async with storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_approval_id(records[0].approval_id)
            audits_after = await uow.audit_logs.list_for_tenant(identity.tenant_id)
        assert claim is None
        assert (
            len([record for record in audits_after if record.action == "policy.decision"])
            == policy_before
        )
        validation = [
            record
            for record in caplog.records
            if record.name == "agent_harness.tools.registry.validation"
        ]
        assert len(validation) == 1
        assert (
            validation[0]
            .getMessage()
            .startswith('{"action":"tool.intent.validation","catalog_digest":')
        )
        assert "weather" not in validation[0].getMessage()
        assert "search" not in validation[0].getMessage()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_approval_resume_catalog_drift_writes_validation_before_tool_effects(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """审批等待后目录事实漂移时，恢复在Registry前也必须留下脱敏摘要。"""

    dsn = sqlite_dsn(tmp_path / "approval-catalog-drift.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    identity = IdentityContext.local_default(session_id="approval-catalog-drift")
    handler_effects: list[dict[str, Any]] = []
    preflight_effects: list[dict[str, Any]] = []
    loop_catalog_state: list[ToolCatalog] = []
    approvals, orchestrator, provider = _build_runtime(
        storage=storage,
        tmp_path=tmp_path,
        identity=identity,
        final_text=False,
        handler_effects=handler_effects,
        preflight_effects=preflight_effects,
        loop_catalog_state=loop_catalog_state,
    )
    waiting = await orchestrator.start_run(
        agent_id="agent-a",
        input={"prompt": "use search"},
    )
    records = await approvals.list_for_run(actor=identity, run_id=waiting.run_id)
    assert waiting.status == RunStatus.WAITING
    assert len(records) == 1 and len(loop_catalog_state) == 1
    continuation = cast(dict[str, Any], records[0].metadata["continuation"])
    frozen_catalog = loop_catalog_state[0]
    frozen_entry = frozen_catalog.tools[0]
    loop_catalog_state[0] = build_tool_catalog(
        allowed_tools=(frozen_entry.name,),
        registry_descriptors=(
            ToolCatalogSourceDescriptor(
                name=frozen_entry.name,
                action="tool.search.changed",
                resource=frozen_entry.resource,
                input_schema=frozen_entry.input_schema,
                registry_ordinal=0,
            ),
        ),
        selection=None,
    )
    async with storage.uow() as uow:
        audits_before = await uow.audit_logs.list_for_tenant(identity.tenant_id)
    policy_before = len([record for record in audits_before if record.action == "policy.decision"])
    caplog.set_level("WARNING", logger="agent_harness.tools.registry.validation")

    try:
        resolved = await approvals.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=records[0].approval_id,
        )
        assert resolved.run is not None and resolved.run.status == RunStatus.FAILED
        assert handler_effects == []
        assert preflight_effects == []
        assert provider.send_count == 1
        async with storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_approval_id(records[0].approval_id)
            loop = await uow.model_tool_loops.get(identity.tenant_id, continuation["loop_id"])
            audits_after = await uow.audit_logs.list_for_tenant(identity.tenant_id)
        assert claim is None
        assert loop is not None and loop.status in {"failed", "cancelled"}
        events = await LocalJsonlEventSink(tmp_path / "events.jsonl").read(run_id=waiting.run_id)
        assert all(event.event_type != CanonicalEventType.RUN_RESUMED for event in events)
        assert (
            len([record for record in audits_after if record.action == "policy.decision"])
            == policy_before
        )
        validation = [
            record
            for record in caplog.records
            if record.name == "agent_harness.tools.registry.validation"
        ]
        assert len(validation) == 1
        summary = validation[0].getMessage()
        assert summary.startswith('{"action":"tool.intent.validation","catalog_digest":')
        assert '"code":"model.tool_catalog_conflict"' in summary
        assert "weather" not in summary
        assert "search" not in summary
        assert "changed" not in summary
    finally:
        await storage.dispose()


async def assert_database_approved_exact_replay_recovers_pending_final_event(
    *,
    dsn: str,
    tmp_path: Path,
) -> None:
    """在给定数据库验证approved exact claim补投pending final且不重跑工具。"""

    identity = IdentityContext.local_default(session_id="approved-event-recovery")
    handler_effects: list[dict[str, Any]] = []
    storage = SQLAlchemyStorage.from_dsn(dsn)
    first_approvals, first_orchestrator, first_provider = _build_runtime(
        storage=storage,
        tmp_path=tmp_path,
        identity=identity,
        final_text=False,
        handler_effects=handler_effects,
    )
    waiting = await first_orchestrator.start_run(
        agent_id="agent-a",
        input={"prompt": "use search"},
    )
    records = await first_approvals.list_for_run(actor=identity, run_id=waiting.run_id)
    assert waiting.status == RunStatus.WAITING
    assert len(records) == 1
    approval = records[0]
    continuation = cast(dict[str, Any], approval.metadata["continuation"])
    assert first_provider.send_count == 1
    await storage.dispose()

    recovered_storage = SQLAlchemyStorage.from_dsn(dsn)
    recovered_approvals, _, recovered_provider = _build_runtime(
        storage=recovered_storage,
        tmp_path=tmp_path,
        identity=identity,
        final_text=True,
        handler_effects=handler_effects,
        fail_tool_final_publish=True,
    )
    try:
        with pytest.raises(ModelToolLoopEventPublishPending):
            await recovered_approvals.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
            )
        assert handler_effects == [{"q": "weather"}]
        assert recovered_provider.send_count == 0
        async with recovered_storage.uow() as uow:
            pending = [
                item
                for item in await uow.evidence_outbox.pending(run_id=waiting.run_id)
                if item.operation_kind == EvidenceOperationKind.TOOL_INVOCATION.value
                and item.state == "result_persisted"
            ]
            pending_count = len(pending)
            final_event_id = pending[0].event_id if pending else None
            claim = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
            resolution = await uow.approvals.get_resolution(approval.approval_id)
        assert pending_count == 1
        assert final_event_id is not None
        assert claim is not None and claim.execution_state == "completed"
        assert resolution is not None and resolution.state == "recovery_pending"

        resolved = await recovered_approvals.recover_claimed(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=approval.approval_id,
        )

        assert resolved.approval.status == "approved"
        assert resolved.run is not None and resolved.run.status == RunStatus.COMPLETED
        assert handler_effects == [{"q": "weather"}]
        assert recovered_provider.send_count == 1
        events = await LocalJsonlEventSink(tmp_path / "events.jsonl").read(run_id=waiting.run_id)
        assert [
            event.event_id
            for event in events
            if event.event_type == CanonicalEventType.TOOL_CALL_COMPLETED
        ] == [final_event_id]
        async with recovered_storage.uow() as uow:
            loop = await uow.model_tool_loops.get("default", continuation["loop_id"])
            pending_after = [
                item
                for item in await uow.evidence_outbox.pending(run_id=waiting.run_id)
                if item.operation_kind == EvidenceOperationKind.TOOL_INVOCATION.value
            ]
        assert loop is not None and loop.status == "completed"
        assert pending_after == []
    finally:
        await recovered_storage.dispose()
