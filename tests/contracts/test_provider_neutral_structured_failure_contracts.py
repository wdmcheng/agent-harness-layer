"""结构化失败、repair exhaustion与零调用preflight合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.config import ModelRouteRef
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    BoundModelInvocationService,
    FakeModelProvider,
    FakeStructuredScript,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    OutputSchemaDefinition,
    StructuredSchemaResolutionError,
    UsageEvidenceContext,
    UsageInvocationReplayError,
    compile_output_schema,
    stable_usage_call_id,
)
from agent_harness.registry import AgentModelPolicy
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_models import RunEvidenceOutboxModel


class _Output(BaseModel):
    """失败夹具使用的严格 schema。"""

    model_config = ConfigDict(extra="forbid")

    answer: str


async def _bound_fake(
    tmp_path: Path,
    *,
    candidates: tuple[str | dict[str, object], ...],
    resolver: bool = True,
    resolver_error: StructuredSchemaResolutionError | None = None,
    policy: AgentModelPolicy | None = None,
) -> tuple[
    ModelInvocationService,
    SQLAlchemyStorage,
    FakeModelProvider,
    BoundModelInvocationService,
    str,
]:
    """构造单 run 的公开 bound seam；候选只由显式 fake script 提供。"""

    schema = compile_output_schema(
        _Output,
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
    )
    provider = FakeModelProvider(structured_script=FakeStructuredScript(candidates=candidates))
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured-failure.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    def resolve_schema(_agent_id: str) -> OutputSchemaDefinition:
        """按夹具开关返回 schema 或模拟 Registry unknown。"""

        if resolver_error is not None:
            raise resolver_error
        if not resolver:
            raise LookupError
        return schema

    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="fake", default_model="fake-basic"),
            providers={"fake": provider},
        ),
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=tmp_path / "events.jsonl"),
        output_schema_resolver=resolve_schema,
        agent_policy_resolver=(None if policy is None else lambda _agent_id: policy),
    )
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
    return service, storage, provider, bound, run_id


def _request() -> ModelRequest:
    """所有失败分支共享同一业务语义，便于验证 exact replay。"""

    return ModelRequest(
        provider="fake",
        model="fake-basic",
        prompt="return an answer",
        max_output_tokens=8,
    )


@pytest.mark.asyncio
async def test_limit_zero_extra_fields_is_durable_and_exact_replay_does_not_resend(
    tmp_path: Path,
) -> None:
    """首次 extra 在 limit=0 时直接失败，exact replay 只恢复同一错误。"""

    service, storage, provider, bound, _run_id = await _bound_fake(
        tmp_path,
        candidates=({"answer": "ok", "extra": True},),
    )
    try:
        for _ in range(2):
            with pytest.raises(ModelProviderInvocationError) as failure:
                await bound.complete_structured(
                    _request(),
                    operation_key="extra-fields",
                    repair_limit=0,
                )
            assert failure.value.code == "model.structured_extra_fields"
        assert provider.structured_send_count == 1
        assert provider.structured_close_count == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_repair_exhaustion_counts_every_request_and_never_crosses_provider(
    tmp_path: Path,
) -> None:
    """Initial 与唯一 repair 都 invalid 时，actual 两次计量不能伪装为零。"""

    service, storage, provider, bound, _run_id = await _bound_fake(
        tmp_path,
        candidates=({"wrong": 1}, {"wrong": 2}),
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                _request(),
                operation_key="repair-exhausted",
                repair_limit=1,
            )
        assert failure.value.code == "model.structured_repair_exhausted"
        assert failure.value.provider_called is True
        assert failure.value.attempt_count == 2
        assert provider.structured_send_count == 2
        assert provider.structured_close_count == 2
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_unknown_bound_schema_fails_before_usage_claim_or_provider_handle(
    tmp_path: Path,
) -> None:
    """Registry resolver 失败不得创建 claim、prepared handle 或 fake send。"""

    service, storage, provider, bound, run_id = await _bound_fake(
        tmp_path,
        candidates=({"answer": "unused"},),
        resolver=False,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                _request(),
                operation_key="unknown-schema",
                repair_limit=0,
            )
        assert failure.value.code == "model.structured_schema_unknown"
        assert provider.structured_send_count == 0
        assert provider.structured_close_count == 0
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
        assert rows == []
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_schema_identity_conflict_is_distinct_zero_call_preflight_failure(
    tmp_path: Path,
) -> None:
    """Registry identity 冲突必须保留独立错误码，且不能创建 claim 或 provider handle。"""

    service, storage, provider, bound, run_id = await _bound_fake(
        tmp_path,
        candidates=({"answer": "unused"},),
        resolver_error=StructuredSchemaResolutionError("model.structured_schema_conflict"),
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                _request(),
                operation_key="schema-conflict",
                repair_limit=0,
            )
        assert failure.value.code == "model.structured_schema_conflict"
        assert provider.structured_send_count == 0
        assert provider.structured_close_count == 0
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
        assert rows == []
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_any_explicit_fallback_route_is_rejected_before_claim_even_with_one_candidate(
    tmp_path: Path,
) -> None:
    """Structured 不得把单条显式 route-chain 降级成 legacy 单 route。"""

    policy = AgentModelPolicy(
        deployment_id="fake_default",
        provider="fake",
        allowed_models=["fake-basic"],
        default_model="fake-basic",
        fallback_models=[],
        fallback_routes=(ModelRouteRef(deployment_id="fake_default", model_id="fake-basic"),),
    )
    service, storage, provider, bound, run_id = await _bound_fake(
        tmp_path,
        candidates=({"answer": "unused"},),
        policy=policy,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                _request(),
                operation_key="explicit-route-chain",
            )
        assert failure.value.code == "model.structured_route_not_allowed"
        assert provider.structured_send_count == 0
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
        assert rows == []
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_repair_policy_overreach_fails_before_claim_or_provider_handle(
    tmp_path: Path,
) -> None:
    """调用方不能越过 legacy deployment 上限或全局 0..2 封闭区间。"""

    service, storage, provider, bound, run_id = await _bound_fake(
        tmp_path,
        candidates=({"answer": "unused"},),
    )
    try:
        for repair_limit in (2, 3):
            with pytest.raises(ModelProviderInvocationError) as failure:
                await bound.complete_structured(
                    _request(),
                    operation_key=f"repair-overreach-{repair_limit}",
                    repair_limit=repair_limit,
                )
            assert failure.value.code == "model.structured_policy_invalid"
        assert provider.structured_send_count == 0
        assert provider.structured_close_count == 0
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
        assert rows == []
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_sqlite_durable_structured_value_tamper_is_rejected_without_resend(
    tmp_path: Path,
) -> None:
    """篡改 durable value 但保留 replay digest 时，publication validator 必须关闭失败。"""

    service, storage, provider, bound, run_id = await _bound_fake(
        tmp_path,
        candidates=({"answer": "original"},),
    )
    usage_call_id = stable_usage_call_id(
        context=UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        ),
        operation_key="tampered-value",
    )
    try:
        first = await bound.complete_structured(
            _request(),
            operation_key="tampered-value",
        )
        assert first.structured_output is not None
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
                _request(),
                operation_key="tampered-value",
            )
        assert provider.structured_send_count == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_background_recovery_rejects_tampered_structured_result_without_resend(
    tmp_path: Path,
) -> None:
    """后台补投与公开 replay 共用完整 validator，不能发布被篡改的 value。"""

    service, storage, provider, bound, run_id = await _bound_fake(
        tmp_path,
        candidates=({"answer": "original"},),
    )
    try:
        await bound.complete_structured(
            _request(),
            operation_key="recovery-tampered-value",
        )
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
            assert len(rows) == 1
            row = rows[0]
            assert row.result_json is not None
            result: dict[str, Any] = deepcopy(row.result_json)
            result["response"]["structured_output"]["value"]["answer"] = "tampered"
            row.result_json = result
            row.state = "result_persisted"
            await uow.commit()

        with pytest.raises(UsageInvocationReplayError):
            await service.recover_pending(run_id=run_id)
        assert provider.structured_send_count == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.parametrize(
    "mutation",
    ("route-reservation", "attempt-order", "started-summary"),
)
@pytest.mark.asyncio
async def test_sqlite_structured_evidence_invariant_tamper_is_rejected_without_resend(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Route 公式、attempt 顺序或 started 联合体任一被篡改都不得恢复或重发。"""

    service, storage, provider, bound, run_id = await _bound_fake(
        tmp_path,
        candidates=({"wrong": 1}, {"answer": "fixed"}),
    )
    operation_key = f"tampered-{mutation}"
    usage_call_id = stable_usage_call_id(
        context=UsageEvidenceContext(
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        ),
        operation_key=operation_key,
    )
    try:
        response = await bound.complete_structured(
            _request(),
            operation_key=operation_key,
            repair_limit=1,
        )
        assert response.structured_output is not None
        assert provider.structured_send_count == 2
        async with storage.uow() as uow:
            row = await uow.session.scalar(
                select(RunEvidenceOutboxModel).where(
                    RunEvidenceOutboxModel.usage_call_id == usage_call_id
                )
            )
            assert row is not None
            assert row.result_json is not None
            result: dict[str, Any] = deepcopy(row.result_json)
            if mutation == "route-reservation":
                for anchor in (result["started"], result["evidence"]):
                    anchor["decision"]["route"]["reserved_token_bound"] += 1
            elif mutation == "attempt-order":
                result["evidence"]["decision"]["attempts"][1]["structured_output"][
                    "transport_ordinal"
                ] = 2
            else:
                result["started"]["decision"]["structured_output"]["provider_request_count"] = 1
            row.result_json = result
            await uow.commit()

        with pytest.raises(UsageInvocationReplayError):
            await bound.complete_structured(
                _request(),
                operation_key=operation_key,
                repair_limit=1,
            )
        assert provider.structured_send_count == 2
    finally:
        await service.aclose()
        await storage.dispose()
