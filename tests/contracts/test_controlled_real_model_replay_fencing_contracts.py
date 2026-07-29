"""真实调用耐久失败重放、unknown reservation 与取消围栏合同。"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from tests.contracts.controlled_real_model_retry_budget_test_support import (
    ResultWithoutUsageDouble,
    assert_unresolved_real_settlement,
    managed_real_invocation,
)
from tests.contracts.controlled_real_model_runtime_composition_test_support import ResultDouble

from agent_harness.adapters.models.pydantic_ai import (
    ModelProviderError,
)
from agent_harness.models import (
    ModelDecision,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelResponse,
    UsageInvocationReplayError,
)
from agent_harness.models._invocation_settlement import DurableMarkStateUnknown
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.storage import SQLAlchemyStorage
from agent_harness.storage.models import RunEvidenceOutboxModel
from agent_harness.storage.shared_budget_models import BudgetOperationClaimModel


async def _rewind_controlled_success(
    *,
    storage: SQLAlchemyStorage,
    usage_call_id: str,
    corruption: str | None,
) -> None:
    """把已发布的真实 double 成功结果退回恢复窗口，并按场景损坏公开 evidence。"""

    async with storage.uow() as uow:
        usage = await uow.evidence_outbox.get_usage(
            tenant_id="tenant-a",
            usage_call_id=usage_call_id,
        )
        assert usage.result_json is not None
        result = deepcopy(usage.result_json)
        evidence = result["evidence"]
        decision = evidence["decision"]
        if corruption == "missing_response":
            result.pop("response")
        elif corruption == "success_with_failure":
            result["failure"] = {
                "error_code": "model.provider_failed",
                "provider_called": True,
                "attempt_count": 1,
                "latency_ms": evidence["latency_ms"],
            }
        elif corruption in {"response_provider_mismatch", "response_model_mismatch"}:
            response = dict(result["response"])
            response["provider" if corruption == "response_provider_mismatch" else "model"] = (
                "fake" if corruption == "response_provider_mismatch" else "forged-model"
            )
            result["response"] = response
        elif corruption == "rejected_with_response":
            result["outcome"] = "rejected"
        elif corruption == "all_nested_missing":
            decision.pop("route")
            decision.pop("attempts")
            decision.pop("budget_charge")
        elif corruption == "nested_explicit_null":
            decision["attempts"] = None
            decision["budget_charge"] = None
        elif corruption == "provider_called_with_zero_attempts":
            route = decision["route"]
            cost_enabled = route["input_token_price_usd"] is not None
            decision["attempts"] = []
            decision["budget_charge"] = {
                "charged_tokens": 0,
                "charged_cost_usd": 0.0 if cost_enabled else None,
                "charge_status": "actual",
                "unresolved_attempts": [],
            }
            evidence["input_tokens"] = 0
            evidence["output_tokens"] = 0
            evidence["cost_usd"] = 0.0 if cost_enabled else None
            evidence["cost_status"] = "reported" if cost_enabled else "unavailable"
        elif corruption == "malformed_attempt_schema":
            decision["attempts"] = [{"attempt": 1}]
        elif corruption == "forged_budget_charge":
            decision["budget_charge"] = {"forged": True}
        elif corruption == "missing_route":
            decision.pop("route")
        elif corruption is not None:
            route = dict(decision["route"])
            if corruption == "missing_reserved_token_bound":
                route.pop("reserved_token_bound")
            elif corruption == "bool_reserved_token_bound":
                route["reserved_token_bound"] = True
            elif corruption == "negative_reserved_token_bound":
                route["reserved_token_bound"] = -1
            elif corruption == "route_provider_mismatch":
                route["provider"] = "fake"
            elif corruption == "route_model_mismatch":
                route["model"] = "forged-model"
            elif corruption == "route_endpoint_digest_mismatch":
                route["endpoint_policy_digest"] = "0" * 64
            elif corruption == "route_catalog_digest_mismatch":
                route["model_catalog_digest"] = "0" * 64
            elif corruption == "route_deployment_mismatch":
                route["deployment_id"] = "forged-deployment"
            elif corruption == "route_credential_mismatch":
                route["credential_ref"] = "forged-credential"
            elif corruption == "route_origin_mismatch":
                route["endpoint_origin"] = "https://forged.example"
            elif corruption == "retry_without_classifier":
                route["completion_classifier_ref"] = None
                route["completion_classifier_version"] = None
                route["retry_policy"] = {
                    **route["retry_policy"],
                    "retryable_http_statuses": [429],
                }
            else:
                charged_tokens = decision["budget_charge"]["charged_tokens"]
                assert isinstance(charged_tokens, int) and charged_tokens > 0
                route["reserved_token_bound"] = charged_tokens - 1
            decision["route"] = route
        await uow.session.execute(
            update(RunEvidenceOutboxModel)
            .where(RunEvidenceOutboxModel.id == usage.id)
            .values(state="result_persisted", result_json=result)
        )
        await uow.session.execute(
            update(BudgetOperationClaimModel)
            .where(BudgetOperationClaimModel.usage_call_id == usage_call_id)
            .values(result_json={key: value for key, value in result.items() if key != "started"})
        )
        await uow.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["complete", "recover_pending"])
@pytest.mark.parametrize(
    "corruption",
    [
        "missing_response",
        "success_with_failure",
        "response_provider_mismatch",
        "response_model_mismatch",
        "rejected_with_response",
        "all_nested_missing",
        "nested_explicit_null",
        "provider_called_with_zero_attempts",
        "malformed_attempt_schema",
        "forged_budget_charge",
        "missing_route",
        "missing_reserved_token_bound",
        "bool_reserved_token_bound",
        "negative_reserved_token_bound",
        "actual_over_reserved_token_bound",
        "route_provider_mismatch",
        "route_model_mismatch",
        "route_endpoint_digest_mismatch",
        "route_catalog_digest_mismatch",
        "route_deployment_mismatch",
        "route_credential_mismatch",
        "route_origin_mismatch",
        "retry_without_classifier",
    ],
)
async def test_controlled_success_replay_validates_nested_route_before_publication(
    tmp_path: Path,
    entrypoint: str,
    corruption: str,
) -> None:
    """成功恢复同样必须先验证 5.29 route/attempt/charge，损坏时零二次副作用。"""

    (
        storage,
        sink,
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
        classifier=False,
        outcomes=[ResultDouble()],
        database_name=f"success-replay-{entrypoint}-{corruption}.sqlite3",
    )
    usage_call_id = f"usage-success-replay-{entrypoint}-{corruption}"
    context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    try:
        response = await service.complete(
            request,
            context=context,
            usage_call_id=usage_call_id,
        )
        assert response.output_text == "adapter-result"
        assert agent.calls == 1
        events_before = await sink.read(run_id=run_id)
        await _rewind_controlled_success(
            storage=storage,
            usage_call_id=usage_call_id,
            corruption=corruption,
        )
        with pytest.raises(UsageInvocationReplayError):
            if entrypoint == "complete":
                await service.complete(
                    request,
                    context=context,
                    usage_call_id=usage_call_id,
                )
            else:
                await service.recover_pending(run_id=run_id)

        assert agent.calls == 1
        assert await sink.read(run_id=run_id) == events_before
        async with storage.uow() as uow:
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
            )
            assert usage.state == "result_persisted"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_controlled_success_replay_accepts_valid_nested_evidence(
    tmp_path: Path,
) -> None:
    """合法 route/attempt/charge 可从 published 安全重放且不二次调用 provider。"""

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
        classifier=False,
        outcomes=[ResultDouble()],
        database_name="valid-success-replay.sqlite3",
    )
    usage_call_id = "usage-valid-success-replay"
    context = UsageEvidenceContext(
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    try:
        first = await service.complete(request, context=context, usage_call_id=usage_call_id)
        replayed = await service.complete(
            request,
            context=context,
            usage_call_id=usage_call_id,
        )
        assert first.output_text == "adapter-result"
        assert replayed.output_text == "adapter-result"
        assert agent.calls == 1
        async with storage.uow() as uow:
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
            )
            assert usage.state == "published"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_bulkhead_saturation_replays_same_safe_failure_without_provider_call(
    tmp_path: Path,
) -> None:
    """真实排队饱和必须耐久重放同一零调用事实，不能再次进入 provider。"""

    (
        storage,
        _sink,
        service,
        request,
        run_id,
        _reserved_tokens,
        agent,
        _client_factory,
        provider,
        plan,
    ) = await managed_real_invocation(
        tmp_path,
        classifier=False,
        outcomes=[ResultDouble()],
        database_name="bulkhead-saturation-replay.sqlite3",
    )
    maximum = int(plan.to_payload()["bulkhead_policy"]["max_in_flight"])
    held = [await provider.prepare(request, plan=plan) for _ in range(maximum)]
    usage_call_id = "usage-bulkhead-saturation-replay"
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

        assert [error.code for error in errors] == [
            "model.bulkhead_saturated",
            "model.bulkhead_saturated",
        ]
        assert [error.provider_called for error in errors] == [False, False]
        assert [error.attempt_count for error in errors] == [0, 0]
        assert errors[0].latency_ms is not None
        assert errors[1].latency_ms == errors[0].latency_ms
        assert agent.calls == 0
    finally:
        for prepared in held:
            await prepared.aclose()
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "missing_failure",
        "stable_error_with_response",
        "malformed_response_without_failure",
        "extra_failure_field",
        "invalid_latency",
        "provider_call_attempt_mismatch",
    ],
)
async def test_malformed_durable_failure_summary_fails_closed_without_provider_replay(
    tmp_path: Path,
    corruption: str,
) -> None:
    """稳定错误只能从封闭 failure 恢复，冲突 response 与校验异常都必须收敛。"""

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
        classifier=False,
        outcomes=[
            ModelProviderError(
                "model.provider_failed",
                status_code=500,
                side_effect_state="started",
            )
        ],
        database_name=f"malformed-failure-{corruption}.sqlite3",
    )
    usage_call_id = f"usage-malformed-failure-{corruption}"
    try:
        with pytest.raises(ModelProviderInvocationError):
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

        async with storage.uow() as uow:
            usage = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
            )
            assert usage.result_json is not None
            failure = dict(usage.result_json["failure"])
            corrupted_result = {**usage.result_json, "failure": failure}
            if corruption == "missing_failure":
                corrupted_result.pop("failure")
            elif corruption == "stable_error_with_response":
                corrupted_result["response"] = ModelResponse(
                    provider="openai-compatible",
                    model="fixture-text-1",
                    output_text="forged-success",
                    decision=ModelDecision(action="call", estimated_tokens=1),
                    token_usage={"input_tokens": 1, "output_tokens": 1},
                ).model_dump(mode="json")
            elif corruption == "malformed_response_without_failure":
                corrupted_result.pop("failure")
                corrupted_result["response"] = {"model": "fixture-text-1"}
            elif corruption == "extra_failure_field":
                failure["raw_error"] = "provider raw detail"
            elif corruption == "invalid_latency":
                failure["latency_ms"] = True
            else:
                failure.update(provider_called=False, attempt_count=1)
            await uow.session.execute(
                update(RunEvidenceOutboxModel)
                .where(RunEvidenceOutboxModel.id == usage.id)
                .values(result_json=corrupted_result)
            )
            await uow.session.execute(
                update(BudgetOperationClaimModel)
                .where(BudgetOperationClaimModel.usage_call_id == usage_call_id)
                .values(
                    result_json={
                        key: value for key, value in corrupted_result.items() if key != "started"
                    }
                )
            )
            await uow.commit()

        with pytest.raises(UsageInvocationReplayError):
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
        assert agent.calls == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_started_completed_or_failed_without_usage_keeps_reservation_and_fences_terminal(
    tmp_path: Path,
    outcome: str,
) -> None:
    """started 已完成或已失败但缺 actual 时，都保留 reservation 与 terminal 围栏。"""

    outcomes: list[object]
    if outcome == "completed":
        outcomes = [ResultWithoutUsageDouble()]
    else:
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
        reserved_tokens,
        agent,
        _client_factory,
        _provider,
        _plan,
    ) = await managed_real_invocation(
        tmp_path,
        classifier=False,
        outcomes=outcomes,
        database_name=f"{outcome}-unresolved.sqlite3",
    )
    usage_call_id = f"usage-{outcome}-unresolved"
    try:
        if outcome == "completed":
            response = await service.complete(
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
            assert response.output_text == "adapter-result-without-usage"
            assert response.token_usage == {}
        else:
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
            assert exc_info.value.code == "model.provider_failed"
        assert agent.calls == 1
        await assert_unresolved_real_settlement(
            storage=storage,
            run_id=run_id,
            usage_call_id=usage_call_id,
            reserved_tokens=reserved_tokens,
            expected_attempt_count=1,
        )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_locked_sdk_read_timeout_keeps_reservation_and_fences_terminal(
    tmp_path: Path,
) -> None:
    """真实 SDK read timeout 必须贯穿公开 invocation 为 unknown，并保留 durable 预算。"""

    calls = 0

    async def handler(inbound: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("fixture durable read timeout", request=inbound)

    (
        storage,
        _sink,
        service,
        request,
        run_id,
        reserved_tokens,
        _agent,
        client_factory,
        _provider,
        _plan,
    ) = await managed_real_invocation(
        tmp_path,
        classifier=True,
        outcomes=[],
        database_name="sdk-read-timeout-unresolved.sqlite3",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    usage_call_id = "usage-sdk-read-timeout-unresolved"
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
                usage_call_id=usage_call_id,
            )
        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert exc_info.value.provider_called is True
        assert exc_info.value.attempt_count == 1
        assert exc_info.value.latency_ms is not None
        assert exc_info.value.latency_ms >= 0
        assert calls == 1
        await assert_unresolved_real_settlement(
            storage=storage,
            run_id=run_id,
            usage_call_id=usage_call_id,
            reserved_tokens=reserved_tokens,
            expected_attempt_count=1,
        )
    finally:
        if client_factory is not None:
            await client_factory.aclose()
        await storage.dispose()


class _CancelAfterDurableMarkInvocation(ModelInvocationService):
    """在真实 mark 已提交后注入取消，复现 await 返回边界的竞态。"""

    async def _mark_side_effect_started(self, **kwargs: Any) -> None:
        await super()._mark_side_effect_started(**kwargs)
        raise DurableMarkStateUnknown


@pytest.mark.asyncio
async def test_cancel_after_durable_mark_commit_is_unknown_and_fences_terminal(
    tmp_path: Path,
) -> None:
    """mark 已提交但 await 未返回时的取消不得退款、重放或放行终态。"""

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
        outcomes=[ResultDouble()],
        database_name="cancel-after-durable-mark.sqlite3",
        service_type=_CancelAfterDurableMarkInvocation,
    )
    usage_call_id = "usage-cancel-after-durable-mark"
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
                usage_call_id=usage_call_id,
            )

        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert agent.calls == 0
        await assert_unresolved_real_settlement(
            storage=storage,
            run_id=run_id,
            usage_call_id=usage_call_id,
            reserved_tokens=reserved_tokens,
            expected_attempt_count=1,
        )
    finally:
        await storage.dispose()
