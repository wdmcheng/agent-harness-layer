"""Tool registry 执行失败脱敏合同测试。"""

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
