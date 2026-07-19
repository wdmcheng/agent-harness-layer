"""Agent registry、模型上下文和 embedding 的公开契约测试。"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import pytest

from agent_harness.contracts import ErrorDetail
from agent_harness.events import LocalJsonlEventSink
from agent_harness.runtime import RunOrchestrator
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def _agent_config(agent_id: str, *, delegation_edges: list[str] | None = None) -> str:
    """渲染最小有效 agent 配置，并仅允许测试覆盖 delegation target 列表。"""

    edges = delegation_edges or []
    edge_lines = "\n".join(f"  - {edge}" for edge in edges) or "  []"
    return f"""agent_id: {agent_id}
version: 0.1.0
name: Basic Example Agent
description: Offline fake model smoke agent.
input_schema: agents.examples.basic.schemas.Input
output_schema: agents.examples.basic.schemas.Output
executor: executor:executor
model:
  provider: fake
  default_model: fake-basic
  fallback_models: []
budget:
  max_tokens_per_run: 8192
  max_cost_usd_per_run: null
tool_allowlist: []
eval_dataset: eval-cases/drafts/basic.yaml
delegation_edges:
{edge_lines}
"""


def _write_agent_config(root: Path, relative: str, content: str) -> None:
    """写入可真实 import 的临时 agent 包，动态修正 schema 引用到测试命名空间。"""

    path = root / relative / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    namespace = root.resolve().name
    schema_module = f"{namespace}.{relative.replace('/', '.')}.schemas"
    rendered = content.replace(
        "agents.examples.basic.schemas.Input",
        f"{schema_module}.Input",
    ).replace(
        "agents.examples.basic.schemas.Output",
        f"{schema_module}.Output",
    )
    path.write_text(rendered, encoding="utf-8")
    (path.parent / "schemas.py").write_text(
        """from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    value: str = ""

class Output(HarnessDTO):
    result: str = "fixture-ok"
""",
        encoding="utf-8",
    )
    (path.parent / "executor.py").write_text(
        """from agent_harness.runtime import AgentExecutionResult

class Executor:
    async def run(self, request, context):
        return AgentExecutionResult.completed({"result": "fixture-ok"})
    async def resume(self, request, context, grant):
        return AgentExecutionResult.completed({"resumed": True})

executor = Executor()
""",
        encoding="utf-8",
    )


def sqlite_dsn(path: Path) -> str:
    """生成 registry/model/context 合同测试专用 SQLite DSN。"""

    return f"sqlite+aiosqlite:///{path}"


async def _asgi_get_json(
    app: Callable[
        [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Any]],
        Awaitable[None],
    ],
    path: str,
) -> tuple[int, dict[str, Any]]:
    """以最小 ASGI 调用执行 GET 并收集 JSON 响应，隔离测试与 HTTP 客户端实现。"""

    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        """提供单段空请求体，满足 ASGI 应用的 receive 协议。"""

        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        """按 ASGI 顺序收集响应帧，稍后由外层解析状态和完整 body。"""

        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"x-request-id", b"req-agents")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return cast(int, status), cast(dict[str, Any], json.loads(body))


async def _asgi_post_json(
    app: Callable[
        [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Any]],
        Awaitable[None],
    ],
    path: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """以最小 ASGI 调用执行 JSON POST，并保留 request-id/内容类型等公开边界。"""

    messages: list[dict[str, Any]] = []
    raw_body = json.dumps(body).encode()

    async def receive() -> dict[str, Any]:
        """提供一次性编码后的 JSON 请求体，不引入网络或第三方 client 行为。"""

        return {"type": "http.request", "body": raw_body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        """收集响应帧以便外层组合 body 并验证统一错误或成功封套。"""

        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [
                (b"x-request-id", b"req-run-agent"),
                (b"content-type", b"application/json"),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return cast(int, status), cast(dict[str, Any], json.loads(response_body))


__all__ = [
    "Any",
    "Awaitable",
    "Callable",
    "ErrorDetail",
    "LocalJsonlEventSink",
    "Path",
    "ROOT",
    "RunOrchestrator",
    "_agent_config",
    "_asgi_get_json",
    "_asgi_post_json",
    "_write_agent_config",
    "cast",
    "create_app",
    "json",
    "pytest",
    "sqlite_dsn",
    "subprocess",
    "sys",
    "tomllib",
]
