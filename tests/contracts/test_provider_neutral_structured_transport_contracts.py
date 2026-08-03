"""结构化transport、cleanup与capability的公开seam合同。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.config import ModelSettings
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    BoundModelInvocationService,
    ModelAttemptEvidence,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
    OutputSchemaDefinition,
    StructuredModelAttemptEvidence,
    StructuredProviderCallError,
    StructuredProviderCandidate,
    StructuredProviderPrepareError,
    UsageInvocationReplayError,
    compile_output_schema,
)
from agent_harness.registry import AgentModelPolicy
from agent_harness.storage import SQLAlchemyStorage, run_migrations


class _Output(BaseModel):
    """Transport 夹具使用的严格输出。"""

    model_config = ConfigDict(extra="forbid")

    answer: str


class _TwoAttemptRouter(ModelRouter):
    """只把测试 route 的核心 transport 上限冻结为二。"""

    def plan(
        self,
        request: ModelRequest,
        *,
        config: ModelRouterConfig | None = None,
        agent_policy: Any | None = None,
    ) -> ModelRoutePlan:
        """保留父类 planning，只调整本合同验证的 retry/deadline 参数。"""

        plan = super().plan(request, config=config, agent_policy=agent_policy)
        return plan.model_copy(
            update={
                "max_attempts": 2,
                "connect_timeout_ms": 1_000,
                "read_timeout_ms": 1_000,
                "total_timeout_ms": 1_000,
                "retry_policy": plan.retry_policy.model_copy(
                    update={
                        "max_attempts": 2,
                        "max_wait_ms": 10,
                        "backoff_initial_ms": 1,
                        "backoff_max_ms": 1,
                    }
                ),
            }
        )


class _ShortDeadlineRouter(_TwoAttemptRouter):
    """把 total deadline 缩短到 100ms，稳定触发 send timeout。"""

    def plan(
        self,
        request: ModelRequest,
        *,
        config: ModelRouterConfig | None = None,
        agent_policy: Any | None = None,
    ) -> ModelRoutePlan:
        """复用双 attempt route，只缩短本 case 的单一绝对 deadline。"""

        return (
            super()
            .plan(request, config=config, agent_policy=agent_policy)
            .model_copy(
                update={
                    "connect_timeout_ms": 100,
                    "read_timeout_ms": 100,
                    "total_timeout_ms": 100,
                }
            )
        )


class _BackoffCancellationRouter(_TwoAttemptRouter):
    """延长 prepare retry backoff，给取消窗口提供确定性调度边界。"""

    def plan(
        self,
        request: ModelRequest,
        *,
        config: ModelRouterConfig | None = None,
        agent_policy: Any | None = None,
    ) -> ModelRoutePlan:
        """保留双 attempt 语义，只把 backoff 延长到测试可控范围。"""

        plan = super().plan(request, config=config, agent_policy=agent_policy)
        return plan.model_copy(
            update={
                "total_timeout_ms": 5_000,
                "retry_policy": plan.retry_policy.model_copy(
                    update={
                        "max_wait_ms": 2_000,
                        "backoff_initial_ms": 2_000,
                        "backoff_max_ms": 2_000,
                    }
                ),
            }
        )


class _Prepared:
    """一次性 prepared handle；行为由 owner provider 显式控制。"""

    def __init__(self, owner: _ControlledProvider) -> None:
        self._owner = owner
        self._closed = False

    async def send_structured(
        self,
        *,
        provider_prompt: str,
        repair_ordinal: int,
        transport_ordinal: int,
    ) -> StructuredProviderCandidate:
        """每次调用只记录一个 request，不在 double 内部重试。"""

        assert provider_prompt
        self._owner.sends.append((repair_ordinal, transport_ordinal))
        if self._owner.send_started is not None:
            self._owner.send_started.set()
        if self._owner.send_gate is not None:
            await self._owner.send_gate.wait()
        if self._owner.send_delay:
            await asyncio.sleep(self._owner.send_delay)
        if self._owner.crash_after_send:
            raise _CrashAfterSend
        attempt = ModelAttemptEvidence(
            attempt=1,
            side_effect_state="started",
            outcome="failed" if self._owner.call_error else "completed",
            completion_observed=True,
            input_tokens=None if self._owner.omit_usage else 1,
            output_tokens=None if self._owner.omit_usage else 1,
            cost_status="unavailable",
            latency_ms=1,
            error_code="model.provider_failed" if self._owner.call_error else None,
        )
        if self._owner.call_error:
            raise StructuredProviderCallError(
                code="model.provider_failed",
                attempts=[attempt],
            )
        return StructuredProviderCandidate(
            schema_identity=(
                self._owner.schema.identity.model_copy(update={"digest": "f" * 64})
                if self._owner.schema_identity_drift
                else self._owner.schema.identity
            ),
            provider=self._owner.candidate_provider or self._owner.provider_id,
            model="model-a",
            candidate={"answer": "ok"},
            attempts=[attempt],
        )

    async def aclose(self) -> None:
        """close 只允许一次，并可显式模拟 cleanup failure。"""

        assert not self._closed
        self._closed = True
        self._owner.closes += 1
        try:
            if self._owner.close_started is not None:
                self._owner.close_started.set()
            if self._owner.close_gate is not None:
                while True:
                    try:
                        await self._owner.close_gate.wait()
                        break
                    except asyncio.CancelledError:
                        self._owner.close_cancellations += 1
                        if not self._owner.close_resists_cancel:
                            raise
            if self._owner.close_error:
                raise RuntimeError("controlled close failure")
        finally:
            if self._owner.close_finished is not None:
                self._owner.close_finished.set()


class _ControlledProvider:
    """覆盖 prepare retry、send failure 与 cleanup failure 的 provider double。"""

    provider_id = "provider-a"

    def __init__(
        self,
        schema: OutputSchemaDefinition,
        *,
        prepare_failures: int = 0,
        call_error: bool = False,
        close_error: bool = False,
        send_delay: float = 0,
        prepare_gate: asyncio.Event | None = None,
        prepare_started: asyncio.Event | None = None,
        send_gate: asyncio.Event | None = None,
        send_started: asyncio.Event | None = None,
        close_gate: asyncio.Event | None = None,
        close_started: asyncio.Event | None = None,
        close_finished: asyncio.Event | None = None,
        close_resists_cancel: bool = False,
        crash_after_send: bool = False,
        schema_identity_drift: bool = False,
        candidate_provider: str | None = None,
        omit_usage: bool = False,
    ) -> None:
        self.schema = schema
        self.prepare_failures = prepare_failures
        self.call_error = call_error
        self.close_error = close_error
        self.send_delay = send_delay
        self.prepare_gate = prepare_gate
        self.prepare_started = prepare_started
        self.send_gate = send_gate
        self.send_started = send_started
        self.close_gate = close_gate
        self.close_started = close_started
        self.close_finished = close_finished
        self.close_resists_cancel = close_resists_cancel
        self.crash_after_send = crash_after_send
        self.schema_identity_drift = schema_identity_drift
        self.candidate_provider = candidate_provider
        self.omit_usage = omit_usage
        self.prepares = 0
        self.sends: list[tuple[int, int]] = []
        self.closes = 0
        self.close_cancellations = 0

    async def prepare_structured(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        schema: OutputSchemaDefinition,
    ) -> _Prepared:
        """显式 retryable 失败证明 prepare 边界尚未 send。"""

        assert request.capability == "structured_output"
        assert plan.provider == self.provider_id
        assert schema == self.schema
        self.prepares += 1
        if self.prepare_started is not None:
            self.prepare_started.set()
        if self.prepare_gate is not None:
            await self.prepare_gate.wait()
        if self.prepares <= self.prepare_failures:
            raise StructuredProviderPrepareError(retryable=True)
        return _Prepared(self)


class _TextOnlyProvider:
    """只实现普通文本协议，用于证明 capability 在 claim 前关闭。"""

    provider_id = "provider-a"

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        """本方法不应被 structured seam 调用。"""

        raise AssertionError((request, plan))


class _CrashAfterSend(BaseException):
    """模拟 durable mark 已提交、send 边界后进程被强制终止。"""


async def _bound(
    tmp_path: Path,
    *,
    provider: object,
    schema: OutputSchemaDefinition,
    router_type: type[_TwoAttemptRouter] = _TwoAttemptRouter,
    model_settings: ModelSettings | None = None,
    agent_policy_resolver: Callable[[str], AgentModelPolicy] | None = None,
    provider_key: str = "provider-a",
) -> tuple[ModelInvocationService, SQLAlchemyStorage, BoundModelInvocationService, str]:
    """构造两次 transport 上限的 SQLite 公开 bound seam。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured-transport.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    router = router_type(
        config=ModelRouterConfig(default_provider=provider_key, default_model="model-a"),
        providers={provider_key: provider},  # type: ignore[dict-item]
        model_settings=model_settings,
    )
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=tmp_path / "events.jsonl"),
        output_schema_resolver=lambda _agent_id: schema,
        agent_policy_resolver=agent_policy_resolver,
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
    return service, storage, bound, run_id


def _schema() -> OutputSchemaDefinition:
    """所有 transport case 共享同一稳定 schema identity。"""

    return compile_output_schema(
        _Output,
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
    )


def _request() -> ModelRequest:
    """返回 provider-neutral structured 业务请求。"""

    return ModelRequest(
        provider="provider-a",
        model="model-a",
        prompt="return an answer",
        max_output_tokens=8,
    )


# 取消与崩溃恢复测试分文件后通过稳定夹具名复用受控provider与公开bound入口。
ControlledStructuredProvider = _ControlledProvider
CrashAfterStructuredSend = _CrashAfterSend
BackoffCancellationRouter = _BackoffCancellationRouter
ShortStructuredDeadlineRouter = _ShortDeadlineRouter
build_structured_bound = _bound
structured_request = _request
structured_schema = _schema


@pytest.mark.asyncio
async def test_retryable_prepare_advances_transport_before_single_send(tmp_path: Path) -> None:
    """唯一允许的 retry 发生在 prepare 明确证明未 send 之后。"""

    schema = _schema()
    provider = _ControlledProvider(schema, prepare_failures=1)
    service, storage, bound, _run_id = await _bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        response = await bound.complete_structured(
            _request(),
            operation_key="prepare-retry",
        )
        assert response.structured_output is not None
        assert response.structured_output.provider_request_count == 1
        assert provider.prepares == 2
        assert provider.sends == [(0, 2)]
        assert provider.closes == 1
        assert len(response.attempts) == 2
        assert isinstance(response.attempts[0], StructuredModelAttemptEvidence)
        assert isinstance(response.attempts[1], StructuredModelAttemptEvidence)
        assert response.attempts[0].side_effect_state == "not_started"
        assert response.attempts[0].structured_output.not_started_proof is not None
        assert response.attempts[1].structured_output.cleanup_status == "completed"
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_durable_not_started_proof_tamper_is_rejected_without_resend(
    tmp_path: Path,
) -> None:
    """Proof 任一 identity/digest 被同步篡改都不得通过公开 replay validator。"""

    schema = _schema()
    provider = _ControlledProvider(schema, prepare_failures=1)
    service, storage, bound, run_id = await _bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        await bound.complete_structured(
            _request(),
            operation_key="proof-tamper",
        )
        assert provider.sends == [(0, 2)]
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
            assert len(rows) == 1
            row = rows[0]
            assert row.result_json is not None
            result = deepcopy(row.result_json)
            proof = result["evidence"]["decision"]["attempts"][0]["structured_output"][
                "not_started_proof"
            ]
            proof["route_digest"] = "f" * 64
            row.result_json = result
            await uow.commit()

        with pytest.raises(UsageInvocationReplayError):
            await bound.complete_structured(
                _request(),
                operation_key="proof-tamper",
            )
        assert provider.sends == [(0, 2)]
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_retryable_prepare_exhaustion_is_bounded_and_never_sends(tmp_path: Path) -> None:
    """两次带 proof 的 prepare 失败耗尽冻结 transport 上限后不得形成 request。"""

    schema = _schema()
    provider = _ControlledProvider(schema, prepare_failures=2)
    service, storage, bound, _run_id = await _bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                _request(),
                operation_key="prepare-retry-exhausted",
            )
        assert failure.value.code == "model.provider_retry_exhausted"
        assert failure.value.provider_called is False
        assert failure.value.attempt_count == 2
        assert provider.prepares == 2
        assert provider.sends == []
        assert provider.closes == 0
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_send_failure_never_uses_remaining_transport_attempt(tmp_path: Path) -> None:
    """一旦到达 send 边界，确定失败也不得启动第二个 transport。"""

    schema = _schema()
    provider = _ControlledProvider(schema, call_error=True)
    service, storage, bound, _run_id = await _bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                _request(),
                operation_key="send-failed",
            )
        assert failure.value.code == "model.provider_failed"
        assert provider.prepares == 1
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_cleanup_failure_after_candidate_is_needs_review(tmp_path: Path) -> None:
    """候选存在但 cleanup 失败时禁止发布 valid 或启动 repair。"""

    schema = _schema()
    provider = _ControlledProvider(schema, close_error=True)
    service, storage, bound, _run_id = await _bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                _request(),
                operation_key="cleanup-unknown",
                repair_limit=1,
            )
        assert failure.value.code == "model.provider_side_effect_unknown"
        assert provider.prepares == 1
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_missing_structured_protocol_fails_before_usage_claim(tmp_path: Path) -> None:
    """Provider 未实现公开协议时不得创建 outbox 或调用 text fallback。"""

    schema = _schema()
    service, storage, bound, run_id = await _bound(
        tmp_path,
        provider=_TextOnlyProvider(),
        schema=schema,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                _request(),
                operation_key="unsupported-provider",
            )
        assert failure.value.code == "model.structured_capability_unsupported"
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
        assert rows == []
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_send_timeout_is_needs_review_and_never_retries(tmp_path: Path) -> None:
    """单一 deadline 在 send 中耗尽时，已到达副作用边界并立即围栏。"""

    schema = _schema()
    provider = _ControlledProvider(schema, send_delay=1)
    service, storage, bound, _run_id = await _bound(
        tmp_path,
        provider=provider,
        schema=schema,
        router_type=_ShortDeadlineRouter,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                _request(),
                operation_key="send-timeout",
            )
        assert failure.value.code == "model.provider_side_effect_unknown"
        assert provider.prepares == 1
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1
    finally:
        await service.aclose()
        await storage.dispose()
