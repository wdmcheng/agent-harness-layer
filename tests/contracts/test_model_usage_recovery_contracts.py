"""Model usage 耐久失败 payload 校验与恢复合同测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import update
from tests.contracts.model_usage_recovery_test_support import usage_run as _usage_run

from agent_harness.events import CanonicalEvent, CanonicalEventType, EventBus, LocalJsonlEventSink
from agent_harness.models import (
    FakeModelProvider,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
    UsageInvocationReplayError,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.models import RunEvidenceOutboxModel


class _CountingFailProvider(FakeModelProvider):
    """制造一次可计数的 provider 失败，供耐久失败恢复断言外部副作用次数。"""

    def __init__(self) -> None:
        """按实例保存调用次数，避免参数化用例之间共享可变状态。"""

        self.calls = 0

    async def complete(self, request: ModelRequest, *, plan: object) -> ModelResponse:
        """记录调用后抛出封闭前的原始异常，让生产路径生成标准稳定失败。"""

        del request, plan
        self.calls += 1
        raise RuntimeError("injected provider failure")


class _FailFinalWriteSink:
    """允许 started 落盘，但在 final 写入前失败以冻结 result_persisted 窗口。"""

    manages_event_capacity = False

    def __init__(self, delegate: LocalJsonlEventSink) -> None:
        """保存耐久 delegate；恢复阶段会绕过本故障包装器。"""

        self.delegate = delegate

    async def write(self, event: CanonicalEvent) -> CanonicalEvent:
        """只阻断模型 usage 最终事件，其他事件保持真实持久化行为。"""

        if event.event_type is CanonicalEventType.MODEL_USAGE_UPDATED:
            raise OSError("injected final write failure")
        return await self.delegate.write(event)

    async def read(self, *, run_id: str, after_seq: int = 0) -> list[CanonicalEvent]:
        """透传读取以满足 EventBus sink 协议。"""

        return await self.delegate.read(run_id=run_id, after_seq=after_seq)

    async def latest_seq(self, run_id: str) -> int:
        """透传最后序号查询以满足 EventBus sink 协议。"""

        return await self.delegate.latest_seq(run_id)

    async def has_terminal(self, run_id: str) -> bool:
        """透传终结查询以满足 EventBus sink 协议。"""

        return await self.delegate.has_terminal(run_id)


async def _seed_result_persisted_failure(
    tmp_path: Path,
    *,
    case: str,
) -> tuple[
    SQLAlchemyStorage,
    LocalJsonlEventSink,
    ModelRouter,
    ModelRequest,
    UsageEvidenceContext,
    str,
    _CountingFailProvider,
]:
    """经公开调用制造真实稳定失败，并把状态停在 final 尚未发布的耐久窗口。"""

    database = tmp_path / f"settlement-validation-{case}.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)

    async def resolve_trace(**_: object) -> str:
        """为恢复用例提供固定 trace，使断言只聚焦结算 payload。"""

        return "trace-a"

    sink = LocalJsonlEventSink(
        tmp_path / f"settlement-validation-{case}.jsonl",
        run_trace_resolver=resolve_trace,
    )
    run_id = await _usage_run(storage)
    provider = _CountingFailProvider()
    router = ModelRouter(
        config=ModelRouterConfig(default_model="fake-basic"),
        providers={"fake": provider},
    )
    request = ModelRequest(provider="fake", prompt="hello", max_output_tokens=1)
    context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        trace_id="trace-a",
    )
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=EventBus(
            sink=_FailFinalWriteSink(sink),
            run_trace_resolver=resolve_trace,
        ),
    )
    with pytest.raises(OSError, match="injected final write failure"):
        await service.complete(
            request,
            context=context,
            usage_call_id=f"usage-{case}",
        )
    assert provider.calls == 1
    async with storage.uow() as uow:
        usage = await uow.evidence_outbox.get_usage(
            tenant_id="tenant-a",
            usage_call_id=f"usage-{case}",
        )
        assert usage.state == "result_persisted"
    return storage, sink, router, request, context, f"usage-{case}", provider


async def _corrupt_result_persisted_failure(
    storage: SQLAlchemyStorage,
    *,
    usage_call_id: str,
    corruption: str,
) -> None:
    """模拟历史脏数据或存储损坏；服务入口仍必须把它视为不可信恢复输入。"""

    async with storage.uow() as uow:
        usage = await uow.evidence_outbox.get_usage(
            tenant_id="tenant-a",
            usage_call_id=usage_call_id,
        )
        assert usage.result_json is not None
        result = dict(usage.result_json)
        if corruption == "failure_response_conflict":
            result["response"] = {
                "provider": "fake",
                "model": "fake-basic",
                "output_text": "forged-success",
                "decision": {"action": "call", "estimated_tokens": 1},
                "token_usage": {"input_tokens": 1, "output_tokens": 1},
            }
        elif corruption == "missing_evidence":
            result.pop("evidence")
        elif corruption == "malformed_evidence":
            result["evidence"] = {"usage_kind": "model"}
        elif corruption == "failure_evidence_call_mismatch":
            evidence = dict(result["evidence"])
            evidence["decision"] = {
                **evidence["decision"],
                "provider_called": False,
                "attempts": [],
            }
            result["evidence"] = evidence
        elif corruption == "failure_evidence_attempt_mismatch":
            evidence = dict(result["evidence"])
            evidence["decision"] = {
                **evidence["decision"],
                "attempts": [{"attempt": 1}, {"attempt": 2}],
            }
            result["evidence"] = evidence
        elif corruption == "failure_evidence_latency_mismatch":
            evidence = dict(result["evidence"])
            evidence["latency_ms"] = evidence["latency_ms"] + 1
            result["evidence"] = evidence
        elif corruption.startswith("failure_evidence_nested_"):
            evidence = dict(result["evidence"])
            decision = dict(evidence["decision"])
            attempt = {
                "attempt": 1,
                "outcome": "unknown",
                "side_effect_state": "unknown",
                "completion_observed": None,
                "http_status": None,
                "retry_after_ms": None,
                "input_tokens": None,
                "output_tokens": None,
                "cost_usd": None,
                "cost_status": "unavailable",
                "budget_charge_tokens": None,
                "budget_charge_cost_usd": None,
                "latency_ms": 0,
                "error_code": "model.provider_failed",
            }
            budget_charge = {
                "charged_tokens": None,
                "charged_cost_usd": None,
                "charge_status": "unknown",
                "unresolved_attempts": [1],
            }
            if corruption == "failure_evidence_nested_attempt_schema":
                attempt = {"attempt": 1}
            elif corruption == "failure_evidence_nested_attempt_ordinal":
                attempt["attempt"] = 2
            elif corruption == "failure_evidence_nested_attempt_charge":
                attempt["budget_charge_tokens"] = 0
            elif corruption == "failure_evidence_nested_attempt_boolean":
                attempt["completion_observed"] = 1
            elif corruption == "failure_evidence_nested_budget_schema":
                budget_charge.pop("unresolved_attempts")
            else:
                budget_charge.update(charge_status="unknown", unresolved_attempts=[])
            decision.update(attempts=[attempt], budget_charge=budget_charge)
            evidence["decision"] = decision
            result["evidence"] = evidence
        elif corruption == "missing_outcome":
            result.pop("outcome")
        else:
            result["outcome"] = {"forged": "completed"}
        await uow.session.execute(
            update(RunEvidenceOutboxModel)
            .where(RunEvidenceOutboxModel.id == usage.id)
            .values(result_json=result)
        )
        await uow.commit()


async def _assert_result_persisted(
    storage: SQLAlchemyStorage,
    *,
    usage_call_id: str,
) -> None:
    """证明校验失败没有越过 final 发布与 outbox 状态转换边界。"""

    async with storage.uow() as uow:
        usage = await uow.evidence_outbox.get_usage(
            tenant_id="tenant-a",
            usage_call_id=usage_call_id,
        )
        assert usage.state == "result_persisted"


_CORRUPTED_SETTLEMENT_CASES = [
    "failure_response_conflict",
    "missing_evidence",
    "malformed_evidence",
    "failure_evidence_call_mismatch",
    "failure_evidence_attempt_mismatch",
    "failure_evidence_latency_mismatch",
    "failure_evidence_nested_attempt_schema",
    "failure_evidence_nested_attempt_ordinal",
    "failure_evidence_nested_attempt_charge",
    "failure_evidence_nested_attempt_boolean",
    "failure_evidence_nested_budget_schema",
    "failure_evidence_nested_budget_charge",
    "missing_outcome",
    "malformed_outcome",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", _CORRUPTED_SETTLEMENT_CASES)
async def test_complete_validates_durable_failure_before_final_publication(
    tmp_path: Path,
    corruption: str,
) -> None:
    """公开 complete 重放遇到脏结果时必须零 provider 重放、零 final、零状态推进。"""

    seeded = await _seed_result_persisted_failure(
        tmp_path,
        case=f"complete-{corruption}",
    )
    storage, sink, router, request, context, usage_call_id, provider = seeded

    try:
        await _corrupt_result_persisted_failure(
            storage,
            usage_call_id=usage_call_id,
            corruption=corruption,
        )
        service = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=sink),
        )
        with pytest.raises(UsageInvocationReplayError):
            await service.complete(
                request,
                context=context,
                usage_call_id=usage_call_id,
            )

        assert provider.calls == 1
        events = await sink.read(run_id=context.run_id)
        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
        await _assert_result_persisted(storage, usage_call_id=usage_call_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", _CORRUPTED_SETTLEMENT_CASES)
async def test_recover_pending_validates_durable_failure_before_final_publication(
    tmp_path: Path,
    corruption: str,
) -> None:
    """后台恢复与 complete 共用 fail-closed 边界，不能发布或吞掉损坏的稳定失败。"""

    seeded = await _seed_result_persisted_failure(
        tmp_path,
        case=f"recover-{corruption}",
    )
    storage, sink, router, _request, context, usage_call_id, provider = seeded

    try:
        await _corrupt_result_persisted_failure(
            storage,
            usage_call_id=usage_call_id,
            corruption=corruption,
        )
        service = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=sink),
        )
        with pytest.raises(UsageInvocationReplayError):
            await service.recover_pending(run_id=context.run_id)

        assert provider.calls == 1
        events = await sink.read(run_id=context.run_id)
        assert [event.event_type for event in events] == [CanonicalEventType.MODEL_REQUEST_STARTED]
        await _assert_result_persisted(storage, usage_call_id=usage_call_id)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["complete", "recover_pending"])
async def test_legal_result_persisted_failure_publishes_once_without_provider_replay(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    """合法稳定失败仍可补投 final；共享校验不能把正常故障恢复误判为脏数据。"""

    seeded = await _seed_result_persisted_failure(tmp_path, case=f"legal-{entrypoint}")
    storage, sink, router, request, context, usage_call_id, provider = seeded

    try:
        service = ModelInvocationService(
            router=router,
            storage=storage,
            event_bus=EventBus(sink=sink),
        )
        if entrypoint == "complete":
            with pytest.raises(ModelProviderInvocationError) as exc_info:
                await service.complete(
                    request,
                    context=context,
                    usage_call_id=usage_call_id,
                )
            assert exc_info.value.code == "model.provider_failed"
            assert exc_info.value.provider_called is True
            assert exc_info.value.attempt_count == 1
        else:
            assert await service.recover_pending(run_id=context.run_id) == 1

        assert provider.calls == 1
        events = await sink.read(run_id=context.run_id)
        assert [event.event_type for event in events] == [
            CanonicalEventType.MODEL_REQUEST_STARTED,
            CanonicalEventType.MODEL_USAGE_UPDATED,
        ]
        async with storage.uow() as uow:
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
            )
            assert usage.state == "published"
    finally:
        await storage.dispose()
