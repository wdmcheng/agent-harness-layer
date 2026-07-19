"""Service runtime durable execution 的 migration 与私有 storage 边界合同。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import update

from agent_harness.approvals import ApprovalService, ApprovalStateConflict
from agent_harness.audit import AuditService
from agent_harness.events import (
    EventBus,
    LocalJsonlEventSink,
)
from agent_harness.identity import IdentityContext
from agent_harness.runtime import InMemoryRunQueue, RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.approval_records import ApprovalCreate
from agent_harness.storage.models import ApprovalModel
from agent_harness.storage.repositories import RunCreate, SessionCreate


def _dsn(path: Path) -> str:
    """生成独立 SQLite 异步连接串，保证运行时存储合同不共享本地数据库。"""

    return f"sqlite+aiosqlite:///{path}"


__all__ = [
    "ApprovalCreate",
    "ApprovalModel",
    "ApprovalService",
    "ApprovalStateConflict",
    "AuditService",
    "EventBus",
    "IdentityContext",
    "InMemoryRunQueue",
    "LocalJsonlEventSink",
    "Path",
    "RunCreate",
    "RunOrchestrator",
    "SQLAlchemyStorage",
    "SessionCreate",
    "UTC",
    "_dsn",
    "datetime",
    "pytest",
    "run_migrations",
    "sqlite3",
    "timedelta",
    "update",
    "uuid4",
]
