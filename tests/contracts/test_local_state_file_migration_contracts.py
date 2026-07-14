"""Local state 文件 scope 判别与原子迁移合同。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.contracts.local_state_migration_contract_helpers import write_jsonl as _write_jsonl

from agent_harness.local_state import (
    JOURNAL_NAME,
    LocalStateMigrationError,
    migrate_local_state,
)


def test_file_only_uses_nested_telemetry_run_scope_and_is_atomic(tmp_path: Path) -> None:
    """合成 envelope run_id 不等于真实 run；nested run_id 才决定 telemetry scope。"""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    non_run_path = state_dir / "non-run.jsonl"
    _write_jsonl(
        non_run_path,
        [
            {
                "run_id": "synthetic-envelope-trace",
                "payload": {"telemetry": {"context": {"run_id": None, "trace_id": None}}},
                "trace_id": None,
            },
            {
                "run_id": "telemetry",
                "payload": {"telemetry": {"context": {"run_id": None}}},
            },
        ],
    )

    result = migrate_local_state(
        state_dir=state_dir,
        event_paths=[non_run_path],
        file_only=True,
    )
    migrated = [json.loads(line) for line in non_run_path.read_text(encoding="utf-8").splitlines()]
    assert result.mode == "file-only"
    assert [record["record_scope"] for record in migrated] == ["non_run", "non_run"]
    assert migrated[0]["trace_id"] is None
    assert "trace_id" not in migrated[1]
    assert (state_dir / f"{non_run_path.name}.pre-0013.bak").exists()
    assert (
        json.loads((state_dir / JOURNAL_NAME).read_text(encoding="utf-8"))["state"] == "completed"
    )

    run_path = state_dir / "run.jsonl"
    original = _write_jsonl(
        run_path,
        [
            {
                "run_id": "synthetic-envelope-trace",
                "payload": {"telemetry": {"context": {"run_id": "real-run"}}},
            }
        ],
    )
    with pytest.raises(LocalStateMigrationError) as error:
        migrate_local_state(state_dir=state_dir, event_paths=[run_path], file_only=True)
    assert error.value.code == "local_state.run_scope_requires_profile"
    assert run_path.read_bytes() == original


def test_explicit_record_scope_precedes_legacy_telemetry_classification(
    tmp_path: Path,
) -> None:
    """显式 scope 优先；legacy nested run_id 只在缺少 discriminator 时参与判别。"""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    run_path = state_dir / "explicit-run.jsonl"
    run_original = _write_jsonl(
        run_path,
        [
            {
                "record_scope": "run",
                "run_id": "run-a",
                "trace_id": None,
                "payload": {
                    "telemetry": {
                        "context": {
                            "run_id": None,
                            "trace_id": None,
                        }
                    }
                },
            }
        ],
    )

    with pytest.raises(LocalStateMigrationError) as error:
        migrate_local_state(
            state_dir=state_dir,
            event_paths=[run_path],
            file_only=True,
        )
    assert error.value.code == "local_state.run_scope_requires_profile"
    assert run_path.read_bytes() == run_original

    result = migrate_local_state(
        state_dir=state_dir,
        event_paths=[run_path],
        file_only=False,
        trace_by_run_id={"run-a": "trace-a"},
    )
    migrated_run = json.loads(run_path.read_text(encoding="utf-8"))
    assert result.mode == "profile"
    assert migrated_run["record_scope"] == "run"
    assert migrated_run["trace_id"] == "trace-a"
    assert migrated_run["payload"]["telemetry"]["context"] == {
        "run_id": None,
        "trace_id": "trace-a",
    }

    non_run_state_dir = tmp_path / "non-run-state"
    non_run_state_dir.mkdir()
    non_run_path = non_run_state_dir / "explicit-non-run.jsonl"
    _write_jsonl(
        non_run_path,
        [
            {
                "record_scope": "non_run",
                "run_id": "telemetry",
                "trace_id": None,
                "payload": {"value": 1},
            }
        ],
    )

    migrate_local_state(
        state_dir=non_run_state_dir,
        event_paths=[non_run_path],
        file_only=True,
    )
    migrated_non_run = json.loads(non_run_path.read_text(encoding="utf-8"))
    assert migrated_non_run == {
        "record_scope": "non_run",
        "run_id": "telemetry",
        "trace_id": None,
        "payload": {"value": 1},
    }

    profile_non_run_state_dir = tmp_path / "profile-non-run-state"
    profile_non_run_state_dir.mkdir()
    profile_non_run_path = profile_non_run_state_dir / "explicit-non-run.jsonl"
    _write_jsonl(
        profile_non_run_path,
        [
            {
                "record_scope": "non_run",
                "run_id": "run-a",
                "trace_id": None,
                "payload": {
                    "telemetry": {
                        "context": {
                            "run_id": "run-b",
                            "trace_id": None,
                        }
                    }
                },
            }
        ],
    )

    migrate_local_state(
        state_dir=profile_non_run_state_dir,
        event_paths=[profile_non_run_path],
        file_only=False,
        trace_by_run_id={"run-a": "trace-a", "run-b": "trace-b"},
    )
    migrated_profile_non_run = json.loads(profile_non_run_path.read_text(encoding="utf-8"))
    assert migrated_profile_non_run["record_scope"] == "non_run"
    assert migrated_profile_non_run["trace_id"] is None
    assert migrated_profile_non_run["payload"]["telemetry"]["context"] == {
        "run_id": "run-b",
        "trace_id": None,
    }


@pytest.mark.parametrize(
    ("record", "expected_code"),
    (
        (
            {"record_scope": "run", "run_id": None, "payload": {}},
            "local_state.run_owner_invalid",
        ),
        (
            {"record_scope": "unexpected", "run_id": "run-a", "payload": {}},
            "local_state.record_scope_invalid",
        ),
    ),
)
def test_invalid_explicit_record_scope_fails_before_local_state_mutation(
    tmp_path: Path,
    record: dict[str, object],
    expected_code: str,
) -> None:
    """显式 discriminator 或 run owner 无效时，完整 bundle 保持原字节。"""

    state_dir = tmp_path / expected_code
    state_dir.mkdir()
    event_path = state_dir / "events.jsonl"
    original = _write_jsonl(event_path, [record])

    with pytest.raises(LocalStateMigrationError) as error:
        migrate_local_state(
            state_dir=state_dir,
            event_paths=[event_path],
            file_only=False,
            trace_by_run_id={"run-a": "trace-a"},
        )

    assert error.value.code == expected_code
    assert event_path.read_bytes() == original
    assert not (state_dir / f"{event_path.name}.pre-0013.bak").exists()
