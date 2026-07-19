"""真实 PostgreSQL/Redis worker reclaim 下的 delegation 唯一执行合同。"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.adapters.queue import RedisRunQueue
from agent_harness.delegation import (
    AgentDelegateInput,
    AgentDelegationModule,
    DelegationRequest,
)
from agent_harness.delegation.service import DelegationError, DelegationExecutionResult
from agent_harness.events import CanonicalEvent, CanonicalEventType, PostgreSQLEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import ModelDecision, ModelResponse
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import AgentExecutionResult
from agent_harness.runtime import services as runtime_services
from agent_harness.runtime.executor import (
    AgentExecutionContext,
    AgentExecutionRequest,
    build_execution_context,
)
from agent_harness.runtime.state import RunStatus
from agent_harness.storage import RunCreate, SessionCreate, run_migrations
from agent_harness.storage.delegation_models import (
    AgentDelegationModel,
    DelegationAggregateModel,
    DelegationBudgetReservationModel,
)
from agent_harness.storage.event_capacity_repositories import MAX_EVENT_SEQ
from agent_harness.storage.models import AgentRunModel, SessionModel
from app import runtime as app_runtime
from app.workers import runtime_worker

pytestmark = pytest.mark.skipif(
    not (os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN") and os.environ.get("REDIS_TEST_DSN")),
    reason="service delegation 合同需要真实 PostgreSQL 与 Redis。",
)


def _service_profiles(
    tmp_path: Path,
    *,
    source_token_limit: int | None = None,
    source_cost_limit: float | None = None,
) -> Path:
    """复制服务模板并只调整源 agent 的 delegation edge 与预算上限。"""

    source = Path(__file__).resolve().parents[2] / "templates" / "service-app"
    target = tmp_path / "service-app"
    shutil.copytree(source, target)
    config = target / "agents" / "examples" / "basic" / "config.yaml"
    content = (
        config.read_text(encoding="utf-8")
        .replace(
            "delegation_edges: []",
            "delegation_edges:\n  - examples.ticket_triage",
        )
        .replace(
            "max_tokens_per_run: 8192",
            f"max_tokens_per_run: {source_token_limit or 8192}",
        )
    )
    if source_cost_limit is not None:
        content = content.replace(
            "max_cost_usd_per_run: null",
            f"max_cost_usd_per_run: {source_cost_limit}",
        )
    config.write_text(content, encoding="utf-8")
    return target / "configs" / "profiles"


class _DelegatingExecutor:
    """以真实 execution context 取得绑定 module，业务输入不携带 identity。"""

    def __init__(self) -> None:
        """初始化调用计数，用于证明 worker 重放没有重复执行 delegation。"""

        self.calls = 0

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """从可信服务上下文获取 delegation module，并将业务输入映射为 child 请求。"""

        self.calls += 1
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


async def _seed_parent(
    components: app_runtime.RuntimeComponents,
    *,
    identity: IdentityContext,
) -> str:
    """为真实 service worker 场景创建运行中的父 run，并返回其持久化标识。"""

    async with components.storage.uow() as uow:
        await uow.tenants.ensure(identity.tenant_id)
        session = await uow.sessions.ensure(
            SessionCreate(
                session_id=identity.session_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                agent_id="examples.basic",
            )
        )
        parent = await uow.runs.create(
            RunCreate(
                tenant_id=identity.tenant_id,
                session_id=session.id,
                agent_id="examples.basic",
                trace_id="trace-delegation-service",
            )
        )
        await uow.runs.set_status(parent.id, "running")
        await uow.commit()
        return parent.id


__all__ = [
    "AgentDelegateInput",
    "AgentDelegationModel",
    "AgentDelegationModule",
    "AgentExecutionContext",
    "AgentExecutionRequest",
    "AgentExecutionResult",
    "AgentRegistry",
    "AgentRunModel",
    "Any",
    "CanonicalEvent",
    "CanonicalEventType",
    "DelegationAggregateModel",
    "DelegationBudgetReservationModel",
    "DelegationError",
    "DelegationExecutionResult",
    "DelegationRequest",
    "IdentityContext",
    "MAX_EVENT_SEQ",
    "ModelDecision",
    "ModelResponse",
    "Path",
    "PostgreSQLEventSink",
    "RedisRunQueue",
    "RunCreate",
    "RunStatus",
    "SessionCreate",
    "SessionModel",
    "_DelegatingExecutor",
    "_seed_parent",
    "_service_profiles",
    "app_runtime",
    "asyncio",
    "build_execution_context",
    "cast",
    "isolated_database",
    "os",
    "pytest",
    "pytestmark",
    "run_migrations",
    "runtime_services",
    "runtime_worker",
    "select",
    "shutil",
    "update",
    "uuid4",
]
