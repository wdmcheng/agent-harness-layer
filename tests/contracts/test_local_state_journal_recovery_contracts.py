"""Local-state 中断 journal 的恢复边界合同。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.local_state_migration_contract_helpers import write_jsonl

from agent_harness.local_state import (
    JOURNAL_NAME,
    LocalStateMigrationError,
    migrate_local_state,
)


def _entry(path: Path, *, kind: str = "events") -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "backup": str(path.with_name(f"{path.name}.pre-0013.bak").resolve()),
        "rollback": str(path.with_name(f".{path.name}.0013.rollback").resolve()),
        "kind": kind,
    }


def _outside_inventory(
    journal: dict[str, Any],
    inventory_path: Path,
    external_target: Path,
    external_source: Path,
) -> None:
    del inventory_path
    external_source.write_text("OVERWRITTEN", encoding="utf-8")
    item = _entry(external_target)
    item["rollback"] = str(external_source.resolve())
    journal["files"] = [item]


def _forged_backup(
    journal: dict[str, Any],
    inventory_path: Path,
    _external_target: Path,
    external_source: Path,
) -> None:
    external_source.write_text("OVERWRITTEN", encoding="utf-8")
    journal["files"][0]["backup"] = str(external_source.resolve())


def _forged_rollback(
    journal: dict[str, Any],
    inventory_path: Path,
    _external_target: Path,
    external_source: Path,
) -> None:
    del inventory_path
    external_source.write_text("OVERWRITTEN", encoding="utf-8")
    journal["files"][0]["rollback"] = str(external_source.resolve())


def _duplicate_path(
    journal: dict[str, Any],
    _inventory_path: Path,
    _external_target: Path,
    _external_source: Path,
) -> None:
    journal["files"].append(dict(journal["files"][0]))


def _wrong_kind(
    journal: dict[str, Any],
    _inventory_path: Path,
    _external_target: Path,
    _external_source: Path,
) -> None:
    journal["files"][0]["kind"] = "scores"


def _wrong_version(
    journal: dict[str, Any],
    _inventory_path: Path,
    _external_target: Path,
    _external_source: Path,
) -> None:
    journal["version"] = 2


def _wrong_state(
    journal: dict[str, Any],
    _inventory_path: Path,
    _external_target: Path,
    _external_source: Path,
) -> None:
    journal["state"] = "unknown"


def _wrong_mode(
    journal: dict[str, Any],
    _inventory_path: Path,
    _external_target: Path,
    _external_source: Path,
) -> None:
    journal["mode"] = "profile"


def _wrong_path_type(
    journal: dict[str, Any],
    _inventory_path: Path,
    _external_target: Path,
    _external_source: Path,
) -> None:
    journal["files"][0]["path"] = 7


JournalMutation = Callable[[dict[str, Any], Path, Path, Path], None]


@pytest.mark.parametrize(
    "mutate",
    [
        _outside_inventory,
        _forged_backup,
        _forged_rollback,
        _duplicate_path,
        _wrong_kind,
        _wrong_version,
        _wrong_state,
        _wrong_mode,
        _wrong_path_type,
    ],
    ids=[
        "outside-inventory",
        "forged-backup",
        "forged-rollback",
        "duplicate-path",
        "wrong-kind",
        "wrong-version",
        "wrong-state",
        "wrong-mode",
        "wrong-path-type",
    ],
)
def test_incomplete_journal_metadata_is_validated_before_any_restore_write(
    tmp_path: Path,
    mutate: JournalMutation,
) -> None:
    """篡改的恢复元数据必须 fail closed，不能先写文件再报告失败。"""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    inventory_path = state_dir / "events.jsonl"
    write_jsonl(
        inventory_path,
        [{"run_id": "telemetry", "payload": {"telemetry": {"context": {"run_id": None}}}}],
    )
    rollback = inventory_path.with_name(f".{inventory_path.name}.0013.rollback")
    rollback.write_bytes(inventory_path.read_bytes())
    external_target = tmp_path / "outside.txt"
    external_target.write_text("ORIGINAL", encoding="utf-8")
    external_source = tmp_path / "outside-source.txt"
    external_source.write_text("SOURCE", encoding="utf-8")
    journal: dict[str, Any] = {
        "version": 1,
        "state": "writing",
        "mode": "file-only",
        "files": [_entry(inventory_path)],
    }
    mutate(journal, inventory_path, external_target, external_source)
    journal_path = state_dir / JOURNAL_NAME
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    watched = (inventory_path, rollback, external_target, external_source, journal_path)
    before = {path: path.read_bytes() for path in watched if path.exists()}

    with pytest.raises(LocalStateMigrationError) as error:
        migrate_local_state(
            state_dir=state_dir,
            event_paths=[inventory_path],
            file_only=True,
        )

    assert error.value.code == "local_state.journal_invalid"
    assert {path: path.read_bytes() for path in watched if path.exists()} == before
