"""Embedding 调用的 durable usage 生命周期。"""

from __future__ import annotations

from time import perf_counter

from agent_harness.embeddings.provider import EmbeddingProvider, EmbeddingRequest, EmbeddingResponse
from agent_harness.events import EventBus
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
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    UsageSettlementClaim,
)


class EmbeddingProviderInvocationError(RuntimeError):
    """embedding provider 原异常已封闭，避免 input/header/response 泄露。"""

    code = "embedding.provider_failed"


class BoundEmbeddingInvocationService:
    """只向单个 run 的业务 executor 暴露 embedding 请求与操作槽位。"""

    def __init__(
        self,
        *,
        service: EmbeddingInvocationService,
        context: UsageEvidenceContext,
    ) -> None:
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


class EmbeddingInvocationService:
    """在 cache/provider 副作用前预约，并用统一 model event type 结算。"""

    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        telemetry: TelemetryFacade | None = None,
    ) -> None:
        self._provider = provider
        self._storage = storage
        self._event_bus = event_bus
        self._telemetry = telemetry

    def bind_execution(
        self,
        *,
        tenant_id: str,
        run_id: str,
        agent_id: str,
        request_id: str | None,
        trace_id: str,
    ) -> BoundEmbeddingInvocationService:
        """把原始 invocation seam 封闭为单个 runtime execution 的 facade。"""

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
        selected_provider = self._provider.provider
        selected_model = self._provider.model
        await self._event_bus.reconcile_local_capacity(run_id=context.run_id)
        started = self._evidence(
            context=context,
            provider=selected_provider,
            model=selected_model,
            cache_hit=False,
            latency_ms=0,
            decision={"cache_status": "lookup", "provider_called": False},
        )
        async with self._storage.uow() as uow:
            claim = await uow.evidence_outbox.claim_usage(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                usage_call_id=call_id,
                event_id=self._final_event_id(context.tenant_id, call_id),
                operation_kind=EvidenceOperationKind.EMBEDDING_USAGE,
                started_evidence=started.to_payload(),
            )
            await uow.commit()
        if not claim.created:
            await self._resume_existing_settlement(claim=claim, usage_call_id=call_id)
        started_event = await UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=started,
            usage_call_id=call_id,
        ).publish_started()
        if self._telemetry is not None:
            await self._telemetry.publish_event(started_event)

        invoked_at = perf_counter()
        try:
            provider_response = await self._provider.embed(request)
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
            )
            raise EmbeddingProviderInvocationError("embedding provider invocation failed") from None

        await self._finalize(
            evidence=evidence,
            usage_call_id=call_id,
            outcome="completed",
            error_code=None,
        )
        return response

    async def _resume_existing_settlement(
        self,
        *,
        claim: UsageSettlementClaim,
        usage_call_id: str,
    ) -> None:
        """已有确定结果只补投 evidence；未知或已发布状态一律不重放 cache/provider。"""

        if claim.state == "result_persisted" and claim.result_json is not None:
            result = claim.result_json
            await self._publish_final(
                evidence=ModelUsageEvidence.model_validate(result["evidence"]),
                usage_call_id=usage_call_id,
                outcome=str(result["outcome"]),
                error_code=claim.error_code,
            )
            raise UsageInvocationReplayError("published")
        raise UsageInvocationReplayError(claim.state)

    async def recover_pending(self, *, run_id: str) -> int:
        """只补投已持久化 embedding 结果，不重新查询 cache/provider。"""

        async with self._storage.uow() as uow:
            pending = [
                (
                    item.state,
                    item.operation_kind,
                    item.result_json,
                    item.usage_call_id,
                    item.error_code,
                )
                for item in await uow.evidence_outbox.pending(run_id=run_id)
            ]
        recovered = 0
        for state, operation_kind, result, usage_call_id, error_code in pending:
            if state != "result_persisted" or operation_kind != "embedding_usage" or result is None:
                continue
            await self._publish_final(
                evidence=ModelUsageEvidence.model_validate(result["evidence"]),
                usage_call_id=str(usage_call_id),
                outcome=str(result["outcome"]),
                error_code=error_code,
            )
            recovered += 1
        return recovered

    async def _finalize(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        outcome: str,
        error_code: str | None,
    ) -> None:
        async with self._storage.uow() as uow:
            await uow.evidence_outbox.persist_result(
                tenant_id=evidence.tenant_id,
                usage_call_id=usage_call_id,
                result={"evidence": evidence.to_payload(), "outcome": outcome},
                error_code=error_code,
            )
            await uow.commit()
        await self._publish_final(
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome=outcome,
            error_code=error_code,
        )

    async def _publish_final(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        outcome: str,
        error_code: str | None,
    ) -> None:
        final = await UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=evidence,
            usage_call_id=usage_call_id,
        ).publish_final(outcome=outcome, error_code=error_code)
        if self._telemetry is not None:
            await self._telemetry.publish_event(final)
        async with self._storage.uow() as uow:
            item = await uow.evidence_outbox.get_usage(
                tenant_id=evidence.tenant_id,
                usage_call_id=usage_call_id,
            )
            if not self._event_bus.capacity_managed:
                await uow.event_capacity.record_local_published(
                    run_id=evidence.run_id,
                    reserved_event_count=item.reserved_event_count,
                    highest_persisted_seq=final.seq,
                )
            await uow.evidence_outbox.mark_published(
                tenant_id=evidence.tenant_id,
                usage_call_id=usage_call_id,
            )
            await uow.commit()

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
        return f"usage:{tenant_id}:{usage_call_id}:final"


__all__ = [
    "BoundEmbeddingInvocationService",
    "EmbeddingInvocationService",
    "EmbeddingProviderInvocationError",
]
