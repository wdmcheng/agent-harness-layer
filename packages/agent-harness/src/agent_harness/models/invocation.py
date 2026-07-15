"""Model 调用的 durable usage 预约、结算与补投 seam。"""

from __future__ import annotations

from time import perf_counter
from typing import Any, cast

from agent_harness.events import EventBus
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.router import ModelRouter
from agent_harness.models.usage import (
    ModelUsageEvidence,
    UsageEvidenceContext,
    UsageInvocationReplayError,
    stable_usage_call_id,
)
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
    UsageSettlementClaim,
)


class ModelProviderInvocationError(RuntimeError):
    """provider 原异常已封闭，调用方只能看到稳定错误码。"""

    code = "model.provider_failed"


class BoundModelInvocationService:
    """只向单个 run 的业务 executor 暴露请求与稳定操作槽位。"""

    def __init__(
        self,
        *,
        service: ModelInvocationService,
        context: UsageEvidenceContext,
    ) -> None:
        self._service = service
        self._context = context

    async def complete(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
    ) -> ModelResponse:
        """由可信 runtime 关联生成 call ID，业务输入不能覆盖身份。"""

        return await self._service.complete(
            request,
            context=self._context,
            usage_call_id=stable_usage_call_id(
                context=self._context,
                operation_key=operation_key,
            ),
        )


class ModelInvocationService:
    """在 provider 副作用前建立 settlement，并只补投 evidence。"""

    def __init__(
        self,
        *,
        router: ModelRouter,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        telemetry: TelemetryFacade | None = None,
    ) -> None:
        self._router = router
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
    ) -> BoundModelInvocationService:
        """把原始 invocation seam 封闭为单个 runtime execution 的 facade。"""

        return BoundModelInvocationService(
            service=self,
            context=UsageEvidenceContext(
                tenant_id=tenant_id,
                run_id=run_id,
                agent_id=agent_id,
                request_id=request_id,
                trace_id=trace_id,
            ),
        )

    async def complete(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
    ) -> ModelResponse:
        """执行一次 model 调用；任何 provider 副作用都晚于 durable 预约。"""

        call_id = usage_call_id
        await self._event_bus.reconcile_local_capacity(run_id=context.run_id)
        plan = self._router.plan(request)
        started_evidence = self._started_evidence(
            context=context,
            provider=plan.provider,
            model=plan.model,
            decision=self._safe_decision(
                plan.decision.to_payload(),
                {"provider_called": plan.decision.action != "policy_required"},
            ),
        )
        claim = await self._start_settlement(evidence=started_evidence, usage_call_id=call_id)
        if not claim.created:
            await self._resume_existing_settlement(claim=claim, usage_call_id=call_id)
        lifecycle = UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=started_evidence,
            usage_call_id=call_id,
        )
        started = await lifecycle.publish_started()
        if self._telemetry is not None:
            await self._telemetry.publish_event(started)

        invoked_at = perf_counter()
        try:
            provider_response = self._router.execute(request, plan=plan)
            # adapter 可能错误地用 model_construct 绕过 DTO；副作用后必须重新校验。
            response = ModelResponse.model_validate(provider_response.model_dump(mode="python"))
            if response.provider != plan.provider or response.model != plan.model:
                raise ValueError("model response identity does not match routing plan")
            provider_called = plan.decision.action != "policy_required"
            evidence = ModelUsageEvidence(
                usage_kind="model",
                tenant_id=context.tenant_id,
                provider=response.provider,
                model=response.model,
                input_tokens=response.token_usage.get("input_tokens"),
                output_tokens=response.token_usage.get("output_tokens"),
                cost_usd=response.cost_usd,
                cost_status=response.cost_status,
                latency_ms=response.latency_ms,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
                    response.decision.to_payload(),
                    {"provider_called": provider_called},
                ),
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            )
        except Exception:
            evidence = self._started_evidence(
                context=context,
                provider=plan.provider,
                model=plan.model,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
                    {"provider_called": True},
                ),
                latency_ms=int((perf_counter() - invoked_at) * 1000),
            )
            await self._finalize(
                evidence=evidence,
                usage_call_id=call_id,
                outcome="failed",
                error_code="model.provider_failed",
            )
            raise ModelProviderInvocationError("model provider invocation failed") from None

        rejected = response.decision.action == "policy_required"
        await self._finalize(
            evidence=evidence,
            usage_call_id=call_id,
            outcome="rejected" if rejected else "completed",
            error_code="model.policy_required" if rejected else None,
        )
        return response

    async def recover_pending(self, *, run_id: str) -> int:
        """只补投已有确定性结果；started/未知结果继续阻止 terminal。"""

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
            if (
                state != "result_persisted"
                or operation_kind != EvidenceOperationKind.MODEL_USAGE.value
                or result is None
            ):
                continue
            evidence = ModelUsageEvidence.model_validate(result["evidence"])
            await self._publish_final(
                evidence=evidence,
                usage_call_id=str(usage_call_id),
                outcome=str(result["outcome"]),
                error_code=error_code,
            )
            recovered += 1
        return recovered

    async def _start_settlement(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
    ) -> UsageSettlementClaim:
        async with self._storage.uow() as uow:
            claim = await uow.evidence_outbox.claim_usage(
                tenant_id=evidence.tenant_id,
                run_id=evidence.run_id,
                usage_call_id=usage_call_id,
                event_id=self._final_event_id(evidence.tenant_id, usage_call_id),
                operation_kind=EvidenceOperationKind.MODEL_USAGE,
                started_evidence=evidence.to_payload(),
            )
            await uow.commit()
            return claim

    async def _resume_existing_settlement(
        self,
        *,
        claim: UsageSettlementClaim,
        usage_call_id: str,
    ) -> None:
        """已有确定结果只补投 evidence；未知或已发布状态一律不重放 provider。"""

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
        lifecycle = UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=evidence,
            usage_call_id=usage_call_id,
        )
        final = await lifecycle.publish_final(outcome=outcome, error_code=error_code)
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

    @staticmethod
    def _started_evidence(
        *,
        context: UsageEvidenceContext,
        provider: str,
        model: str,
        decision: dict[str, object],
        latency_ms: int = 0,
    ) -> ModelUsageEvidence:
        return ModelUsageEvidence(
            usage_kind="model",
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

    @staticmethod
    def _safe_decision(*parts: dict[str, object]) -> dict[str, Any]:
        merged: dict[str, object] = {}
        for part in parts:
            merged.update(part)
        safe = redact_secrets(merged)
        if not isinstance(safe, dict):  # pragma: no cover - mapping input 保证输出形状
            raise RuntimeError("model decision redaction changed payload shape")
        return cast(dict[str, Any], safe)


__all__ = [
    "BoundModelInvocationService",
    "ModelInvocationService",
    "ModelProviderInvocationError",
]
