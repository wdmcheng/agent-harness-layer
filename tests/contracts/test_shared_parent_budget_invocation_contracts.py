"""Model/embedding 与 0014/0016 application UoW 组合合同。"""

# ruff: noqa: F401

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _build_service as build_delegation_service,
)
from tests.contracts.agent_delegation_service_runtime_test_support import (
    _identity as delegation_identity,
)

from agent_harness.delegation import DelegationRequest
from agent_harness.embeddings import (
    EmbeddingInvocationService,
    EmbeddingRequest,
    LocalEmbeddingProvider,
    StorageEmbeddingCache,
)
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.models.providers import ModelResponse
from agent_harness.models.usage import UsageInvocationReplayError
from agent_harness.runtime import RunStatus
from agent_harness.runtime.executor import ApprovalGrant
from agent_harness.runtime.shared_budget import SharedBudgetRuntime
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.event_capacity_repositories import (
    MAX_EVENT_SEQ,
    EventCapacityExceeded,
    EventSequenceStateInvalid,
)
from agent_harness.storage.models import RunEventCapacityModel
from agent_harness.storage.repositories import EmbeddingCacheCreate
from agent_harness.storage.shared_budget import (
    BudgetOperationConflict,
    BudgetOperationOwnership,
    BudgetReservationRejected,
    DirectBudgetClaim,
    LedgerCreate,
    OperationIdentity,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
    ParentBudgetLedgerModel,
)


class TestIdentityRuntime:
    """提供真实 SharedBudgetRuntime 所需的身份与价格投影 seam 的轻量测试宿主。"""

    __test__ = False

    def operation_identity(self, **values: Any) -> OperationIdentity:
        """以固定测试密钥构造语义身份，保证重放断言不依赖环境 secret。"""

        return OperationIdentity.from_semantic_request(
            fingerprint_key=b"test-only-budget-fingerprint-key",
            fingerprint_key_version="test-v1",
            **values,
        )

    def model_router_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        base: ModelRouterConfig,
    ) -> ModelRouterConfig:
        """复用生产路由投影逻辑，验证冻结 snapshot 对模型路由的约束。"""

        return SharedBudgetRuntime.model_router_config(
            self,  # type: ignore[arg-type]
            snapshot=snapshot,
            agent_id=agent_id,
            base=base,
        )

    def embedding_price_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        provider: str,
        model: str,
    ) -> tuple[Decimal | None, str, str]:
        """复用生产价格投影逻辑，验证 embedding 结算只使用冻结目录信息。"""

        return SharedBudgetRuntime.embedding_price_config(
            self,  # type: ignore[arg-type]
            snapshot=snapshot,
            agent_id=agent_id,
            provider=provider,
            model=model,
        )


class CountingFakeModelProvider:
    """记录真实 provider 调用次数，证明三个恢复窗口不会重复副作用。"""

    provider_id = "fake"

    def __init__(self) -> None:
        """初始化调用计数和真实 fake provider，保持业务响应形状不变。"""

        self.calls = 0
        self._delegate = FakeModelProvider()

    async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
        """记录外部模型副作用次数后委托 fake provider，支撑恢复窗口断言。"""

        self.calls += 1
        return await self._delegate.complete(request, plan=plan)


def model_service(
    *,
    storage: SQLAlchemyStorage,
    sink: LocalJsonlEventSink,
    provider: CountingFakeModelProvider,
) -> ModelInvocationService:
    """组装使用固定价格目录和 trace resolver 的模型调用服务。"""

    return ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(
                default_model="fake-basic",
                max_tokens_per_call=100,
                input_token_price_usd=Decimal("0"),
                output_token_price_usd=Decimal("0"),
                price_source_ref="catalog:fake",
                price_source_version="catalog-v1",
            ),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        shared_budget=TestIdentityRuntime(),
    )


def model_request() -> ModelRequest:
    """生成最小可计费的模型请求，供重放和预算场景共享。"""

    return ModelRequest(
        provider="fake",
        prompt="abc",
        estimated_input_tokens=1,
        max_output_tokens=2,
    )


async def seed_managed_root(
    storage: SQLAlchemyStorage,
    *,
    token_limit: int = 100,
    target_token_limit: int | None = None,
    cost_limit: Decimal | None = None,
    include_fallback: bool = False,
    embedding_price: Decimal | None = Decimal("0"),
    model_input_price: Decimal | None = Decimal("0"),
    model_output_price: Decimal | None = Decimal("0"),
    soft_token_limit: int = 100,
    fallback_soft_token_limit: int = 100,
) -> str:
    """创建带冻结 shared-budget snapshot 的 root run，并返回其 durable 标识。

    参数只控制各测试需要的预算和价格分支；租户、会话、目录版本保持固定，
    让断言聚焦调用/恢复语义而不是初始化噪声。
    """

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-a",
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id="session-a",
                agent_id="agent-a",
                trace_id="trace-a",
            )
        )
        await uow.shared_budget.create_ledger(
            LedgerCreate(
                tenant_id="tenant-a",
                budget_owner_run_id=run.id,
                token_limit=token_limit,
                cost_limit=cost_limit,
                registry_version="registry-v1",
                config_version="config-v1",
                catalog_version="catalog-v1",
                snapshot_id=f"snapshot:{run.id}",
                snapshot={
                    "owner": {
                        "agent_id": "agent-a",
                        "root_run_id": run.id,
                        "delegation_targets": [],
                        "max_tokens_per_run": token_limit,
                        "max_cost_usd_per_run": (None if cost_limit is None else str(cost_limit)),
                        "cost_enabled": cost_limit is not None,
                    },
                    "registry_version": "registry-v1",
                    "config_version": "config-v1",
                    "catalog_version": "catalog-v1",
                    "agents": {
                        "agent-a": {
                            "agent_id": "agent-a",
                            "descriptor_version": "agent-a-v1",
                            "model_policy": {
                                "provider": "fake",
                                "default_model": "fake-basic",
                                "fallback_models": (["fake-fallback"] if include_fallback else []),
                            },
                            "target_budget": {
                                "max_tokens_per_run": (
                                    token_limit
                                    if target_token_limit is None
                                    else target_token_limit
                                ),
                                "max_cost_usd_per_run": (
                                    None if cost_limit is None else str(cost_limit)
                                ),
                            },
                            "routes": [
                                {
                                    "usage_kind": "model",
                                    "provider": "fake",
                                    "model": "fake-basic",
                                    "price_source_ref": "catalog:fake",
                                    "price_source_version": "catalog-v1",
                                    "input_token_price_usd": (
                                        None
                                        if model_input_price is None
                                        else str(model_input_price)
                                    ),
                                    "output_token_price_usd": (
                                        None
                                        if model_output_price is None
                                        else str(model_output_price)
                                    ),
                                    "soft_max_tokens_per_call": soft_token_limit,
                                },
                                *(
                                    [
                                        {
                                            "usage_kind": "model",
                                            "provider": "fake",
                                            "model": "fake-fallback",
                                            "price_source_ref": "catalog:fake",
                                            "price_source_version": "catalog-v1",
                                            "input_token_price_usd": (
                                                None
                                                if model_input_price is None
                                                else str(model_input_price)
                                            ),
                                            "output_token_price_usd": (
                                                None
                                                if model_output_price is None
                                                else str(model_output_price)
                                            ),
                                            "soft_max_tokens_per_call": fallback_soft_token_limit,
                                        }
                                    ]
                                    if include_fallback
                                    else []
                                ),
                                {
                                    "usage_kind": "embedding",
                                    "provider": "local",
                                    "model": "mock-small",
                                    "price_source_ref": "catalog:local:mock-small",
                                    "price_source_version": "catalog-v1",
                                    "input_token_price_usd": (
                                        None if embedding_price is None else str(embedding_price)
                                    ),
                                },
                            ],
                        }
                    },
                },
            )
        )
        await uow.commit()
        return run.id


def context(run_id: str) -> UsageEvidenceContext:
    """构造与 seed root 对齐的使用证据上下文，避免测试重复手写关联字段。"""

    return UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )


async def resolve_trace(**_: object) -> str:
    """为测试事件总线返回固定 trace，隔离调用预算合同与 trace 查询实现。"""

    return "trace-a"


__all__ = [
    "Any",
    "ApprovalGrant",
    "BudgetOperationConflict",
    "BudgetOperationClaimModel",
    "BudgetOperationOwnership",
    "BudgetReservationRejected",
    "CountingFakeModelProvider",
    "Decimal",
    "DelegationBudgetAllocationModel",
    "DelegationRequest",
    "DirectBudgetClaim",
    "EmbeddingCacheCreate",
    "EmbeddingInvocationService",
    "EmbeddingRequest",
    "EventBus",
    "EventCapacityExceeded",
    "EventSequenceStateInvalid",
    "FakeModelProvider",
    "IdentityContext",
    "LedgerCreate",
    "LocalEmbeddingProvider",
    "LocalJsonlEventSink",
    "MAX_EVENT_SEQ",
    "ModelInvocationService",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ModelRouterConfig",
    "OperationIdentity",
    "ParentBudgetLedgerModel",
    "Path",
    "RunCreate",
    "RunEventCapacityModel",
    "RunStatus",
    "SQLAlchemyStorage",
    "SessionCreate",
    "SharedBudgetRuntime",
    "StorageEmbeddingCache",
    "TestIdentityRuntime",
    "UsageEvidenceContext",
    "UsageInvocationReplayError",
    "build_delegation_service",
    "context",
    "delegation_identity",
    "hashlib",
    "json",
    "model_request",
    "model_service",
    "pytest",
    "resolve_trace",
    "run_migrations",
    "seed_managed_root",
    "select",
    "update",
]
