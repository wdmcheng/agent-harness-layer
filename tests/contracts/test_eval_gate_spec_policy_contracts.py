"""Eval gate 规格、OpenAPI 与策略拒绝合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_gate_trace_loop_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    Any as Any,
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
    pytest as pytest,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    seed_persisted_run as seed_persisted_run,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    sqlite3 as sqlite3,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_eval_gate_trace_loop_contracts import (
    table_count as table_count,
)


def test_main_spec_declares_eval_gate_trace_loop_capability() -> None:
    """长期合同只锁主规格中的产品能力，不依赖 change 生命周期状态。"""

    spec = (ROOT / "openspec" / "specs" / "eval-gate-trace-loop" / "spec.md").read_text(
        encoding="utf-8"
    )

    for marker in [
        "EvalCaseFactory",
        "failed/low-score detector",
        "ReviewDatasetAdapter",
        "EvalRunner",
        "ScoreSink",
        "EVL-001",
        "EVL-002",
        "EVL-003",
        "provider failure",
        "secret redaction",
    ]:
        assert marker in spec


def test_api_contract_documents_eval_gate_endpoints() -> None:
    """EVL-* 不能停在保留索引，必须成为正式 API 契约。"""

    contract = (ROOT / "API-Contract.md").read_text(encoding="utf-8")

    assert "### EVL-001 draft eval case" in contract
    assert "### EVL-002 approve eval case" in contract
    assert "### EVL-003 run eval and read scores" in contract
    # API 契约已把复合 Method/Path 行扩成第 3 节字段表；这里锁稳定路径，
    # method 由 service-app 全量 OpenAPI operation 矩阵逐项验证。
    assert "| 路径 | `/api/v1/eval-cases/drafts` |" in contract
    assert "| 路径 | `/api/v1/eval-cases/{case_id}/approve` |" in contract
    assert "| 路径 | `/api/v1/evals/runs` |" in contract
    assert "| `EVL-001` | 规划中 | Eval Gate |" not in contract
    assert (
        "Secret 不得进入 API body、error envelope、event payload、trace、eval case、"
        "audit log 或 local/jsonl" in contract
    )


def test_openapi_exposes_eval_gate_paths_and_error_envelopes(tmp_path: Path) -> None:
    """局部 OpenAPI drift test 锁住 EVL 路由、安全方案和错误 envelope。"""

    from agent_harness.auth import StaticTokenVerifier
    from app.main import create_app

    app = create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier({"valid-token": IdentityContext.local_default()}),
        eval_service=cast(Any, object()),
    )
    openapi = app.openapi()
    paths = openapi["paths"]

    for path, method in [
        ("/api/v1/eval-cases/drafts", "post"),
        ("/api/v1/eval-cases/drafts", "get"),
        ("/api/v1/eval-cases/{case_id}/approve", "post"),
        ("/api/v1/eval-cases/approved", "get"),
        ("/api/v1/evals/runs", "post"),
        ("/api/v1/evals/runs/{eval_run_id}", "get"),
        ("/api/v1/evals/runs/{eval_run_id}/scores", "get"),
    ]:
        operation = paths[path][method]
        assert {"HTTPBearer": []} in operation.get("security", [])
        for status in ("401", "403", "500"):
            schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("/ApiErrorEnvelope")
        if method == "post":
            validation_schema = operation["responses"]["422"]["content"]["application/json"][
                "schema"
            ]
            assert validation_schema["$ref"].endswith("/ApiErrorEnvelope")
    run_response = openapi["components"]["schemas"]["EvalRunResponse"]
    assert "local_refs" in run_response["properties"]


def test_low_score_detector_generates_draft_case_with_score_metadata() -> None:
    """低分 detector 必须把 score signal 转成 low_score draft，不自动 approve。"""

    from agent_harness.evals import EvalDraftDetector, EvalTraceSource

    detector = EvalDraftDetector()
    draft = detector.detect(
        EvalTraceSource(
            tenant_id="default",
            agent_id="examples.basic",
            run_id="run-low-score",
            trace_id="trace-low-score",
            trigger="score_signal",
            input={"prompt": "answer"},
            output={"answer": "bad"},
            expected={"answer": "good"},
            scores={"exact_match": 0.0, "faithfulness": 0.42},
        ),
        score_threshold=0.7,
    )
    ignored = detector.detect(
        EvalTraceSource(
            tenant_id="default",
            agent_id="examples.basic",
            run_id="run-ok-score",
            trace_id="trace-ok-score",
            trigger="score_signal",
            scores={"faithfulness": 0.95},
        ),
        score_threshold=0.7,
    )

    assert draft is not None
    assert draft.trigger == "low_score"
    assert draft.payload["trigger"] == "low_score"
    assert draft.payload["scores"] == {"exact_match": 0.0, "faithfulness": 0.42}
    assert draft.metadata["trace"]["trigger"] == "low_score"
    assert draft.metadata["score_signal"]["threshold"] == 0.7
    assert draft.metadata["score_signal"]["low_scores"] == {
        "exact_match": 0.0,
        "faithfulness": 0.42,
    }
    assert ignored is None


@pytest.mark.asyncio
async def test_invalid_token_cannot_create_eval_side_effects(tmp_path: Path) -> None:
    """认证失败要在 route 业务执行前返回，不能创建 case/run/score/audit。"""

    from agent_harness.auth import StaticTokenVerifier
    from app.main import create_app

    db_path = tmp_path / "eval-api.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    app = create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier({"valid-token": IdentityContext.local_default()}),
        eval_service=cast(Any, object()),
    )

    try:
        status, body = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/eval-cases/drafts",
            body={
                "agent_id": "examples.basic",
                "run_id": "run-1",
                "trace_id": "trace-1",
                "trigger": "failed_run",
                "input": {"prompt": "hello"},
            },
            headers=[(b"authorization", b"Bearer invalid-token")],
        )
    finally:
        await storage.dispose()

    assert status == 401
    assert body["error"]["code"] == "auth.invalid_token"
    assert table_count(db_path, "eval_cases") == 0
    assert table_count(db_path, "eval_runs") == 0
    assert table_count(db_path, "eval_scores") == 0
    assert table_count(db_path, "audit_logs") == 0


@pytest.mark.asyncio
async def test_eval_approve_api_policy_denied_has_no_side_effects(tmp_path: Path) -> None:
    """EVL-002 必须先过 policy seam；deny 时不得写 approved/audit。"""

    from agent_harness.auth import StaticTokenVerifier
    from agent_harness.evals import EvalCaseFactory, EvalService, EvalTraceSource, ScoreSink
    from app.main import create_app

    db_path = tmp_path / "eval-policy-deny.db"
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
    policy_provider = RecordingEvalPolicyProvider(decision="deny")
    app = create_app(
        orchestrator=cast(Any, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=AgentRegistry([descriptor()]),
        auth_verifier=StaticTokenVerifier({"valid-token": IdentityContext.local_default()}),
        policy_engine=PolicyEngine(provider=policy_provider),
        eval_service=service,
    )

    try:
        source_run_id = await seed_persisted_run(storage, trace_id="trace-policy-deny")
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id=source_run_id,
                trace_id="trace-policy-deny",
                trigger="failed_run",
                input={"prompt": "hello"},
            )
        )
        status, body = await asgi_request(
            cast(Any, app),
            method="POST",
            path=f"/api/v1/eval-cases/{draft.case_id}/approve",
            body={"reason": "should be denied"},
            headers=[(b"authorization", b"Bearer valid-token")],
        )
    finally:
        await storage.dispose()

    assert status == 403
    assert body["error"]["code"] == "policy.denied"
    assert policy_provider.checks[0].action == "eval.case.approve"
    assert policy_provider.checks[0].resource == f"eval_case:{draft.case_id}"
    assert (tmp_path / "eval-cases" / "drafts" / f"{draft.case_id}.json").exists()
    assert not (tmp_path / "eval-cases" / "approved" / f"{draft.case_id}.json").exists()
    assert table_count(db_path, "audit_logs") == 0
    with sqlite3.connect(db_path) as connection:
        status_row = connection.execute(
            "select status from eval_cases where id = ?",
            (draft.case_id,),
        ).fetchone()
    assert status_row == ("draft",)
