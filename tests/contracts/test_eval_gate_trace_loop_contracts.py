"""Phase 11 Eval Gate 与 trace/eval 闭环合同测试。

这些用例只穿过公开 seam：OpenSpec artifact、`agent_harness.evals`
DTO/service、Repository/UoW、ScoreSink、template API 和 CLI/Makefile 入口。
它们刻意避免直接操作 SQLAlchemy session，防止 eval runner 绕过 storage 边界。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import (
    ROOT,
    asgi_request,
    descriptor,
    sqlite_dsn,
    table_count,
    table_json_payloads,
)

from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.observability import (
    ProviderTelemetryAdapter,
    TelemetryStatus,
)
from agent_harness.policy import PolicyCheck, PolicyEngine, PolicyEvaluation
from agent_harness.registry import AgentRegistry
from agent_harness.storage import SQLAlchemyStorage, run_migrations

CHANGE = ROOT / "openspec" / "changes" / "eval-gate-trace-loop"
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


class FailingScoreProvider(ProviderTelemetryAdapter):
    """ScoreSink provider failure fixture，验证 local evidence 不被外部错误拖垮。"""

    provider_name = "score-provider"

    async def send(self, record: Any) -> TelemetryStatus:
        raise RuntimeError(
            "provider failed Authorization: Bearer score-secret-12345; "
            "Cookie: sessionid=score-cookie-12345"
        )


class RecordingEvalPolicyProvider:
    """记录 EVL-002 policy check，并按测试指定决策返回。"""

    def __init__(self, decision: str = "allow") -> None:
        self.decision = decision
        self.checks: list[PolicyCheck] = []

    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        self.checks.append(check)
        return PolicyEvaluation(
            decision=self.decision,
            reason=f"eval approve {self.decision}",
            actor=check.actor,
            action=check.action,
            resource=check.resource,
            metadata={"context": check.context},
        )


def test_openspec_declares_eval_gate_trace_loop_scope() -> None:
    """Phase 11 change 必须覆盖 eval 闭环，且明确不自动 archive。"""

    proposal = (CHANGE / "proposal.md").read_text(encoding="utf-8")
    spec = (CHANGE / "specs" / "eval-gate-trace-loop" / "spec.md").read_text(encoding="utf-8")
    tasks = (CHANGE / "tasks.md").read_text(encoding="utf-8")

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
        assert marker in proposal or marker in spec or marker in tasks
    assert "不执行 `openspec archive`" in proposal
    assert "openspec validate eval-gate-trace-loop --type change --strict" in tasks


def test_api_contract_documents_eval_gate_endpoints() -> None:
    """EVL-* 不能停在保留索引，必须成为正式 API 契约。"""

    contract = (ROOT / "API-Contract.md").read_text(encoding="utf-8")

    assert "### EVL-001 draft eval case" in contract
    assert "### EVL-002 approve eval case" in contract
    assert "### EVL-003 run eval and read scores" in contract
    assert "POST /api/v1/eval-cases/drafts" in contract
    assert "POST /api/v1/eval-cases/{case_id}/approve" in contract
    assert "POST /api/v1/evals/runs" in contract
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
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id="run-policy-deny",
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
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id="run-api-approve",
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
                local_sink=LocalJsonlEventSink(tmp_path / "telemetry.jsonl"),
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
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id="run-api-eval",
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


def test_local_migration_creates_eval_gate_schema(tmp_path: Path) -> None:
    """Phase 11 migration 必须补齐 eval_scores 和 eval 关联字段。"""

    db_path = tmp_path / "eval-schema.db"
    run_migrations(sqlite_dsn(db_path))

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        eval_case_columns = {
            row[1] for row in connection.execute("pragma table_info(eval_cases)").fetchall()
        }
        eval_run_columns = {
            row[1] for row in connection.execute("pragma table_info(eval_runs)").fetchall()
        }
        eval_score_columns = {
            row[1] for row in connection.execute("pragma table_info(eval_scores)").fetchall()
        }
        revision = connection.execute("select version_num from alembic_version").fetchone()

    assert "eval_scores" in tables
    assert revision == ("0007_eval_gate_trace_loop",)
    assert {
        "agent_id",
        "run_id",
        "trace_id",
        "trigger",
        "dataset",
        "source_refs_json",
        "artifact_refs_json",
        "approved_by",
        "approved_at",
    } <= eval_case_columns
    assert {"agent_id", "dataset", "case_count", "score_summary_json"} <= eval_run_columns
    assert {
        "eval_run_id",
        "case_id",
        "agent_id",
        "run_id",
        "trace_id",
        "metric",
        "value",
        "provider_status_json",
    } <= eval_score_columns


@pytest.mark.asyncio
async def test_eval_service_drafts_approves_runs_and_scores_without_secret_leaks(
    tmp_path: Path,
) -> None:
    """核心闭环：failed trace -> draft -> manual approve -> runner -> local/provider score。"""

    from agent_harness.evals import (
        EvalCaseFactory,
        EvalRunner,
        EvalService,
        EvalTraceSource,
        ScoreSink,
    )
    from agent_harness.observability import TelemetryFacade

    db_path = tmp_path / "eval-service.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    local_scores = tmp_path / "scores.jsonl"
    telemetry_events = tmp_path / "telemetry.jsonl"
    score_sink = ScoreSink(
        local_path=local_scores,
        telemetry=TelemetryFacade(
            local_sink=LocalJsonlEventSink(telemetry_events),
            providers=[FailingScoreProvider()],
        ),
    )
    service = EvalService(
        storage=storage,
        factory=EvalCaseFactory(inline_payload_bytes=128),
        score_sink=score_sink,
        drafts_dir=tmp_path / "eval-cases" / "drafts",
        approved_dir=tmp_path / "eval-cases" / "approved",
    )
    reviewer = IdentityContext(
        tenant_id="default",
        user_id="reviewer-1",
        session_id="review-session",
        roles=["developer"],
        permissions=["eval.approve", "eval.run"],
    )

    try:
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id="run-1",
                trace_id="trace-1",
                trigger="failed_run",
                input={"prompt": "api_key=eval-secret-12345"},
                output={"answer": "wrong"},
                expected={"answer": "correct"},
                source_refs=["event://run-1/3"],
                artifact_refs=["artifact://trace-input"],
            )
        )
        approved_result = await service.approve_case(
            actor=reviewer,
            case_id=draft.case_id,
            reason="failed run regression",
        )
        approved = approved_result.case
        result = await EvalRunner(service=service, score_sink=score_sink).run_approved(
            tenant_id="default",
            agent_id="examples.basic",
            dataset="default",
        )
    finally:
        await storage.dispose()

    assert draft.status == "draft"
    assert approved.status == "approved"
    assert approved.approved_by == "reviewer-1"
    assert approved_result.audit_ref
    assert result.status == "completed"
    assert result.case_count == 1
    assert result.score_summary["case_count"] == 1
    assert result.local_refs == [str(local_scores)]
    assert result.provider_statuses[0].status == "degraded"
    assert not (tmp_path / "eval-cases" / "drafts" / f"{draft.case_id}.json").exists()
    assert (tmp_path / "eval-cases" / "approved" / f"{draft.case_id}.json").exists()
    serialized = json.dumps(
        [
            draft.to_payload(),
            approved.to_payload(),
            result.to_payload(),
            local_scores.read_text(encoding="utf-8"),
            telemetry_events.read_text(encoding="utf-8"),
            table_json_payloads(db_path, "audit_logs"),
        ],
        ensure_ascii=False,
    )
    assert "eval-secret-12345" not in serialized
    assert "score-secret-12345" not in serialized
    assert "score-cookie-12345" not in serialized
    assert table_count(db_path, "eval_cases") == 1
    assert table_count(db_path, "eval_runs") == 1
    assert table_count(db_path, "eval_scores") == 1
    assert table_count(db_path, "audit_logs") == 1
    with sqlite3.connect(db_path) as connection:
        audit_actions = [
            row[0] for row in connection.execute("select action from audit_logs").fetchall()
        ]
    assert audit_actions == ["eval.case.approved"]


@pytest.mark.asyncio
async def test_eval_approve_dataset_failure_keeps_draft_reviewable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """approved dataset 写入失败时，DB/audit 不得提交，draft 文件仍可审。"""

    from agent_harness.evals import EvalCaseFactory, EvalService, EvalTraceSource, ScoreSink

    db_path = tmp_path / "eval-approve-rollback.db"
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
    reviewer = IdentityContext(
        tenant_id="default",
        user_id="reviewer-1",
        session_id="review-session",
        roles=["developer"],
        permissions=["eval.approve"],
    )

    try:
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id="run-rollback",
                trace_id="trace-rollback",
                trigger="failed_run",
                input={"prompt": "hello"},
            )
        )

        def fail_write_approved(_case: object) -> Path:
            raise OSError("approved dataset unavailable")

        monkeypatch.setattr(service.dataset, "write_approved", fail_write_approved)

        with pytest.raises(OSError, match="approved dataset unavailable"):
            await service.approve_case(
                actor=reviewer,
                case_id=draft.case_id,
                reason="must stay draft",
            )
    finally:
        await storage.dispose()

    assert (tmp_path / "eval-cases" / "drafts" / f"{draft.case_id}.json").exists()
    assert not (tmp_path / "eval-cases" / "approved" / f"{draft.case_id}.json").exists()
    assert table_count(db_path, "audit_logs") == 0
    with sqlite3.connect(db_path) as connection:
        status = connection.execute(
            "select status from eval_cases where id = ?",
            (draft.case_id,),
        ).fetchone()
    assert status == ("draft",)


def test_eval_cli_draft_and_approve_use_storage_and_audit(tmp_path: Path) -> None:
    """CLI approve 也必须走 repository/audit，并把 draft 移出 review queue。"""

    db_path = tmp_path / "eval-cli.db"
    dsn = sqlite_dsn(db_path)
    dataset_dir = tmp_path / "eval-cases"
    scores_path = tmp_path / "scores.jsonl"

    draft = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "eval",
            "draft",
            "examples.basic",
            "--dataset-dir",
            str(dataset_dir),
            "--storage-dsn",
            dsn,
            "--scores-path",
            str(scores_path),
            "--run-id",
            "run-cli",
            "--trace-id",
            "trace-cli",
            "--prompt",
            "hello",
            "--score",
            "exact_match=0.2",
            "--score-threshold",
            "0.8",
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert draft.returncode == 0, draft.stderr
    case_id = next(
        line.removeprefix("case_id: ").strip()
        for line in draft.stdout.splitlines()
        if line.startswith("case_id: ")
    )
    assert (dataset_dir / "drafts" / f"{case_id}.json").exists()
    draft_payload = json.loads(
        (dataset_dir / "drafts" / f"{case_id}.json").read_text(encoding="utf-8")
    )
    assert draft_payload["trigger"] == "low_score"
    assert draft_payload["metadata"]["score_signal"]["low_scores"] == {"exact_match": 0.2}

    approved = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "eval",
            "approve",
            case_id,
            "--dataset-dir",
            str(dataset_dir),
            "--storage-dsn",
            dsn,
            "--scores-path",
            str(scores_path),
            "--reviewer",
            "cli-reviewer",
            "--reason",
            "covered by CLI review",
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert approved.returncode == 0, approved.stderr
    assert "status: approved" in approved.stdout
    assert not (dataset_dir / "drafts" / f"{case_id}.json").exists()
    assert (dataset_dir / "approved" / f"{case_id}.json").exists()
    with sqlite3.connect(db_path) as connection:
        case_status = connection.execute(
            "select status, approved_by from eval_cases where id = ?",
            (case_id,),
        ).fetchone()
        audit_action = connection.execute("select action from audit_logs").fetchone()
    assert case_status == ("approved", "cli-reviewer")
    assert audit_action == ("eval.case.approved",)


def test_make_eval_runs_cli_against_approved_cases_only(tmp_path: Path) -> None:
    """`make eval` 的真实命令路径必须只消费 approved dataset。"""

    service_root = tmp_path / "service-app"
    drafts = service_root / "eval-cases" / "drafts"
    approved = service_root / "eval-cases" / "approved"
    drafts.mkdir(parents=True)
    approved.mkdir(parents=True)
    (drafts / "draft.json").write_text(
        json.dumps(
            {
                "case_id": "draft-case",
                "tenant_id": "default",
                "agent_id": "examples.basic",
                "status": "draft",
                "input": {"prompt": "draft should not run"},
                "expected": {"answer": "draft"},
            }
        ),
        encoding="utf-8",
    )
    (approved / "approved.json").write_text(
        json.dumps(
            {
                "case_id": "approved-case",
                "tenant_id": "default",
                "agent_id": "examples.basic",
                "status": "approved",
                "input": {"prompt": "hello"},
                "expected": {"answer": "hello"},
                "output": {"answer": "hello"},
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "eval",
            "run",
            "--dataset-dir",
            str(service_root / "eval-cases"),
            "--scores-path",
            str(tmp_path / "scores.jsonl"),
            "--agent-id",
            "examples.basic",
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert "case_count: 1" in result.stdout
    assert "skipped_drafts: 1" in result.stdout
    assert "approved-case" in (tmp_path / "scores.jsonl").read_text(encoding="utf-8")
    assert "draft-case" not in (tmp_path / "scores.jsonl").read_text(encoding="utf-8")
