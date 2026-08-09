"""Local state profile、CLI、schema 与中断恢复合同。"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest
from click import unstyle
from tests.contracts.local_state_migration_contract_helpers import (
    ROOT,
)
from tests.contracts.local_state_migration_contract_helpers import (
    write_jsonl as _write_jsonl,
)
from typer.testing import CliRunner

from agent_harness import local_state as local_state_module
from agent_harness.cli import app as core_cli
from agent_harness.local_state import (
    JOURNAL_NAME,
    LOCK_NAME,
    LocalStateMigrationError,
    migrate_local_state,
    migrate_profile_local_state,
)
from agent_harness.storage import (
    SchemaMigrationRequiredError,
    get_current_revision,
    require_migration_head,
    run_migrations,
)


def test_profile_mode_rewrites_run_trace_preserves_backup_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    """profile bundle 用 DB canonical trace 重写真实 run evidence，重放不改结果。"""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    event_path = state_dir / "events.jsonl"
    original = _write_jsonl(
        event_path,
        [{"run_id": "run-a", "trace_id": None, "payload": {"value": 1}}],
    )

    first = migrate_local_state(
        state_dir=state_dir,
        event_paths=[event_path],
        file_only=False,
        trace_by_run_id={"run-a": "trace-a"},
    )
    first_bytes = event_path.read_bytes()
    second = migrate_local_state(
        state_dir=state_dir,
        event_paths=[event_path],
        file_only=False,
        trace_by_run_id={"run-a": "trace-a"},
    )

    migrated = json.loads(first_bytes)
    assert migrated["trace_id"] == "trace-a"
    assert migrated["record_scope"] == "run"
    assert event_path.read_bytes() == first_bytes
    assert (state_dir / f"{event_path.name}.pre-0013.bak").read_bytes() == original
    assert first.paths == second.paths == (event_path.resolve(),)


def test_cli_exposes_single_safe_mode_boundary_without_dsn_argument(tmp_path: Path) -> None:
    """CLI 强制 profile/file-only 二选一，且完整 DSN 不允许进入 argv。"""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    event_path = state_dir / "legacy.jsonl"
    _write_jsonl(
        event_path,
        [
            {
                "run_id": "telemetry",
                "payload": {"telemetry": {"context": {"run_id": None}}},
            }
        ],
    )
    runner = CliRunner()
    help_result = runner.invoke(
        core_cli,
        ["migrate-local-state", "--help"],
        env={"COLUMNS": "120"},
        terminal_width=120,
    )
    missing_mode = runner.invoke(
        core_cli,
        ["migrate-local-state", "--state-dir", str(state_dir)],
    )
    conflicting_mode = runner.invoke(
        core_cli,
        [
            "migrate-local-state",
            "--state-dir",
            str(state_dir),
            "--file-only",
            "--profile",
            "local",
        ],
    )
    migrated = runner.invoke(
        core_cli,
        [
            "migrate-local-state",
            "--state-dir",
            str(state_dir),
            "--file-only",
            "--event-path",
            str(event_path),
        ],
    )

    assert help_result.exit_code == 0
    help_text = unstyle(help_result.stdout)
    assert "--event-path" in help_text and "--score-path" in help_text
    assert "--storage-dsn" not in help_text
    assert missing_mode.exit_code == conflicting_mode.exit_code == 1
    assert "local_state.mode_conflict" in missing_mode.stderr
    assert "local_state.mode_conflict" in conflicting_mode.stderr
    assert migrated.exit_code == 0, migrated.stderr
    assert "mode: file-only" in migrated.stdout
    assert str(event_path.resolve()) in migrated.stdout


def test_profile_cli_preflights_all_jsonl_before_database_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法 inventory 必须在关系库 revision 推进之前失败。"""

    database = tmp_path / "state.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn, "0012a_embedding_cache_tenant_scope")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    invalid_events = state_dir / "events.jsonl"
    invalid_events.write_text("{not-json}\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_HARNESS_STORAGE__DSN", dsn)

    result = CliRunner().invoke(
        core_cli,
        [
            "migrate-local-state",
            "--state-dir",
            str(state_dir),
            "--profile",
            "local",
            "--profiles-dir",
            str(ROOT / "templates/service-app/configs/profiles"),
            "--event-path",
            str(invalid_events),
        ],
    )

    assert result.exit_code == 1
    assert "local_state.record_invalid" in result.stderr
    assert get_current_revision(dsn) == "0012a_embedding_cache_tenant_scope"
    assert invalid_events.read_text(encoding="utf-8") == "{not-json}\n"


def test_ordinary_schema_gate_fails_closed_without_advancing_revision(tmp_path: Path) -> None:
    """普通入口共用只读 revision gate，旧 schema 不会被静默升级。"""

    database = tmp_path / "state.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn, "0012a_embedding_cache_tenant_scope")

    with pytest.raises(SchemaMigrationRequiredError):
        require_migration_head(dsn)

    assert get_current_revision(dsn) == "0012a_embedding_cache_tenant_scope"
    ordinary_sources = (
        ROOT / "packages/agent-harness/src/agent_harness/cli.py",
        ROOT / "packages/agent-harness/src/agent_harness/cli_local_state.py",
        ROOT / "packages/agent-harness/src/agent_harness/cli_eval.py",
        ROOT / "packages/agent-harness/src/agent_harness/cli_eval_experiment.py",
        ROOT / "packages/agent-harness/src/agent_harness/cli_access.py",
        ROOT / "packages/agent-harness/src/agent_harness/tools/cli_runtime.py",
        ROOT / "templates/service-app/app/runtime.py",
    )
    for source in ordinary_sources:
        text = source.read_text(encoding="utf-8")
        if source.name == "cli_local_state.py":
            # 数据库推进只允许存在于显式 local-state 迁移命令模块。
            assert text.count("run_migrations(dsn)") == 1
            assert "run_migrations(resolved_dsn)" not in text
        else:
            assert "run_migrations(" not in text


def test_lock_and_incomplete_journal_recover_before_retry(tmp_path: Path) -> None:
    """并发进程被锁拒绝；中断 journal 下次先恢复全旧，再完成确定性重写。"""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    event_path = state_dir / "events.jsonl"
    original = _write_jsonl(
        event_path,
        [
            {
                "run_id": "telemetry",
                "payload": {"telemetry": {"context": {"run_id": None}}},
            }
        ],
    )
    lock_path = state_dir / LOCK_NAME
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(LocalStateMigrationError) as error:
            migrate_local_state(
                state_dir=state_dir,
                event_paths=[event_path],
                file_only=True,
            )
        assert error.value.code == "local_state.locked"
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    rollback = state_dir / f".{event_path.name}.0013.rollback"
    rollback.write_bytes(original)
    event_path.write_text('{"partial":true}\n', encoding="utf-8")
    (state_dir / JOURNAL_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "state": "writing",
                "mode": "file-only",
                "files": [
                    {
                        "path": str(event_path),
                        "backup": str(state_dir / f"{event_path.name}.pre-0013.bak"),
                        "rollback": str(rollback),
                        "kind": "events",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    migrate_local_state(
        state_dir=state_dir,
        event_paths=[event_path],
        file_only=True,
    )
    record = json.loads(event_path.read_text(encoding="utf-8"))
    assert record["record_scope"] == "non_run"
    assert (state_dir / f"{event_path.name}.pre-0013.bak").read_bytes() == original


def test_profile_bundle_recovers_after_database_upgrade_and_file_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 已升级但文件切换失败时保留 journal；重试幂等继续到全新。"""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    event_path = state_dir / "events.jsonl"
    original = _write_jsonl(
        event_path,
        [{"run_id": "run-a", "trace_id": None, "payload": {"value": 1}}],
    )
    database = {"revision": "0012a_embedding_cache_tenant_scope"}

    def upgrade_database() -> dict[str, str]:
        """模拟配置 profile 对数据库的成功升级，并返回 run 到 canonical trace 的映射。"""

        database["revision"] = "0013a_run_trace_event_hardening"
        return {"run-a": "trace-a"}

    original_replace = local_state_module._atomic_replace_jsonl  # pyright: ignore[reportPrivateUsage]

    def fail_file_replace(*_args: object, **_kwargs: object) -> None:
        """在文件原子切换处注入一次中断，验证 journal 能保留可恢复状态。"""

        raise OSError("simulated interruption")

    monkeypatch.setattr(local_state_module, "_atomic_replace_jsonl", fail_file_replace)
    with pytest.raises(LocalStateMigrationError) as error:
        migrate_profile_local_state(
            state_dir=state_dir,
            event_paths=[event_path],
            known_run_ids={"run-a"},
            database_upgrade=upgrade_database,
        )

    assert error.value.code == "local_state.migration_failed"
    assert database["revision"] == "0013a_run_trace_event_hardening"
    assert event_path.read_bytes() == original
    assert json.loads((state_dir / JOURNAL_NAME).read_text(encoding="utf-8"))["state"] == (
        "failed_files_restored"
    )

    monkeypatch.setattr(local_state_module, "_atomic_replace_jsonl", original_replace)
    result = migrate_profile_local_state(
        state_dir=state_dir,
        event_paths=[event_path],
        known_run_ids={"run-a"},
        database_upgrade=upgrade_database,
    )

    assert result.mode == "profile"
    assert json.loads(event_path.read_text(encoding="utf-8"))["trace_id"] == "trace-a"
    assert json.loads((state_dir / JOURNAL_NAME).read_text(encoding="utf-8"))["state"] == (
        "completed"
    )
