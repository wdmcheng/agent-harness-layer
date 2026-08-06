"""模型工具结果未知、制品与提交确认窗口的双数据库合同。"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.run_trace_revision_hardening_postgresql_helpers import postgres_database
from tests.contracts.test_policy_gated_model_tool_loop_approved_event_atomicity_contracts import (
    _approved_model_fixture,
    _call_approved,
    _call_unapproved,
)

from agent_harness.events import CanonicalEventType
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.tools import (
    ApprovedToolExecutionUncertain,
    ModelToolExecutionNeedsReview,
)


async def _assert_handler_unknown_fences_public_entry(
    tmp_path: Path,
    *,
    mode: str,
    storage_dsn: str | None,
) -> None:
    """真实Registry与存储必须把handler后异常原子提升为needs-review。"""

    fixture = await _approved_model_fixture(
        tmp_path,
        storage_dsn=storage_dsn,
        handler_failure=True,
    )
    expected = (
        ApprovedToolExecutionUncertain if mode == "approved" else ModelToolExecutionNeedsReview
    )
    try:
        with pytest.raises(expected):
            if mode == "approved":
                await _call_approved(fixture)
            else:
                await _call_unapproved(fixture)
        assert fixture.handler_count == [1]
        async with fixture.storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
            loop = await uow.model_tool_loops.get(
                fixture.context.actor.tenant_id,
                fixture.intent.loop_id,
            )
            resolution = await uow.approvals.get_resolution(fixture.approval_id)
        assert claim is not None and claim.execution_state == "needs_review"
        assert claim.result_ref is None
        assert loop is not None and loop.status == "needs_review"
        if mode == "approved":
            assert resolution is not None and resolution.state == "needs_review"
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_sqlite_handler_unknown_fences_normal_and_approved_public_entries(
    tmp_path: Path,
    mode: str,
) -> None:
    """SQLite两条公开入口均拒绝把handler后异常固化成确定失败。"""

    await _assert_handler_unknown_fences_public_entry(
        tmp_path,
        mode=mode,
        storage_dsn=None,
    )


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实PostgreSQL handler未知合同需要本地测试DSN。",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_postgresql_handler_unknown_fences_normal_and_approved_public_entries(
    tmp_path: Path,
    mode: str,
) -> None:
    """PostgreSQL两条公开入口以同一UoW围栏claim、loop与审批lease。"""

    async with postgres_database(f"agent_harness_handler_unknown_{mode}") as (dsn, _engine):
        await _assert_handler_unknown_fences_public_entry(
            tmp_path,
            mode=mode,
            storage_dsn=dsn,
        )


async def _assert_result_guard_failure_fences_public_entry(
    tmp_path: Path,
    *,
    mode: str,
    storage_dsn: str | None,
) -> None:
    """handler返回不可守卫值时，公开入口关闭未知结果且不再执行handler。"""

    fixture = await _approved_model_fixture(
        tmp_path,
        storage_dsn=storage_dsn,
        result_guard_failure=True,
    )
    expected = (
        ApprovedToolExecutionUncertain if mode == "approved" else ModelToolExecutionNeedsReview
    )
    invoke = _call_approved if mode == "approved" else _call_unapproved
    try:
        with pytest.raises(expected):
            await invoke(fixture)
        with pytest.raises(expected):
            await invoke(fixture)
        assert fixture.handler_count == [1]
        async with fixture.storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
            loop = await uow.model_tool_loops.get(
                fixture.context.actor.tenant_id,
                fixture.intent.loop_id,
            )
            resolution = await uow.approvals.get_resolution(fixture.approval_id)
        assert claim is not None and claim.execution_state == "needs_review"
        assert claim.result_ref is None
        assert loop is not None and loop.status == "needs_review"
        if mode == "approved":
            assert resolution is not None and resolution.state == "needs_review"
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_sqlite_result_guard_failure_fences_normal_and_approved_public_entries(
    tmp_path: Path,
    mode: str,
) -> None:
    """SQLite普通/批准入口共享不可序列化结果的needs-review围栏。"""

    await _assert_result_guard_failure_fences_public_entry(
        tmp_path,
        mode=mode,
        storage_dsn=None,
    )


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实PostgreSQL结果守卫失败合同需要本地测试DSN。",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_postgresql_result_guard_failure_fences_normal_and_approved_public_entries(
    tmp_path: Path,
    mode: str,
) -> None:
    """PostgreSQL不可序列化结果不得逃逸为raw异常或再次执行。"""

    async with postgres_database(f"agent_harness_result_guard_failure_{mode}") as (dsn, _engine):
        await _assert_result_guard_failure_fences_public_entry(
            tmp_path,
            mode=mode,
            storage_dsn=dsn,
        )


async def _assert_result_artifact_failure_fences_public_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    storage_dsn: str | None,
) -> None:
    """handler返回后若结果制品无法耐久化，禁止自动重放其副作用。"""

    fixture = await _approved_model_fixture(tmp_path, storage_dsn=storage_dsn)
    artifact_store = fixture.registry._artifact_store
    original_write_json = artifact_store.write_json

    def fail_result_write(payload: dict[str, Any]) -> object:
        """只破坏最终ToolCallResult写入，保留参数制品与handler执行路径。"""

        if "tool_name" in payload and "status" in payload:
            raise OSError("result artifact write acknowledgement unavailable")
        return original_write_json(payload)

    monkeypatch.setattr(artifact_store, "write_json", fail_result_write)
    expected = (
        ApprovedToolExecutionUncertain if mode == "approved" else ModelToolExecutionNeedsReview
    )
    invoke = _call_approved if mode == "approved" else _call_unapproved
    try:
        with pytest.raises(expected):
            await invoke(fixture)
        assert fixture.handler_count == [1]
        with pytest.raises(expected):
            await invoke(fixture)
        assert fixture.handler_count == [1]
        async with fixture.storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
            loop = await uow.model_tool_loops.get(
                fixture.context.actor.tenant_id,
                fixture.intent.loop_id,
            )
            resolution = await uow.approvals.get_resolution(fixture.approval_id)
        assert claim is not None and claim.execution_state == "needs_review"
        assert claim.result_ref is None
        assert loop is not None and loop.status == "needs_review"
        if mode == "approved":
            assert resolution is not None and resolution.state == "needs_review"
        terminal_types = {
            CanonicalEventType.TOOL_CALL_COMPLETED,
            CanonicalEventType.TOOL_CALL_FAILED,
        }
        assert [
            event.event_type
            for event in await fixture.sink.read(run_id=fixture.run_id)
            if event.event_type in terminal_types
        ] == []
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_sqlite_result_artifact_failure_fences_normal_and_approved_public_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """SQLite结果制品失败统一关闭claim、loop与可选审批lease。"""

    await _assert_result_artifact_failure_fences_public_entry(
        tmp_path,
        monkeypatch,
        mode=mode,
        storage_dsn=None,
    )


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实PostgreSQL结果制品失败合同需要本地测试DSN。",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_postgresql_result_artifact_failure_fences_normal_and_approved_public_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """PostgreSQL结果制品失败不得留下可自动重放的executing claim。"""

    async with postgres_database(f"agent_harness_result_artifact_failure_{mode}") as (
        dsn,
        _engine,
    ):
        await _assert_result_artifact_failure_fences_public_entry(
            tmp_path,
            monkeypatch,
            mode=mode,
            storage_dsn=dsn,
        )


async def _assert_result_commit_ack_unknown_replays_exact_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    storage_dsn: str | None,
) -> None:
    """完成提交已落地但确认丢失时，重读exact终态而不重跑handler。"""

    fixture = await _approved_model_fixture(tmp_path, storage_dsn=storage_dsn)
    original_commit = SQLAlchemyUnitOfWork.commit
    acknowledgement_lost = False

    async def commit_then_lose_terminal_ack(uow: SQLAlchemyUnitOfWork) -> None:
        """先提交真实终态，再只对首次完成提交模拟确认丢失。"""

        nonlocal acknowledgement_lost
        await original_commit(uow)
        claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
        if (
            not acknowledgement_lost
            and claim is not None
            and claim.execution_state in {"completed", "failed"}
            and claim.result_ref is not None
        ):
            acknowledgement_lost = True
            raise OSError("result commit acknowledgement unavailable")

    monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", commit_then_lose_terminal_ack)
    invoke = _call_approved if mode == "approved" else _call_unapproved
    try:
        first = await invoke(fixture)
        second = await invoke(fixture)
        assert acknowledgement_lost
        assert first == second
        assert fixture.handler_count == [1]
        async with fixture.storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
            loop = await uow.model_tool_loops.get(
                fixture.context.actor.tenant_id,
                fixture.intent.loop_id,
            )
        assert claim is not None and claim.execution_state == "completed"
        assert claim.result_ref is not None
        assert loop is not None and loop.status == "active"
        terminal_types = {
            CanonicalEventType.TOOL_CALL_COMPLETED,
            CanonicalEventType.TOOL_CALL_FAILED,
        }
        assert [
            event.event_type
            for event in await fixture.sink.read(run_id=fixture.run_id)
            if event.event_type in terminal_types
        ] == [CanonicalEventType.TOOL_CALL_COMPLETED]
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_sqlite_result_commit_ack_unknown_replays_exact_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """SQLite完成确认丢失重读exact终态，handler仍恰好一次。"""

    await _assert_result_commit_ack_unknown_replays_exact_terminal(
        tmp_path,
        monkeypatch,
        mode=mode,
        storage_dsn=None,
    )


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实PostgreSQL完成提交确认丢失合同需要本地测试DSN。",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_postgresql_result_commit_ack_unknown_replays_exact_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """PostgreSQL完成确认丢失重读exact终态，禁止第二次handler副作用。"""

    async with postgres_database(f"agent_harness_result_commit_ack_{mode}") as (dsn, _engine):
        await _assert_result_commit_ack_unknown_replays_exact_terminal(
            tmp_path,
            monkeypatch,
            mode=mode,
            storage_dsn=dsn,
        )


async def _assert_result_commit_unknown_without_terminal_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    storage_dsn: str | None,
) -> None:
    """完成提交未落地时，重读executing并关闭claim、loop与审批lease。"""

    fixture = await _approved_model_fixture(tmp_path, storage_dsn=storage_dsn)
    original_commit = SQLAlchemyUnitOfWork.commit
    acknowledgement_lost = False

    async def lose_ack_before_terminal_commit(uow: SQLAlchemyUnitOfWork) -> None:
        """只在首次未提交终态可见时抛错，让UoW退出路径真实回滚。"""

        nonlocal acknowledgement_lost
        claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
        if (
            not acknowledgement_lost
            and claim is not None
            and claim.execution_state in {"completed", "failed"}
            and claim.result_ref is not None
        ):
            acknowledgement_lost = True
            raise OSError("result commit outcome unavailable before terminal commit")
        await original_commit(uow)

    monkeypatch.setattr(SQLAlchemyUnitOfWork, "commit", lose_ack_before_terminal_commit)
    expected = (
        ApprovedToolExecutionUncertain if mode == "approved" else ModelToolExecutionNeedsReview
    )
    invoke = _call_approved if mode == "approved" else _call_unapproved
    try:
        with pytest.raises(expected):
            await invoke(fixture)
        with pytest.raises(expected):
            await invoke(fixture)
        assert acknowledgement_lost
        assert fixture.handler_count == [1]
        async with fixture.storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_tool_call_id(fixture.intent.tool_call_id)
            loop = await uow.model_tool_loops.get(
                fixture.context.actor.tenant_id,
                fixture.intent.loop_id,
            )
            resolution = await uow.approvals.get_resolution(fixture.approval_id)
        assert claim is not None and claim.execution_state == "needs_review"
        assert claim.result_ref is None
        assert loop is not None and loop.status == "needs_review"
        if mode == "approved":
            assert resolution is not None and resolution.state == "needs_review"
        terminal_types = {
            CanonicalEventType.TOOL_CALL_COMPLETED,
            CanonicalEventType.TOOL_CALL_FAILED,
        }
        assert [
            event.event_type
            for event in await fixture.sink.read(run_id=fixture.run_id)
            if event.event_type in terminal_types
        ] == []
    finally:
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_sqlite_result_commit_unknown_without_terminal_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """SQLite完成提交未落地时转needs-review，禁止再次执行handler。"""

    await _assert_result_commit_unknown_without_terminal_fences(
        tmp_path,
        monkeypatch,
        mode=mode,
        storage_dsn=None,
    )


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实PostgreSQL未落地完成提交合同需要本地测试DSN。",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unapproved", "approved"])
async def test_postgresql_result_commit_unknown_without_terminal_fences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """PostgreSQL完成提交未落地时围栏claim、loop与可选审批lease。"""

    async with postgres_database(f"agent_harness_result_commit_unknown_{mode}") as (
        dsn,
        _engine,
    ):
        await _assert_result_commit_unknown_without_terminal_fences(
            tmp_path,
            monkeypatch,
            mode=mode,
            storage_dsn=dsn,
        )
