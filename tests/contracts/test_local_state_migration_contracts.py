"""Local state 普通入口门禁与新 sink manifest 合同。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.contracts.local_state_migration_contract_helpers import (
    AGENTS,
    PROFILES,
)
from tests.contracts.local_state_migration_contract_helpers import (
    table_count as _table_count,
)
from tests.contracts.local_state_migration_contract_helpers import (
    trace_a_resolver as _trace_a_resolver,
)
from tests.contracts.local_state_migration_contract_helpers import (
    write_jsonl as _write_jsonl,
)
from typer.testing import CliRunner

from agent_harness.cli import app as core_cli
from agent_harness.evals import ScoreSink
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventType,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
)
from agent_harness.local_state import (
    JOURNAL_NAME,
    MANIFEST_NAME,
    LocalStateMigrationError,
    require_local_state_ready,
)
from agent_harness.storage import EvalScoreCreate, run_migrations
from app.runtime import build_runtime_components


def test_readiness_gate_is_read_only_and_rejects_incomplete_or_legacy_state(
    tmp_path: Path,
) -> None:
    """普通入口只读校验；新空路径可用，非空 legacy 与未完成 journal 拒绝。"""

    missing_state = tmp_path / "missing"
    missing_event = missing_state / "events.jsonl"
    require_local_state_ready(event_paths=(missing_event,), state_dir=missing_state)
    assert not missing_state.exists()

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    empty_score = state_dir / "scores.jsonl"
    empty_score.touch()
    require_local_state_ready(score_paths=(empty_score,), state_dir=state_dir)
    assert not (state_dir / MANIFEST_NAME).exists()

    legacy_event = state_dir / "events.jsonl"
    original = _write_jsonl(legacy_event, [{"run_id": "legacy-run"}])
    with pytest.raises(LocalStateMigrationError) as legacy_error:
        require_local_state_ready(event_paths=(legacy_event,), state_dir=state_dir)
    assert legacy_error.value.code == "local_state.migration_required"
    assert legacy_event.read_bytes() == original

    (state_dir / JOURNAL_NAME).write_text(
        json.dumps({"version": 1, "state": "writing", "files": []}),
        encoding="utf-8",
    )
    with pytest.raises(LocalStateMigrationError) as journal_error:
        require_local_state_ready(score_paths=(empty_score,), state_dir=state_dir)
    assert journal_error.value.code == "local_state.migration_required"
    assert not (state_dir / MANIFEST_NAME).exists()


def test_cli_local_state_gate_precedes_run_eval_and_file_side_effects(tmp_path: Path) -> None:
    """未完成 bundle 在 run/eval 组合前失败，DB、provider、event/score 均保持空。"""

    database = tmp_path / "ordinary.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    state_dir = tmp_path / ".agent-harness"
    state_dir.mkdir()
    events_path = state_dir / "events.jsonl"
    scores_path = state_dir / "eval" / "scores.jsonl"
    (state_dir / JOURNAL_NAME).write_text(
        json.dumps({"version": 1, "state": "writing", "files": []}),
        encoding="utf-8",
    )
    runner = CliRunner()

    run_result = runner.invoke(
        core_cli,
        [
            "run",
            "examples.basic",
            "--profile",
            "local",
            "--profiles-dir",
            str(PROFILES),
            "--agents-dir",
            str(AGENTS),
            "--storage-dsn",
            dsn,
            "--events-path",
            str(events_path),
        ],
    )
    draft_result = runner.invoke(
        core_cli,
        [
            "eval",
            "draft",
            "examples.basic",
            "--profile",
            "local",
            "--profiles-dir",
            str(PROFILES),
            "--storage-dsn",
            dsn,
            "--dataset-dir",
            str(tmp_path / "eval-cases"),
            "--scores-path",
            str(scores_path),
        ],
    )
    eval_result = runner.invoke(
        core_cli,
        [
            "eval",
            "run",
            "--dataset-dir",
            str(tmp_path / "eval-cases"),
            "--scores-path",
            str(scores_path),
        ],
    )

    for result in (run_result, draft_result, eval_result):
        assert result.exit_code == 1
        assert "local_state.migration_required" in result.stderr
    for table in (
        "agent_runs",
        "run_trace_bindings",
        "audit_logs",
        "eval_cases",
        "eval_runs",
        "eval_scores",
        "canonical_events",
    ):
        assert _table_count(database, table) == 0
    assert not events_path.exists()
    assert not scores_path.exists()
    assert not (tmp_path / "eval-cases").exists()
    assert not (state_dir / "artifacts").exists()


def test_approval_list_gate_precedes_access_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """access 命令会构造本地 event sink，因此必须在 list audit 前执行同一门禁。"""

    database = tmp_path / "access.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    state_dir = tmp_path / ".agent-harness"
    state_dir.mkdir()
    (state_dir / JOURNAL_NAME).write_text(
        json.dumps({"version": 1, "state": "writing", "files": []}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        core_cli,
        [
            "approvals",
            "list",
            "missing-run",
            "--profile",
            "local",
            "--profiles-dir",
            str(PROFILES),
            "--storage-dsn",
            dsn,
        ],
    )

    assert result.exit_code == 1
    assert "local_state.migration_required" in result.stderr
    assert _table_count(database, "audit_logs") == 0


def test_runtime_local_gate_precedes_components_but_service_uses_postgresql_sink(
    tmp_path: Path,
) -> None:
    """local runtime 在组件构造前拒绝；service 的 PostgreSQL sink 不读本地 bundle。"""

    local_database = tmp_path / "local.db"
    local_dsn = f"sqlite+aiosqlite:///{local_database}"
    run_migrations(local_dsn)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    events_path = state_dir / "events.jsonl"
    (state_dir / JOURNAL_NAME).write_text(
        json.dumps({"version": 1, "state": "writing", "files": []}),
        encoding="utf-8",
    )

    with pytest.raises(LocalStateMigrationError) as local_error:
        build_runtime_components(
            profile="local",
            profiles_dir=PROFILES,
            storage_dsn=local_dsn,
            events_path=events_path,
            artifact_root=tmp_path / "local-artifacts",
        )
    assert local_error.value.code == "local_state.migration_required"
    assert _table_count(local_database, "agent_runs") == 0
    assert _table_count(local_database, "run_trace_bindings") == 0
    assert _table_count(local_database, "audit_logs") == 0
    assert not events_path.exists()
    assert not (tmp_path / "local-artifacts").exists()

    service_database = tmp_path / "service.db"
    service_dsn = f"sqlite+aiosqlite:///{service_database}"
    run_migrations(service_dsn)
    components = build_runtime_components(
        profile="service",
        profiles_dir=PROFILES,
        storage_dsn=service_dsn,
        events_path=events_path,
        artifact_root=tmp_path / "service-artifacts",
    )
    try:
        assert isinstance(components.event_sink, PostgreSQLEventSink)
    finally:
        import asyncio

        asyncio.run(components.close())


@pytest.mark.asyncio
async def test_new_event_and_score_sinks_register_manifest_before_first_write(
    tmp_path: Path,
) -> None:
    """manifest 保存 canonical path/kind/version/state-dir，且不靠事后目录扫描。"""

    state_dir = tmp_path / ".agent-harness"
    event_path = state_dir / "events.jsonl"
    score_path = state_dir / "eval" / "scores.jsonl"
    await LocalJsonlEventSink(
        event_path,
        state_dir=state_dir,
        run_trace_resolver=_trace_a_resolver,
    ).write(
        CanonicalEvent(
            tenant_id="tenant-a",
            run_id="run-a",
            event_type=CanonicalEventType.RUN_STARTED,
            seq=1,
            trace_id="trace-a",
        )
    )
    await ScoreSink(local_path=score_path, state_dir=state_dir).write_score(
        EvalScoreCreate(
            tenant_id="tenant-a",
            eval_run_id="eval-a",
            case_id="case-a",
            metric="quality",
            value=1.0,
        )
    )

    manifest = json.loads((state_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 1
    expected_files = [
        {
            "format_version": 2,
            "kind": "events",
            "path": str(event_path.resolve()),
            "state_dir": str(state_dir.resolve()),
        },
        {
            "format_version": 2,
            "kind": "scores",
            "path": str(score_path.resolve()),
            "state_dir": str(state_dir.resolve()),
        },
    ]
    assert manifest["files"] == sorted(expected_files, key=lambda item: item["path"])


@pytest.mark.asyncio
async def test_ordinary_sink_refuses_unregistered_legacy_file(tmp_path: Path) -> None:
    """普通写入口不能把既有 legacy JSONL 登记成已迁移的新格式。"""

    state_dir = tmp_path / "state"
    event_path = state_dir / "events.jsonl"
    original = _write_jsonl(event_path, [{"run_id": "legacy-run"}])

    with pytest.raises(LocalStateMigrationError, match="offline migration") as error:
        await LocalJsonlEventSink(
            event_path,
            state_dir=state_dir,
            run_trace_resolver=_trace_a_resolver,
        ).write(
            CanonicalEvent(
                tenant_id="tenant-a",
                run_id="run-a",
                event_type=CanonicalEventType.RUN_STARTED,
                seq=1,
                trace_id="trace-a",
            )
        )

    assert error.value.code == "local_state.migration_required"
    assert event_path.read_bytes() == original


@pytest.mark.asyncio
async def test_ordinary_sink_refuses_incomplete_bundle_journal(tmp_path: Path) -> None:
    """DB/file bundle 未完成时，普通 sink 不得开放新写入。"""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    event_path = state_dir / "events.jsonl"
    (state_dir / JOURNAL_NAME).write_text(
        json.dumps({"version": 1, "state": "database_migrated", "files": []}),
        encoding="utf-8",
    )

    with pytest.raises(LocalStateMigrationError) as error:
        await LocalJsonlEventSink(
            event_path,
            state_dir=state_dir,
            run_trace_resolver=_trace_a_resolver,
        ).write(
            CanonicalEvent(
                tenant_id="tenant-a",
                run_id="run-a",
                event_type=CanonicalEventType.RUN_STARTED,
                seq=1,
                trace_id="trace-a",
            )
        )

    assert error.value.code == "local_state.migration_required"
    assert not event_path.exists()
