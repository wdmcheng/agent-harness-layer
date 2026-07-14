"""Local-state 公共类型、锁与原子文件操作。"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

LOCAL_STATE_FORMAT_VERSION = 2
MANIFEST_NAME = "local-state-manifest.json"
JOURNAL_NAME = "local-state-migration-journal.json"
LOCK_NAME = ".local-state-migration.lock"

LocalStateKind = Literal["events", "scores"]
LocalStateMigrationMode = Literal["file-only", "profile"]

_JOURNAL_STATES: dict[LocalStateMigrationMode, frozenset[str]] = {
    "file-only": frozenset({"planned", "writing", "restored", "completed"}),
    "profile": frozenset(
        {
            "planned",
            "database_migrating",
            "database_migrated",
            "writing",
            "failed_files_restored",
            "restored",
            "completed",
        }
    ),
}


class LocalStateMigrationError(RuntimeError):
    """离线迁移的稳定失败边界，不在错误中回显 credential 或 record 内容。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LocalStateMigrationResult:
    """只报告被显式 inventory 覆盖的路径，不暗示扫描了其他文件。"""

    paths: tuple[Path, ...]
    migrated_records: int
    mode: str


def infer_state_dir(path: Path) -> Path:
    """优先把 `.agent-harness` 作为 state-dir，否则使用文件父目录。"""

    resolved = path.expanduser().resolve()
    parts = resolved.parts
    if ".agent-harness" in parts:
        index = parts.index(".agent-harness")
        return Path(*parts[: index + 1])
    return resolved.parent


@dataclass(frozen=True, slots=True)
class _JournalFile:
    path: Path
    backup: Path
    rollback: Path
    kind: LocalStateKind


def recover_incomplete_journal(
    state_dir: Path,
    *,
    inventory: Sequence[tuple[Path, LocalStateKind]],
    mode: LocalStateMigrationMode,
) -> None:
    """先验证完整恢复计划，再对未完成 journal 执行任何文件写入。"""

    journal_path = state_dir / JOURNAL_NAME
    if not journal_path.exists():
        return
    journal = load_json_object(journal_path)
    journal_mode, state = _validate_journal_header(journal)
    if state == "completed":
        _validate_journal_files(journal, inventory=None)
        return
    if journal_mode != mode:
        raise _journal_invalid()
    files = _validate_journal_files(journal, inventory=inventory)
    _restore_journal_files(files)
    journal["state"] = "restored"
    atomic_write_json(journal_path, journal)


def restore_from_journal(
    journal: Mapping[str, Any],
    *,
    inventory: Sequence[tuple[Path, LocalStateKind]],
    mode: LocalStateMigrationMode,
) -> None:
    """恢复当前进程刚写入的 journal，仍按同一 inventory 做 fail-closed 校验。"""

    journal_mode, state = _validate_journal_header(journal)
    if journal_mode != mode or state == "completed":
        raise _journal_invalid()
    files = _validate_journal_files(journal, inventory=inventory)
    _restore_journal_files(files)


def _validate_journal_header(
    journal: Mapping[str, Any],
) -> tuple[LocalStateMigrationMode, str]:
    allowed_keys = {"version", "state", "mode", "files", "database", "migrated_records"}
    if set(journal) - allowed_keys or type(journal.get("version")) is not int:
        raise _journal_invalid()
    if journal.get("version") != 1:
        raise _journal_invalid()
    raw_mode = journal.get("mode")
    if raw_mode not in _JOURNAL_STATES:
        raise _journal_invalid()
    mode = raw_mode
    state = journal.get("state")
    if not isinstance(state, str) or state not in _JOURNAL_STATES[mode]:
        raise _journal_invalid()
    migrated_records = journal.get("migrated_records")
    if migrated_records is not None and (type(migrated_records) is not int or migrated_records < 0):
        raise _journal_invalid()
    database = journal.get("database")
    if mode == "file-only" and database is not None:
        raise _journal_invalid()
    if database is not None:
        if not isinstance(database, dict):
            raise _journal_invalid()
        database_mapping = cast(dict[str, Any], database)
        if set(database_mapping) != {"state"}:
            raise _journal_invalid()
        if database_mapping.get("state") not in {"pending", "migrated"}:
            raise _journal_invalid()
    return mode, state


def _validate_journal_files(
    journal: Mapping[str, Any],
    *,
    inventory: Sequence[tuple[Path, LocalStateKind]] | None,
) -> tuple[_JournalFile, ...]:
    raw_files = journal.get("files")
    if not isinstance(raw_files, list):
        raise _journal_invalid()
    expected = None if inventory is None else {path: kind for path, kind in inventory}
    if inventory is not None and expected is not None and len(expected) != len(inventory):
        raise _journal_invalid()
    files: list[_JournalFile] = []
    seen: set[Path] = set()
    for raw_item in cast(list[Any], raw_files):
        if not isinstance(raw_item, dict):
            raise _journal_invalid()
        item = cast(dict[str, Any], raw_item)
        if set(item) != {
            "path",
            "backup",
            "rollback",
            "kind",
        }:
            raise _journal_invalid()
        path = _canonical_journal_path(item.get("path"))
        backup = _canonical_journal_path(item.get("backup"))
        rollback = _canonical_journal_path(item.get("rollback"))
        raw_kind = item.get("kind")
        if raw_kind not in {"events", "scores"} or path in seen:
            raise _journal_invalid()
        kind = cast(LocalStateKind, raw_kind)
        if backup != backup_path(path) or rollback != rollback_path(path):
            raise _journal_invalid()
        if expected is not None and expected.get(path) != kind:
            raise _journal_invalid()
        _validate_restore_source(backup)
        _validate_restore_source(rollback)
        seen.add(path)
        files.append(_JournalFile(path=path, backup=backup, rollback=rollback, kind=kind))
    if expected is not None and seen != set(expected):
        raise _journal_invalid()
    return tuple(files)


def _canonical_journal_path(raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise _journal_invalid()
    path = Path(raw_path)
    if not path.is_absolute():
        raise _journal_invalid()
    canonical = path.resolve()
    if str(canonical) != raw_path:
        raise _journal_invalid()
    return canonical


def _validate_restore_source(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise _journal_invalid()


def _restore_journal_files(files: Sequence[_JournalFile]) -> None:
    for item in files:
        source = item.rollback if item.rollback.is_file() else item.backup
        if source.is_file():
            copy_bytes_atomic(source, item.path)


def _journal_invalid() -> LocalStateMigrationError:
    return LocalStateMigrationError("local_state.journal_invalid", "journal is invalid")


def backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.pre-0013.bak")


def rollback_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.0013.rollback")


def copy_bytes_atomic(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.tmp")
    data = source.read_bytes()
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    fsync_directory(target.parent)


def atomic_replace_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.0013.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def load_json_object(path: Path, *, missing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if missing is None:
            raise LocalStateMigrationError("local_state.journal_invalid", "journal is invalid")
        return dict(missing)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise LocalStateMigrationError(
            "local_state.metadata_invalid", "local state metadata is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise LocalStateMigrationError(
            "local_state.metadata_invalid", "local state metadata is invalid"
        )
    return cast(dict[str, Any], value)


@contextmanager
def state_lock(state_dir: Path, *, shared_name: str) -> Generator[None, None, None]:
    lock_path = state_dir / shared_name
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LocalStateMigrationError("local_state.locked", "local state is locked") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"pid={os.getpid()}\n".encode())
        os.fsync(descriptor)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        fsync_directory(state_dir)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
