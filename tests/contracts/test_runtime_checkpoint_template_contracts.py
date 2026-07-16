"""Service 模板 run 路由、worker 与事件读取合同测试。"""

from __future__ import annotations

from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    Any as Any,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    CanonicalEventType as CanonicalEventType,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    Path as Path,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    Request as Request,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    RunCreateRequest as RunCreateRequest,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    RunStatus as RunStatus,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    asgi_request as asgi_request,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    build_orchestrator as build_orchestrator,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    cast as cast,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    create_app as create_app,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    create_run_for_test as create_run_for_test,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    json as json,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    public_events as public_events,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    pytest as pytest,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    worker_run_once as worker_run_once,
)


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
    # OpenAPI 是公开管理面。response schema 必须带 request_id，错误也必须
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
