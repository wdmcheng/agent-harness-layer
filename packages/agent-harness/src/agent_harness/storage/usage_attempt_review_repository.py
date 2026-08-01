"""Usage settlement 的不可结算 attempt-review 持久化边界。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.event_capacity_repositories import EvidenceOperationKind
from agent_harness.storage.models import RunEvidenceOutboxModel


def normalize_attempt_review(review: Mapping[str, object]) -> dict[str, object]:
    """封闭 needs-review 的脱敏 shape，禁止混入可结算 final evidence。"""

    expected_keys = {
        "provider_close_state",
        "usage_finality",
        "outcome",
        "error_code",
        "provider_called",
        "latency_ms",
        "attempts",
        "budget_charge",
    }
    if set(review) != expected_keys:
        raise ValueError("attempt review shape is invalid")
    close_state = review.get("provider_close_state")
    usage_finality = review.get("usage_finality")
    outcome = review.get("outcome")
    error_code = review.get("error_code")
    latency_ms = review.get("latency_ms")
    if (
        close_state not in {"stopped", "unknown"}
        or usage_finality not in {None, "partial", "complete"}
        or outcome not in {"cancelled", "failed"}
        or not isinstance(error_code, str)
        or not error_code
        or review.get("provider_called") is not True
        or isinstance(latency_ms, bool)
        or not isinstance(latency_ms, int)
        or latency_ms < 0
    ):
        raise ValueError("attempt review classification is invalid")
    raw_attempts = review.get("attempts")
    raw_charge = review.get("budget_charge")
    if not isinstance(raw_attempts, list):
        raise ValueError("attempt review requires exactly one attempt")
    attempts = cast(list[object], raw_attempts)
    if len(attempts) != 1:
        raise ValueError("attempt review requires exactly one attempt")
    if not isinstance(attempts[0], Mapping) or not isinstance(raw_charge, Mapping):
        raise ValueError("attempt review evidence is invalid")
    # 局部 import 避免 storage 初始化时触发 models -> storage 环。
    from agent_harness.models.providers import ModelAttemptEvidence

    attempt = ModelAttemptEvidence.model_validate(attempts[0])
    expected_side_effect = "unknown" if close_state == "unknown" else "started"
    if attempt.side_effect_state != expected_side_effect:
        raise ValueError("attempt review side-effect state is invalid")
    charge = dict(cast(Mapping[str, object], raw_charge))
    if charge != {
        "charged_tokens": None,
        "charged_cost_usd": None,
        "charge_status": "unknown",
        "unresolved_attempts": [attempt.attempt],
    }:
        raise ValueError("attempt review must preserve the unresolved reservation")
    return {
        "provider_close_state": close_state,
        "usage_finality": usage_finality,
        "outcome": outcome,
        "error_code": error_code,
        "provider_called": True,
        "latency_ms": latency_ms,
        "attempts": [attempt.to_payload()],
        "budget_charge": charge,
    }


class UsageAttemptReviewRepositoryMixin:
    """在既有 UoW 中持久化不可自动结算的 provider attempt。"""

    _session: AsyncSession

    async def persist_attempt_review(
        self,
        *,
        tenant_id: str,
        usage_call_id: str,
        review: Mapping[str, object],
        error_code: str,
    ) -> RunEvidenceOutboxModel:
        """耐久记录不可信计量 attempt，但不生成可发布的 final usage。

        该状态保留 usage 的剩余事件容量；预算仓储在同一 UoW 把 reservation
        提升为 ``needs_review``。恢复不能据此补写 final 或退款。
        """

        model = await self._session.scalar(
            select(RunEvidenceOutboxModel)
            .where(
                RunEvidenceOutboxModel.tenant_id == tenant_id,
                RunEvidenceOutboxModel.usage_call_id == usage_call_id,
            )
            .with_for_update()
        )
        if model is None:
            raise LookupError("usage settlement not found")
        if model.operation_kind != EvidenceOperationKind.MODEL_USAGE.value:
            raise ValueError("attempt review only accepts model usage settlements")
        started = (
            model.result_json.get("started") if isinstance(model.result_json, Mapping) else None
        )
        if not isinstance(started, Mapping):
            raise RuntimeError("usage settlement is missing its durable started identity")
        normalized_review = normalize_attempt_review(review)
        normalized_result = {
            "started": dict(cast(Mapping[str, object], started)),
            "attempt_review": normalized_review,
        }
        if model.state == "needs_review":
            if model.result_json != normalized_result or model.error_code != error_code:
                raise RuntimeError("persisted attempt review conflict")
            return model
        if model.state != "started":
            raise RuntimeError(f"usage settlement cannot enter review from state: {model.state}")
        model.result_json = normalized_result
        model.error_code = error_code
        model.state = "needs_review"
        await self._session.flush()
        return model


__all__ = ["UsageAttemptReviewRepositoryMixin", "normalize_attempt_review"]
