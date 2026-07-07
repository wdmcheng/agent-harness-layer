"""持久化、migration 和 Unit of Work 的公开入口。"""

from __future__ import annotations

from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage as SQLAlchemyStorage
from agent_harness.storage.migrations.runner import get_current_revision as get_current_revision
from agent_harness.storage.migrations.runner import run_migrations as run_migrations
from agent_harness.storage.repositories import (
    CheckpointCreate as CheckpointCreate,
)
from agent_harness.storage.repositories import (
    CheckpointRecord as CheckpointRecord,
)
from agent_harness.storage.repositories import (
    RunCreate as RunCreate,
)
from agent_harness.storage.repositories import (
    RunRecord as RunRecord,
)
from agent_harness.storage.repositories import (
    SessionCreate as SessionCreate,
)
from agent_harness.storage.repositories import (
    SessionRecord as SessionRecord,
)
from agent_harness.storage.repositories import (
    TenantRecord as TenantRecord,
)
from agent_harness.storage.settings import storage_dsn_from_settings as storage_dsn_from_settings

_REPOSITORY_DTO_EXPORTS = [
    "CheckpointCreate",
    "CheckpointRecord",
    "RunCreate",
    "RunRecord",
    "SessionCreate",
    "SessionRecord",
    "TenantRecord",
]

_STORAGE_ADAPTER_EXPORTS = [
    "SQLAlchemyStorage",
]

_MIGRATION_EXPORTS = [
    "get_current_revision",
    "run_migrations",
]

_SETTINGS_EXPORTS = [
    "storage_dsn_from_settings",
]

__all__ = [  # pyright: ignore[reportUnsupportedDunderAll]
    *_REPOSITORY_DTO_EXPORTS,
    *_STORAGE_ADAPTER_EXPORTS,
    *_MIGRATION_EXPORTS,
    *_SETTINGS_EXPORTS,
]
