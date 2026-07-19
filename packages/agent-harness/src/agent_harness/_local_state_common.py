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
    """离线迁移的稳定失败边界，不在错误中回显 credential 或 record 内容。

    本地状态文件可能含有请求、评分或连接配置；所有解析、锁和恢复异常都应映射到有限
    错误码，而不是把文件内容、路径细节或凭据形态带到 CLI 输出中。
    """

    def __init__(self, code: str, message: str) -> None:
        """保存可供入口映射的稳定错误码，同时维持简短的维护者错误文本。"""

        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LocalStateMigrationResult:
    """只报告被显式 inventory 覆盖的路径，不暗示扫描了其他文件。

    ``paths`` 不是工作目录枚举结果；它让调用方明确知道本轮迁移只触碰了哪些受控
    本地状态文件，避免把未登记文件误解为已验证或已升级。
    """

    paths: tuple[Path, ...]
    migrated_records: int
    mode: str


def infer_state_dir(path: Path) -> Path:
    """优先把 `.agent-harness` 作为状态根目录，否则使用目标文件的父目录。

    文件可能位于状态根目录的深层子目录，锁和 journal 必须落在所有相关文件共享的根
    位置，不能随着单个 events 或 scores 文件的位置漂移。
    """

    resolved = path.expanduser().resolve()
    parts = resolved.parts
    if ".agent-harness" in parts:
        index = parts.index(".agent-harness")
        return Path(*parts[: index + 1])
    return resolved.parent


@dataclass(frozen=True, slots=True)
class _JournalFile:
    """已验证 journal 中一项文件恢复坐标的类型化表示。"""

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
    """先验证完整恢复计划，再对未完成 journal 执行任何文件写入。

    已完成 journal 只做结构验证而不重复恢复；其余状态必须同时匹配当前模式和完整
    inventory，防止不同命令或过期目录把不属于本轮迁移的文件带入回滚。
    """

    journal_path = state_dir / JOURNAL_NAME
    if not journal_path.exists():
        return
    journal = load_json_object(journal_path)
    journal_mode, state = _validate_journal_header(journal)
    if state == "completed":
        # 已完成记录仍需保持可解析，避免损坏 journal 被悄悄当作成功证据。
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
    """恢复当前进程刚写入的 journal，仍按同一 inventory 做 fail-closed 校验。

    此入口用于本进程后续步骤失败时的补偿；它不因为 journal 来自内存就跳过结构和
    inventory 校验，保证崩溃恢复与同步回滚遵循同一安全边界。
    """

    journal_mode, state = _validate_journal_header(journal)
    if journal_mode != mode or state == "completed":
        raise _journal_invalid()
    files = _validate_journal_files(journal, inventory=inventory)
    _restore_journal_files(files)


def _validate_journal_header(
    journal: Mapping[str, Any],
) -> tuple[LocalStateMigrationMode, str]:
    """封闭式校验 journal 头部、模式状态集合和可选数据库迁移坐标。"""

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
    # 仅 profile 模式允许写入数据库坐标，文件模式不能借未知字段扩大恢复范围。
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
    """将 journal 文件列表验证为与当前 inventory 一一对应的恢复坐标。"""

    raw_files = journal.get("files")
    if not isinstance(raw_files, list):
        raise _journal_invalid()
    expected = None if inventory is None else {path: kind for path, kind in inventory}
    # 重复 inventory 会让映射静默覆盖路径，因此在继续前明确拒绝。
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
        # backup/rollback 名称由正式路径确定，journal 不能任意指定恢复源。
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
    """接受已规范化绝对路径，拒绝空值、相对路径与可在恢复时重解释的文本。"""

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
    """确认恢复源不是符号链接或特殊文件；缺失文件可由上层按 journal 语义处理。"""

    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise _journal_invalid()


def _restore_journal_files(files: Sequence[_JournalFile]) -> None:
    """按每项优先 rollback、其次 backup 的顺序原子恢复原始字节内容。"""

    for item in files:
        # rollback 是当前轮写入前的精确快照；只有它不存在时才退回较早的 backup。
        source = item.rollback if item.rollback.is_file() else item.backup
        if source.is_file():
            copy_bytes_atomic(source, item.path)


def _journal_invalid() -> LocalStateMigrationError:
    """构造统一的 journal 损坏错误，避免向上层泄露哪一项校验失败。"""

    return LocalStateMigrationError("local_state.journal_invalid", "journal is invalid")


def backup_path(path: Path) -> Path:
    """返回兼容既有迁移格式的原始备份路径，不检查文件是否存在。"""

    return path.with_name(f"{path.name}.pre-0013.bak")


def rollback_path(path: Path) -> Path:
    """返回本轮写入前的短期回滚路径，不检查文件是否存在。"""

    return path.with_name(f".{path.name}.0013.rollback")


def copy_bytes_atomic(source: Path, target: Path) -> None:
    """把恢复源复制到同目录临时文件后原子替换目标，并同步目录元数据。"""

    temporary = target.with_name(f".{target.name}.tmp")
    data = source.read_bytes()
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    fsync_directory(target.parent)


def atomic_replace_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    """完整重写 JSONL 到同目录临时文件后替换，避免读者看到半份迁移结果。"""

    temporary = path.with_name(f".{path.name}.0013.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """原子写入格式化 JSON 元数据，并在替换后同步目录以支持硬退出恢复。"""

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
    """读取 JSON 对象；仅在调用方明确提供默认值时将缺失文件视为可接受。"""

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
    """获取非阻塞排他文件锁，防止并发迁移或恢复改写同一状态目录。

    锁文件保存持锁进程号仅用于本地诊断，不作为所有权判断；真正的互斥由 ``flock``
    保证，释放后同步目录以耐久化锁文件元数据的变化。
    """

    lock_path = state_dir / shared_name
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LocalStateMigrationError("local_state.locked", "local state is locked") from exc
        os.ftruncate(descriptor, 0)
        # 进程号是排障线索，不应被其他进程当作可接管锁的依据。
        os.write(descriptor, f"pid={os.getpid()}\n".encode())
        os.fsync(descriptor)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        fsync_directory(state_dir)


def fsync_directory(path: Path) -> None:
    """同步目录项，保证重命名、替换与锁文件更新在硬退出后可被观察到。"""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
