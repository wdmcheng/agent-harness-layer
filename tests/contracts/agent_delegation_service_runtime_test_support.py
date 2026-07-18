"""DelegationService 合同的 runtime、registry 与 storage 共享夹具。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from agent_harness.contracts import GuardrailDecisionStatus
from agent_harness.delegation.service import DelegationMode, DelegationService
from agent_harness.events import (
    EventBus,
    EventSink,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
)
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelDecision,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.policy import PolicyCheck, PolicyEvaluation
from agent_harness.registry import (
    AgentBudget,
    AgentDescriptor,
    AgentModelPolicy,
    AgentRegistry,
    AgentToolPolicy,
)
from agent_harness.runtime import RunDetailResult, RunResult, RunStatus
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.repositories import RunCreate, SessionCreate
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver
from agent_harness.storage.shared_budget import LedgerCreate, OperationIdentity
from agent_harness.storage.shared_budget_models import ParentBudgetLedgerModel


def _descriptor(
    agent_id: str,
    *,
    targets: list[str],
    max_tokens: int = 100,
    max_cost_usd: float | None = 10.0,
) -> AgentDescriptor:
    return AgentDescriptor(
        agent_id=agent_id,
        version="1",
        name=agent_id,
        description="delegation contract agent",
        input_schema_ref="schemas/input.json",
        output_schema_ref="schemas/output.json",
        config_ref=f"agents/{agent_id}/config.yaml",
        tool_policy=AgentToolPolicy(allowed_tools=["agent.delegate"]),
        model_policy=AgentModelPolicy(
            provider="fake",
            default_model="fake-basic",
            fallback_models=[],
        ),
        budget=AgentBudget(
            max_tokens_per_run=max_tokens,
            max_cost_usd_per_run=max_cost_usd,
        ),
        eval_dataset=None,
        delegation_targets=targets,
    )


def _identity(*, permissions: list[str] | None = None) -> IdentityContext:
    return IdentityContext(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
        roles=["operator"],
        permissions=permissions if permissions is not None else ["agent.delegate"],
        auth_method="api-key",
    )


class _Policy:
    async def evaluate(self, check: PolicyCheck) -> PolicyEvaluation:
        decision = (
            GuardrailDecisionStatus.ALLOW.value
            if "agent.delegate" in check.actor.permissions
            else GuardrailDecisionStatus.DENY.value
        )
        return PolicyEvaluation(
            decision=decision,
            reason="contract policy",
            actor=check.actor,
            action=check.action,
            resource=check.resource,
        )


class _SharedBudgetRuntimeFixture:
    """让 delegation 合同夹具走与真实 composition 相同的 allocation seam。"""

    def operation_identity(self, **values: Any) -> OperationIdentity:
        return OperationIdentity.from_semantic_request(
            fingerprint_key=b"delegation-contract-budget-key",
            fingerprint_key_version="delegation-contract-v1",
            **values,
        )

    model_router_config = SharedBudgetRuntime.model_router_config


class _ParentDetailOrchestrator:
    async def get_run_detail(self, run_id: str, **_: object) -> RunDetailResult:
        return RunDetailResult(
            run_id=run_id,
            agent_id="agent-source",
            status=RunStatus.RUNNING,
            terminal_event=None,
            parent_run_id=None,
        )


class _InlineChildRuntime:
    """只模拟 runtime 持久化 child；usage 缺失应由 service 保持 needs_review。"""

    def __init__(
        self,
        storage: SQLAlchemyStorage,
        *,
        usage_service: ModelInvocationService | None = None,
        launch_error: bool = False,
        child_status: RunStatus = RunStatus.COMPLETED,
    ) -> None:
        self.storage = storage
        self.usage_service = usage_service
        self.launch_error = launch_error
        self.child_status = child_status
        self.calls = 0

    async def start_run(self, **kwargs: Any) -> RunResult:
        self.calls += 1
        if self.launch_error:
            raise RuntimeError("deterministic child creation failure")
        identity = kwargs["identity"]
        async with self.storage.uow() as uow:
            session = await uow.sessions.ensure(
                SessionCreate(
                    session_id=identity.session_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    agent_id=kwargs["agent_id"],
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=identity.tenant_id,
                    session_id=session.id,
                    agent_id=kwargs["agent_id"],
                    idempotency_key=str(kwargs["idempotency_key"]),
                    parent_run_id=kwargs["parent_run_id"],
                    trace_id=kwargs["trace_id"],
                    input=kwargs["input"],
                )
            )
            await uow.commit()
        if self.usage_service is not None:
            await self.usage_service.complete(
                ModelRequest(provider="fake", prompt="child usage", max_output_tokens=2),
                context=UsageEvidenceContext(
                    tenant_id=identity.tenant_id,
                    run_id=run.id,
                    agent_id=kwargs["agent_id"],
                    request_id=kwargs["request_id"],
                    trace_id=kwargs["trace_id"],
                ),
                usage_call_id=f"delegation-child:{run.id}:model",
            )
            async with self.storage.uow() as uow:
                usage_rows = await uow.delegations.usage_evidence_for_child(run.id)
            assert len(usage_rows) == 1
        async with self.storage.uow() as uow:
            await uow.runs.set_status(
                run.id,
                self.child_status.value,
                output={"ok": True} if self.child_status == RunStatus.COMPLETED else None,
                error=(
                    {
                        "code": "child.failed",
                        "message": "redacted deterministic child failure",
                    }
                    if self.child_status == RunStatus.FAILED
                    else None
                ),
            )
            await uow.commit()
        return RunResult(run_id=run.id, status=self.child_status)

    async def resume_run(self, resume_token: str, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected parent resume: {resume_token}")

    async def submit_run(self, **kwargs: Any) -> RunResult:
        return await self.start_run(**kwargs)


async def _build_service(
    tmp_path: Path,
    *,
    database_events: bool = False,
    source_targets: list[str] | None = None,
    include_target: bool = True,
    delegated_parent: bool = False,
    trustworthy_usage: bool = False,
    mode: DelegationMode = "local",
    launch_error: bool = False,
    child_status: RunStatus = RunStatus.COMPLETED,
    source_cost_limit: float | None = 10.0,
    target_token_limit: int = 100,
    target_cost_limit: float | None = 10.0,
    usage_input_tokens: int | None = 3,
    usage_output_tokens: int | None = 2,
    usage_cost_usd: float = 0.25,
) -> tuple[
    SQLAlchemyStorage,
    DelegationService,
    _InlineChildRuntime,
    str,
    EventSink,
]:
    frozen_targets = source_targets if source_targets is not None else ["agent-target"]
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'delegation-service.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    usage_token_total = (
        usage_input_tokens + usage_output_tokens
        if isinstance(usage_input_tokens, int)
        and isinstance(usage_output_tokens, int)
        and usage_input_tokens + usage_output_tokens > 0
        else 1
    )
    usage_token_price = (
        Decimal("0")
        if source_cost_limit is None
        else Decimal(str(usage_cost_usd)) / Decimal(usage_token_total)
    )
    target_snapshots: dict[str, dict[str, Any]] = {
        target_id: {
            "agent_id": target_id,
            "descriptor_version": f"{target_id}-v1",
            "model_policy": {
                "provider": "fake",
                "default_model": "fake-basic",
                "fallback_models": [],
            },
            "target_budget": {
                "max_tokens_per_run": target_token_limit,
                "max_cost_usd_per_run": (None if source_cost_limit is None else target_cost_limit),
            },
            "routes": [
                {
                    "usage_kind": "model",
                    "provider": "fake",
                    "model": "fake-basic",
                    "price_source_ref": "catalog:fake",
                    "price_source_version": "catalog-v1",
                    "input_token_price_usd": str(usage_token_price),
                    "output_token_price_usd": str(usage_token_price),
                    "soft_max_tokens_per_call": 100,
                },
                {
                    "usage_kind": "embedding",
                    "provider": "local",
                    "model": "mock-small",
                    "price_source_ref": "catalog:local:mock-small",
                    "price_source_version": "catalog-v1",
                    "input_token_price_usd": "0",
                },
            ],
        }
        for target_id in frozen_targets
        if target_id != "agent-source"
    }
    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        session = await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-source",
            )
        )
        root = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=session.id,
                agent_id="agent-source",
                trace_id="trace-parent",
            )
        )
        await uow.shared_budget.create_ledger(
            LedgerCreate(
                tenant_id="tenant-a",
                budget_owner_run_id=root.id,
                token_limit=100,
                cost_limit=(None if source_cost_limit is None else Decimal(str(source_cost_limit))),
                registry_version="registry-v1",
                config_version="config-v1",
                catalog_version="catalog-v1",
                snapshot_id=f"snapshot:{root.id}",
                snapshot={
                    "owner": {
                        "agent_id": "agent-source",
                        "root_run_id": root.id,
                        "delegation_targets": list(frozen_targets),
                        "max_tokens_per_run": 100,
                        "max_cost_usd_per_run": source_cost_limit,
                        "cost_enabled": source_cost_limit is not None,
                    },
                    "registry_version": "registry-v1",
                    "config_version": "config-v1",
                    "catalog_version": "catalog-v1",
                    "agents": {
                        "agent-source": {
                            "agent_id": "agent-source",
                            "descriptor_version": "agent-source-v1",
                            "model_policy": {
                                "provider": "fake",
                                "default_model": "fake-basic",
                                "fallback_models": [],
                            },
                            "target_budget": {
                                "max_tokens_per_run": 100,
                                "max_cost_usd_per_run": source_cost_limit,
                            },
                            "routes": [
                                {
                                    "usage_kind": "model",
                                    "provider": "fake",
                                    "model": "fake-basic",
                                    "price_source_ref": "catalog:fake",
                                    "price_source_version": "catalog-v1",
                                    "input_token_price_usd": str(usage_token_price),
                                    "output_token_price_usd": str(usage_token_price),
                                    "soft_max_tokens_per_call": 100,
                                },
                                {
                                    "usage_kind": "embedding",
                                    "provider": "local",
                                    "model": "mock-small",
                                    "price_source_ref": "catalog:local:mock-small",
                                    "price_source_version": "catalog-v1",
                                    "input_token_price_usd": "0",
                                },
                            ],
                        },
                        **target_snapshots,
                    },
                },
            )
        )
        if not include_target:
            # 授权层的损坏 catalog 反例必须先经过合法首次写入，再模拟数据库外部
            # 对 target sub-snapshot 与 hash 的一致篡改，不能弱化生产创建门禁。
            ledger = await uow.session.get(ParentBudgetLedgerModel, ("tenant-a", root.id))
            assert ledger is not None
            snapshot = dict(ledger.snapshot_json)
            agents = dict(snapshot["agents"])
            for target_id in frozen_targets:
                if target_id != "agent-source":
                    agents.pop(target_id, None)
            snapshot["agents"] = agents
            ledger.snapshot_json = snapshot
            ledger.snapshot_hash = hashlib.sha256(
                json.dumps(
                    snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        parent = root
        if delegated_parent:
            parent = await uow.runs.create(
                RunCreate(
                    tenant_id="tenant-a",
                    session_id=session.id,
                    agent_id="agent-source",
                    parent_run_id=root.id,
                    trace_id=root.trace_id,
                )
            )
        await uow.commit()
    resolver = StorageRunTraceResolver(storage)
    sink: EventSink = (
        PostgreSQLEventSink(storage)
        if database_events
        else LocalJsonlEventSink(
            tmp_path / "events.jsonl",
            state_dir=tmp_path,
            run_trace_resolver=resolver,
        )
    )
    event_bus = EventBus(
        sink=sink,
        run_trace_resolver=resolver,
        capacity_storage=None if database_events else storage,
    )
    usage_service = None
    if trustworthy_usage:
        usage_service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={
                    "fake": _UsageProvider(
                        input_tokens=usage_input_tokens,
                        output_tokens=usage_output_tokens,
                        cost_usd=usage_cost_usd,
                    )
                },
            ),
            storage=storage,
            event_bus=event_bus,
            shared_budget=_SharedBudgetRuntimeFixture(),
        )
    runtime = _InlineChildRuntime(
        storage,
        usage_service=usage_service,
        launch_error=launch_error,
        child_status=child_status,
    )
    service = DelegationService(
        storage=storage,
        registry=AgentRegistry(
            [
                _descriptor(
                    "agent-source",
                    targets=frozen_targets,
                    max_cost_usd=source_cost_limit,
                )
            ]
            + (
                [
                    _descriptor(
                        "agent-target",
                        targets=[],
                        max_tokens=target_token_limit,
                        max_cost_usd=target_cost_limit,
                    )
                ]
                if include_target
                else []
            )
        ),
        policy=_Policy(),
        event_bus=event_bus,
        orchestrator=runtime,
        mode=mode,
    )
    return storage, service, runtime, parent.id, sink


class _UsageProvider:
    provider_id = "fake"

    def __init__(
        self,
        *,
        input_tokens: int | None = 3,
        output_tokens: int | None = 2,
        latency_ms: int = 7,
        cost_usd: float | None = 0.25,
        cost_status: Literal["reported", "estimated", "unavailable"] = "reported",
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.cost_usd = cost_usd
        self.cost_status: Literal["reported", "estimated", "unavailable"] = cost_status

    def complete(self, request: ModelRequest, *, model: str) -> ModelResponse:
        return ModelResponse(
            provider=self.provider_id,
            model=model,
            output_text="child result",
            decision=ModelDecision(action="call", estimated_tokens=0),
            token_usage=cast(
                dict[str, int],
                {
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                },
            ),
            latency_ms=self.latency_ms,
            cost_usd=self.cost_usd,
            cost_status=self.cost_status,
        )


__all__ = [
    "_InlineChildRuntime",
    "_ParentDetailOrchestrator",
    "_Policy",
    "_SharedBudgetRuntimeFixture",
    "_UsageProvider",
    "_build_service",
    "_descriptor",
    "_identity",
]
