"""模型策略、审计与既有 approval continuation 的组合合同。"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import cast

from tests.contracts.provider_neutral_structured_output_test_support import (
    fixture_output_schema_identity,
)
from tests.contracts.test_controlled_real_model_config_contracts import PROFILES

from agent_harness.approvals import ApprovalService
from agent_harness.audit import AuditService
from agent_harness.config import load_settings
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    BoundModelInvocationService,
    FakeModelProvider,
    FakeModelStreamScript,
    ModelApprovalRequired,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    RunOrchestrator,
)
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.shared_budget import DirectBudgetClaim, OperationIdentity


class AuditAwareFakeProvider(FakeModelProvider):
    """发送前检查 policy audit 已耐久化，并记录实际 provider 次数。"""

    def __init__(
        self,
        database: Path,
        *,
        stream_script: FakeModelStreamScript | None = None,
    ) -> None:
        super().__init__(stream_script=stream_script)
        self.database = database
        self.calls = 0

    def _assert_policy_audit(self) -> None:
        """provider 任一调用形态都必须发生在 policy audit 已提交之后。"""

        with sqlite3.connect(self.database) as connection:
            actions = [row[0] for row in connection.execute("select action from audit_logs")]
        assert "policy.decision" in actions

    async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
        """如果策略审计尚未提交就立即失败，锁定 audit → provider 顺序。"""

        self._assert_policy_audit()
        self.calls += 1
        return await super().complete(request, plan=plan)

    async def prepare_stream(self, request: ModelRequest, *, plan: object):  # type: ignore[no-untyped-def]
        """流式 adapter 复用相同 audit 顺序，并以 prepare 次数表示外部调用次数。"""

        self._assert_policy_audit()
        self.calls += 1
        return await super().prepare_stream(request, plan=plan)


class ModelApprovalExecutor:
    """把 ModelApprovalRequired 交回既有 AgentExecutionResult.waiting 状态机。"""

    def __init__(self, *, streaming: bool = False) -> None:
        self.resume_calls = 0
        self.bound_model: BoundModelInvocationService | None = None
        self.streaming = streaming

    def _request(self) -> ModelRequest:
        """普通与增量审批复用同一受控 prompt，只切换公开 capability。"""

        return ModelRequest(
            provider="fake",
            capability="text_stream" if self.streaming else "text_completion",
            prompt="需要审批",
            max_output_tokens=2,
        )

    @staticmethod
    def _model(context: AgentExecutionContext) -> BoundModelInvocationService:
        return cast(
            BoundModelInvocationService,
            context.require_service("model_invocation"),
        )

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """首次执行若需审批，只返回标准 waiting DTO，不触发 provider。"""

        self.bound_model = self._model(context)
        try:
            if self.streaming:
                response = await self.bound_model.stream(
                    self._request(), operation_key="primary-model-call"
                )
            else:
                response = await self.bound_model.complete(
                    self._request(), operation_key="primary-model-call"
                )
        except ModelApprovalRequired as exc:
            return AgentExecutionResult.waiting(exc.request)
        return AgentExecutionResult.completed({"text": response.output_text})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """续跑只使用既有全绑定 grant 的 approved seam。"""

        self.resume_calls += 1
        if self.streaming:
            response = await self._model(context).stream_approved(
                self._request(), operation_key="primary-model-call", grant=grant
            )
        else:
            response = await self._model(context).complete_approved(
                self._request(), operation_key="primary-model-call", grant=grant
            )
        return AgentExecutionResult.completed({"text": response.output_text})


async def policy_flow(
    tmp_path: Path,
    *,
    require_approval: bool,
    database_stem: str = "model-policy",
    streaming: bool = False,
    stream_script: FakeModelStreamScript | None = None,
) -> tuple[
    SQLAlchemyStorage,
    ApprovalService,
    RunOrchestrator,
    IdentityContext,
    AuditAwareFakeProvider,
    ModelApprovalExecutor,
]:
    """组装共享 PolicyEngine/AuditService/ApprovalService 的离线真实流程。"""

    database = tmp_path / f"{database_stem}.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / f"{database_stem}-events.jsonl")
    event_bus = EventBus(sink=sink, capacity_storage=storage)
    identity = IdentityContext.local_default(session_id="model-policy-contract")
    audit = AuditService(storage)
    policy = PolicyEngine(
        provider=YamlPolicyProvider(
            require_approval_actions=["model.invoke"] if require_approval else []
        ),
        audit=audit,
    )
    provider = AuditAwareFakeProvider(database, stream_script=stream_script)
    router_config = ModelRouterConfig(
        default_provider="fake",
        default_model="fake-basic",
        max_tokens_per_call=64,
        input_token_price_usd=Decimal("0"),
        output_token_price_usd=Decimal("0"),
        price_source_ref="catalog:fake",
        price_source_version="catalog-v1",
    )
    registry = AgentRegistry(
        [
            AgentDescriptor(
                agent_id="agent-a",
                version="v1",
                name="审批预算合同 Agent",
                description="只使用离线 fake provider 验证审批续跑",
                input_schema_ref="fixture.Input",
                output_schema_ref="fixture.Output",
                output_schema_identity=fixture_output_schema_identity(),
                config_ref="fixture/config.yaml",
                tool_policy=AgentToolPolicy(allowed_tools=[]),
                model_policy=AgentModelPolicy(
                    deployment_id="fake_default",
                    provider="fake",
                    allowed_models=["fake-basic"],
                    default_model="fake-basic",
                    fallback_models=[],
                ),
                budget=AgentBudget(max_tokens_per_run=64, max_cost_usd_per_run=None),
                eval_dataset=None,
                delegation_targets=[],
            )
        ]
    )
    shared_budget = SharedBudgetRuntime(
        settings=load_settings(profile="local", profiles_dir=PROFILES),
        registry=registry,
        model_config=router_config,
    )
    invocation = ModelInvocationService(
        router=ModelRouter(
            config=router_config,
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=event_bus,
        policy_engine=policy,
        shared_budget=shared_budget,
    )
    executor = ModelApprovalExecutor(streaming=streaming)
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=event_bus,
        identity=identity,
        executor_resolver=lambda _agent_id: executor,
        executor_services={
            "model_invocation": invocation,
            "shared_budget": shared_budget,
        },
    )
    approval = ApprovalService(
        storage=storage,
        event_bus=event_bus,
        orchestrator=orchestrator,
        audit=audit,
    )
    return storage, approval, orchestrator, identity, provider, executor


async def reserve_competing_budget(
    storage: SQLAlchemyStorage,
    *,
    run_id: str,
    tokens: int,
    usage_call_id: str,
) -> int:
    """在 approval 等待窗口写入另一笔真实父账本预留，并返回当前影响量。"""

    async with storage.uow() as uow:
        ledger = await uow.shared_budget.get_ledger("default", run_id)
        assert ledger is not None
        identity = OperationIdentity.from_semantic_request(
            tenant_id="default",
            fingerprint_key=b"approval-budget-contract-key",
            fingerprint_key_version="test-v1",
            ownership_kind="direct",
            run_id=run_id,
            agent_id="agent-a",
            delegation_claim_id=None,
            usage_kind="model",
            operation_slot=usage_call_id,
            semantic_request={"kind": "competing-budget", "tokens": tokens},
            tree_snapshot_id=ledger.snapshot_id,
            agent_sub_snapshot_id=f"{ledger.snapshot_id}:agent-a",
            provider="fake",
            model="fake-basic",
            price_source_ref="catalog:fake",
            price_source_version="catalog-v1",
            cache_key_digest=None,
            cost_enabled=False,
            trusted_token_bound=tokens,
            trusted_cost_bound=None,
        )
        await uow.shared_budget.claim_direct(
            DirectBudgetClaim(
                tenant_id="default",
                budget_owner_run_id=run_id,
                usage_call_id=usage_call_id,
                identity=identity,
                token_reservation=tokens,
                cost_reservation=None,
            )
        )
        await uow.shared_budget.mark_direct_started(
            tenant_id="default",
            budget_owner_run_id=run_id,
            usage_call_id=usage_call_id,
        )
        await uow.shared_budget.settle_direct(
            tenant_id="default",
            budget_owner_run_id=run_id,
            usage_call_id=usage_call_id,
            actual_tokens=tokens,
            actual_cost=None,
            cost_status="unavailable",
            result={"outcome": "completed", "source": "competing-budget-contract"},
        )
        await uow.commit()
    async with storage.uow() as uow:
        updated = await uow.shared_budget.get_ledger("default", run_id)
        assert updated is not None
        return updated.token_impact
