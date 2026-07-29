"""真实调用 usage 聚合、可信 actual 结算与失败证据合同。"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.contracts.controlled_real_model_retry_budget_test_support import (
    assert_unresolved_real_settlement,
    managed_real_invocation,
)
from tests.contracts.controlled_real_model_runtime_composition_test_support import ResultDouble

from agent_harness.adapters.models.pydantic_ai import (
    ModelProviderError,
)
from agent_harness.models import (
    ModelProviderInvocationError,
)
from agent_harness.models.usage import UsageEvidenceContext


@pytest.mark.asyncio
async def test_retry_attempts_reserve_and_settle_only_trusted_actual_usage(
    tmp_path: Path,
) -> None:
    """retry 成功也不能用最后一次 usage 释放首个 started 未决 reservation。"""

    (
        storage,
        _sink,
        service,
        request,
        run_id,
        reserved_tokens,
        agent,
        _client_factory,
        _provider,
        _plan,
    ) = await managed_real_invocation(
        tmp_path,
        classifier=True,
        outcomes=[
            ModelProviderError(
                "model.provider_failed",
                status_code=429,
                retry_after_ms=0,
                completion_observed=False,
                side_effect_state="started",
            ),
            ResultDouble(),
        ],
        database_name="retry-unresolved.sqlite3",
    )
    try:
        response = await service.complete(
            request,
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                request_id="request-a",
                trace_id="trace-a",
            ),
            usage_call_id="usage-retry-unresolved",
        )
        assert agent.calls == 2
        assert response.token_usage == {}
        await assert_unresolved_real_settlement(
            storage=storage,
            run_id=run_id,
            usage_call_id="usage-retry-unresolved",
            reserved_tokens=reserved_tokens,
            expected_attempt_count=2,
        )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["429", "503", "connect"])
async def test_retryable_failure_exhaustion_carries_safe_attempt_evidence_to_public_facade(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    """状态码与 connect 重试用尽后统一返回带完整摘要的 exhausted 错误。"""

    def retryable_error() -> ModelProviderError:
        """每次构造独立异常，避免复用 traceback 污染第二个 attempt。"""

        if failure_kind == "connect":
            return ModelProviderError(
                "model.provider_failed",
                completion_observed=False,
                side_effect_state="not_started",
            )
        return ModelProviderError(
            "model.provider_failed",
            status_code=int(failure_kind),
            retry_after_ms=0,
            completion_observed=False,
            side_effect_state="started",
        )

    (
        storage,
        _sink,
        service,
        request,
        run_id,
        _reserved_tokens,
        agent,
        _client_factory,
        _provider,
        _plan,
    ) = await managed_real_invocation(
        tmp_path,
        classifier=True,
        outcomes=[retryable_error(), retryable_error()],
        database_name=f"retry-exhausted-{failure_kind}-safe-evidence.sqlite3",
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await service.complete(
                request,
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    request_id="request-a",
                    trace_id="trace-a",
                ),
                usage_call_id=f"usage-retry-exhausted-{failure_kind}-safe-evidence",
            )

        assert exc_info.value.code == "model.provider_retry_exhausted"
        assert exc_info.value.provider_called is True
        assert exc_info.value.attempt_count == 2
        assert exc_info.value.latency_ms is not None
        assert exc_info.value.latency_ms >= 0
        assert agent.calls == 2
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "expected_code", "expected_attempts"),
    [
        ("provider_failed", "model.provider_failed", 1),
        ("unknown", "model.provider_side_effect_unknown", 1),
        ("retry_exhausted", "model.provider_retry_exhausted", 2),
    ],
)
async def test_failed_settlement_replay_preserves_safe_error_evidence_without_provider_replay(
    tmp_path: Path,
    failure_kind: str,
    expected_code: str,
    expected_attempts: int,
) -> None:
    """同一 usage call 的失败重放必须保持原错误摘要且不能再次触发 provider。"""

    if failure_kind == "retry_exhausted":
        classifier = True
        outcomes: list[object] = [
            ModelProviderError(
                "model.provider_failed",
                status_code=429,
                retry_after_ms=0,
                completion_observed=False,
                side_effect_state="started",
            ),
            ModelProviderError(
                "model.provider_failed",
                status_code=429,
                retry_after_ms=0,
                completion_observed=False,
                side_effect_state="started",
            ),
        ]
    elif failure_kind == "unknown":
        classifier = False
        outcomes = [
            ModelProviderError(
                "model.provider_side_effect_unknown",
                side_effect_state="unknown",
            )
        ]
    else:
        classifier = False
        outcomes = [
            ModelProviderError(
                "model.provider_failed",
                status_code=500,
                side_effect_state="started",
            )
        ]

    (
        storage,
        _sink,
        service,
        request,
        run_id,
        _reserved_tokens,
        agent,
        _client_factory,
        _provider,
        _plan,
    ) = await managed_real_invocation(
        tmp_path,
        classifier=classifier,
        outcomes=outcomes,
        database_name=f"failed-replay-{failure_kind}.sqlite3",
    )
    usage_call_id = f"usage-failed-replay-{failure_kind}"
    errors: list[ModelProviderInvocationError] = []
    try:
        for _ in range(2):
            with pytest.raises(ModelProviderInvocationError) as exc_info:
                await service.complete(
                    request,
                    context=UsageEvidenceContext(
                        tenant_id="tenant-a",
                        run_id=run_id,
                        agent_id="agent-a",
                        request_id="request-a",
                        trace_id="trace-a",
                    ),
                    usage_call_id=usage_call_id,
                )
            errors.append(exc_info.value)

        assert agent.calls == expected_attempts
        assert [error.code for error in errors] == [expected_code, expected_code]
        assert [error.provider_called for error in errors] == [True, True]
        assert [error.attempt_count for error in errors] == [
            expected_attempts,
            expected_attempts,
        ]
        assert errors[0].latency_ms is not None
        assert errors[1].latency_ms == errors[0].latency_ms
        assert "provider raw" not in repr(errors)
    finally:
        await storage.dispose()
