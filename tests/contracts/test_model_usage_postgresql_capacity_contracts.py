"""Model usage event capacity 的真实 PostgreSQL 合同测试。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from sqlalchemy import text, update
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)

from agent_harness.embeddings import (
    EmbeddingCacheInfo,
    EmbeddingInvocationService,
    EmbeddingRequest,
    EmbeddingResponse,
)
from agent_harness.events import (
    CanonicalEvent,
    CanonicalEventEnvelopeStateInvalid,
    CanonicalEventType,
    EventBus,
    PostgreSQLEventSink,
)
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import (
    MAX_EVENT_SEQ,
    EventCapacityExceeded,
    EvidenceOperationKind,
)
from agent_harness.storage.models import RunEventCapacityModel
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL capacity 合同需要 AGENT_HARNESS_TEST_POSTGRES_DSN。",
)


async def _seed_run(storage: SQLAlchemyStorage, *, suffix: str) -> tuple[str, str, str]:
    """创建独立租户、会话和 run，并返回 capacity 场景所需的三项关联标识。"""

    tenant_id = f"tenant-{suffix}"
    trace_id = f"trace-{suffix}"
    async with storage.uow() as uow:
        await uow.tenants.ensure(tenant_id)
        session = await uow.sessions.create(
            SessionCreate(tenant_id=tenant_id, user_id="user-a", agent_id="agent-a")
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id=tenant_id,
                session_id=session.id,
                agent_id="agent-a",
                trace_id=trace_id,
            )
        )
        await uow.commit()
    return tenant_id, run.id, trace_id


def _started_evidence(
    *,
    tenant_id: str,
    run_id: str,
    trace_id: str,
) -> dict[str, object]:
    """构造并发 claim 需要逐值一致的 durable started 身份。"""

    return {
        "usage_kind": "model",
        "tenant_id": tenant_id,
        "provider": "fake",
        "model": "fake-basic",
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "cost_status": "unavailable",
        "latency_ms": 0,
        "decision": {"provider_called": False},
        "run_id": run_id,
        "agent_id": "agent-a",
        "request_id": None,
        "trace_id": trace_id,
    }


def _json(value: Any) -> str:
    """使用禁止 NaN 的稳定 JSON 编码，避免 PG JSON 断言受非标准数值影响。"""

    return json.dumps(value, ensure_ascii=False, allow_nan=False)


__all__ = [
    "Any",
    "CanonicalEvent",
    "CanonicalEventEnvelopeStateInvalid",
    "CanonicalEventType",
    "EmbeddingCacheInfo",
    "EmbeddingInvocationService",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "EventBus",
    "EventCapacityExceeded",
    "EvidenceOperationKind",
    "FakeModelProvider",
    "MAX_EVENT_SEQ",
    "ModelInvocationService",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ModelRouterConfig",
    "PostgreSQLEventSink",
    "RunCreate",
    "RunEventCapacityModel",
    "SQLAlchemyStorage",
    "SessionCreate",
    "StorageRunTraceResolver",
    "UsageEvidenceContext",
    "_json",
    "_seed_run",
    "_started_evidence",
    "asyncio",
    "isolated_database",
    "json",
    "os",
    "pytest",
    "pytestmark",
    "run_migrations",
    "text",
    "update",
]
