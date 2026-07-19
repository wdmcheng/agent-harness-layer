"""三条 event 读取入口共用的 ownership 与只读 policy 合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from tests.contracts.test_runtime_checkpoint_runs_contracts import build_orchestrator

from agent_harness.identity import IdentityContext
from agent_harness.policy import (
    PolicyCheck,
    PolicyDeniedError,
    PolicyEngine,
    YamlPolicyProvider,
)


@pytest.mark.asyncio
async def test_run_read_authorization_never_repairs_missing_terminal_evidence(
    tmp_path: Path,
) -> None:
    """读取授权只能检查 ownership，不能借只读入口修补被截断的终态事件或改写存储。"""

    orchestrator, storage, events_path = await build_orchestrator(tmp_path)
    try:
        created = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "readonly-authorization"},
            idempotency_key="readonly-authorization",
        )
        lines = events_path.read_text(encoding="utf-8").splitlines()
        events_path.write_text(lines[0] + "\n", encoding="utf-8")
        before = events_path.read_bytes()

        authorization = await orchestrator.authorize_run_read(created.run_id)

        assert authorization.run_id == created.run_id
        assert authorization.tenant_id == "default"
        assert authorization.trace_id
        assert events_path.read_bytes() == before
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_run_read_authorization_hides_cross_tenant_run(tmp_path: Path) -> None:
    """跨租户读取应表现为不存在，避免通过授权端点枚举其他租户的运行标识。"""

    orchestrator, storage, _ = await build_orchestrator(tmp_path)
    try:
        created = await orchestrator.start_run(
            agent_id="fake-agent",
            input={"prompt": "cross-tenant"},
            idempotency_key="cross-tenant",
        )
        other_tenant = IdentityContext.local_default().model_copy(
            update={"tenant_id": "other-tenant"}
        )

        with pytest.raises(LookupError):
            await orchestrator.authorize_run_read(created.run_id, identity=other_tenant)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_internal_read_policy_uses_same_provider_without_audit_write() -> None:
    """内部事件读取复用同一策略 provider，但作为只读检查不能产生 audit evidence。"""

    class RejectAudit:
        """若只读授权错误写入审计即立即失败的替身，保护查询路径的无副作用约束。"""

        async def record(self, **_kwargs: Any) -> object:
            """拒绝所有审计写入，避免测试仅凭最终状态遗漏中途的写副作用。"""

            pytest.fail("read-only policy must not create audit evidence")

    identity = IdentityContext.local_default()
    engine = PolicyEngine(
        provider=YamlPolicyProvider.default(),
        audit=cast(Any, RejectAudit()),
    )

    result = await engine.require_allowed_readonly(
        PolicyCheck(
            actor=identity,
            action="events.read_internal",
            resource="run:run-1:events",
        )
    )

    assert result.decision == "allow"
    assert "audit_ref" not in result.metadata


@pytest.mark.asyncio
async def test_internal_read_policy_requires_explicit_allow() -> None:
    """internal event 读取不能把尚待审批的 decision 当成已有授权。"""

    identity = IdentityContext.local_default()
    engine = PolicyEngine(
        provider=YamlPolicyProvider(require_approval_actions={"events.read_internal"})
    )

    with pytest.raises(PolicyDeniedError):
        await engine.require_allowed_readonly(
            PolicyCheck(
                actor=identity,
                action="events.read_internal",
                resource="run:run-1:events",
            )
        )
