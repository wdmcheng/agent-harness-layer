"""Agent scaffold CLI 的路径、原子发布、runtime 与 eval 合同。"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from typer.testing import CliRunner

import agent_harness.scaffold as scaffold_module
from agent_harness.cli import app
from agent_harness.evals import EvalCaseFactory, EvalRunner, EvalService, EvalTraceSource, ScoreSink
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import AgentExecutionContext, AgentExecutionRequest, RunOrchestrator
from agent_harness.scaffold import (
    ScaffoldError,
    executor_rollback_preflight,
    scaffold_agent_package,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]


class _RegistryCaseExecutor:
    """把人工批准的 file case 交给生成 executor，而不是复用 expected。"""

    def __init__(self, registry: AgentRegistry, agent_id: str) -> None:
        """解析并保存生成 agent 的真实 executor，使 eval 用例验证运行能力而非硬编码 expected。"""

        self._executor = registry.resolve_executor(agent_id)
        self._agent_id = agent_id

    async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
        """将批准用例的输入送入生成 executor，返回其输出以连接手工 eval gate 与运行时 seam。"""

        payload = cast(dict[str, Any], case["payload"])
        input_payload = cast(dict[str, Any], payload["input"])
        result = await self._executor.run(
            AgentExecutionRequest(
                agent_id=self._agent_id,
                run_id="eval-scaffold-run",
                input=input_payload,
            ),
            AgentExecutionContext(identity=IdentityContext.local_default()),
        )
        assert result.status == "completed"
        assert result.output is not None
        return result.output


def _agents_root(tmp_path: Path) -> Path:
    """创建每个 scaffold 用例独占的 agents 根目录，避免生成包和注册表缓存跨测试污染。"""

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    return agents_dir


def _sqlite_dsn(path: Path) -> str:
    """生成 scaffold 运行时合同使用的异步 SQLite DSN，使 generated agent 有可验证的持久化环境。"""

    return f"sqlite+aiosqlite:///{path}"


__all__ = [
    "AgentExecutionContext",
    "AgentExecutionRequest",
    "AgentRegistry",
    "Any",
    "CliRunner",
    "EvalCaseFactory",
    "EvalRunner",
    "EvalService",
    "EvalTraceSource",
    "EventBus",
    "IdentityContext",
    "LocalJsonlEventSink",
    "Path",
    "ROOT",
    "RunOrchestrator",
    "SQLAlchemyStorage",
    "ScaffoldError",
    "ScoreSink",
    "_RegistryCaseExecutor",
    "_agents_root",
    "_sqlite_dsn",
    "app",
    "cast",
    "executor_rollback_preflight",
    "importlib",
    "json",
    "pytest",
    "run_migrations",
    "runner",
    "scaffold_agent_package",
    "scaffold_module",
    "sys",
    "yaml",
]
