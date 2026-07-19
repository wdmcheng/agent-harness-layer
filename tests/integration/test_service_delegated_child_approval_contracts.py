"""真实 PostgreSQL/Redis 下 delegated child approval 终态聚合合同。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.adapters.queue import RedisRunQueue
from agent_harness.delegation import AgentDelegateInput
from agent_harness.identity import IdentityContext
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import (
    AgentApprovalRequest,
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    RunStatus,
)
from agent_harness.storage import run_migrations
from app import runtime as app_runtime
from app.workers import runtime_worker

pytestmark = pytest.mark.skipif(
    not (os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN") and os.environ.get("REDIS_TEST_DSN")),
    reason="delegated child approval 合同需要真实 PostgreSQL 与 Redis。",
)


def _service_profiles(tmp_path: Path) -> Path:
    """复制 service 模板并只打开父到子委派边，避免污染仓库内的示例配置。"""

    source = Path(__file__).resolve().parents[2] / "templates" / "service-app"
    target = tmp_path / "service-app"
    shutil.copytree(source, target)
    config = target / "agents" / "examples" / "basic" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "delegation_edges: []",
            "delegation_edges:\n  - examples.ticket_triage",
        ),
        encoding="utf-8",
    )
    return target / "configs" / "profiles"


class _DelegatingParentExecutor:
    """父运行执行器替身：只委派一个子运行，用来隔离审批后的聚合路径。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """通过公开委派服务创建固定 idempotency key 的子运行，并返回其标识供父运行收敛。"""

        module = cast(Any, context.require_service("agent.delegate"))
        result = await module.delegate(
            AgentDelegateInput(
                target_agent_id="examples.ticket_triage",
                child_input={"text": str(request.input["text"])},
                idempotency_key="delegated-child-approval",
            )
        )
        return AgentExecutionResult.completed({"child_run_id": result.child_run_id})


class _ApprovalChildExecutor:
    """子运行执行器替身：首次稳定等待审批，恢复时可按参数完成或确定性失败。"""

    def __init__(self, *, fail_resume: bool) -> None:
        """保存恢复分支开关，使同一集成流程覆盖成功和失败终态聚合。"""

        self.fail_resume = fail_resume

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """始终返回审批等待态，确保后续 worker 消费的是 continuation 而非普通执行请求。"""

        del request, context
        return AgentExecutionResult.waiting(
            AgentApprovalRequest(
                action="agent.execute",
                resource="agent:examples.ticket_triage",
                reason="child requires review",
                arguments_ref="artifact://delegated-child-approval",
                arguments_hash="c" * 64,
                continuation={"kind": "delegated_child"},
            )
        )

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """模拟已获授权的 continuation：失败分支必须仍把子运行及预算聚合为需人工复核。"""

        del request, context, grant
        if self.fail_resume:
            raise RuntimeError("deterministic delegated child approval failure")
        return AgentExecutionResult.completed({"approved": True})


@pytest.mark.parametrize("fail_resume", [False, True])
@pytest.mark.asyncio
async def test_service_child_approval_terminal_reconciles_before_ack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fail_resume: bool,
) -> None:
    """approval handler 不走普通 execute handler，也必须生成 child aggregate。"""

    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="delegated-child-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )
    reviewer = IdentityContext(
        tenant_id=identity.tenant_id,
        user_id="delegated-child-reviewer",
        session_id=str(uuid4()),
        roles=["reviewer"],
        permissions=["*"],
        auth_method="api-key",
    )
    original_resolve = AgentRegistry.resolve_executor
    parent_executor = _DelegatingParentExecutor()
    child_executor = _ApprovalChildExecutor(fail_resume=fail_resume)

    def resolve_executor(self: AgentRegistry, agent_id: str) -> Any:
        """仅替换本合同涉及的两个 executor，其余 agent 继续复用真实 registry 解析规则。"""

        if agent_id == "examples.basic":
            return parent_executor
        if agent_id == "examples.ticket_triage":
            return child_executor
        return original_resolve(self, agent_id)

    monkeypatch.setattr(AgentRegistry, "resolve_executor", resolve_executor)

    async with isolated_database(f"delegated_child_approval_{fail_resume}") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        api = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        try:
            parent = await api.orchestrator.submit_run(
                agent_id="examples.basic",
                input={"text": "delegate with child approval"},
                identity=identity,
                request_id="request-delegated-child-parent",
            )
        finally:
            await api.close()

        assert (
            await runtime_worker.run_once(
                profile="service",
                profiles_dir=profiles,
                storage_dsn=postgres_dsn,
                artifact_root=tmp_path / "artifacts",
            )
            == parent.run_id
        )
        child_worker_run_id = await runtime_worker.run_once(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        resolver = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        try:
            async with resolver.storage.uow() as uow:
                approval = (await uow.approvals.list_by_run(child_worker_run_id))[0]
                claim = (
                    await uow.delegations.list_for_parent(
                        tenant_id=identity.tenant_id,
                        parent_run_id=parent.run_id,
                    )
                )[0]
            await resolver.approval_service.approve(
                actor=reviewer,
                run_id=child_worker_run_id,
                approval_id=approval.approval_id,
                request_id="request-delegated-child-approve",
            )
        finally:
            await resolver.close()

        assert (
            await runtime_worker.run_once(
                profile="service",
                profiles_dir=profiles,
                storage_dsn=postgres_dsn,
                artifact_root=tmp_path / "artifacts",
            )
            == child_worker_run_id
        )
        reader = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        try:
            async with reader.storage.uow() as uow:
                child = await uow.runs.get(child_worker_run_id)
                refreshed_claim = await uow.delegations.get(claim.id)
                aggregates = await uow.delegations.list_aggregates_for_parent(
                    tenant_id=identity.tenant_id,
                    parent_run_id=parent.run_id,
                )
                reservation = await uow.delegations.get_reservation(claim.id)
            assert reader.queue is not None
            redelivery = await reader.queue.reclaim(
                consumer_id="delegated-child-late-worker",
                min_idle_seconds=0,
            )
        finally:
            assert isinstance(reader.queue, RedisRunQueue)
            await reader.queue.cleanup_namespace()
            await reader.close()

    assert child is not None
    assert child.status == (RunStatus.FAILED.value if fail_resume else RunStatus.COMPLETED.value)
    assert refreshed_claim is not None and refreshed_claim.status == "needs_review"
    assert len(aggregates) == 1 and aggregates[0].status == "needs_review"
    assert reservation is not None and reservation.state == "needs_review"
    assert redelivery is None
