"""Local-state manifest、readiness 与显式 inventory。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from agent_harness._local_state_common import (
    JOURNAL_NAME,
    LOCAL_STATE_FORMAT_VERSION,
    MANIFEST_NAME,
    LocalStateKind,
    LocalStateMigrationError,
    atomic_write_json,
    infer_state_dir,
    load_json_object,
    state_lock,
)


def require_local_state_ready(
    *,
    event_paths: Sequence[Path] = (),
    score_paths: Sequence[Path] = (),
    state_dir: Path | None = None,
) -> None:
    """只读校验普通进程显式使用的 local-state bundle 已可安全读写。

    普通入口不能恢复 journal、登记 manifest 或创建目录；这些动作只属于显式
    离线迁移和 sink 首次写入。新路径（不存在或为空）仍可按首次写入语义创建，
    但已有非空文件必须已经由当前版本 manifest 登记。
    """

    explicit_state_dir = state_dir.expanduser().resolve() if state_dir is not None else None
    inventory = [(path.expanduser().resolve(), "events") for path in event_paths] + [
        (path.expanduser().resolve(), "scores") for path in score_paths
    ]
    bundles: dict[Path, list[tuple[Path, LocalStateKind]]] = {}
    for path, raw_kind in inventory:
        kind = cast(LocalStateKind, raw_kind)
        bundle_dir = explicit_state_dir or infer_state_dir(path)
        entries = bundles.setdefault(bundle_dir, [])
        existing = next(
            (entry_kind for entry_path, entry_kind in entries if entry_path == path), None
        )
        if existing is not None and existing != kind:
            raise LocalStateMigrationError("local_state.path_kind_conflict", "path kind conflicts")
        if existing is None:
            entries.append((path, kind))

    for bundle_dir, explicit_inventory in sorted(bundles.items(), key=lambda item: str(item[0])):
        _require_bundle_ready(bundle_dir, explicit_inventory=explicit_inventory)


def _require_bundle_ready(
    state_dir: Path,
    *,
    explicit_inventory: Sequence[tuple[Path, LocalStateKind]],
) -> None:
    """读取一个 bundle 的 journal/manifest，并校验显式 inventory。"""

    if state_dir.exists() and not state_dir.is_dir():
        raise LocalStateMigrationError("local_state.state_dir_invalid", "state-dir is invalid")
    if not state_dir.exists():
        return

    journal_path = state_dir / JOURNAL_NAME
    if journal_path.exists():
        journal = load_json_object(journal_path)
        if journal.get("version") != 1 or not isinstance(journal.get("files"), list):
            raise LocalStateMigrationError("local_state.journal_invalid", "journal is invalid")
        if journal.get("state") != "completed":
            raise LocalStateMigrationError(
                "local_state.migration_required",
                "local state has an incomplete offline migration",
            )

    manifest_path = state_dir / MANIFEST_NAME
    manifest = load_json_object(
        manifest_path,
        missing={"manifest_version": 1, "files": []},
    )
    if manifest.get("manifest_version") != 1:
        raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")

    registered: dict[Path, tuple[LocalStateKind, int, Path]] = {}
    for raw_file in cast(list[Any], raw_files):
        if not isinstance(raw_file, dict):
            raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")
        entry = cast(dict[str, Any], raw_file)
        raw_kind = entry.get("kind")
        raw_path = entry.get("path")
        raw_state_dir = entry.get("state_dir")
        raw_format_version = entry.get("format_version")
        if (
            raw_kind not in {"events", "scores"}
            or not isinstance(raw_path, str)
            or not raw_path
            or not isinstance(raw_state_dir, str)
            or not raw_state_dir
            or not isinstance(raw_format_version, int)
        ):
            raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")
        canonical_path = Path(raw_path).expanduser().resolve()
        canonical_entry_state_dir = Path(raw_state_dir).expanduser().resolve()
        if canonical_entry_state_dir != state_dir:
            raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")
        if canonical_path in registered:
            raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")
        if canonical_path.exists() and not canonical_path.is_file():
            raise LocalStateMigrationError("local_state.path_invalid", "inventory path is invalid")
        if (
            canonical_path.exists()
            and canonical_path.stat().st_size > 0
            and raw_format_version != LOCAL_STATE_FORMAT_VERSION
        ):
            raise LocalStateMigrationError(
                "local_state.migration_required",
                "existing local state requires offline migration",
            )
        registered[canonical_path] = (
            cast(LocalStateKind, raw_kind),
            raw_format_version,
            canonical_entry_state_dir,
        )

    for path, kind in explicit_inventory:
        if path.exists() and not path.is_file():
            raise LocalStateMigrationError("local_state.path_invalid", "inventory path is invalid")
        current = registered.get(path)
        if current is not None and current[0] != kind:
            raise LocalStateMigrationError("local_state.path_kind_conflict", "path kind conflicts")
        if current is not None and current[1] != LOCAL_STATE_FORMAT_VERSION:
            raise LocalStateMigrationError(
                "local_state.migration_required",
                "existing local state requires offline migration",
            )
        if path.exists() and path.stat().st_size > 0 and current is None:
            raise LocalStateMigrationError(
                "local_state.migration_required",
                "existing local state requires offline migration",
            )


def register_local_state_file(
    path: Path,
    *,
    kind: LocalStateKind,
    state_dir: Path | None = None,
    allow_existing: bool = False,
) -> None:
    """在 sink 首写前原子登记 canonical path、kind、版本与 state-dir。"""

    canonical_path = path.expanduser().resolve()
    canonical_state_dir = (state_dir or infer_state_dir(canonical_path)).expanduser().resolve()
    canonical_state_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = canonical_state_dir / MANIFEST_NAME
    with state_lock(canonical_state_dir, shared_name=".local-state-manifest.lock"):
        journal_path = canonical_state_dir / JOURNAL_NAME
        if journal_path.exists():
            journal = load_json_object(journal_path)
            if journal.get("state") != "completed":
                raise LocalStateMigrationError(
                    "local_state.migration_required",
                    "local state has an incomplete offline migration",
                )
        manifest = load_json_object(manifest_path, missing={"manifest_version": 1, "files": []})
        raw_files = manifest.get("files")
        if not isinstance(raw_files, list):
            raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")
        files: list[dict[str, Any]] = []
        for raw_file in cast(list[Any], raw_files):
            if not isinstance(raw_file, dict):
                raise LocalStateMigrationError(
                    "local_state.manifest_invalid", "manifest is invalid"
                )
            files.append(cast(dict[str, Any], raw_file))
        current = next(
            (item for item in files if item.get("path") == str(canonical_path)),
            None,
        )
        if canonical_path.exists() and canonical_path.stat().st_size > 0 and not allow_existing:
            if current is None or current.get("format_version") != LOCAL_STATE_FORMAT_VERSION:
                raise LocalStateMigrationError(
                    "local_state.migration_required",
                    "existing local state requires offline migration",
                )
        entry = {
            "path": str(canonical_path),
            "kind": kind,
            "format_version": LOCAL_STATE_FORMAT_VERSION,
            "state_dir": str(canonical_state_dir),
        }
        retained = [item for item in files if item.get("path") != str(canonical_path)]
        retained.append(entry)
        manifest["files"] = sorted(retained, key=lambda item: str(item.get("path", "")))
        atomic_write_json(manifest_path, manifest)


def inventory(
    state_dir: Path,
    *,
    event_paths: Sequence[Path],
    score_paths: Sequence[Path],
) -> list[tuple[Path, LocalStateKind]]:
    """合并 manifest 和显式路径，并拒绝 kind 冲突或无效路径。"""

    manifest = load_json_object(state_dir / MANIFEST_NAME, missing={"files": []})
    raw_files = manifest.get("files", [])
    if not isinstance(raw_files, list):
        raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")
    entries: dict[Path, LocalStateKind] = {}
    for raw_value in cast(list[Any], raw_files):
        if not isinstance(raw_value, dict):
            raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")
        raw = cast(dict[str, Any], raw_value)
        raw_kind = raw.get("kind")
        if raw_kind not in {"events", "scores"}:
            raise LocalStateMigrationError("local_state.manifest_invalid", "manifest is invalid")
        path = Path(str(raw.get("path", ""))).expanduser().resolve()
        _add_inventory_entry(entries, path, cast(LocalStateKind, raw_kind))
    for path in event_paths:
        _add_inventory_entry(entries, path.expanduser().resolve(), "events")
    for path in score_paths:
        _add_inventory_entry(entries, path.expanduser().resolve(), "scores")
    if not entries:
        raise LocalStateMigrationError(
            "local_state.inventory_empty", "local state inventory is empty"
        )
    for path in entries:
        if not path.exists() or not path.is_file():
            raise LocalStateMigrationError("local_state.path_invalid", "inventory path is invalid")
    return sorted(entries.items(), key=lambda item: str(item[0]))


def _add_inventory_entry(
    entries: dict[Path, LocalStateKind], path: Path, kind: LocalStateKind
) -> None:
    """向合并 inventory 加入唯一规范路径；同一路径不能同时声明两种记录类型。"""

    existing = entries.get(path)
    if existing is not None and existing != kind:
        raise LocalStateMigrationError("local_state.path_kind_conflict", "path kind conflicts")
    entries[path] = kind
