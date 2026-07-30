"""流关闭事实的可信度分类、needs-review 与中断结算。"""

from __future__ import annotations

from typing import Literal, cast

from agent_harness.models._settlement_contracts import ModelProviderInvocationError
from agent_harness.models._streaming_contracts import StreamingRuntime
from agent_harness.models.providers import ModelAttemptEvidence, ModelStreamCloseResult
from agent_harness.models.router import ModelRoutePlan
from agent_harness.models.usage import CostStatus, ModelUsageEvidence, UsageEvidenceContext
from agent_harness.storage.shared_budget import BudgetOperationOwnership

StreamInterruptionOutcome = Literal["cancelled", "failed"]
AttemptOutcome = Literal["completed", "failed", "retryable_status", "cancelled", "unknown"]


def has_trustworthy_stopped_usage(
    *,
    plan: ModelRoutePlan,
    close_result: ModelStreamCloseResult,
) -> bool:
    """只接受足以关闭 token 与启用成本维度的 stopped final usage。"""

    usage = close_result.usage
    if (
        close_result.state != "stopped"
        or usage is None
        or usage.finality != "complete"
        or usage.input_tokens is None
        or usage.output_tokens is None
    ):
        return False
    cost_enabled = plan.input_token_price_usd is not None
    return not cost_enabled or usage.cost_usd is not None


async def handle_interrupted_stream(
    runtime: StreamingRuntime,
    *,
    context: UsageEvidenceContext,
    usage_call_id: str,
    plan: ModelRoutePlan,
    ownership: BudgetOperationOwnership | None,
    used_delta_count: int,
    close_result: ModelStreamCloseResult,
    latency_ms: int,
    error_code: str,
    outcome: StreamInterruptionOutcome,
    failure_domain: Literal["provider", "runtime"],
) -> None:
    """只按 adapter 可证明的停止事实收口；unknown 保持全部围栏。"""

    provider_called = close_result.state != "not_started"
    usage = close_result.usage
    async with runtime.storage.uow() as uow:
        group = await uow.evidence_outbox.ordered_group(group_id=f"model-stream:{usage_call_id}")
        # 数据库提交可能已成功而调用方尚未收到确认，此时进程内 chunk 计数会偏小。
        # 结算必须扫描完整 durable group，不能据本地计数取消已持久化的槽位。
        durable_delta_pending = any(item.state == "result_persisted" for item in group)
    requires_review = (
        durable_delta_pending
        or close_result.state == "unknown"
        or (
            close_result.state == "stopped"
            and not has_trustworthy_stopped_usage(plan=plan, close_result=close_result)
        )
    )
    if requires_review:
        if durable_delta_pending:
            error_code = "model.provider_side_effect_unknown"
        attempt = ModelAttemptEvidence(
            attempt=1,
            side_effect_state=("unknown" if close_result.state == "unknown" else "started"),
            outcome=(
                "unknown" if close_result.state == "unknown" else cast(AttemptOutcome, outcome)
            ),
            completion_observed=False,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            cost_usd=usage.cost_usd if usage is not None else None,
            cost_status=usage.cost_status if usage is not None else "unavailable",
            latency_ms=usage.latency_ms if usage is not None else latency_ms,
            error_code=error_code,
        )
        # 观察到的 partial/null 数值不能升级为 actual charge；预算继续绑定未知 reservation。
        review: dict[str, object] = {
            "provider_close_state": close_result.state,
            "usage_finality": usage.finality if usage is not None else None,
            "outcome": outcome,
            "error_code": error_code,
            "provider_called": True,
            "latency_ms": latency_ms,
            "attempts": [attempt.model_dump(mode="python")],
            "budget_charge": {
                "charged_tokens": None,
                "charged_cost_usd": None,
                "charge_status": "unknown",
                "unresolved_attempts": [1],
            },
        }
        budget_result = {"attempt_review": review}
        async with runtime.storage.uow() as uow:
            await uow.evidence_outbox.persist_attempt_review(
                tenant_id=context.tenant_id,
                usage_call_id=usage_call_id,
                review=review,
                error_code="model.provider_side_effect_unknown",
            )
            if ownership is not None and ownership.kind == "direct":
                await uow.shared_budget.settle_direct(
                    tenant_id=context.tenant_id,
                    budget_owner_run_id=ownership.budget_owner_run_id,
                    usage_call_id=usage_call_id,
                    actual_tokens=None,
                    actual_cost=None,
                    cost_status="unavailable",
                    result=budget_result,
                )
            elif ownership is not None:
                assert ownership.delegation_id is not None
                await uow.shared_budget.settle_allocation(
                    tenant_id=context.tenant_id,
                    budget_owner_run_id=ownership.budget_owner_run_id,
                    delegation_id=ownership.delegation_id,
                    usage_call_id=usage_call_id,
                    actual_tokens=None,
                    actual_cost=None,
                    cost_status="unavailable",
                    result=budget_result,
                )
            await uow.commit()
        raise ModelProviderInvocationError(
            "model.provider_side_effect_unknown",
            provider_called=True,
            attempt_count=1,
            latency_ms=latency_ms,
            failure_domain=failure_domain,
        )

    async with runtime.storage.uow() as uow:
        await uow.evidence_outbox.cancel_unused_stream(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            usage_call_id=usage_call_id,
            used_delta_count=used_delta_count,
            keep_completed=False,
        )
        await uow.commit()
    attempts: list[ModelAttemptEvidence] = []
    if plan.provider == "openai-compatible" and provider_called:
        assert usage is not None
        attempts = [
            ModelAttemptEvidence(
                attempt=1,
                side_effect_state="started",
                outcome=cast(AttemptOutcome, outcome),
                completion_observed=False,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
                cost_status=usage.cost_status,
                latency_ms=usage.latency_ms,
                error_code=error_code,
            )
        ]
    attempt_summary = (
        runtime.attempt_summary(
            attempts=attempts,
            plan=plan,
            provider_called=provider_called,
        )
        if plan.provider == "openai-compatible"
        else None
    )
    evidence = ModelUsageEvidence(
        usage_kind="model",
        tenant_id=context.tenant_id,
        provider=plan.provider,
        model=plan.model,
        input_tokens=(
            cast(int | None, attempt_summary["input_tokens"])
            if attempt_summary is not None
            else (usage.input_tokens if usage is not None else None)
        ),
        output_tokens=(
            cast(int | None, attempt_summary["output_tokens"])
            if attempt_summary is not None
            else (usage.output_tokens if usage is not None else None)
        ),
        cost_usd=(
            cast(float | None, attempt_summary["cost_usd"])
            if attempt_summary is not None
            else (usage.cost_usd if usage is not None else None)
        ),
        cost_status=(
            cast(CostStatus, attempt_summary["cost_status"])
            if attempt_summary is not None
            else (usage.cost_status if usage is not None else "unavailable")
        ),
        latency_ms=usage.latency_ms if usage is not None else latency_ms,
        decision=runtime.safe_decision(
            plan.decision.to_payload(),
            {"route": runtime.route_evidence(plan)},
            {"provider_called": provider_called},
            {
                "usage_event_identity": {
                    "ref": "stream-usage",
                    "version": "v1",
                }
            },
            (
                {
                    "attempts": attempt_summary["attempts"],
                    "budget_charge": attempt_summary["budget_charge"],
                }
                if attempt_summary is not None
                else {}
            ),
        ),
        run_id=context.run_id,
        agent_id=context.agent_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
    )
    await runtime.finalize(
        evidence=evidence,
        usage_call_id=usage_call_id,
        outcome=outcome,
        error_code=error_code,
        ownership=ownership,
        response=None,
    )
    raise ModelProviderInvocationError(
        error_code,
        provider_called=provider_called,
        attempt_count=int(provider_called),
        latency_ms=latency_ms,
        failure_domain=failure_domain,
    )


__all__ = ["StreamInterruptionOutcome", "handle_interrupted_stream"]
