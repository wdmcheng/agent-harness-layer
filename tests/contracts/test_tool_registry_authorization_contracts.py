"""Tool registry 预检与 agent allowlist 合同测试。"""

from __future__ import annotations

from tests.contracts.test_tool_registry_contracts import (
    Any as Any,
)
from tests.contracts.test_tool_registry_contracts import (
    Path as Path,
)
from tests.contracts.test_tool_registry_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_tool_registry_contracts import (
    pytest as pytest,
)
from tests.contracts.test_tool_registry_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_tool_registry_contracts import (
    sqlite_dsn as sqlite_dsn,
)


@pytest.mark.asyncio
async def test_tool_registry_preflight_errors_are_not_masked_by_approval(
    tmp_path: Path,
) -> None:
    """Registry 最终 seam 必须先返回 disabled/allowlist_denied，再进入 approval。"""

    from agent_harness.artifacts import FileArtifactStore
    from agent_harness.audit import AuditService
    from agent_harness.config.schemas import (
        AgentConfig,
        HarnessSettings,
        MCPServerSettings,
        ModelSettings,
        ObservabilitySettings,
        PolicySettings,
        QueueSettings,
        ShellToolSettings,
        StorageSettings,
        ToolSettings,
    )
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider
    from agent_harness.tools import ToolCallRequest, ToolErrorCode, ToolRuntimeContext
    from agent_harness.tools.cli_runtime import build_tool_registry
    from agent_harness.tools.workspace import WorkspacePolicy

    settings = HarnessSettings(
        profile="local",
        storage=StorageSettings(kind="sqlite", dsn="sqlite+aiosqlite:///:memory:"),
        queue=QueueSettings(kind="in-memory"),
        policy=PolicySettings(provider="yaml"),
        model=ModelSettings(provider="fake"),
        observability=ObservabilitySettings(kind="local-jsonl"),
        tools=ToolSettings(
            shell=ShellToolSettings(enabled=False, allowlist=["cat"]),
            mcp_servers=[MCPServerSettings(name="demo", allowlist=[])],
        ),
        agent=AgentConfig(tool_allowlist=["shell.execute", "mcp.demo.unsafe"]),
    )
    db_path = tmp_path / "tool-preflight.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    audit = AuditService(storage=storage)
    policy = PolicyEngine(provider=YamlPolicyProvider.default(), audit=audit)
    registry = build_tool_registry(
        settings=settings,
        workspace_policy=WorkspacePolicy(root=tmp_path),
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        policy=policy,
        audit=audit,
        requested_tool_name="mcp.demo.unsafe",
    )
    context = ToolRuntimeContext(
        actor=settings.identity.default,
        agent_id="examples.basic",
    )

    try:
        shell_result = await registry.call(
            ToolCallRequest(
                tool_name="shell.execute",
                arguments={"command": "cat sample.txt"},
                agent_id="examples.basic",
            ),
            context=context,
        )
        mcp_result = await registry.call(
            ToolCallRequest(
                tool_name="mcp.demo.unsafe",
                arguments={},
                agent_id="examples.basic",
            ),
            context=context,
        )

        assert shell_result.status == "disabled"
        assert shell_result.error is not None
        assert shell_result.error.code == ToolErrorCode.DISABLED
        assert mcp_result.status == "denied"
        assert mcp_result.error is not None
        assert mcp_result.error.code == ToolErrorCode.ALLOWLIST_DENIED
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_tool_registry_enforces_agent_tool_allowlist(tmp_path: Path) -> None:
    """agent 级 tool_allowlist 必须由执行层强制，不能只停留在 agent descriptor。"""

    from agent_harness.artifacts import FileArtifactStore
    from agent_harness.audit import AuditService
    from agent_harness.identity import IdentityContext
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider
    from agent_harness.tools import (
        BuiltinTool,
        ToolCallRequest,
        ToolErrorCode,
        ToolRegistry,
        ToolRuntimeContext,
    )

    db_path = tmp_path / "tool-allowlist.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    audit = AuditService(storage=storage)
    policy = PolicyEngine(provider=YamlPolicyProvider.default(), audit=audit)
    artifacts = FileArtifactStore(tmp_path / "artifacts")

    def ok(arguments: dict[str, Any]) -> dict[str, str]:
        _ = arguments
        return {"ok": "true"}

    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="fake.allowed",
                action="tool.fake_allowed",
                resource="tool:fake.allowed",
                input_schema={"type": "object"},
                handler=ok,
            ),
            BuiltinTool(
                name="fake.denied_by_agent",
                action="tool.fake_denied",
                resource="tool:fake.denied_by_agent",
                input_schema={"type": "object"},
                handler=ok,
            ),
        ],
        policy=policy,
        audit=audit,
        artifact_store=artifacts,
        agent_tool_allowlist=["fake.allowed"],
        enforce_agent_tool_allowlist=True,
    )
    context = ToolRuntimeContext(
        actor=IdentityContext.local_default(),
        agent_id="examples.basic",
    )

    try:
        assert [descriptor.name for descriptor in registry.list_tools()] == ["fake.allowed"]
        result = await registry.call(
            ToolCallRequest(
                tool_name="fake.denied_by_agent",
                arguments={},
                agent_id="examples.basic",
            ),
            context=context,
        )
        assert result.status == "denied"
        assert result.error is not None
        assert result.error.code == ToolErrorCode.POLICY_DENIED
        assert result.truncation["truncated"] is False
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_tool_registry_empty_agent_allowlist_denies_every_tool(tmp_path: Path) -> None:
    """空 allowlist 表示无工具权限，不能被翻译成全部放行。"""

    from agent_harness.artifacts import FileArtifactStore
    from agent_harness.audit import AuditService
    from agent_harness.identity import IdentityContext
    from agent_harness.policy import PolicyEngine, YamlPolicyProvider
    from agent_harness.tools import (
        BuiltinTool,
        ToolCallRequest,
        ToolErrorCode,
        ToolRegistry,
        ToolRuntimeContext,
    )

    db_path = tmp_path / "tool-empty-allowlist.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    audit = AuditService(storage=storage)
    policy = PolicyEngine(provider=YamlPolicyProvider.default(), audit=audit)
    artifacts = FileArtifactStore(tmp_path / "artifacts")

    def ok(arguments: dict[str, Any]) -> dict[str, str]:
        _ = arguments
        return {"ok": "true"}

    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="fake.any",
                action="tool.fake_any",
                resource="tool:fake.any",
                input_schema={"type": "object"},
                handler=ok,
            )
        ],
        policy=policy,
        audit=audit,
        artifact_store=artifacts,
        agent_tool_allowlist=[],
        enforce_agent_tool_allowlist=True,
    )
    context = ToolRuntimeContext(
        actor=IdentityContext.local_default(),
        agent_id="examples.basic",
    )

    try:
        assert registry.list_tools() == []
        result = await registry.call(
            ToolCallRequest(tool_name="fake.any", arguments={}, agent_id="examples.basic"),
            context=context,
        )
        assert result.status == "denied"
        assert result.error is not None
        assert result.error.code == ToolErrorCode.POLICY_DENIED
    finally:
        await storage.dispose()
