"""Eval gate 审计引用与 provider 降级合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_gate_trace_loop_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    Any as Any,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    FailingScoreProvider as FailingScoreProvider,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    IdentityContext as IdentityContext,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    Path as Path,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    PolicyEngine as PolicyEngine,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    RecordingEvalPolicyProvider as RecordingEvalPolicyProvider,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    asgi_request as asgi_request,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    cast as cast,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    descriptor as descriptor,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    json as json,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    pytest as pytest,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    seed_persisted_run as seed_persisted_run,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    sqlite_dsn as sqlite_dsn,
)


@pytest.mark.asyncio
async def test_eval_approve_api_returns_audit_ref(tmp_path: Path) -> None:
    """EVL-002 approve response 要暴露 audit evidence ref 摘要。"""

    from agent_harness.auth import StaticTokenVerifier
    from agent_harness.evals import EvalCaseFactory, EvalService, EvalTraceSource, ScoreSink
    from app.main import create_app

    db_path = tmp_path / "eval-api-approve.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    service = EvalService(
        storage=storage,
        factory=EvalCaseFactory(),
        score_sink=ScoreSink(local_path=tmp_path / "scores.jsonl"),
        drafts_dir=tmp_path / "eval-cases" / "drafts",
        approved_dir=tmp_path / "eval-cases" / "approved",
    )
    policy_provider = RecordingEvalPolicyProvider()
    app = create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier({"valid-token": IdentityContext.local_default()}),
        policy_engine=PolicyEngine(provider=policy_provider),
        eval_service=service,
    )

    try:
        source_run_id = await seed_persisted_run(storage, trace_id="trace-api-approve")
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id=source_run_id,
                trace_id="trace-api-approve",
                trigger="failed_run",
                input={"prompt": "hello"},
                source_refs=["event://run-api-approve/1"],
                artifact_refs=["artifact://trace-input"],
            )
        )
        status, body = await asgi_request(
            cast(Any, app),
            method="POST",
            path=f"/api/v1/eval-cases/{draft.case_id}/approve",
            body={"reason": "human reviewed", "dataset": "regression"},
            headers=[(b"authorization", b"Bearer valid-token")],
        )
    finally:
        await storage.dispose()

    assert status == 200
    assert body["case"]["status"] == "approved"
    assert body["case"]["approved_by"] == "local-user"
    assert body["case"]["review_reason"] == "human reviewed"
    assert body["case"]["dataset"] == "regression"
    assert body["case"]["source_refs"] == ["event://run-api-approve/1"]
    assert body["case"]["artifact_refs"] == ["artifact://trace-input"]
    assert body["audit_ref"]
    assert policy_provider.checks[0].action == "eval.case.approve"


@pytest.mark.asyncio
async def test_eval_run_api_returns_local_refs_and_degraded_provider_status(
    tmp_path: Path,
) -> None:
    """EVL-003 API route 要真实返回 local evidence refs 和 provider degraded status。"""

    from agent_harness.auth import StaticTokenVerifier
    from agent_harness.evals import EvalCaseFactory, EvalService, EvalTraceSource, ScoreSink
    from agent_harness.observability import TelemetryFacade
    from agent_harness.storage.run_trace_gate import StorageRunTraceResolver
    from app.main import create_app

    db_path = tmp_path / "eval-api-run.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    local_scores = tmp_path / "scores.jsonl"
    service = EvalService(
        storage=storage,
        factory=EvalCaseFactory(),
        score_sink=ScoreSink(
            local_path=local_scores,
            telemetry=TelemetryFacade(
                local_sink=LocalJsonlEventSink(
                    tmp_path / "telemetry.jsonl",
                    run_trace_resolver=StorageRunTraceResolver(storage),
                ),
                providers=[FailingScoreProvider()],
            ),
        ),
        drafts_dir=tmp_path / "eval-cases" / "drafts",
        approved_dir=tmp_path / "eval-cases" / "approved",
    )
    app = create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier({"valid-token": IdentityContext.local_default()}),
        eval_service=service,
    )
    reviewer = IdentityContext.local_default()

    try:
        source_run_id = await seed_persisted_run(storage, trace_id="trace-api-eval")
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id=source_run_id,
                trace_id="trace-api-eval",
                trigger="failed_run",
                input={"prompt": "hello"},
                output={"answer": "wrong"},
                expected={"answer": "correct"},
            )
        )
        await service.approve_case(
            actor=reviewer,
            case_id=draft.case_id,
            reason="route response coverage",
        )
        status, body = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/evals/runs",
            body={"agent_id": "examples.basic", "dataset": "default"},
            headers=[(b"authorization", b"Bearer valid-token")],
        )
    finally:
        await storage.dispose()

    assert status == 200
    assert body["status"] == "completed"
    assert body["case_count"] == 1
    assert body["local_refs"] == [str(local_scores)]
    assert body["provider_statuses"][0]["provider"] == "score-provider"
    assert body["provider_statuses"][0]["status"] == "degraded"
    assert "score-secret-12345" not in json.dumps(body, ensure_ascii=False)
