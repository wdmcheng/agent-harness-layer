"""0016 fresh upgrade、legacy 分类与 evidence-aware downgrade 合同。"""

# ruff: noqa: F401

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from tests.contracts.run_trace_migration_test_helpers import migration_config, seed_identity

from agent_harness.storage import get_current_revision, run_migrations
from agent_harness.storage.shared_budget import OperationIdentity


def sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "Any",
    "Decimal",
    "OperationIdentity",
    "Path",
    "canonical_hash",
    "cast",
    "command",
    "get_current_revision",
    "hashlib",
    "json",
    "migration_config",
    "pytest",
    "run_migrations",
    "seed_identity",
    "sqlite3",
    "sqlite_dsn",
]
