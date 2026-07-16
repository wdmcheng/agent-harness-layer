"""Tool registry 公开 seam、策略与输出元数据合同测试。"""

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
    seed_persisted_run as seed_persisted_run,
)
from tests.contracts.test_tool_registry_contracts import (
    sqlite_dsn as sqlite_dsn,
)


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
