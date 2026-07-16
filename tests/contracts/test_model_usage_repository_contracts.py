"""Usage settlement、outbox 与 event capacity repository 合同测试。"""

from __future__ import annotations

from asyncio import gather
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import update

from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EventCapacityExceeded,
    EvidenceOperationKind,
)
from agent_harness.storage.models import RunEventCapacityModel, RunEvidenceOutboxModel


def sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


_MISSING = object()


def usage_result(
    *,
    run_id: str,
    outcome: str = "completed",
    evidence_updates: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造 repository write-once 合同使用的完整统一 usage result。"""

    evidence: dict[str, object] = {
        "usage_kind": "model",
        "tenant_id": "tenant-a",
        "provider": "fake",
        "model": "fake-basic",
        "input_tokens": 1,
        "output_tokens": 2,
        "cost_usd": None,
        "cost_status": "unavailable",
        "latency_ms": 3,
        "decision": {"provider_called": True},
        "run_id": run_id,
        "agent_id": "agent-a",
        "request_id": None,
        "trace_id": "trace-a",
    }
    evidence.update(evidence_updates or {})
    return {"evidence": evidence, "outcome": outcome}


def usage_started(*, run_id: str) -> dict[str, object]:
    """返回 repository claim 需要持久冻结的 started 身份。"""

    return cast(dict[str, object], usage_result(run_id=run_id)["evidence"])


__all__ = [
    "Any",
    "EventCapacityExceeded",
    "EvidenceOperationKind",
    "MAX_EVENT_SEQ",
    "Path",
    "RunCreate",
    "RunEventCapacityModel",
    "RunEvidenceOutboxModel",
    "SQLAlchemyStorage",
    "SessionCreate",
    "_MISSING",
    "cast",
    "gather",
    "pytest",
    "run_migrations",
    "sqlite_dsn",
    "update",
    "usage_result",
    "usage_started",
]
