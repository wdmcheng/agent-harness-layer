"""Phase 7 auth/policy/HITL contract test helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, cast

from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentToolPolicy,
)

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


def sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def descriptor(agent_id: str = "examples.basic") -> AgentDescriptor:
    return AgentDescriptor(
        agent_id=agent_id,
        version="0.1.0",
        name="Basic Example Agent",
        description="Offline fake model smoke agent.",
        input_schema_ref="agents.examples.basic.schemas.Input",
        output_schema_ref="agents.examples.basic.schemas.Output",
        config_ref="agents/examples/basic/config.yaml",
        tool_policy=AgentToolPolicy(allowed_tools=[]),
        model_policy=AgentModelPolicy(
            provider="fake",
            default_model="fake-basic",
            fallback_models=[],
        ),
        budget=AgentBudget(max_tokens_per_run=8192, max_cost_usd_per_run=None),
        eval_dataset="eval-cases/drafts/basic.yaml",
        delegation_targets=[],
    )


async def asgi_request(
    app: Callable[
        [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Any]],
        Awaitable[None],
    ],
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: Sequence[tuple[bytes, bytes]] = (),
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    raw_body = b"" if body is None else json.dumps(body).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw_body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    request_headers = [
        (b"x-request-id", b"req-phase7"),
        *list(headers),
    ]
    if body is not None:
        request_headers.append((b"content-type", b"application/json"))

    raw_path, _, query_string = path.partition("?")
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": query_string.encode(),
            "headers": request_headers,
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


def table_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as connection:
        return cast(int, connection.execute(f"select count(*) from {table}").fetchone()[0])


def table_json_payloads(db_path: Path, table: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(f"select payload_json from {table}").fetchall()
    return [cast(dict[str, Any], json.loads(row[0])) for row in rows]
