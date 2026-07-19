"""真实 PostgreSQL/Redis 下 approval 与 delegation continuation 组合合同。"""

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
from agent_harness.events import CanonicalEventType
from agent_harness.identity import IdentityContext
from agent_harness.models import ModelDecision, ModelResponse
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import (
    AgentApprovalRequest,
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    RunStatus,
)
from agent_harness.runtime import services as runtime_services
from agent_harness.storage import run_migrations
from app import runtime as app_runtime
from app.workers import runtime_worker

pytestmark = pytest.mark.skipif(
    not (os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN") and os.environ.get("REDIS_TEST_DSN")),
    reason="service approval/delegation 合同需要真实 PostgreSQL 与 Redis。",
)


def _service_profiles(tmp_path: Path) -> Path:
    """复制 service 模板并仅打开测试所需的父子委派边，避免修改仓库内示例配置。"""

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


class _ApprovalDelegatingExecutor:
    """首次执行请求审批；持有 grant 后才从可信 context 发起 delegation。"""

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """首次执行稳定进入审批等待态，确保委派动作不会在授权前触达服务。"""

        del request, context
        return AgentExecutionResult.waiting(
            AgentApprovalRequest(
                action="agent.delegate",
                resource="agent:examples.ticket_triage",
                reason="delegation requires review",
                arguments_ref="artifact://service-delegation-arguments",
                arguments_hash="b" * 64,
                continuation={"kind": "delegation"},
            )
        )

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """获得 grant 后从可信 context 调用委派模块，并返回 parent 恢复所需的 child 关联坐标。"""

        del grant
        module = cast(Any, context.require_service("agent.delegate"))
        result = await module.delegate(
            AgentDelegateInput(
                target_agent_id="examples.ticket_triage",
                child_input={"text": str(request.input["text"])},
                idempotency_key=str(request.input["idempotency_key"]),
            )
        )
        return AgentExecutionResult.completed(
            {
                "delegation_id": result.delegation_id,
                "child_run_id": result.child_run_id,
            }
        )


class _ReportedCostFakeProvider:
    """提供固定已报告成本的模型替身，使 service 集成测试能验证预算与证据收敛而不访问供应商。"""

    provider_id = "fake"

    def complete(self, request: Any, *, model: str) -> ModelResponse:
        """返回确定性正文、token 和成本，消除模型结果差异对审批委派恢复顺序的干扰。"""

        return ModelResponse(
            provider=self.provider_id,
            model=model,
            output_text=f"fake:{request.prompt}",
            decision=ModelDecision(action="call", estimated_tokens=1),
            token_usage={"input_tokens": 3, "output_tokens": 2},
            latency_ms=1,
            cost_usd=0.25,
            cost_status="reported",
        )


@pytest.mark.asyncio
async def test_service_approval_delegation_resumes_parent_and_evidence_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """approval worker 可返回 waiting，最后一个 child 随后自动完成两类 evidence。"""

    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    profiles = _service_profiles(tmp_path)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    identity = IdentityContext(
        tenant_id=f"tenant-{uuid4()}",
        user_id="approval-delegation-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )
    reviewer = IdentityContext(
        tenant_id=identity.tenant_id,
        user_id="approval-delegation-reviewer",
        session_id=str(uuid4()),
        roles=["reviewer"],
        permissions=["*"],
        auth_method="api-key",
    )
    original_resolve = AgentRegistry.resolve_executor
    parent_executor = _ApprovalDelegatingExecutor()

    def resolve_executor(self: AgentRegistry, agent_id: str) -> Any:
        """只替换父 agent 的审批委派 executor，其余 agent 继续按真实 registry 解析。"""

        if agent_id == "examples.basic":
            return parent_executor
        return original_resolve(self, agent_id)

    monkeypatch.setattr(AgentRegistry, "resolve_executor", resolve_executor)
    monkeypatch.setattr(runtime_services, "FakeModelProvider", _ReportedCostFakeProvider)

    async with isolated_database("service_approval_delegation") as postgres_dsn:
        monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
        run_migrations(postgres_dsn)
        api = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        try:
            submitted = await api.orchestrator.submit_run(
                agent_id="examples.basic",
                input={
                    "text": "production outage",
                    "idempotency_key": "approval-delegation-parent",
                },
                identity=identity,
                request_id="request-approval-delegation-parent",
            )
        finally:
            await api.close()

        first_worker = await runtime_worker.run_once(
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
                approval = (await uow.approvals.list_by_run(submitted.run_id))[0]
            queued = await resolver.approval_service.approve(
                actor=reviewer,
                run_id=submitted.run_id,
                approval_id=approval.approval_id,
                request_id="request-approve-delegation",
            )
        finally:
            await resolver.close()

        second_worker = await runtime_worker.run_once(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        waiting_reader = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        try:
            async with waiting_reader.storage.uow() as uow:
                waiting_parent = await uow.runs.get(submitted.run_id)
                checkpoint = await uow.checkpoints.get_latest(submitted.run_id)
                claims = await uow.delegations.list_for_parent(
                    tenant_id=identity.tenant_id,
                    parent_run_id=submitted.run_id,
                )
                resolution_state = await uow.approvals.get_resolution_state(approval.approval_id)
        finally:
            await waiting_reader.close()

        assert first_worker == submitted.run_id
        assert queued.approval.status == "waiting"
        assert second_worker == submitted.run_id
        assert waiting_parent is not None and waiting_parent.status == RunStatus.WAITING.value
        assert checkpoint is not None and checkpoint.state["kind"] == "delegation_terminal"
        assert checkpoint.state["approval_recovery"]["approval_id"] == approval.approval_id
        assert resolution_state == "completed"
        assert len(claims) == 1 and claims[0].child_run_id is not None

        third_worker = await runtime_worker.run_once(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        final_reader = app_runtime.build_runtime_components(
            profile="service",
            profiles_dir=profiles,
            storage_dsn=postgres_dsn,
            artifact_root=tmp_path / "artifacts",
        )
        try:
            async with final_reader.storage.uow() as uow:
                parent = await uow.runs.get(submitted.run_id)
                final_approval = await uow.approvals.get(approval.approval_id)
                group = await uow.evidence_outbox.ordered_group(
                    group_id=f"approval:{approval.approval_id}:resolution"
                )
                group_states = {item.state for item in group}
                capacity = await uow.event_capacity.snapshot(submitted.run_id)
            events = await final_reader.event_sink.read(run_id=submitted.run_id)
            assert final_reader.queue is not None
            redelivery = await final_reader.queue.reclaim(
                consumer_id="approval-delegation-late-worker",
                min_idle_seconds=0,
            )
        finally:
            assert isinstance(final_reader.queue, RedisRunQueue)
            await final_reader.queue.cleanup_namespace()
            await final_reader.close()

    resolution_seq = next(
        event.seq for event in events if event.event_type == CanonicalEventType.APPROVAL_RESOLVED
    )
    terminal_seq = next(event.seq for event in events if event.terminal)
    assert third_worker == claims[0].child_run_id
    assert parent is not None and parent.status == RunStatus.COMPLETED.value
    assert final_approval is not None and final_approval.status == "approved"
    assert group_states == {"published"}
    assert capacity.outstanding_reserved_event_count == 0
    assert resolution_seq < terminal_seq
    assert sum(event.terminal for event in events) == 1
    assert redelivery is None
