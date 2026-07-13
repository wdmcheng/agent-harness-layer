"""Eval Gate 与 trace/eval 闭环的 API、策略和 provider 合同测试。

这些用例只穿过公开 seam：OpenSpec artifact、`agent_harness.evals`
DTO/service、Repository/UoW、ScoreSink、template API 和 CLI/Makefile 入口。
它们刻意避免直接操作 SQLAlchemy session，防止 eval runner 绕过 storage 边界。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import (
    ROOT,
    asgi_request,
    descriptor,
    sqlite_dsn,
    table_count,
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
