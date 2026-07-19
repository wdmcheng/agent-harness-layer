"""嵌入调用的耐久用量生命周期，协调缓存、预算、事件与 Provider 副作用。"""

from __future__ import annotations

from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol

from agent_harness.embeddings._invocation_settlement import (
    EmbeddingProviderInvocationError,
    _EmbeddingSettlementMixin,
)
from agent_harness.embeddings.provider import (
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResponse,
    PreflightEmbeddingCacheProvider,
)
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.models.usage import (
    ModelUsageEvidence,
    UsageEvidenceContext,
    UsageInvocationReplayError,
    embedding_usage_evidence,
    stable_usage_call_id,
)
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.event_capacity_repositories import EventCapacityExceeded
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
)
from agent_harness.storage.shared_budget import (
    BudgetReservationRejected,
    OperationIdentity,
)


class _SharedBudgetIdentityRuntime(Protocol):
    """嵌入服务依赖的共享预算身份与定价解析最小协议。"""

    def operation_identity(self, **values: Any) -> OperationIdentity:
        """从受控运行快照构造不可由 Agent 输入伪造的预算操作身份。"""
        ...

    def embedding_price_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        provider: str,
        model: str,
    ) -> tuple[Decimal | None, str, str]:
        """从快照解析嵌入单价和定价来源版本，供结算保留可复核依据。"""
        ...


class BoundEmbeddingInvocationService:
    """只向单个 run 的业务 executor 暴露 embedding 请求与操作槽位。"""

    def __init__(
        self,
        *,
        service: EmbeddingInvocationService,
        context: UsageEvidenceContext,
    ) -> None:
        """绑定原始服务与已固定的运行上下文，禁止业务 executor 传入任意身份。"""
        self._service = service
        self._context = context

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        operation_key: str,
    ) -> EmbeddingResponse:
        """由可信 runtime 关联生成 call ID，业务输入不能覆盖身份。"""

        return await self._service.embed(
            request,
            context=self._context,
            usage_call_id=stable_usage_call_id(
                context=self._context,
                operation_key=operation_key,
            ),
        )


class EmbeddingInvocationService(_EmbeddingSettlementMixin):
    """在 cache/provider 副作用前预约，并用统一 model event type 结算。"""

    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        telemetry: TelemetryFacade | None = None,
        shared_budget: _SharedBudgetIdentityRuntime | None = None,
        input_token_price_usd: Decimal | None = None,
        price_source_ref: str | None = None,
        price_source_version: str | None = None,
    ) -> None:
        """装配 Provider、存储、事件和可选预算/遥测依赖。

        价格与预算配置来自受控 composition，而非调用请求；这样同一 usage call
        在重放和恢复时能恢复首次选择的身份与结算规则。
        """
        self._provider = provider
        self._storage = storage
        self._event_bus = event_bus
        self._telemetry = telemetry
        self._shared_budget = shared_budget
        self._input_token_price_usd = input_token_price_usd
        self._price_source_ref = price_source_ref
        self._price_source_version = price_source_version

    def bind_execution(
        self,
        *,
        identity: IdentityContext,
        tenant_id: str,
        run_id: str,
        agent_id: str,
        request_id: str | None,
        trace_id: str,
    ) -> BoundEmbeddingInvocationService:
        """把原始 invocation seam 封闭为单个 runtime execution 的 facade。"""

        del identity
        return BoundEmbeddingInvocationService(
            service=self,
            context=UsageEvidenceContext(
                tenant_id=tenant_id,
                run_id=run_id,
                agent_id=agent_id,
                request_id=request_id,
                trace_id=trace_id,
            ),
        )

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
    ) -> EmbeddingResponse:
        """执行一次 cache lookup/embedding，并产生恰好一组 started/final。"""

        if request.tenant_id != context.tenant_id:
            raise ValueError("embedding request tenant does not match usage context")
        call_id = usage_call_id
        replay = await self._replay_settlement_before_current_snapshot(
            request=request,
            context=context,
            usage_call_id=call_id,
        )
        if replay is not None:
            return await self._resume_existing_settlement(
                claim=replay.usage,
                usage_call_id=call_id,
            )
        selected_provider = self._provider.provider
        selected_model = self._provider.model
        await self._event_bus.reconcile_local_capacity(run_id=context.run_id)
        durable_started = await self._durable_started_evidence(
            tenant_id=context.tenant_id,
            usage_call_id=call_id,
        )
        cached = None
        if durable_started is None:
            cached = (
                await self._provider.lookup_cache(request)
                if isinstance(self._provider, PreflightEmbeddingCacheProvider)
                else None
            )
            started = self._evidence(
                context=context,
                provider=selected_provider,
                model=selected_model,
                cache_hit=cached is not None,
                latency_ms=0 if cached is None else cached.latency_ms,
                decision=(
                    {"cache_status": "lookup", "provider_called": False}
                    if cached is None
                    else {"cache_status": "hit", "provider_called": False}
                ),
            )
        else:
            # durable replay 不能受首次执行后写入的 cache 影响；否则 miss 会漂移成
            # hit，令同一 usage_call_id 的 started identity 无法恢复。
            started = durable_started
        try:
            settlement = await self._start_settlement(
                request=request,
                context=context,
                usage_call_id=call_id,
                started=started,
                cached=cached,
                expect_replay=durable_started is not None,
            )
        except BudgetReservationRejected as exc:
            try:
                await self._record_budget_rejection(
                    evidence=started,
                    usage_call_id=call_id,
                    reason=exc.reason,
                )
            except EventCapacityExceeded:
                # 与 model seam 相同，hard budget code 不能被较低优先级容量错误覆盖。
                pass
            raise
        if not settlement.usage.created and not settlement.safe_to_start:
            return await self._resume_existing_settlement(
                claim=settlement.usage,
                usage_call_id=call_id,
            )
        started_event = await UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=started,
            usage_call_id=call_id,
        ).publish_started()
        if self._telemetry is not None:
            await self._telemetry.publish_event(started_event)

        if cached is not None:
            await self._publish_final(
                evidence=started,
                usage_call_id=call_id,
                outcome="completed",
                error_code=None,
            )
            return cached

        await self._mark_side_effect_started(
            context=context,
            usage_call_id=call_id,
            ownership=settlement.ownership,
        )

        invoked_at = perf_counter()
        try:
            provider_response = (
                await self._provider.embed_cache_miss(request)
                if isinstance(self._provider, PreflightEmbeddingCacheProvider)
                else await self._provider.embed(request)
            )
            # adapter 可能错误地用 model_construct 绕过 DTO；副作用后必须重新校验。
            response = EmbeddingResponse.model_validate(provider_response.model_dump(mode="python"))
            if response.provider != selected_provider or response.model != selected_model:
                raise ValueError("embedding response identity does not match selected provider")
            evidence = embedding_usage_evidence(
                provider=response.provider,
                model=response.model,
                cache_hit=response.cache.hit,
                latency_ms=response.latency_ms,
                context=context,
                input_tokens=len(request.input.encode("utf-8")),
            )
        except Exception:
            failed = self._evidence(
                context=context,
                provider=selected_provider,
                model=selected_model,
                cache_hit=False,
                latency_ms=int((perf_counter() - invoked_at) * 1000),
                decision={"cache_status": "unknown", "provider_called": True},
            )
            await self._finalize(
                evidence=failed,
                usage_call_id=call_id,
                outcome="failed",
                error_code="embedding.provider_failed",
                ownership=settlement.ownership,
                response=None,
            )
            raise EmbeddingProviderInvocationError("embedding provider invocation failed") from None

        await self._finalize(
            evidence=evidence,
            usage_call_id=call_id,
            outcome="completed",
            error_code=None,
            ownership=settlement.ownership,
            response=response,
        )
        return response

    async def _durable_started_evidence(
        self,
        *,
        tenant_id: str,
        usage_call_id: str,
    ) -> ModelUsageEvidence | None:
        """读取首次 started identity；只返回稳定 DTO，不把 ORM 行带出 UoW。"""

        async with self._storage.uow() as uow:
            try:
                item = await uow.evidence_outbox.get_usage(
                    tenant_id=tenant_id,
                    usage_call_id=usage_call_id,
                )
            except LookupError:
                return None
            if item.operation_kind != EvidenceOperationKind.EMBEDDING_USAGE.value:
                raise UsageInvocationReplayError(item.state)
            started = (
                item.result_json.get("started") if isinstance(item.result_json, dict) else None
            )
            state = item.state
        if not isinstance(started, dict):
            raise UsageInvocationReplayError(state)
        return ModelUsageEvidence.model_validate(started)

    def _evidence(
        self,
        *,
        context: UsageEvidenceContext,
        provider: str,
        model: str,
        cache_hit: bool,
        latency_ms: int,
        decision: dict[str, object],
    ) -> ModelUsageEvidence:
        """构造 started 或失败路径所需的嵌入用量证据。

        已确认 hit/miss 时委托统一 helper 生成完整 token 形状；纯预检或异常
        状态保留 ``unavailable``，避免 Provider 尚未调用时虚构用量数据。
        """
        if decision.get("cache_status") in {"hit", "miss"}:
            return embedding_usage_evidence(
                provider=provider,
                model=model,
                cache_hit=cache_hit,
                latency_ms=latency_ms,
                context=context,
            )
        return ModelUsageEvidence(
            usage_kind="embedding",
            tenant_id=context.tenant_id,
            provider=provider,
            model=model,
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            cost_status="unavailable",
            latency_ms=latency_ms,
            decision=decision,
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )

    @staticmethod
    def _final_event_id(tenant_id: str, usage_call_id: str) -> str:
        """生成租户内稳定的 final 事件 ID，使重放只会收敛到同一用量证据。"""
        return f"usage:{tenant_id}:{usage_call_id}:final"


__all__ = [
    "BoundEmbeddingInvocationService",
    "EmbeddingInvocationService",
    "EmbeddingProviderInvocationError",
]
