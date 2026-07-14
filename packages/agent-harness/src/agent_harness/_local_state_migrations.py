"""Local-state file-only 与 profile 离线迁移编排。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence, Set
from pathlib import Path
from typing import Any

from agent_harness._local_state_common import (
    JOURNAL_NAME,
    LOCK_NAME,
    LocalStateMigrationError,
    LocalStateMigrationResult,
    atomic_write_json,
    backup_path,
    copy_bytes_atomic,
    recover_incomplete_journal,
    restore_from_journal,
    rollback_path,
    state_lock,
)
from agent_harness._local_state_manifest import inventory, register_local_state_file
from agent_harness._local_state_records import preflight_file

AtomicJsonlReplace = Callable[[Path, Sequence[Mapping[str, Any]]], None]


def migrate_local_state(
    *,
    state_dir: Path,
    event_paths: Sequence[Path] = (),
    score_paths: Sequence[Path] = (),
    file_only: bool,
    trace_by_run_id: Mapping[str, str] | None = None,
    atomic_replace_jsonl: AtomicJsonlReplace,
) -> LocalStateMigrationResult:
    """在单一 state-dir 锁内预检 inventory，并以 backup+journal 原子迁移。"""

    canonical_state_dir = state_dir.expanduser().resolve()
    if not canonical_state_dir.exists() or not canonical_state_dir.is_dir():
        raise LocalStateMigrationError("local_state.state_dir_invalid", "state-dir is invalid")
    mode = "file-only" if file_only else "profile"
    if file_only and trace_by_run_id is not None:
        raise LocalStateMigrationError("local_state.mode_conflict", "migration mode is invalid")
    if not file_only and trace_by_run_id is None:
        raise LocalStateMigrationError(
            "local_state.profile_required", "profile trace map is required"
        )

    with state_lock(canonical_state_dir, shared_name=LOCK_NAME):
        paths = inventory(
            canonical_state_dir,
            event_paths=event_paths,
            score_paths=score_paths,
        )
        recover_incomplete_journal(
            canonical_state_dir,
            inventory=paths,
            mode=mode,
        )
        plans = [
            preflight_file(
                path,
                kind=kind,
                file_only=file_only,
                trace_by_run_id=trace_by_run_id,
            )
            for path, kind in paths
        ]
        journal_path = canonical_state_dir / JOURNAL_NAME
        journal = {
            "version": 1,
            "state": "planned",
            "mode": mode,
            "files": [
                {
                    "path": str(path),
                    "backup": str(backup_path(path)),
                    "rollback": str(rollback_path(path)),
                    "kind": kind,
                }
                for path, kind in paths
            ],
        }
        atomic_write_json(journal_path, journal)

        migrated_records = 0
        try:
            journal["state"] = "writing"
            atomic_write_json(journal_path, journal)
            for (path, _kind), records in zip(paths, plans, strict=True):
                backup = backup_path(path)
                if not backup.exists():
                    copy_bytes_atomic(path, backup)
                copy_bytes_atomic(path, rollback_path(path))
                atomic_replace_jsonl(path, records)
                migrated_records += len(records)
            journal["state"] = "completed"
            journal["migrated_records"] = migrated_records
            atomic_write_json(journal_path, journal)
            for path, _kind in paths:
                rollback_path(path).unlink(missing_ok=True)
        except Exception as exc:
            restore_from_journal(journal, inventory=paths, mode=mode)
            raise LocalStateMigrationError(
                "local_state.migration_failed",
                "local state migration failed and was restored",
            ) from exc

        for path, kind in paths:
            register_local_state_file(
                path,
                kind=kind,
                state_dir=canonical_state_dir,
                allow_existing=True,
            )
        return LocalStateMigrationResult(
            paths=tuple(path for path, _kind in paths),
            migrated_records=migrated_records,
            mode=mode,
        )


def migrate_profile_local_state(
    *,
    state_dir: Path,
    event_paths: Sequence[Path] = (),
    score_paths: Sequence[Path] = (),
    known_run_ids: Set[str],
    database_upgrade: Callable[[], Mapping[str, str]],
    atomic_replace_jsonl: AtomicJsonlReplace,
) -> LocalStateMigrationResult:
    """在同一 journal 中先预检全部文件，再推进 DB 并原子切换 JSONL。"""

    canonical_state_dir = state_dir.expanduser().resolve()
    if not canonical_state_dir.exists() or not canonical_state_dir.is_dir():
        raise LocalStateMigrationError("local_state.state_dir_invalid", "state-dir is invalid")

    with state_lock(canonical_state_dir, shared_name=LOCK_NAME):
        paths = inventory(
            canonical_state_dir,
            event_paths=event_paths,
            score_paths=score_paths,
        )
        recover_incomplete_journal(
            canonical_state_dir,
            inventory=paths,
            mode="profile",
        )
        # 只表达“run 已存在”的占位 trace 不会写盘，用于保证非法记录在 DB 升级前失败。
        preflight_trace_map = {run_id: "preflight" for run_id in known_run_ids}
        for path, kind in paths:
            preflight_file(
                path,
                kind=kind,
                file_only=False,
                trace_by_run_id=preflight_trace_map,
            )

        journal_path = canonical_state_dir / JOURNAL_NAME
        journal: dict[str, Any] = {
            "version": 1,
            "state": "planned",
            "mode": "profile",
            "database": {"state": "pending"},
            "files": [
                {
                    "path": str(path),
                    "backup": str(backup_path(path)),
                    "rollback": str(rollback_path(path)),
                    "kind": kind,
                }
                for path, kind in paths
            ],
        }
        atomic_write_json(journal_path, journal)
        for path, _kind in paths:
            backup = backup_path(path)
            if not backup.exists():
                copy_bytes_atomic(path, backup)
            copy_bytes_atomic(path, rollback_path(path))

        migrated_records = 0
        try:
            journal["state"] = "database_migrating"
            atomic_write_json(journal_path, journal)
            trace_by_run_id = dict(database_upgrade())
            journal["database"] = {"state": "migrated"}
            journal["state"] = "database_migrated"
            atomic_write_json(journal_path, journal)

            plans = [
                preflight_file(
                    path,
                    kind=kind,
                    file_only=False,
                    trace_by_run_id=trace_by_run_id,
                )
                for path, kind in paths
            ]
            journal["state"] = "writing"
            atomic_write_json(journal_path, journal)
            for (path, _kind), records in zip(paths, plans, strict=True):
                atomic_replace_jsonl(path, records)
                migrated_records += len(records)
            journal["state"] = "completed"
            journal["migrated_records"] = migrated_records
            atomic_write_json(journal_path, journal)
            for path, _kind in paths:
                rollback_path(path).unlink(missing_ok=True)
        except Exception as exc:
            restore_from_journal(journal, inventory=paths, mode="profile")
            journal["state"] = "failed_files_restored"
            atomic_write_json(journal_path, journal)
            raise LocalStateMigrationError(
                "local_state.migration_failed",
                "local state migration failed and was restored",
            ) from exc

        for path, kind in paths:
            register_local_state_file(
                path,
                kind=kind,
                state_dir=canonical_state_dir,
                allow_existing=True,
            )
        return LocalStateMigrationResult(
            paths=tuple(path for path, _kind in paths),
            migrated_records=migrated_records,
            mode="profile",
        )
