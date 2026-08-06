"""Eval gate schema、service 状态机与原子失败合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_gate_storage_cli_contracts import (
    FailingScoreProvider as FailingScoreProvider,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    IdentityContext as IdentityContext,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    Path as Path,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    StorageRunTraceResolver as StorageRunTraceResolver,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    json as json,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    pytest as pytest,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    seed_persisted_run as seed_persisted_run,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    sqlite3 as sqlite3,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    table_count as table_count,
)
from tests.contracts.test_eval_gate_storage_cli_contracts import (
    table_json_payloads as table_json_payloads,
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
    assert revision == ("0018_model_tool_loop_state",)
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
            local_sink=LocalJsonlEventSink(
                telemetry_events,
                run_trace_resolver=StorageRunTraceResolver(storage),
            ),
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
        source_run_id = await seed_persisted_run(storage, trace_id="trace-1")
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id=source_run_id,
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
        source_run_id = await seed_persisted_run(storage, trace_id="trace-rollback")
        draft = await service.draft_from_trace(
            EvalTraceSource(
                tenant_id="default",
                agent_id="examples.basic",
                run_id=source_run_id,
                trace_id="trace-rollback",
                trigger="failed_run",
                input={"prompt": "hello"},
            )
        )

        def fail_write_approved(_case: object) -> Path:
            """模拟批准数据集落盘失败，检验事务不会先于文件提交。"""

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
