"""Structured provider transport、repair与cleanup控制器。"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any, cast

from agent_harness.models._invocation_structured_result import (
    ModelInvocationStructuredResultMixin,
    StructuredTerminalStatus,
)
from agent_harness.models._invocation_structured_support import StructuredPromptBuilder
from agent_harness.models._settlement_contracts import (
    DurableMarkStateUnknown,
    SettlementStart,
)
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelRequest,
    ModelResponse,
    PreparedStructuredModelCall,
    StructuredModelAttemptEvidence,
    StructuredProviderCallError,
    StructuredProviderCandidate,
    StructuredProviderPrepareError,
)
from agent_harness.models.router import ModelRoutePlan
from agent_harness.models.structured import (
    OutputSchemaDefinition,
    structured_not_started_proof,
    validate_structured_candidate,
)
from agent_harness.models.usage import UsageEvidenceContext


class ModelInvocationStructuredExecutionMixin(ModelInvocationStructuredResultMixin):
    """在已冻结hard plan与durable claim上执行有限structured控制器。"""

    @staticmethod
    def _structured_send_method(
        prepared: PreparedStructuredModelCall,
    ) -> Callable[..., Awaitable[object]] | None:
        """解析不可信send属性；返回None精确表示尚未越过调用边界。"""

        try:
            send = cast(object, prepared.send_structured)
        except Exception:
            return None
        if not callable(send):
            return None
        return cast(Callable[..., Awaitable[object]], send)

    async def _execute_structured(
        self,
        *,
        request: ModelRequest,
        structured_request: ModelRequest,
        context: UsageEvidenceContext,
        usage_call_id: str,
        operation_identity_digest: str,
        schema: OutputSchemaDefinition,
        plan: ModelRoutePlan,
        initial_prompt: str,
        prompt_limit: int | None,
        effective_limit: int,
        transport_limit: int,
        provider_request_limit: int,
        route_evidence: dict[str, object],
        structured_route_identity: dict[str, Any],
        route_digest: str,
        settlement: SettlementStart,
        prompt_builder: StructuredPromptBuilder,
    ) -> ModelResponse:
        """发送provider请求并以唯一终态完成结算；不再执行policy或重建身份。"""
        attempts: list[StructuredModelAttemptEvidence] = []
        validation_issues: list[dict[str, str]] = []
        provider_request_count = 0
        mark_written = False
        final_status: StructuredTerminalStatus = "failed"
        error_code: str | None = None
        valid_value: dict[str, Any] | None = None
        current_codes: tuple[str, ...] = ()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + plan.total_timeout_ms / 1000
        stop = False
        for repair_ordinal in range(effective_limit + 1):
            provider_prompt = prompt_builder(
                business_prompt=request.prompt,
                schema=schema,
                repair_ordinal=repair_ordinal,
                validation_codes=current_codes,
            )
            # Planning冻结的是所有允许轮次的保守上界；实际发送边界仍须复核
            # 本轮完整UTF-8 prompt，防止producer漂移越过deployment cap。
            if prompt_limit is not None and len(provider_prompt.encode("utf-8")) > prompt_limit:
                error_code = "model.input_too_large"
                final_status = "failed"
                stop = True
                break
            for transport_ordinal in range(1, transport_limit + 1):
                prepared: PreparedStructuredModelCall | None = None
                prompt_digest = hashlib.sha256(provider_prompt.encode("utf-8")).hexdigest()
                global_attempt = len(attempts) + 1
                try:
                    async with asyncio.timeout_at(deadline):
                        prepared = await self._router.prepare_structured(
                            structured_request,
                            plan=plan,
                            schema=schema,
                        )
                except StructuredProviderPrepareError as exc:
                    prepare_retryable = (
                        StructuredProviderPrepareError.validated_retryable(exc) is True
                    )
                    proof = structured_not_started_proof(
                        kind="client_prepare_not_started",
                        usage_call_id=usage_call_id,
                        operation_identity_digest=operation_identity_digest,
                        route_digest=route_digest,
                        schema_identity=schema.identity,
                        prompt_digest=prompt_digest,
                        attempt=global_attempt,
                        repair_ordinal=repair_ordinal,
                        transport_ordinal=transport_ordinal,
                    )
                    attempts.append(
                        self._structured_attempt(
                            ModelAttemptEvidence(
                                attempt=1,
                                side_effect_state="not_started",
                                outcome="failed",
                                completion_observed=False,
                                latency_ms=0,
                                error_code="model.provider_failed",
                            ),
                            attempt=global_attempt,
                            schema=schema,
                            prompt_digest=prompt_digest,
                            repair_ordinal=repair_ordinal,
                            transport_ordinal=transport_ordinal,
                            trigger_codes=current_codes,
                            validation_codes=None,
                            cleanup_status="not_applicable",
                            not_started_proof=proof,
                        )
                    )
                    if prepare_retryable and transport_ordinal < transport_limit:
                        delay = self._structured_backoff_seconds(
                            plan,
                            transport_ordinal=transport_ordinal,
                        )
                        if loop.time() + delay >= deadline:
                            error_code = "model.provider_retry_exhausted"
                            final_status = "failed"
                            stop = True
                            break
                        if delay:
                            try:
                                await asyncio.sleep(delay)
                            except asyncio.CancelledError:
                                # 下一transport ordinal尚未开始；取消是当前零请求attempt的
                                # 最终事实，必须替换prepare retry proof，不能留下与公开
                                # invocation_cancelled终态互相矛盾的耐久证据。
                                cancelled_proof = structured_not_started_proof(
                                    kind="cancelled_before_send",
                                    usage_call_id=usage_call_id,
                                    operation_identity_digest=operation_identity_digest,
                                    route_digest=route_digest,
                                    schema_identity=schema.identity,
                                    prompt_digest=prompt_digest,
                                    attempt=global_attempt,
                                    repair_ordinal=repair_ordinal,
                                    transport_ordinal=transport_ordinal,
                                )
                                attempts[-1] = self._structured_attempt(
                                    ModelAttemptEvidence(
                                        attempt=1,
                                        side_effect_state="not_started",
                                        outcome="cancelled",
                                        completion_observed=False,
                                        latency_ms=0,
                                        error_code="model.invocation_cancelled",
                                    ),
                                    attempt=global_attempt,
                                    schema=schema,
                                    prompt_digest=prompt_digest,
                                    repair_ordinal=repair_ordinal,
                                    transport_ordinal=transport_ordinal,
                                    trigger_codes=current_codes,
                                    validation_codes=None,
                                    cleanup_status="not_applicable",
                                    not_started_proof=cancelled_proof,
                                )
                                error_code = "model.invocation_cancelled"
                                final_status = "failed"
                                stop = True
                                break
                        continue
                    error_code = (
                        "model.provider_retry_exhausted"
                        if prepare_retryable
                        else "model.provider_failed"
                    )
                    final_status = "failed"
                    stop = True
                    break
                except (asyncio.CancelledError, TimeoutError, Exception) as exc:
                    cancelled = isinstance(exc, asyncio.CancelledError)
                    proof = structured_not_started_proof(
                        kind=(
                            "cancelled_before_send" if cancelled else "client_prepare_not_started"
                        ),
                        usage_call_id=usage_call_id,
                        operation_identity_digest=operation_identity_digest,
                        route_digest=route_digest,
                        schema_identity=schema.identity,
                        prompt_digest=prompt_digest,
                        attempt=global_attempt,
                        repair_ordinal=repair_ordinal,
                        transport_ordinal=transport_ordinal,
                    )
                    prepare_error = (
                        "model.invocation_cancelled" if cancelled else "model.provider_failed"
                    )
                    attempts.append(
                        self._structured_attempt(
                            ModelAttemptEvidence(
                                attempt=1,
                                side_effect_state="not_started",
                                outcome="cancelled" if cancelled else "failed",
                                completion_observed=False,
                                latency_ms=0,
                                error_code=prepare_error,
                            ),
                            attempt=global_attempt,
                            schema=schema,
                            prompt_digest=prompt_digest,
                            repair_ordinal=repair_ordinal,
                            transport_ordinal=transport_ordinal,
                            trigger_codes=current_codes,
                            validation_codes=None,
                            cleanup_status="not_applicable",
                            not_started_proof=proof,
                        )
                    )
                    error_code = prepare_error
                    final_status = "failed"
                    stop = True
                    break

                candidate: StructuredProviderCandidate | None = None
                call_error_code: str | None = None
                call_error_attempt: ModelAttemptEvidence | None = None
                unknown_attempt: ModelAttemptEvidence | None = None
                pre_send_failure_code: str | None = None
                pre_send_needs_review = False
                cleanup_status = "completed"
                send_started_at = perf_counter()
                try:
                    send_structured = self._structured_send_method(prepared)
                    if send_structured is None:
                        pre_send_failure_code = "model.provider_failed"
                    elif not mark_written:
                        async with asyncio.timeout_at(deadline):
                            await self._mark_side_effect_started(
                                context=context,
                                usage_call_id=usage_call_id,
                                ownership=settlement.ownership,
                            )
                        mark_written = True
                    if send_structured is not None:
                        provider_request_count += 1
                        async with asyncio.timeout_at(deadline):
                            raw_candidate = await send_structured(
                                provider_prompt=provider_prompt,
                                repair_ordinal=repair_ordinal,
                                transport_ordinal=transport_ordinal,
                            )
                        candidate = StructuredProviderCandidate.validated_snapshot(raw_candidate)
                        if candidate is None:
                            unknown_attempt = ModelAttemptEvidence(
                                attempt=1,
                                side_effect_state="unknown",
                                outcome="unknown",
                                latency_ms=int((perf_counter() - send_started_at) * 1000),
                                error_code="model.provider_side_effect_unknown",
                            )
                except StructuredProviderCallError as exc:
                    call_error_snapshot = StructuredProviderCallError.validated_attempt(exc)
                    if call_error_snapshot is None:
                        unknown_attempt = ModelAttemptEvidence(
                            attempt=1,
                            side_effect_state="unknown",
                            outcome="unknown",
                            latency_ms=int((perf_counter() - send_started_at) * 1000),
                            error_code="model.provider_side_effect_unknown",
                        )
                    else:
                        call_error_code, call_error_attempt = call_error_snapshot
                except DurableMarkStateUnknown:
                    # send仍可精确证明未调用，但mark事务的commit ack未知；终态必须
                    # 保留预算预约，不能借零provider request把claim按actual-zero结算。
                    pre_send_failure_code = "model.provider_side_effect_unknown"
                    pre_send_needs_review = True
                except asyncio.CancelledError:
                    if provider_request_count == 0:
                        pre_send_failure_code = "model.invocation_cancelled"
                    else:
                        unknown_attempt = ModelAttemptEvidence(
                            attempt=1,
                            side_effect_state="unknown",
                            outcome="cancelled",
                            latency_ms=int((perf_counter() - send_started_at) * 1000),
                            error_code="model.provider_side_effect_unknown",
                        )
                except TimeoutError:
                    if provider_request_count == 0:
                        # mark自身耗尽deadline时send仍未调用；以本地确定失败和
                        # 零请求proof收口，不把内部事务超时伪造成provider副作用。
                        pre_send_failure_code = "model.provider_failed"
                    else:
                        unknown_attempt = ModelAttemptEvidence(
                            attempt=1,
                            side_effect_state="unknown",
                            outcome="unknown",
                            latency_ms=int((perf_counter() - send_started_at) * 1000),
                            error_code="model.provider_side_effect_unknown",
                        )
                except Exception:
                    unknown_attempt = ModelAttemptEvidence(
                        attempt=1,
                        side_effect_state="unknown",
                        outcome="unknown",
                        latency_ms=int((perf_counter() - send_started_at) * 1000),
                        error_code="model.provider_side_effect_unknown",
                    )
                finally:
                    cleanup_status = await self._close_structured_prepared(
                        prepared,
                        deadline=deadline,
                    )

                if pre_send_failure_code is not None:
                    # mark事务的commit ack未知已经要求needs-review；后续cleanup失败
                    # 只能补充清理事实，不能把更保守的副作用未知降级为普通失败。
                    pre_send_error = (
                        pre_send_failure_code
                        if pre_send_needs_review or cleanup_status == "completed"
                        else "model.provider_failed"
                    )
                    proof = structured_not_started_proof(
                        kind="cancelled_before_send",
                        usage_call_id=usage_call_id,
                        operation_identity_digest=operation_identity_digest,
                        route_digest=route_digest,
                        schema_identity=schema.identity,
                        prompt_digest=prompt_digest,
                        attempt=global_attempt,
                        repair_ordinal=repair_ordinal,
                        transport_ordinal=transport_ordinal,
                    )
                    attempts.append(
                        self._structured_attempt(
                            ModelAttemptEvidence(
                                attempt=1,
                                side_effect_state="not_started",
                                outcome="unknown"
                                if pre_send_needs_review
                                else (
                                    "cancelled"
                                    if pre_send_error == "model.invocation_cancelled"
                                    else "failed"
                                ),
                                completion_observed=False,
                                latency_ms=int((perf_counter() - send_started_at) * 1000),
                                error_code=pre_send_error,
                            ),
                            attempt=global_attempt,
                            schema=schema,
                            prompt_digest=prompt_digest,
                            repair_ordinal=repair_ordinal,
                            transport_ordinal=transport_ordinal,
                            trigger_codes=current_codes,
                            validation_codes=None,
                            cleanup_status=cleanup_status,
                            not_started_proof=proof,
                        )
                    )
                    error_code = pre_send_error
                    final_status = "needs_review" if pre_send_needs_review else "failed"
                    stop = True
                    break

                local = (
                    candidate.attempts[0]
                    if candidate is not None
                    else call_error_attempt
                    if call_error_code is not None
                    else unknown_attempt
                )
                assert local is not None
                attempt_validation_codes: tuple[str, ...] | None = None
                validation = None
                route_candidate_matches = candidate is not None and (
                    candidate.provider == plan.provider and candidate.model == plan.model
                )
                usage_complete = (
                    local.input_tokens is not None
                    and local.output_tokens is not None
                    and (plan.input_token_price_usd is None or local.cost_usd is not None)
                )
                definite_call_failure = call_error_code is not None and (
                    call_error_code == "model.provider_failed"
                    and local.side_effect_state == "started"
                    and local.outcome == "failed"
                    and local.completion_observed is True
                    and local.error_code == call_error_code
                )
                if candidate is not None and route_candidate_matches and usage_complete:
                    validation = validate_structured_candidate(candidate, schema=schema)
                    attempt_validation_codes = tuple(
                        sorted({item["code"] for item in validation.issues})
                    )
                attempts.append(
                    self._structured_attempt(
                        local,
                        attempt=global_attempt,
                        schema=schema,
                        prompt_digest=prompt_digest,
                        repair_ordinal=repair_ordinal,
                        transport_ordinal=transport_ordinal,
                        trigger_codes=current_codes,
                        validation_codes=attempt_validation_codes,
                        cleanup_status=cleanup_status,
                    )
                )
                if cleanup_status != "completed" or unknown_attempt is not None:
                    error_code = "model.provider_side_effect_unknown"
                    final_status = "needs_review"
                    stop = True
                    break
                if call_error_code is not None:
                    error_code = (
                        call_error_code
                        if usage_complete and definite_call_failure
                        else "model.provider_side_effect_unknown"
                    )
                    final_status = (
                        "failed" if error_code == "model.provider_failed" else "needs_review"
                    )
                    stop = True
                    break
                assert candidate is not None
                if not route_candidate_matches:
                    error_code = "model.provider_side_effect_unknown"
                    final_status = "needs_review"
                    stop = True
                    break
                if not usage_complete:
                    error_code = "model.provider_side_effect_unknown"
                    final_status = "needs_review"
                    stop = True
                    break
                assert validation is not None
                validation_issues = validation.issues
                if validation.status == "valid":
                    valid_value = validation.value
                    final_status = "valid"
                    stop = True
                    break
                if repair_ordinal == effective_limit:
                    final_status = validation.status if effective_limit == 0 else "repair_exhausted"
                    error_code = (
                        "model.structured_extra_fields"
                        if validation.status == "extra_fields" and effective_limit == 0
                        else "model.structured_invalid"
                        if effective_limit == 0
                        else "model.structured_repair_exhausted"
                    )
                    stop = True
                    break
                current_codes = tuple(sorted({item["code"] for item in validation.issues}))
                break
            if stop:
                break

        return await self._finalize_structured_execution(
            context=context,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
            schema=schema,
            plan=plan,
            initial_prompt=initial_prompt,
            effective_limit=effective_limit,
            transport_limit=transport_limit,
            provider_request_limit=provider_request_limit,
            route_evidence=route_evidence,
            structured_route_identity=structured_route_identity,
            route_digest=route_digest,
            settlement=settlement,
            attempts=attempts,
            validation_issues=validation_issues,
            provider_request_count=provider_request_count,
            final_status=final_status,
            error_code=error_code,
            valid_value=valid_value,
        )
