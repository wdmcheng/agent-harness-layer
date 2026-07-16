"""Runtime、checkpoint 和 run lifecycle 的公开契约测试。

这些测试穿过 runtime module、CLI 和 template API helper 三个入口，证明 fake
agent 在无真实模型 key 时也能创建 run、写 checkpoint、resume、产出 terminal
event，并且 idempotency key 不会因为进程内对象重建而失效。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.requests import Request
from tests.contracts.auth_policy_hitl_contract_helpers import asgi_request
from tests.contracts.runtime_contract_helpers import FakeContractExecutor, sqlite_dsn

from agent_harness.events import (
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
)
from agent_harness.runtime import (
    ApprovalWaitState,
    CheckpointStore,
    IdempotencyKey,
    InvalidRunTransition,
    ResumeToken,
    RunOrchestrator,
    RunStatus,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.api.routes.runs import RunCreateRequest, create_run_for_test, public_events, router
from app.main import create_app
from app.workers.runtime_worker import run_once as worker_run_once

ROOT = Path(__file__).resolve().parents[2]

RUN_OPENAPI_CONTRACTS: dict[
    tuple[str, str],
    tuple[set[str], dict[str, str]],
] = {
    ("/api/v1/agents/{agent_id}/runs", "post"): (
        {"200", "202", "400", "401", "403", "404", "409", "422", "500", "503"},
        {"200": "RunCreateResponse", "202": "RunCreateResponse"},
    ),
    ("/api/v1/runs/{run_id}", "get"): (
        {"200", "401", "403", "404", "500"},
        {"200": "RunDetailResponse"},
    ),
    ("/api/v1/runs/{run_id}/events", "get"): (
        {"200", "401", "403", "404", "422", "500"},
        {"200": "RunEventsResponse"},
    ),
    ("/api/v1/runs/{run_id}/cancel", "post"): (
        {"200", "401", "403", "404", "409", "500"},
        {"200": "RunCreateResponse"},
    ),
    ("/api/v1/runs/{run_id}/resume", "post"): (
        {"200", "401", "403", "404", "409", "422", "500"},
        {"200": "RunCreateResponse"},
    ),
}


async def build_orchestrator(tmp_path: Path) -> tuple[RunOrchestrator, SQLAlchemyStorage, Path]:
    """构造带 SQLite storage 和 local event sink 的测试 runtime。"""

    # 这个 helper 故意返回 storage，让测试在 finally 中显式 dispose。
    # runtime tests 会重建 orchestrator；如果这里把 DB/sink 藏成全局 fixture，
    # 就很容易误把内存状态当成 checkpoint/resume 证据。
    db_path = tmp_path / "runtime.db"
    events_path = tmp_path / "events.jsonl"
    run_migrations(sqlite_dsn(db_path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(db_path))
    bus = EventBus(sink=LocalJsonlEventSink(events_path))
    executor = FakeContractExecutor()
    return (
        RunOrchestrator(
            storage=storage,
            event_bus=bus,
            executor_resolver=lambda _agent_id: executor,
        ),
        storage,
        events_path,
    )


__all__ = [
    "APIRoute",
    "Any",
    "ApprovalWaitState",
    "CanonicalEventType",
    "CheckpointStore",
    "EventBus",
    "FakeContractExecutor",
    "FastAPI",
    "IdempotencyKey",
    "InvalidRunTransition",
    "LocalJsonlEventSink",
    "Path",
    "ROOT",
    "RUN_OPENAPI_CONTRACTS",
    "Request",
    "ResumeToken",
    "RunCreateRequest",
    "RunOrchestrator",
    "RunStatus",
    "SQLAlchemyStorage",
    "asgi_request",
    "build_orchestrator",
    "cast",
    "create_app",
    "create_run_for_test",
    "json",
    "public_events",
    "pytest",
    "router",
    "run_migrations",
    "sqlite_dsn",
    "subprocess",
    "sys",
    "worker_run_once",
]
