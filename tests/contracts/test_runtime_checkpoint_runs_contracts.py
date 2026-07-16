"""Runtime、checkpoint 和 run lifecycle 的公开契约测试。

这些测试穿过 runtime module、CLI 和 template API helper 三个入口，证明 fake
agent 在无真实模型 key 时也能创建 run、写 checkpoint、resume、产出 terminal
event，并且 idempotency key 不会因为进程内对象重建而失效。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.requests import Request
from tests.contracts.auth_policy_hitl_contract_helpers import asgi_request
from tests.contracts.runtime_contract_helpers import FakeContractExecutor, sqlite_dsn

from agent_harness.events import (
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
)
from agent_harness.runtime import (
    ApprovalWaitState,
    CheckpointStore,
    IdempotencyKey,
    InvalidRunTransition,
    ResumeToken,
    RunOrchestrator,
    RunStatus,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.api.routes.runs import RunCreateRequest, create_run_for_test, public_events, router
from app.main import create_app
from app.workers.runtime_worker import run_once as worker_run_once

ROOT = Path(__file__).resolve().parents[2]

RUN_OPENAPI_CONTRACTS: dict[
    tuple[str, str],
    tuple[set[str], dict[str, str]],
] = {
    ("/api/v1/agents/{agent_id}/runs", "post"): (
        {"200", "202", "400", "401", "403", "404", "409", "422", "500", "503"},
        {"200": "RunCreateResponse", "202": "RunCreateResponse"},
    ),
    ("/api/v1/runs/{run_id}", "get"): (
        {"200", "401", "403", "404", "500"},
        {"200": "RunDetailResponse"},
    ),
    ("/api/v1/runs/{run_id}/events", "get"): (
        {"200", "401", "403", "404", "422", "500"},
        {"200": "RunEventsResponse"},
    ),
    ("/api/v1/runs/{run_id}/cancel", "post"): (
        {"200", "401", "403", "404", "409", "500"},
        {"200": "RunCreateResponse"},
    ),
    ("/api/v1/runs/{run_id}/resume", "post"): (
        {"200", "401", "403", "404", "409", "422", "500"},
        {"200": "RunCreateResponse"},
    ),
}


def test_runtime_public_seams_expose_checkpoint_dtos() -> None:
    # Product-Spec 要求 runtime 包暴露 checkpoint/resume/idempotency seam。
    # 这里不实现独立 store，只锁公开 DTO/Protocol，防止调用方继续依赖裸字符串。
    assert ResumeToken(value="resume-1").value == "resume-1"
    assert IdempotencyKey(value="idem-1").value == "idem-1"
    assert ApprovalWaitState(approval_id="approval-1").approval_id == "approval-1"
    assert CheckpointStore is not None


def test_runtime_main_spec_has_durable_purpose() -> None:
    """归档后的 runtime 主规格必须说明长期用途，不能保留生成器占位文本。"""

    spec = (ROOT / "openspec/specs/runtime-checkpoint-runs/spec.md").read_text(encoding="utf-8")
    purpose = spec.split("## Purpose", 1)[1].split("## Requirements", 1)[0]

    assert "TBD - created by archiving change" not in purpose
    for marker in ("run lifecycle", "idempotency", "checkpoint", "API、CLI 和 worker"):
        assert marker in purpose


async def build_orchestrator(tmp_path: Path) -> tuple[RunOrchestrator, SQLAlchemyStorage, Path]:
    """构造带 SQLite storage 和 local event sink 的测试 runtime。"""

    # 这个 helper 故意返回 storage，让测试在 finally 中显式 dispose。
    # runtime tests 会重建 orchestrator；如果这里把 DB/sink 藏成全局 fixture，
    # 就很容易误把内存状态当成 checkpoint/resume 证据。
    db_path = tmp_path / "runtime.db"
    events_path = tmp_path / "events.jsonl"
    run_migrations(sqlite_dsn(db_path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(db_path))
    bus = EventBus(sink=LocalJsonlEventSink(events_path))
    executor = FakeContractExecutor()
    return (
        RunOrchestrator(
            storage=storage,
            event_bus=bus,
            executor_resolver=lambda _agent_id: executor,
        ),
        storage,
        events_path,
    )


@pytest.mark.asyncio
async def test_fake_agent_run_is_idempotent_and_has_one_terminal_event(tmp_path: Path) -> None:
    # start_run 是 runtime 的核心 seam：调用方只给 agent/input/idempotency，
    # 不需要知道 repository、event sink 或 checkpoint 表。重复提交必须返回同一 run。
    orchestrator, storage, events_path = await build_orchestrator(tmp_path)
    try:
        first = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "hello"},
            idempotency_key="idem-runtime",
        )
        second = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "hello"},
            idempotency_key="idem-runtime",
        )
        events = await LocalJsonlEventSink(events_path).read(run_id=first.run_id)

        assert first.run_id == second.run_id
        assert first.status == RunStatus.COMPLETED
        assert [event.event_type.value for event in events] == ["run.started", "run.completed"]
        assert sum(1 for event in events if event.terminal) == 1

        with pytest.raises(InvalidRunTransition):
            await orchestrator.cancel_run(first.run_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_checkpoint_resume_survives_orchestrator_recreation(tmp_path: Path) -> None:
    # checkpoint/resume 必须穿过持久化 seam。这里故意丢弃第一个 orchestrator，
    # 用同一数据库和事件文件重建第二个实例，证明 resume 不依赖内存状态。
    orchestrator, storage, events_path = await build_orchestrator(tmp_path)
    try:
        waiting = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "pause"},
            checkpoint_state={"step": "waiting"},
        )
        assert waiting.status == RunStatus.WAITING
        assert waiting.resume_token is not None
    finally:
        await storage.dispose()

    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(tmp_path / "runtime.db"))
    resumed = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=LocalJsonlEventSink(events_path)),
    )
    try:
        result = await resumed.resume_run(waiting.resume_token or "")
        events = await LocalJsonlEventSink(events_path).read(run_id=waiting.run_id)

        assert result.status == RunStatus.COMPLETED
        assert [event.seq for event in events] == [1, 2, 3]
        assert events[-1].terminal is True
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_waiting_idempotency_replay_invokes_pending_delegation_recovery(
    tmp_path: Path,
) -> None:
    """local 重放不能只返回 WAITING，必须先给 durable delegation 恢复机会。"""

    class _RecoveryProbe:
        def __init__(self) -> None:
            self.parent_run_ids: list[str] = []

        async def recover_pending_for_parent(self, *, parent_run_id: str) -> int:
            self.parent_run_ids.append(parent_run_id)
            return 0

    orchestrator, storage, _events_path = await build_orchestrator(tmp_path)
    recovery = _RecoveryProbe()
    orchestrator.bind_execution_service("agent.delegate", recovery)
    try:
        first = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "delegation waiting"},
            idempotency_key="waiting-delegation-replay",
            checkpoint_state={"step": "waiting"},
        )
        replay = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "delegation waiting"},
            idempotency_key="waiting-delegation-replay",
        )
    finally:
        await storage.dispose()

    assert first.status == replay.status == RunStatus.WAITING
    assert recovery.parent_run_ids == [first.run_id]


@pytest.mark.asyncio
async def test_resume_rejects_mismatched_path_before_mutating_token_run(tmp_path: Path) -> None:
    # API path 中的 run_id 是安全边界。token 属于 A 时，用 B 的 URL 调 resume
    # 必须在完成 A 之前失败，否则一个错误 URL 就会推进别的 run。
    orchestrator, storage, events_path = await build_orchestrator(tmp_path)
    try:
        token_run = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "token"},
            checkpoint_state={"step": "waiting-a"},
        )
        path_run = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "path"},
            checkpoint_state={"step": "waiting-b"},
        )
        assert token_run.resume_token is not None

        with pytest.raises(LookupError):
            await orchestrator.resume_run(
                token_run.resume_token,
                expected_run_id=path_run.run_id,
            )

        token_status = await orchestrator.get_run(token_run.run_id)
        path_status = await orchestrator.get_run(path_run.run_id)
        token_events = await LocalJsonlEventSink(events_path).read(run_id=token_run.run_id)
    finally:
        await storage.dispose()

    assert token_status.status == RunStatus.WAITING
    assert path_status.status == RunStatus.WAITING
    assert [event.event_type.value for event in token_events] == [
        "run.started",
        "checkpoint.created",
    ]


def test_cli_run_fake_agent_returns_terminal_event(tmp_path: Path) -> None:
    # CLI 是 app developer 的最小可运行入口，但 schema 必须由显式 migration
    # 先初始化；运行命令本身不要求真实 provider key。
    db_path = tmp_path / "cli.db"
    events_path = tmp_path / "cli-events.jsonl"
    run_migrations(sqlite_dsn(db_path))
    result = subprocess.run(
        [
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
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "run_id:" in result.stdout
    assert "status: completed" in result.stdout
    assert "terminal_event: run.completed" in result.stdout


def test_template_router_exposes_create_run_route() -> None:
    # OpenSpec 要求 service-app 暴露 run API route；只测 helper 会漏掉
    # APIRouter 没注册的回归。这里不启动服务器，只锁模板的路由表。
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    route_methods = {
        (route.path, method) for route in routes for method in (route.methods or set())
    }

    assert {
        ("/api/v1/agents/{agent_id}/runs", "POST"),
        ("/api/v1/runs/{run_id}", "GET"),
        ("/api/v1/runs/{run_id}/events", "GET"),
        ("/api/v1/runs/{run_id}/cancel", "POST"),
        ("/api/v1/runs/{run_id}/resume", "POST"),
    } <= route_methods
    assert ("/api/v1/runs", "POST") not in route_methods


def test_run_openapi_response_status_and_schema_are_exact(tmp_path: Path) -> None:
    """最终 OpenAPI 必须逐 operation 精确对齐 RUN-001 至 RUN-005。"""

    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "unused-events.jsonl"),
    )
    paths = cast(dict[str, Any], app.openapi()["paths"])

    for (path, method), (expected_statuses, success_schemas) in RUN_OPENAPI_CONTRACTS.items():
        operation = cast(dict[str, Any], paths[path][method])
        responses = cast(dict[str, Any], operation["responses"])
        actual_statuses = set(responses)
        assert actual_statuses == expected_statuses, (
            f"{method.upper()} {path} response status 漂移: "
            f"missing={sorted(expected_statuses - actual_statuses)}, "
            f"extra={sorted(actual_statuses - expected_statuses)}"
        )

        for status, schema_name in success_schemas.items():
            schema = responses[status]["content"]["application/json"]["schema"]
            assert schema["$ref"] == f"#/components/schemas/{schema_name}"

        error_statuses = expected_statuses - set(success_schemas)
        for status in error_statuses:
            schema = responses[status]["content"]["application/json"]["schema"]
            assert schema["$ref"] == "#/components/schemas/ApiErrorEnvelope"

    run_detail_operation = paths["/api/v1/runs/{run_id}"]["get"]
    run_detail_schema = json.dumps(run_detail_operation, sort_keys=True)
    assert "RunDetailResponse" in run_detail_schema
    assert "RunCreateResponse" not in run_detail_schema


def test_fastapi_auto_422_is_removed_only_from_allowlisted_run_operations(
    tmp_path: Path,
) -> None:
    """框架默认 422 存在，但应用只能窄化 RUN-002 与 RUN-004。"""

    framework_app = FastAPI()

    async def _read_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    framework_app.add_api_route("/items/{item_id}", _read_item, methods=["GET"])
    assert "422" in framework_app.openapi()["paths"]["/items/{item_id}"]["get"]["responses"]

    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "unused-events.jsonl"),
    )
    paths = app.openapi()["paths"]
    assert "422" not in paths["/api/v1/runs/{run_id}"]["get"]["responses"]
    assert "422" not in paths["/api/v1/runs/{run_id}/cancel"]["post"]["responses"]
    for path, method in (
        ("/api/v1/agents/{agent_id}/runs", "post"),
        ("/api/v1/runs/{run_id}/events", "get"),
        ("/api/v1/runs/{run_id}/resume", "post"),
    ):
        assert "422" in paths[path][method]["responses"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/agents/examples.basic/runs", {"input": []}),
        ("GET", "/api/v1/runs/run-1/events?after_seq=-1", None),
        ("POST", "/api/v1/runs/run-1/resume", {}),
    ],
)
async def test_run_validation_errors_use_api_error_envelope(
    tmp_path: Path,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """所有声明 422 的 run operation 都要穿过统一 validation handler。"""

    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "unused-events.jsonl"),
    )
    status, response_body = await asgi_request(
        cast(Any, app),
        method=method,
        path=path,
        body=body,
    )

    assert status == 422
    assert response_body["error"]["code"] == "validation_error"
    assert response_body["error"]["request_id"]
    assert "detail" not in response_body


@pytest.mark.asyncio
async def test_template_fastapi_app_registers_run_routes_and_reads_events(tmp_path: Path) -> None:
    # 这里检查真实 FastAPI app include_router 后的路由表，不再只看孤立 APIRouter。
    # event route 通过同一个 LocalJsonlEventSink 读取，证明 API surface 有 stream seam。
    orchestrator, storage, events_path = await build_orchestrator(tmp_path)
    sink = LocalJsonlEventSink(events_path)
    try:
        created = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "api-app"},
            idempotency_key="api-app",
        )
        app = create_app(orchestrator=orchestrator, event_sink=sink)
        paths = app.openapi()["paths"]
        events = await sink.read(run_id=created.run_id, after_seq=0)
    finally:
        await storage.dispose()

    assert "post" in paths["/api/v1/agents/{agent_id}/runs"]
    assert "get" in paths["/api/v1/runs/{run_id}/events"]
    assert [event.event_type.value for event in events] == ["run.started", "run.completed"]


@pytest.mark.asyncio
async def test_runtime_worker_shell_uses_runtime_components(tmp_path: Path) -> None:
    # worker 当前实现不消费真实 Redis queue，但必须共用 API/CLI 的 runtime seam。
    # `run_once` 用临时 profile/DB/events 证明 worker shell 可以创建 fake run。
    db_path = tmp_path / "worker.db"
    events_path = tmp_path / "worker-events.jsonl"
    run_migrations(sqlite_dsn(db_path))

    run_id = await worker_run_once(
        profile="local",
        profiles_dir=ROOT / "templates" / "service-app" / "configs" / "profiles",
        storage_dsn=sqlite_dsn(db_path),
        events_path=events_path,
    )
    events = await LocalJsonlEventSink(events_path).read(run_id=run_id)

    assert [event.event_type.value for event in events] == ["run.started", "run.completed"]


@pytest.mark.asyncio
async def test_template_api_helper_uses_runtime_seam(tmp_path: Path) -> None:
    # API route helper 证明 template app 入口消费 RunOrchestrator，而不是直接碰 ORM
    # session 或 DBOS handle。FastAPI route wiring 只是薄层，核心行为由 helper 锁住。
    orchestrator, storage, _events_path = await build_orchestrator(tmp_path)
    try:
        response = await create_run_for_test(
            RunCreateRequest(agent_id="fake-agent", input={"prompt": "api"}),
            orchestrator=orchestrator,
            request_id="req-helper",
        )
    finally:
        await storage.dispose()

    assert response.request_id == "req-helper"
    assert response.status == RunStatus.COMPLETED
    assert response.terminal_event == "run.completed"


@pytest.mark.asyncio
async def test_template_openapi_and_error_envelope_include_request_id(tmp_path: Path) -> None:
    # OpenAPI 是 P0 管理面。response schema 必须带 request_id，错误也必须
    # 走统一 ApiErrorEnvelope，而不是 FastAPI 默认 {"detail": ...}。
    class MissingRunOrchestrator:
        async def get_run(self, run_id: str) -> object:
            raise LookupError(f"run not found: {run_id}")

    app = create_app(
        orchestrator=cast(RunOrchestrator, MissingRunOrchestrator()),
        event_sink=LocalJsonlEventSink(tmp_path / "unused-events.jsonl"),
    )
    openapi = app.openapi()

    assert "request_id" in openapi["components"]["schemas"]["RunCreateResponse"]["properties"]
    assert "request_id" in openapi["components"]["schemas"]["ErrorDetail"]["properties"]
    not_found_schema = openapi["paths"]["/api/v1/runs/{run_id}"]["get"]["responses"]["404"][
        "content"
    ]["application/json"]["schema"]
    assert not_found_schema["$ref"].endswith("/ApiErrorEnvelope")

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/runs/missing",
        "headers": [(b"x-request-id", b"req-404")],
    }
    handler = cast(Any, app.exception_handlers[LookupError])
    response = await handler(Request(scope), LookupError("run not found: missing"))

    assert cast(int, response.status_code) == 404
    assert cast(dict[str, Any], json.loads(response.body)) == {
        "error": {
            "code": "api.not_found",
            "message": "run not found: missing",
            "request_id": "req-404",
        }
    }

    internal_handler = cast(Any, app.exception_handlers[Exception])
    internal_response = await internal_handler(
        Request({**scope, "headers": [(b"x-request-id", b"req-500")]}),
        RuntimeError("boom"),
    )
    assert cast(int, internal_response.status_code) == 500
    assert cast(dict[str, Any], json.loads(internal_response.body)) == {
        "error": {
            "code": "api.internal_error",
            "message": "internal server error",
            "request_id": "req-500",
        }
    }


@pytest.mark.asyncio
async def test_events_api_filter_hides_internal_evidence_by_default(tmp_path: Path) -> None:
    # reasoning 与 delegation lifecycle 可以进 internal evidence，但普通用户
    # event stream 默认只能看到显式 public 的 run lifecycle event。
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")

    async def resolve_trace(*, tenant_id: str, run_id: str) -> str:
        assert (tenant_id, run_id) == ("default", "run-reasoning")
        return "trace-reasoning"

    bus = EventBus(sink=sink, run_trace_resolver=resolve_trace)
    await bus.publish(
        tenant_id="default",
        run_id="run-reasoning",
        event_type=CanonicalEventType.REASONING_DELTA,
        payload={"text": "hidden reasoning"},
        trace_id="trace-reasoning",
    )
    await bus.publish(
        tenant_id="default",
        run_id="run-reasoning",
        agent_id="agent-source",
        event_type=CanonicalEventType.DELEGATION_CLAIMED,
        event_id="delegation:visibility:claimed",
        payload={
            "delegation_id": "visibility",
            "source_agent_id": "agent-source",
            "target_agent_id": "agent-target",
            "status": "claimed",
        },
        trace_id="trace-reasoning",
    )
    await bus.publish(
        tenant_id="default",
        run_id="run-reasoning",
        event_type=CanonicalEventType.RUN_COMPLETED,
        payload={"status": "completed"},
        terminal=True,
        visibility="public",
        trace_id="trace-reasoning",
    )
    events = await sink.read(run_id="run-reasoning")

    assert [event.event_type.value for event in public_events(events, include_internal=False)] == [
        "run.completed"
    ]
    assert [event.event_type.value for event in public_events(events, include_internal=True)] == [
        "reasoning.delta",
        "delegation.claimed",
        "run.completed",
    ]
