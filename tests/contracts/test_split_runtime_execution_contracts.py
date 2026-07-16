"""Service API 提交、worker 执行与身份 fencing 合同。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.contracts.runtime_contract_helpers import FakeContractExecutor, sqlite_dsn

from agent_harness.approvals import ApprovalService, ApprovalStateConflict
from agent_harness.events import CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    InMemoryRunQueue,
    InvalidRunTransition,
    RunOrchestrator,
    RunStatus,
    build_execute_message,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunCreate, SessionCreate

__all__ = [
    "AgentExecutionContext",
    "AgentExecutionRequest",
    "AgentExecutionResult",
    "Any",
    "ApprovalService",
    "ApprovalStateConflict",
    "CanonicalEventType",
    "EventBus",
    "FakeContractExecutor",
    "IdentityContext",
    "InMemoryRunQueue",
    "InvalidRunTransition",
    "LocalJsonlEventSink",
    "Path",
    "RunCreate",
    "RunOrchestrator",
    "RunStatus",
    "SQLAlchemyStorage",
    "SessionCreate",
    "SimpleNamespace",
    "build_execute_message",
    "cast",
    "pytest",
    "run_migrations",
    "sqlite_dsn",
]
