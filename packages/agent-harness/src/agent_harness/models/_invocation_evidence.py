"""模型 route、attempt、usage 与 durable response 的安全 evidence 投影。"""

from __future__ import annotations

from typing import Any, cast

from agent_harness.models.providers import ModelAttemptEvidence, ModelResponse
from agent_harness.models.router import ModelRoutePlan
from agent_harness.models.usage import CostStatus, ModelUsageEvidence, UsageEvidenceContext
from agent_harness.security.redaction import redact_secrets


class ModelInvocationEvidenceMixin:
    """只负责 provider-neutral evidence 归一化，不执行 provider 或持久化。"""

    @staticmethod
    def _route_evidence(plan: ModelRoutePlan) -> dict[str, object]:
        """投影安全且可重放的 route identity，不包含 URL path 或 credential 值。"""

        return {
            "snapshot_schema_version": plan.snapshot_schema_version,
            "deployment_id": plan.deployment_id,
            "provider_kind": plan.provider_kind,
            "provider": plan.provider,
            "model": plan.model,
            "capability": plan.capability,
            "endpoint_origin": plan.endpoint_origin,
            "endpoint_policy_ref": plan.endpoint_policy_ref,
            "endpoint_policy_version": plan.endpoint_policy_version,
            "endpoint_policy_digest": plan.endpoint_policy_digest,
            "completion_classifier_ref": plan.completion_classifier_ref,
            "completion_classifier_version": plan.completion_classifier_version,
            "credential_ref": plan.credential_ref,
            "model_catalog_ref": plan.model_catalog_ref,
            "model_catalog_version": plan.model_catalog_version,
            "model_catalog_digest": plan.model_catalog_digest,
            "request_shape_ref": plan.request_shape_ref,
            "request_shape_version": plan.request_shape_version,
            "input_bound_strategy_ref": plan.input_bound_strategy_ref,
            "input_bound_strategy_version": plan.input_bound_strategy_version,
            "input_envelope_token_bound": plan.input_envelope_token_bound,
            "input_token_price_usd": (
                None if plan.input_token_price_usd is None else float(plan.input_token_price_usd)
            ),
            "output_token_price_usd": (
                None if plan.output_token_price_usd is None else float(plan.output_token_price_usd)
            ),
            "price_source_ref": plan.price_source_ref,
            "price_source_version": plan.price_source_version,
            "prompt_utf8_bytes": plan.prompt_utf8_bytes,
            "trusted_input_token_bound": plan.trusted_input_token_bound,
            "output_token_cap": plan.output_token_cap,
            "per_attempt_token_bound": plan.per_attempt_token_bound,
            "per_attempt_cost_bound": (
                None if plan.per_attempt_cost_bound is None else float(plan.per_attempt_cost_bound)
            ),
            "max_attempts": plan.max_attempts,
            "reserved_token_bound": plan.reserved_token_bound,
            "reserved_cost_bound": (
                None if plan.reserved_cost_bound is None else float(plan.reserved_cost_bound)
            ),
            "connect_timeout_ms": plan.connect_timeout_ms,
            "read_timeout_ms": plan.read_timeout_ms,
            "total_timeout_ms": plan.total_timeout_ms,
            "retry_policy": plan.retry_policy.model_dump(mode="json"),
            "bulkhead_policy": plan.bulkhead_policy.model_dump(mode="json"),
        }

    @staticmethod
    def _started_evidence(
        *,
        context: UsageEvidenceContext,
        provider: str,
        model: str,
        decision: dict[str, object],
        latency_ms: int = 0,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        cost_status: CostStatus = "unavailable",
    ) -> ModelUsageEvidence:
        """构造 provider 尚未产生计量时的 started 或失败证据基础对象。"""

        return ModelUsageEvidence(
            usage_kind="model",
            tenant_id=context.tenant_id,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cost_status=cost_status,
            latency_ms=latency_ms,
            decision=decision,
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )

    @staticmethod
    def _attempt_summary(
        *,
        attempts: list[ModelAttemptEvidence],
        plan: ModelRoutePlan,
        provider_called: bool,
    ) -> dict[str, object]:
        """按 5.29 穷举矩阵生成公开 attempt、聚合 usage 与预算 charge。

        已知的部分 actual 只留在 attempt；任一 started/unknown attempt 的启用维度
        不完整时，调用级字段保持 null，并让 ledger 继续持有原 reservation。
        """

        cost_enabled = plan.input_token_price_usd is not None
        normalized: list[dict[str, object]] = []
        unresolved: set[int] = set()
        total_input = 0
        total_output = 0
        total_cost = 0.0
        aggregate_cost_status = "reported"

        for expected, item in enumerate(attempts, start=1):
            invalid = item.attempt != expected
            completion_observed = item.completion_observed
            if item.outcome == "completed":
                if completion_observed is False or item.side_effect_state != "started":
                    invalid = True
                completion_observed = True
            if item.side_effect_state == "not_started":
                if any(
                    value is not None
                    for value in (
                        item.input_tokens,
                        item.output_tokens,
                        item.cost_usd,
                    )
                ):
                    invalid = True
                charge_tokens: int | None = 0
                charge_cost: float | None = 0.0 if cost_enabled else None
            elif item.side_effect_state == "started":
                token_known = item.input_tokens is not None and item.output_tokens is not None
                charge_tokens = (
                    (item.input_tokens or 0) + (item.output_tokens or 0) if token_known else None
                )
                charge_cost = item.cost_usd if cost_enabled else None
                if token_known:
                    total_input += item.input_tokens or 0
                    total_output += item.output_tokens or 0
                if cost_enabled and item.cost_usd is not None:
                    total_cost += item.cost_usd
                    if item.cost_status == "estimated":
                        aggregate_cost_status = "estimated"
                if not token_known or (cost_enabled and item.cost_usd is None):
                    unresolved.add(item.attempt)
            else:
                charge_tokens = None
                charge_cost = None
                unresolved.add(item.attempt)

            if item.budget_charge_tokens not in (None, charge_tokens):
                invalid = True
            if item.budget_charge_cost_usd not in (None, charge_cost):
                invalid = True
            if item.cost_usd is None and item.cost_status != "unavailable":
                invalid = True
            if item.cost_usd is not None and item.cost_status == "unavailable":
                invalid = True
            if invalid:
                unresolved.add(item.attempt)
                charge_tokens = None
                charge_cost = None

            normalized.append(
                {
                    "attempt": item.attempt,
                    "outcome": item.outcome,
                    "side_effect_state": item.side_effect_state,
                    "completion_observed": completion_observed,
                    "http_status": item.http_status,
                    "retry_after_ms": item.retry_after_ms,
                    "input_tokens": item.input_tokens,
                    "output_tokens": item.output_tokens,
                    "cost_usd": item.cost_usd,
                    "cost_status": item.cost_status,
                    "budget_charge_tokens": charge_tokens,
                    "budget_charge_cost_usd": charge_cost,
                    "latency_ms": item.latency_ms,
                    "error_code": item.error_code,
                }
            )

        if provider_called and not attempts:
            unresolved.add(1)
        charged_tokens = sum(
            cast(int, item["budget_charge_tokens"])
            for item in normalized
            if item["budget_charge_tokens"] is not None
        )
        charged_cost = sum(
            cast(float, item["budget_charge_cost_usd"])
            for item in normalized
            if item["budget_charge_cost_usd"] is not None
        )
        if charged_tokens > plan.reserved_token_bound:
            unresolved.update(item.attempt for item in attempts)
        if (
            cost_enabled
            and plan.reserved_cost_bound is not None
            and charged_cost > float(plan.reserved_cost_bound)
        ):
            unresolved.update(item.attempt for item in attempts)

        unresolved_attempts = sorted(unresolved)
        actual = not unresolved_attempts
        return {
            "attempts": normalized,
            "budget_charge": {
                "charged_tokens": charged_tokens if actual else None,
                "charged_cost_usd": (charged_cost if actual and cost_enabled else None),
                "charge_status": "actual" if actual else "unknown",
                "unresolved_attempts": unresolved_attempts,
            },
            "input_tokens": total_input if actual and provider_called else None,
            "output_tokens": total_output if actual and provider_called else None,
            "cost_usd": total_cost if actual and provider_called and cost_enabled else None,
            "cost_status": (
                aggregate_cost_status
                if actual and provider_called and cost_enabled
                else "unavailable"
            ),
        }

    @staticmethod
    def _final_event_id(tenant_id: str, usage_call_id: str) -> str:
        """为每个租户调用槽位生成稳定终态事件标识，支持安全补投。"""

        return f"usage:{tenant_id}:{usage_call_id}:final"

    @staticmethod
    def _safe_decision(*parts: dict[str, object]) -> dict[str, object]:
        """按后传入优先合并决策片段，并在进入持久化前整体脱敏。"""

        merged: dict[str, object] = {}
        for part in parts:
            merged.update(part)
        safe = redact_secrets(merged)
        if not isinstance(safe, dict):  # pragma: no cover - mapping input 保证输出形状
            raise RuntimeError("model decision redaction changed payload shape")
        return cast(dict[str, object], safe)

    @staticmethod
    def _durable_response(response: ModelResponse) -> dict[str, Any]:
        """恢复所需 response 必须先整体脱敏，再进入内部 outbox/shared claim。"""

        safe = redact_secrets(response.to_payload())
        if not isinstance(safe, dict):  # pragma: no cover - DTO payload 保证 mapping
            raise RuntimeError("model response redaction changed payload shape")
        validated = ModelResponse.model_validate(safe)
        return validated.to_payload()
