"""Eval Gate migration、service、dataset 与 CLI 闭环合同测试。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import (
    ROOT,
    sqlite_dsn,
    table_count,
    table_json_payloads,
)

from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.observability import ProviderTelemetryAdapter, TelemetryStatus
from agent_harness.storage import SQLAlchemyStorage, run_migrations


class FailingScoreProvider(ProviderTelemetryAdapter):
    """外部 score provider 失败 fixture，验证本地 evidence 不被拖垮。"""

    provider_name = "score-provider"

    async def send(self, record: Any) -> TelemetryStatus:
        del record
        raise RuntimeError(
            "provider failed Authorization: Bearer score-secret-12345; "
            "Cookie: sessionid=score-cookie-12345"
        )


def test_local_migration_creates_eval_gate_schema(tmp_path: Path) -> None:
    """当前 migration 必须补齐 eval_scores 和 eval 关联字段。"""

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
    assert revision == ("0011_eval_experiment_legacy_created_review",)
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
