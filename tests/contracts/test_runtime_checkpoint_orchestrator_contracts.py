"""Runtime checkpoint、orchestrator 与公开 run 合同测试。"""

from __future__ import annotations

from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    RUN_OPENAPI_CONTRACTS as RUN_OPENAPI_CONTRACTS,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    Any as Any,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    APIRoute as APIRoute,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    ApprovalWaitState as ApprovalWaitState,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    CheckpointStore as CheckpointStore,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    FastAPI as FastAPI,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    IdempotencyKey as IdempotencyKey,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    InvalidRunTransition as InvalidRunTransition,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    Path as Path,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    ResumeToken as ResumeToken,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    RunStatus as RunStatus,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
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
    json as json,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    pytest as pytest,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    router as router,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    subprocess as subprocess,
)
from tests.contracts.test_runtime_checkpoint_runs_contracts import (
    sys as sys,
)


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
        ("/api/v1/runs/{run_id}/events/stream", "GET"),
        ("/api/v1/runs/{run_id}/cancel", "POST"),
        ("/api/v1/runs/{run_id}/resume", "POST"),
    } <= route_methods
    assert ("/api/v1/runs", "POST") not in route_methods


def test_run_openapi_response_status_and_schema_are_exact(tmp_path: Path) -> None:
    """最终 OpenAPI 必须逐 operation 精确对齐 RUN-001 至 RUN-006。"""

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

    stream_operation = paths["/api/v1/runs/{run_id}/events/stream"]["get"]
    stream_responses = stream_operation["responses"]
    assert set(stream_responses) == {"200", "401", "403", "404", "422", "500"}
    assert set(stream_responses["200"]["content"]) == {"text/event-stream"}
    assert stream_responses["200"]["content"]["text/event-stream"]["schema"] == {"type": "string"}
    for status in {"401", "403", "404", "422", "500"}:
        assert (
            stream_responses[status]["content"]["application/json"]["schema"]["$ref"]
            == "#/components/schemas/ApiErrorEnvelope"
        )


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
