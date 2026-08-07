"""Usage 事件正文必须逐值绑定 durable settlement，且 started 先于 final。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTES,
    BoundFailoverFixture,
    bound_failover_invocation,
)
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.artifacts import FileArtifactStore
from agent_harness.events import (
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
    canonical_json_bytes,
)
from agent_harness.models import ModelRequest, ModelUsageEvidence, UsageEvidenceLifecycle
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


def _started_evidence(*, run_id: str) -> ModelUsageEvidence:
    """构造 provider 尚未调用时的固定 started evidence，用于绑定身份校验。"""

    return ModelUsageEvidence(
        usage_kind="model",
        tenant_id="tenant-a",
        provider="real-provider",
        model="real-model",
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        cost_status="unavailable",
        latency_ms=0,
        decision={"provider_called": False, "route": "primary"},
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )


def _final_evidence(*, run_id: str) -> ModelUsageEvidence:
    """构造已获得可信使用量后的固定 final evidence，用于逐值对照。"""

    return ModelUsageEvidence(
        usage_kind="model",
        tenant_id="tenant-a",
        provider="real-provider",
        model="real-model",
        input_tokens=7,
        output_tokens=3,
        cost_usd=None,
        cost_status="unavailable",
        latency_ms=4,
        decision={"provider_called": True, "route": "primary"},
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )


async def _claim_usage(
    *,
    storage: SQLAlchemyStorage,
    run_id: str,
    usage_call_id: str,
    started: ModelUsageEvidence,
) -> None:
    """通过受信 outbox 为一次调用建立预约与不可变 started 身份。"""

    async with storage.uow() as uow:
        await uow.evidence_outbox.claim_usage(
            tenant_id="tenant-a",
            run_id=run_id,
            usage_call_id=usage_call_id,
            event_id=f"usage:tenant-a:{usage_call_id}:final",
            operation_kind=EvidenceOperationKind.MODEL_USAGE,
            started_evidence=started.to_payload(),
        )
        await uow.commit()


async def _persist_final(
    *,
    storage: SQLAlchemyStorage,
    usage_call_id: str,
    final: ModelUsageEvidence,
) -> None:
    """把可信 final evidence 写入既有 outbox，不发布任何 CanonicalEvent。"""

    async with storage.uow() as uow:
        await uow.evidence_outbox.persist_result(
            tenant_id="tenant-a",
            usage_call_id=usage_call_id,
            result={"evidence": final.to_payload(), "outcome": "completed"},
        )
        await uow.commit()


async def _publish_forged_final(
    *,
    bus: EventBus,
    usage_call_id: str,
    evidence: ModelUsageEvidence,
    outcome: str = "completed",
    error_code: str | None = None,
) -> None:
    """直接拼装 final 事件正文，用于验证 sink 拒绝绕过 lifecycle 的伪造写入。"""

    payload: dict[str, Any] = {
        "correlation": {"usage_call_id": usage_call_id},
        "usage": evidence.to_payload(),
        "outcome": outcome,
    }
    if error_code is not None:
        payload["error_code"] = error_code
    await bus.publish(
        tenant_id=evidence.tenant_id,
        run_id=evidence.run_id,
        agent_id=evidence.agent_id,
        request_id=evidence.request_id,
        trace_id=evidence.trace_id,
        event_type=CanonicalEventType.MODEL_USAGE_UPDATED,
        payload=payload,
        event_id=f"usage:{evidence.tenant_id}:{usage_call_id}:final",
    )


@pytest.mark.asyncio
async def test_local_usage_events_reject_bound_payload_tampering(tmp_path: Path) -> None:
    """合法 call-id 不能替伪造的 started/final 正文消费预约。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'payload-binding.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "payload-binding.jsonl"
    usage_call_id = "bound-local"
    try:
        run_id = await seed_run(storage)
        started = _started_evidence(run_id=run_id)
        final = _final_evidence(run_id=run_id)
        await _claim_usage(
            storage=storage,
            run_id=run_id,
            usage_call_id=usage_call_id,
            started=started,
        )
        bus = event_bus(storage=storage, event_path=event_path)

        # 同一调用标识只能对应首次 durable started identity；先验证 outbox 层
        # 拒绝篡改，再验证 sink 不会让伪造事件绕过这道持久化边界。
        forged_started = started.model_copy(update={"provider": "forged-provider"})
        with pytest.raises(ValueError, match="another started identity"):
            async with storage.uow() as uow:
                await uow.evidence_outbox.claim_usage(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    usage_call_id=usage_call_id,
                    event_id=f"usage:tenant-a:{usage_call_id}:final",
                    operation_kind=EvidenceOperationKind.MODEL_USAGE,
                    started_evidence=forged_started.to_payload(),
                )
        forged_result = final.model_copy(update={"model": "forged-model"})
        with pytest.raises(ValueError, match="durable started identity"):
            async with storage.uow() as uow:
                await uow.evidence_outbox.persist_result(
                    tenant_id="tenant-a",
                    usage_call_id=usage_call_id,
                    result={
                        "evidence": forged_result.to_payload(),
                        "outcome": "completed",
                    },
                )
        with pytest.raises(ValueError, match="durable settlement"):
            await UsageEvidenceLifecycle(
                event_bus=bus,
                evidence=forged_started,
                usage_call_id=usage_call_id,
            ).publish_started()
        assert not event_path.exists()

        await UsageEvidenceLifecycle(
            event_bus=bus,
            evidence=started,
            usage_call_id=usage_call_id,
        ).publish_started()
        await _persist_final(storage=storage, usage_call_id=usage_call_id, final=final)

        # final 事件必须完整复用已持久化的结果。分别替换 provider、用量、
        # 关联身份和结果状态，确保比较的是完整正文而非只比较调用标识。
        forged_finals = [
            (final.model_copy(update={"provider": "forged-provider"}), "completed", None),
            (final.model_copy(update={"input_tokens": 999_999}), "completed", None),
            (final.model_copy(update={"agent_id": "agent-forged"}), "completed", None),
            (final.model_copy(update={"request_id": "request-forged"}), "completed", None),
            (final, "failed", "model.provider_failed"),
        ]
        for forged, outcome, error_code in forged_finals:
            with pytest.raises(ValueError, match="durable settlement"):
                await _publish_forged_final(
                    bus=bus,
                    usage_call_id=usage_call_id,
                    evidence=forged,
                    outcome=outcome,
                    error_code=error_code,
                )

        events = await LocalJsonlEventSink(event_path).read(run_id=run_id)
        async with storage.uow() as uow:
            snapshot = await uow.event_capacity.snapshot(run_id)
        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
        assert snapshot.highest_persisted_seq == 1
        assert snapshot.outstanding_reserved_event_count == 1

        # 只有真实 final 通过时才消费第二格预约；这也证明前面的失败写入没有
        # 偷偷推进序号或改变容量高水位。
        await UsageEvidenceLifecycle(
            event_bus=bus,
            evidence=final,
            usage_call_id=usage_call_id,
        ).publish_final()
        async with storage.uow() as uow:
            settled = await uow.event_capacity.snapshot(run_id)
        assert settled.highest_persisted_seq == 2
        assert settled.outstanding_reserved_event_count == 0
    finally:
        await storage.dispose()


async def _assert_six_route_externalized_usage(
    tmp_path: Path,
    *,
    storage_dsn: str | None = None,
    use_postgresql_event_sink: bool = False,
) -> None:
    """经公共六路调用 seam 验证超限 usage 的 artifact 与容量绑定。"""

    artifact_store = FileArtifactStore(tmp_path / "six-route-artifacts")
    scripts = {
        str(route["deployment_id"]): [
            "completed" if ordinal == len(ROUTES) else "client_not_started"
        ]
        for ordinal, route in enumerate(ROUTES, start=1)
    }
    fixture: BoundFailoverFixture | None = None
    try:
        fixture = await bound_failover_invocation(
            tmp_path,
            scripts=scripts,
            storage_dsn=storage_dsn,
            route_count=6,
            artifact_store=artifact_store,
            use_postgresql_event_sink=use_postgresql_event_sink,
        )
        response = await fixture.bound.complete(
            ModelRequest(prompt="hello", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )
        assert response.model == ROUTES[-1]["model_id"]
        assert fixture.provider.trace == [
            *(f"prepare:{route['deployment_id']}" for route in ROUTES),
            f"send:{ROUTES[-1]['deployment_id']}",
        ]

        events = await fixture.sink.read(run_id=fixture.run_id)
        usage_events = [
            item
            for item in events
            if item.event_type
            in {
                CanonicalEventType.MODEL_REQUEST_STARTED,
                CanonicalEventType.MODEL_USAGE_UPDATED,
            }
        ]
        assert [item.event_type for item in usage_events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        for item in usage_events:
            assert item.payload_ref is not None
            assert item.payload_checksum is not None
            assert item.payload is not None
            assert item.payload["correlation"]["usage_call_id"] == fixture.usage_call_id
            assert item.payload["usage"]["usage_kind"] == "model"
            artifact_payload = artifact_store.read_json(item.payload_ref)
            artifact_bytes = canonical_json_bytes(artifact_payload)
            artifact_checksum = hashlib.sha256(artifact_bytes).hexdigest()
            assert len(artifact_bytes) > 8_192
            assert item.payload["artifact"]["size_bytes"] == len(artifact_bytes)
            assert item.payload_ref == f"artifact://{artifact_checksum}"
            assert item.payload_checksum == artifact_checksum

            route_chain = artifact_payload["usage"]["decision"]["route_chain"]
            assert route_chain["schema_version"] == "model-route-chain-evidence-v1"
            assert route_chain["identity"]["candidate_count"] == 6
            assert route_chain["state"]["candidate_count"] == 6
            assert [
                candidate["deployment_id"] for candidate in route_chain["identity"]["candidates"]
            ] == [route["deployment_id"] for route in ROUTES]
        async with fixture.storage.uow() as uow:
            snapshot = await uow.event_capacity.snapshot(fixture.run_id)
        assert snapshot.highest_persisted_seq == 2
        assert snapshot.outstanding_reserved_event_count == 0
    finally:
        if fixture is not None:
            await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_local_six_route_externalized_usage_keeps_capacity_binding(tmp_path: Path) -> None:
    """真实六路 decision 超过 8 KiB 后，本地 sink 仍按完整 artifact 结算。"""

    await _assert_six_route_externalized_usage(tmp_path)


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL 六路 usage artifact 合同需要测试 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_six_route_externalized_usage_keeps_capacity_binding(
    tmp_path: Path,
) -> None:
    """同一六路超限 usage 必须通过 PostgreSQL claim、artifact 与容量事务。"""

    async with isolated_database("six_route_usage_artifact") as dsn:
        await _assert_six_route_externalized_usage(
            tmp_path,
            storage_dsn=dsn,
            use_postgresql_event_sink=True,
        )


@pytest.mark.asyncio
async def test_local_usage_final_requires_persisted_started_event(tmp_path: Path) -> None:
    """仅有 claim/result 不能跳过 canonical started 直接写 final。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'final-order.db'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    event_path = tmp_path / "final-order.jsonl"
    usage_call_id = "final-before-started"
    try:
        run_id = await seed_run(storage)
        started = _started_evidence(run_id=run_id)
        final = _final_evidence(run_id=run_id)
        await _claim_usage(
            storage=storage,
            run_id=run_id,
            usage_call_id=usage_call_id,
            started=started,
        )
        await _persist_final(storage=storage, usage_call_id=usage_call_id, final=final)

        with pytest.raises(RuntimeError, match="started event"):
            await UsageEvidenceLifecycle(
                event_bus=event_bus(storage=storage, event_path=event_path),
                evidence=final,
                usage_call_id=usage_call_id,
            ).publish_final()
        assert not event_path.exists()
        async with storage.uow() as uow:
            snapshot = await uow.event_capacity.snapshot(run_id)
        assert snapshot.highest_persisted_seq == 0
        assert snapshot.outstanding_reserved_event_count == 2
    finally:
        await storage.dispose()


@pytest.mark.skipif(
    not os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN"),
    reason="真实 PostgreSQL usage payload binding 合同需要测试 DSN。",
)
@pytest.mark.asyncio
async def test_postgresql_usage_payload_and_phase_are_bound_to_settlement() -> None:
    """PostgreSQL sink 必须与 local 使用同一逐值绑定和 phase 顺序。"""

    async with isolated_database("usage_payload_binding") as dsn:
        run_migrations(dsn)
        storage = SQLAlchemyStorage.from_dsn(dsn)
        usage_call_id = "bound-postgresql"
        try:
            run_id = await seed_run(storage)
            started = _started_evidence(run_id=run_id)
            final = _final_evidence(run_id=run_id)
            await _claim_usage(
                storage=storage,
                run_id=run_id,
                usage_call_id=usage_call_id,
                started=started,
            )

            async def resolve_trace(**_: object) -> str:
                """提供已绑定 trace，避免 PostgreSQL sink 以猜测值补全运行归属。"""

                return "trace-a"

            bus = EventBus(
                sink=PostgreSQLEventSink(storage),
                run_trace_resolver=resolve_trace,
            )
            # 先验证数据库事务不会接受替换后的 started 身份；随后在结果已持久化
            # 但 canonical started 缺失时验证 final 仍被严格阻止。
            forged_started = started.model_copy(update={"model": "forged-model"})
            with pytest.raises(ValueError, match="durable settlement"):
                await UsageEvidenceLifecycle(
                    event_bus=bus,
                    evidence=forged_started,
                    usage_call_id=usage_call_id,
                ).publish_started()

            await _persist_final(storage=storage, usage_call_id=usage_call_id, final=final)
            with pytest.raises(RuntimeError, match="started event"):
                await UsageEvidenceLifecycle(
                    event_bus=bus,
                    evidence=final,
                    usage_call_id=usage_call_id,
                ).publish_final()

            await UsageEvidenceLifecycle(
                event_bus=bus,
                evidence=started,
                usage_call_id=usage_call_id,
            ).publish_started()
            forged_final = final.model_copy(update={"input_tokens": 999_999})
            # started 已落库后，仅篡改 final 使用量仍必须失败，且容量只能保留在
            # 已写的 started 事件所消费的一格，不能被 forged final 结算。
            with pytest.raises(ValueError, match="durable settlement"):
                await _publish_forged_final(
                    bus=bus,
                    usage_call_id=usage_call_id,
                    evidence=forged_final,
                )
            async with storage.uow() as uow:
                snapshot = await uow.event_capacity.snapshot(run_id)
            assert snapshot.highest_persisted_seq == 1
            assert snapshot.outstanding_reserved_event_count == 1
        finally:
            await storage.dispose()
