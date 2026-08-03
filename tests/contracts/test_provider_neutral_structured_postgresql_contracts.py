"""结构化结果在真实PostgreSQL JSON耐久列上的合同。"""

from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.model_usage_capacity_test_helpers import seed_run

from agent_harness.events import EventBus, PostgreSQLEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    FakeStructuredScript,
    ModelInvocationService,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
    UsageInvocationReplayError,
    compile_output_schema,
    stable_usage_call_id,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_models import RunEvidenceOutboxModel
from agent_harness.storage.run_trace_gate import StorageRunTraceResolver

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="结构化真实PostgreSQL合同需要AGENT_HARNESS_TEST_POSTGRES_DSN。",
)


class _Output(BaseModel):
    """PostgreSQL 夹具使用的严格输出 schema。"""

    model_config = ConfigDict(extra="forbid")

    answer: str


@pytest.mark.asyncio
async def test_postgresql_structured_success_repair_exact_replay_and_tamper_fence() -> None:
    """同一 JSON 列逐值持久化 result/evidence/replay，并拒绝 value 篡改。"""

    async with isolated_database("provider_neutral_structured") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        schema = compile_output_schema(_Output, schema_ref="fixture.Output", version="v1")
        provider = FakeModelProvider(
            structured_script=FakeStructuredScript(
                candidates=({"wrong": 1}, {"answer": "postgres"}),
            )
        )
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="fake-basic"),
                providers={"fake": provider},
            ),
            storage=storage,
            event_bus=EventBus(
                sink=PostgreSQLEventSink(storage),
                run_trace_resolver=StorageRunTraceResolver(storage),
            ),
            output_schema_resolver=lambda _agent_id: schema,
        )
        try:
            run_id = await seed_run(storage, request_id="request-a")
            bound = service.bind_execution(
                identity=IdentityContext(
                    tenant_id="tenant-a",
                    user_id="user-a",
                    session_id="session-a",
                ),
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                request_id="request-a",
                trace_id="trace-a",
            )
            request = ModelRequest(
                provider="fake",
                model="fake-basic",
                prompt="return an answer",
                max_output_tokens=8,
            )
            first = await bound.complete_structured(
                request,
                operation_key="postgres-structured",
                repair_limit=1,
            )
            replayed = await bound.complete_structured(
                request,
                operation_key="postgres-structured",
                repair_limit=1,
            )
            assert replayed == first
            assert provider.structured_send_count == 2

            usage_call_id = stable_usage_call_id(
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    request_id="request-a",
                    trace_id="trace-a",
                ),
                operation_key="postgres-structured",
            )
            async with storage.uow() as uow:
                row = await uow.session.scalar(
                    select(RunEvidenceOutboxModel).where(
                        RunEvidenceOutboxModel.usage_call_id == usage_call_id
                    )
                )
                assert row is not None
                assert row.result_json is not None
                result: dict[str, Any] = deepcopy(row.result_json)
                result["response"]["structured_output"]["value"]["answer"] = "tampered"
                row.result_json = result
                await uow.commit()

            with pytest.raises(UsageInvocationReplayError):
                await bound.complete_structured(
                    request,
                    operation_key="postgres-structured",
                    repair_limit=1,
                )
            assert provider.structured_send_count == 2
        finally:
            await service.aclose()
            await storage.dispose()
