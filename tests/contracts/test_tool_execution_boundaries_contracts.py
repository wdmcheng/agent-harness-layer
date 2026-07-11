"""Phase 8 工具执行边界合同测试。

这些测试先锁公开 seam：API-Contract 文本、OpenAPI 无未声明 tools route、
storage migration、ToolRegistry/FileTool/ShellTool/MCP DTO 和 import boundary。
实现可以重构内部结构，但不能让调用方绕过这些边界。
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn

from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]
API_CONTRACT = ROOT / "API-Contract.md"
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"

EXPECTED_TOOL_ERROR_CODES = {
    "tool.not_found",
    "tool.schema_validation_failed",
    "tool.policy_denied",
    "tool.approval_required",
    "tool.disabled",
    "tool.timeout",
    "tool.workspace_denied",
    "tool.allowlist_denied",
    "tool.execution_failed",
}


def test_api_contract_declares_phase8_tool_seam_and_no_http_route() -> None:
    """文档必须先固定 Phase 8 CLI/runtime seam，再允许实现代码进入。"""

    text = API_CONTRACT.read_text(encoding="utf-8")

    for marker in [
        "### 5.19 `ToolCallRequest`",
        "### 5.20 `ToolCallResult`",
        "### 5.21 Tool execution error codes",
        "## 9. Tool Execution CLI / Runtime Seam",
        "`TLS-001`",
        "`TLS-002`",
        "`TLS-003`",
        "`agent-harness tools list/call`",
        "当前 OpenAPI 不得出现未记录的 `/api/v1/tools` route",
        "`ToolCallResult.result.stdout_ref` / `ToolCallResult.result.stderr_ref`",
    ]:
        assert marker in text

    for code in EXPECTED_TOOL_ERROR_CODES:
        assert f"`{code}`" in text


def test_openapi_does_not_expose_undocumented_tools_route() -> None:
    """Phase 8 当前只开放 CLI/runtime/module seam，不偷偷加 HTTP tools route。"""

    app = create_app(registry=cast(Any, object()))
    paths = set(app.openapi()["paths"])

    assert "/api/v1/tools" not in paths
    assert not any(path.startswith("/api/v1/tools/") for path in paths)


def test_local_migration_creates_workspace_and_tool_invocation_tables(tmp_path: Path) -> None:
    """SQLite migration 证据只证明 local schema；PostgreSQL 由 service smoke 单独证明。"""

    db_path = tmp_path / "tool-boundaries.db"
    run_migrations(sqlite_dsn(db_path))

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("select name from sqlite_master where type='table'").fetchall()
        tables = {row[0] for row in rows}
        revision = connection.execute("select version_num from alembic_version").fetchone()

    assert {"workspaces", "tool_invocations"} <= tables
    assert revision == ("0011_eval_experiment_legacy_created_review",)


@pytest.mark.asyncio
async def test_workspace_and_tool_invocation_repository_round_trip(tmp_path: Path) -> None:
    """repository/UoW 是工具持久化公开接缝，调用方不直接碰 ORM session。"""

    from agent_harness.storage import ToolInvocationCreate, WorkspaceCreate

    db_path = tmp_path / "tool-repository.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("default")
            workspace = await uow.workspaces.create(
                WorkspaceCreate(
                    tenant_id="default",
                    agent_id="examples.basic",
                    run_id=None,
                    root_path=str(tmp_path / "workspace"),
                    policy_ref="policy:default",
                    metadata={"source": "contract-test"},
                )
            )
            invocation = await uow.tool_invocations.create(
                ToolInvocationCreate(
                    tenant_id="default",
                    agent_id="examples.basic",
                    run_id=None,
                    tool_name="shell.execute",
                    args_ref="artifact://args",
                    result_ref="artifact://result",
                    status="completed",
                    duration_ms=12,
                    trace_id="trace-tool",
                    request_id="req-tool",
                    metadata={"stdout_ref": "artifact://stdout"},
                )
            )
            await uow.commit()

        async with storage.uow() as uow:
            loaded_workspace = await uow.workspaces.get(workspace.id)
            loaded_invocation = await uow.tool_invocations.get(invocation.id)

        assert loaded_workspace is not None
        assert loaded_workspace.root_path == str(tmp_path / "workspace")
        assert loaded_workspace.policy_ref == "policy:default"
        assert loaded_invocation is not None
        assert loaded_invocation.tool_name == "shell.execute"
        assert loaded_invocation.args_ref == "artifact://args"
        assert loaded_invocation.result_ref == "artifact://result"
        assert loaded_invocation.metadata["stdout_ref"] == "artifact://stdout"
    finally:
        await storage.dispose()


def test_mcp_sdk_import_is_adapter_only() -> None:
    """官方 MCP SDK 只能藏在 adapter 后面，核心 tool/runtime/template 不直接 import。"""

    from scripts.import_boundary_check import check_python_imports

    from agent_harness.contracts.boundaries import BANNED_VENDOR_IMPORTS

    assert "mcp" in BANNED_VENDOR_IMPORTS
    assert check_python_imports() == []
    adapter = importlib.import_module("agent_harness.adapters.mcp.python_sdk")
    assert hasattr(adapter, "PythonMCPClient")


@pytest.mark.asyncio
async def test_mcp_sdk_adapter_supports_sse_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP/SSE 字面合同必须由官方 SDK adapter 支持。"""

    from agent_harness.adapters.mcp.python_sdk import PythonMCPClient

    calls: list[str] = []
    initialized: list[bool] = []

    class FakeClientSession:
        def __init__(self, read_stream: object, write_stream: object) -> None:
            assert read_stream == "read"
            assert write_stream == "write"

        async def __aenter__(self) -> FakeClientSession:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: object,
        ) -> None:
            return None

        async def initialize(self) -> None:
            initialized.append(True)

    class FakeMCPModule(types.ModuleType):
        ClientSession = FakeClientSession

    class FakeSSEModule(types.ModuleType):
        sse_client: Any = None

    @asynccontextmanager
    async def fake_sse_client(url: str):
        calls.append(url)
        yield "read", "write"

    mcp_module = FakeMCPModule("mcp")
    sse_module = FakeSSEModule("mcp.client.sse")
    sse_module.sse_client = fake_sse_client
    monkeypatch.setitem(sys.modules, "mcp", mcp_module)
    monkeypatch.setitem(sys.modules, "mcp.client.sse", sse_module)

    client = PythonMCPClient(server_name="demo", transport="sse", url="http://mcp.test/sse")
    async with client:
        pass

    assert calls == ["http://mcp.test/sse"]
    assert initialized == [True]
