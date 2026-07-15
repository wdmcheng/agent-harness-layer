"""Usage 事件正文必须逐值绑定 durable settlement，且 started 先于 final。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.embedding_cache_postgresql_migration_contract_helpers import (
    isolated_database,
)
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.events import (
    CanonicalEventType,
    EventBus,
    LocalJsonlEventSink,
    PostgreSQLEventSink,
)
from agent_harness.models import ModelUsageEvidence, UsageEvidenceLifecycle
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.evidence_repositories import EvidenceOperationKind


def _started_evidence(*, run_id: str) -> ModelUsageEvidence:
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
    """用受信 invocation 在 provider 副作用前冻结 started 身份。"""

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
                return "trace-a"

            bus = EventBus(
                sink=PostgreSQLEventSink(storage),
                run_trace_resolver=resolve_trace,
            )
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
