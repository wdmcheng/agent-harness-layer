"""四个示例的发现、真实 executor composition 与安全降级合同测试。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_harness.identity import IdentityContext
from agent_harness.registry import AgentRegistry, RegistryLoadError
from agent_harness.runtime import AgentExecutionContext, AgentExecutionResult, RunStatus
from agent_harness.storage import run_migrations
from app.main import create_app
from app.runtime import RuntimeComponents, build_runtime_components

ROOT = Path(__file__).resolve().parents[2]
SERVICE_APP = ROOT / "templates" / "service-app"
PROFILES = SERVICE_APP / "configs" / "profiles"
AGENTS = SERVICE_APP / "agents"
EXAMPLE_AGENT_IDS = {
    "examples.rag_assistant",
    "examples.ticket_triage",
    "examples.repo_analyst",
    "examples.dev_assistant",
}


def _dsn(path: Path) -> str:
    """将每个临时 SQLite 文件转换为 runtime 组件可消费的异步 DSN。"""

    return f"sqlite+aiosqlite:///{path}"


def _components(
    tmp_path: Path,
    *,
    name: str,
    workspace_root: Path | None = None,
) -> tuple[RuntimeComponents, Path, Path]:
    """创建隔离 runtime、数据库和事件文件，避免示例 agent 流程相互污染。"""

    db_path = tmp_path / f"{name}.db"
    events_path = tmp_path / f"{name}-events.jsonl"
    run_migrations(_dsn(db_path))
    components = build_runtime_components(
        profile="local",
        profiles_dir=PROFILES,
        storage_dsn=_dsn(db_path),
        events_path=events_path,
        artifact_root=tmp_path / f"{name}-artifacts",
        workspace_root=workspace_root,
    )
    return components, db_path, events_path


async def _run_output(components: RuntimeComponents, run_id: str) -> dict[str, object]:
    """从持久化运行记录取回最终输出，确保断言不依赖内存中的临时对象。"""

    async with components.storage.uow() as uow:
        row = await uow.runs.get(run_id)
    assert row is not None and row.output is not None
    return row.output


__all__ = [
    "AGENTS",
    "AgentExecutionContext",
    "AgentExecutionResult",
    "AgentRegistry",
    "IdentityContext",
    "EXAMPLE_AGENT_IDS",
    "PROFILES",
    "Path",
    "ROOT",
    "RegistryLoadError",
    "RunStatus",
    "RuntimeComponents",
    "SERVICE_APP",
    "TestClient",
    "ValidationError",
    "_components",
    "_dsn",
    "_run_output",
    "build_runtime_components",
    "create_app",
    "json",
    "pytest",
    "run_migrations",
    "sqlite3",
    "subprocess",
    "sys",
]
