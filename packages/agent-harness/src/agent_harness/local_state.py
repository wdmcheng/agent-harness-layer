"""本地 JSONL inventory、manifest 与 0013 离线迁移公开入口。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence, Set
from pathlib import Path

from agent_harness._local_state_common import (
    JOURNAL_NAME,
    LOCAL_STATE_FORMAT_VERSION,
    LOCK_NAME,
    MANIFEST_NAME,
    LocalStateKind,
    LocalStateMigrationError,
    LocalStateMigrationResult,
    infer_state_dir,
)
from agent_harness._local_state_common import (
    atomic_replace_jsonl as _atomic_replace_jsonl,
)
from agent_harness._local_state_manifest import (
    register_local_state_file,
    require_local_state_ready,
)
from agent_harness._local_state_migrations import (
    migrate_local_state as _migrate_local_state,
)
from agent_harness._local_state_migrations import (
    migrate_profile_local_state as _migrate_profile_local_state,
)


def migrate_local_state(
    *,
    state_dir: Path,
    event_paths: Sequence[Path] = (),
    score_paths: Sequence[Path] = (),
    file_only: bool,
    trace_by_run_id: Mapping[str, str] | None = None,
) -> LocalStateMigrationResult:
    """迁移显式 inventory，并保留公开模块上的文件替换测试 seam。"""

    return _migrate_local_state(
        state_dir=state_dir,
        event_paths=event_paths,
        score_paths=score_paths,
        file_only=file_only,
        trace_by_run_id=trace_by_run_id,
        atomic_replace_jsonl=_atomic_replace_jsonl,
    )


def migrate_profile_local_state(
    *,
    state_dir: Path,
    event_paths: Sequence[Path] = (),
    score_paths: Sequence[Path] = (),
    known_run_ids: Set[str],
    database_upgrade: Callable[[], Mapping[str, str]],
) -> LocalStateMigrationResult:
    """协调数据库与 JSONL 迁移，并保留公开模块上的故障注入 seam。"""

    return _migrate_profile_local_state(
        state_dir=state_dir,
        event_paths=event_paths,
        score_paths=score_paths,
        known_run_ids=known_run_ids,
        database_upgrade=database_upgrade,
        atomic_replace_jsonl=_atomic_replace_jsonl,
    )


__all__ = [
    "JOURNAL_NAME",
    "LOCAL_STATE_FORMAT_VERSION",
    "LOCK_NAME",
    "MANIFEST_NAME",
    "LocalStateKind",
    "LocalStateMigrationError",
    "LocalStateMigrationResult",
    "infer_state_dir",
    "migrate_local_state",
    "migrate_profile_local_state",
    "register_local_state_file",
    "require_local_state_ready",
]
