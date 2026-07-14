"""ToolRegistry 合同测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn
from tests.contracts.run_trace_contract_helpers import seed_persisted_run

from agent_harness.storage import SQLAlchemyStorage, run_migrations


@pytest.mark.asyncio
async def test_tool_registry_public_seam_enforces_errors_policy_and_output_metadata(
    tmp_path: Path,
) -> None:
    """ToolRegistry 必须统一错误码、policy、audit 和 output metadata。"""

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

    db_path = tmp_path / "tool-registry.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    audit = AuditService(storage=storage)
    policy = PolicyEngine(
        provider=YamlPolicyProvider(
            require_approval_actions={"shell.execute"},
            deny_actions={"tool.denied"},
        ),
        audit=audit,
    )
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    identity = IdentityContext.local_default()
    run_id = await seed_persisted_run(storage, trace_id="trace-tool")

    def echo(arguments: dict[str, Any]) -> dict[str, Any]:
        return {"text": arguments["text"]}

    def blocked(arguments: dict[str, Any]) -> dict[str, str]:
        _ = arguments
        return {"text": "blocked"}

    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="fake.echo",
                action="tool.fake_echo",
                resource="tool:fake.echo",
                input_schema={
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
                handler=echo,
            ),
            BuiltinTool(
                name="fake.needs_approval",
                action="shell.execute",
                resource="tool:fake.needs_approval",
                input_schema={"type": "object"},
                handler=blocked,
            ),
            BuiltinTool(
                name="fake.denied",
                action="tool.denied",
                resource="tool:fake.denied",
                input_schema={"type": "object"},
                handler=blocked,
            ),
        ],
        policy=policy,
        audit=audit,
        artifact_store=artifacts,
        inline_result_bytes=128,
    )
    context = ToolRuntimeContext(
        actor=identity,
        agent_id="examples.basic",
        run_id=run_id,
        request_id="req-tool",
        trace_id="trace-tool",
    )

    try:
        descriptors = registry.list_tools()
        assert descriptors[0].to_payload()["name"]
        assert not hasattr(descriptors[0], "handler")

        missing = await registry.call(
            ToolCallRequest(tool_name="fake.missing", arguments={}, agent_id="examples.basic"),
            context=context,
        )
        invalid = await registry.call(
            ToolCallRequest(tool_name="fake.echo", arguments={}, agent_id="examples.basic"),
            context=context,
        )
        allowed = await registry.call(
            ToolCallRequest(
                tool_name="fake.echo",
                arguments={"text": "hello"},
                agent_id="examples.basic",
            ),
            context=context,
        )
        denied = await registry.call(
            ToolCallRequest(tool_name="fake.denied", arguments={}, agent_id="examples.basic"),
            context=context,
        )
        approval = await registry.call(
            ToolCallRequest(
                tool_name="fake.needs_approval",
                arguments={},
                agent_id="examples.basic",
            ),
            context=context,
        )

        assert missing.error is not None
        assert missing.error.code == ToolErrorCode.NOT_FOUND
        assert missing.status == "failed"
        assert missing.truncation["truncated"] is False
        assert invalid.error is not None
        assert invalid.error.code == ToolErrorCode.SCHEMA_VALIDATION_FAILED
        assert invalid.status == "failed"
        assert invalid.truncation["truncated"] is False
        assert allowed.status == "completed"
        assert allowed.result == {"text": "hello"}
        assert allowed.source_ref.startswith("tool://fake.echo/")
        assert allowed.trust_level == "untrusted"
        assert allowed.policy["decision"] == "allow"
        assert allowed.invocation_id
        assert denied.error is not None
        assert denied.error.code == ToolErrorCode.POLICY_DENIED
        assert denied.status == "denied"
        assert denied.truncation["truncated"] is False
        assert approval.error is not None
        assert approval.error.code == ToolErrorCode.APPROVAL_REQUIRED
        assert approval.status == "requires_approval"
        assert approval.truncation["truncated"] is False

        async with storage.uow() as uow:
            audit_records = await uow.audit_logs.list_for_tenant("default")
            audit_count = len(audit_records)
        assert audit_count >= 5
        assert any(record.payload.get("trace_id") == "trace-tool" for record in audit_records)
        assert any(record.payload.get("request_id") == "req-tool" for record in audit_records)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_mcp_builtin_tools_use_connect_policy_action(tmp_path: Path) -> None:
    """MCP 工具默认动作必须命中 `mcp.connect` 审批策略。"""

    from agent_harness.artifacts import FileArtifactStore
    from agent_harness.audit import AuditService
    from agent_harness.config.schemas import (
        HarnessSettings,
        MCPServerSettings,
        ModelSettings,
        ObservabilitySettings,
        PolicySettings,
        QueueSettings,
        StorageSettings,
        ToolSettings,
    )
    from agent_harness.policy import PolicyCheck, PolicyEngine, YamlPolicyProvider
    from agent_harness.tools import WorkspacePolicy
    from agent_harness.tools.cli_runtime import builtin_tools

    settings = HarnessSettings(
        profile="local",
        storage=StorageSettings(kind="sqlite", dsn="sqlite+aiosqlite:///:memory:"),
        queue=QueueSettings(kind="in-memory"),
        policy=PolicySettings(provider="yaml"),
        model=ModelSettings(provider="fake"),
        observability=ObservabilitySettings(kind="local-jsonl"),
        tools=ToolSettings(mcp_servers=[MCPServerSettings(name="demo", allowlist=["unsafe"])]),
    )
    db_path = tmp_path / "mcp-policy.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        audit = AuditService(storage=storage)
        policy = PolicyEngine(provider=YamlPolicyProvider.default(), audit=audit)
        tools = builtin_tools(
            settings=settings,
            workspace_policy=WorkspacePolicy(root=tmp_path),
            artifact_store=FileArtifactStore(tmp_path / "artifacts"),
            policy=policy,
            requested_tool_name="mcp.demo.unsafe",
        )
        mcp_tool = next(tool for tool in tools if tool.name == "mcp.demo.unsafe")
        assert mcp_tool.action == "mcp.connect"
        decision = await policy.evaluate(
            PolicyCheck(
                actor=settings.identity.default,
                action=mcp_tool.action,
                resource=mcp_tool.resource,
            )
        )
        assert decision.decision == "require_approval"
    finally:
        await storage.dispose()


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


@pytest.mark.asyncio
async def test_tool_registry_execution_failed_redacts_error_message(tmp_path: Path) -> None:
    """provider/tool 原始异常里的 secret 不得进入 ToolCallResult.error.message。"""

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

    db_path = tmp_path / "tool-error-redaction.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    audit = AuditService(storage=storage)
    policy = PolicyEngine(provider=YamlPolicyProvider.default(), audit=audit)
    artifacts = FileArtifactStore(tmp_path / "artifacts")

    def fail(arguments: dict[str, Any]) -> dict[str, str]:
        _ = arguments
        raise RuntimeError("provider failed with api_key=sk-1234567890")

    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="fake.fail",
                action="tool.fake_fail",
                resource="tool:fake.fail",
                input_schema={"type": "object"},
                handler=fail,
            )
        ],
        policy=policy,
        audit=audit,
        artifact_store=artifacts,
    )
    context = ToolRuntimeContext(
        actor=IdentityContext.local_default(),
        agent_id="examples.basic",
    )

    try:
        result = await registry.call(
            ToolCallRequest(tool_name="fake.fail", arguments={}, agent_id="examples.basic"),
            context=context,
        )
        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == ToolErrorCode.EXECUTION_FAILED
        assert "sk-1234567890" not in result.error.message
        assert "[REDACTED]" in result.error.message
    finally:
        await storage.dispose()
