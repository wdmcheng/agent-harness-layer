"""Canonical run trace 的 repository、API 与 CLI 公开合同。"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import asgi_request, descriptor
from tests.contracts.run_trace_contract_helpers import persisted_event_bus, sqlite_dsn
from tests.contracts.runtime_contract_helpers import FakeContractExecutor

from agent_harness.events import LocalJsonlEventSink
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import (
    ApprovalCreate,
    AuditLogCreate,
    EvalCaseCreate,
    EvalRunCreate,
    EvalScoreCreate,
    SQLAlchemyStorage,
    ToolInvocationCreate,
    run_migrations,
)
from agent_harness.storage.run_trace_gate import RunTraceScopeConflict
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_run_scoped_repositories_reject_missing_or_mismatched_trace(
    tmp_path: Path,
) -> None:
    """通用 evidence repository 回查 run，non-run audit/eval 仍保持独立语义。"""

    dsn = sqlite_dsn(tmp_path / "repository-gates.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(
            storage,
            LocalJsonlEventSink(tmp_path / "repository-events.jsonl"),
        ),
        executor_resolver=lambda _agent_id: FakeContractExecutor(),
    )
    try:
        run = await orchestrator.start_run(
            agent_id="fake-agent",
            input={},
            trace_id="trace-repository",
        )
        async with storage.uow() as uow:
            with pytest.raises(RunTraceScopeConflict):
                await uow.audit_logs.create(
                    AuditLogCreate(
                        tenant_id="default",
                        action="run.failed",
                        payload={"run_id": run.run_id, "trace_id": "trace-other"},
                    )
                )
            tool = await uow.tool_invocations.create(
                ToolInvocationCreate(
                    tenant_id="default",
                    agent_id="fake-agent",
                    run_id=run.run_id,
                    tool_name="read",
                    args_ref="artifact://args",
                    status="completed",
                    trace_id=None,
                )
            )
            with pytest.raises(RunTraceScopeConflict):
                await uow.eval_cases.create(
                    EvalCaseCreate(
                        tenant_id="default",
                        agent_id="fake-agent",
                        run_id=run.run_id,
                        trace_id="trace-other",
                        name="bad-case",
                    )
                )
            with pytest.raises(RunTraceScopeConflict):
                await uow.approvals.create(
                    ApprovalCreate(
                        tenant_id="default",
                        run_id=run.run_id,
                        agent_id="fake-agent",
                        action="write",
                        resource="file:a",
                        reason="review",
                        trace_id="trace-other",
                    )
                )

            audit = await uow.audit_logs.create(
                AuditLogCreate(
                    tenant_id="default",
                    action="run.completed",
                    payload={"run_id": run.run_id, "trace_id": "trace-repository"},
                )
            )
            non_run_audit = await uow.audit_logs.create(
                AuditLogCreate(
                    tenant_id="default",
                    action="auth.login",
                    payload={"trace_id": None},
                )
            )
            eval_run = await uow.eval_runs.create(
                EvalRunCreate(
                    tenant_id="default",
                    agent_id="fake-agent",
                    run_id=run.run_id,
                )
            )
            non_run_case = await uow.eval_cases.create(
                EvalCaseCreate(
                    tenant_id="default",
                    agent_id="fake-agent",
                    name="manual-case",
                    trigger="manual",
                )
            )
            await uow.commit()

        assert audit.record_scope == "run"
        assert audit.payload["trace_id"] == "trace-repository"
        assert non_run_audit.record_scope == "non_run"
        assert tool.trace_id == "trace-repository"
        assert eval_run.trace_id == "trace-repository"
        assert non_run_case.run_id is None and non_run_case.trace_id is None

        async with storage.uow() as uow:
            with pytest.raises(RunTraceScopeConflict):
                await uow.eval_scores.create(
                    EvalScoreCreate(
                        tenant_id="default",
                        eval_run_id=eval_run.eval_run_id,
                        case_id=non_run_case.case_id,
                        run_id=run.run_id,
                        trace_id="trace-other",
                        metric="exact_match",
                        value=1.0,
                    )
                )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_api_trace_errors_are_stable_and_have_zero_run_event_side_effects(
    tmp_path: Path,
) -> None:
    """HTTP missing/same 重放成功；格式、全局与幂等冲突返回稳定 envelope。"""

    dsn = sqlite_dsn(tmp_path / "api.db")
    events_path = tmp_path / "api-events.jsonl"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(events_path)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=persisted_event_bus(storage, sink),
        executor_resolver=lambda _agent_id: FakeContractExecutor(),
    )
    app = create_app(
        orchestrator=orchestrator,
        event_sink=sink,
        registry=AgentRegistry([descriptor()]),
    )
    app_callable = cast(Any, app)
    operation = app.openapi()["paths"]["/api/v1/agents/{agent_id}/runs"]["post"]
    trace_parameter = next(
        parameter for parameter in operation["parameters"] if parameter["name"] == "X-Trace-Id"
    )
    assert trace_parameter["in"] == "header"
    assert trace_parameter["required"] is False
    try:
        first_status, first = await asgi_request(
            app_callable,
            method="POST",
            path="/api/v1/agents/examples.basic/runs",
            body={"input": {}, "idempotency_key": "idem-api"},
        )
        replay_status, replay = await asgi_request(
            app_callable,
            method="POST",
            path="/api/v1/agents/examples.basic/runs",
            body={"input": {}, "idempotency_key": "idem-api"},
        )
        idem_status, idem = await asgi_request(
            app_callable,
            method="POST",
            path="/api/v1/agents/examples.basic/runs",
            body={"input": {}, "idempotency_key": "idem-api"},
            headers=[(b"x-trace-id", b"trace-other")],
        )
        explicit_status, _explicit = await asgi_request(
            app_callable,
            method="POST",
            path="/api/v1/agents/examples.basic/runs",
            body={"input": {}, "idempotency_key": "idem-explicit"},
            headers=[(b"x-trace-id", b"trace-api")],
        )
        global_status, global_error = await asgi_request(
            app_callable,
            method="POST",
            path="/api/v1/agents/examples.basic/runs",
            body={"input": {}, "idempotency_key": "idem-global"},
            headers=[(b"x-trace-id", b"trace-api")],
        )
        invalid_status, invalid = await asgi_request(
            app_callable,
            method="POST",
            path="/api/v1/agents/examples.basic/runs",
            body={"input": {}, "idempotency_key": "idem-invalid"},
            headers=[(b"x-trace-id", b" invalid")],
        )
        async with storage.uow() as uow:
            runs = await uow.runs.list_for_tenant("default")
    finally:
        await storage.dispose()

    assert first_status == replay_status == explicit_status == 200
    assert replay["run_id"] == first["run_id"]
    assert (idem_status, idem["error"]["code"]) == (409, "trace.idempotency_conflict")
    assert (global_status, global_error["error"]["code"]) == (409, "trace.conflict")
    assert (invalid_status, invalid["error"]["code"]) == (422, "validation_error")
    assert len(runs) == 2
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == 4


def test_cli_trace_errors_use_stderr_and_leave_business_tables_unchanged(tmp_path: Path) -> None:
    """CLI 三类 trace 失败均无业务 stdout，且不新增 run/event。"""

    db_path = tmp_path / "cli.db"
    events_path = tmp_path / "cli-events.jsonl"
    run_migrations(sqlite_dsn(db_path))
    common = [
        sys.executable,
        "-m",
        "agent_harness.cli",
        "run",
        "examples.basic",
        "--profile",
        "local",
        "--profiles-dir",
        str(ROOT / "templates" / "service-app" / "configs" / "profiles"),
        "--agents-dir",
        str(ROOT / "templates" / "service-app" / "agents"),
        "--storage-dsn",
        sqlite_dsn(db_path),
        "--events-path",
        str(events_path),
    ]

    def invoke(*extra: str) -> subprocess.CompletedProcess[str]:
        """以共享 CLI 参数执行子进程，统一捕获各类 trace 边界的外部表现。"""

        return subprocess.run(
            [*common, *extra],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    success = invoke("--idempotency-key", "idem-cli", "--trace-id", "trace-cli")
    successful_event_count = len(events_path.read_text(encoding="utf-8").splitlines())
    global_conflict = invoke("--idempotency-key", "idem-global", "--trace-id", "trace-cli")
    idem_conflict = invoke("--idempotency-key", "idem-cli", "--trace-id", "trace-other")
    invalid = invoke("--idempotency-key", "idem-invalid", "--trace-id", " invalid")

    assert success.returncode == 0 and "status: completed" in success.stdout
    for result, code in (
        (global_conflict, "trace.conflict"),
        (idem_conflict, "trace.idempotency_conflict"),
        (invalid, "validation_error"),
    ):
        assert result.returncode != 0
        assert result.stdout == ""
        assert code in result.stderr
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("select count(*) from agent_runs").fetchone() == (1,)
    assert successful_event_count == 3
    assert len(events_path.read_text(encoding="utf-8").splitlines()) == successful_event_count


def test_downstream_modules_do_not_own_canonical_trace_generation() -> None:
    """approval/event/tool/model adapter 只能传播 trace，不可复用 runtime normalizer。"""

    roots = (
        ROOT / "packages" / "agent-harness" / "src" / "agent_harness" / "approvals",
        ROOT / "packages" / "agent-harness" / "src" / "agent_harness" / "events",
        ROOT / "packages" / "agent-harness" / "src" / "agent_harness" / "tools",
        ROOT / "packages" / "agent-harness" / "src" / "agent_harness" / "adapters" / "models",
    )
    for root in roots:
        for path in root.rglob("*.py"):
            assert "normalize_trace_id" not in path.read_text(encoding="utf-8"), path
