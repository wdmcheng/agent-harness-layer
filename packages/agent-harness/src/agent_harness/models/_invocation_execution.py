"""模型 policy、route、provider 执行与 settlement 协调。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_chain import (
    ModelApprovalGrantLike,
)
from agent_harness.models._invocation_execution_tool_support import (
    ModelApprovalRequired,
    ModelLoopReservationError,
    model_policy_approval_request,
    normalize_tool_turn,
    successful_usage_evidence,
    validate_loop_reservation_bounds,
)
from agent_harness.models._invocation_settlement import (
    DurableMarkStateUnknown,
    ModelProviderInvocationError,
    _ModelSettlementMixin,
)
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelRequest,
    ModelResponse,
    PreparedModelCall,
    PreparedModelToolIntentCall,
)
from agent_harness.models.router import ModelRouter
from agent_harness.models.tool_catalog import ToolCatalog, ToolCatalogSelection
from agent_harness.models.tool_intent import (
    FinalTextTurnResult,
    ModelTurnResult,
    ToolIntentReplaySeed,
    tool_intent_operation_identity_digest,
)
from agent_harness.models.usage import ModelUsageEvidence, UsageEvidenceContext
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.policy import PolicyEngine
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.event_capacity_repositories import EventCapacityExceeded
from agent_harness.storage.shared_budget import BudgetReservationRejected

if TYPE_CHECKING:
    from agent_harness.registry.descriptor import AgentModelPolicy


class ModelInvocationExecutionMixin(_ModelSettlementMixin):
    """协调安全调用顺序；配置、路由和持久化细节由窄协作者提供。"""

    _storage: SQLAlchemyStorage
    _router: ModelRouter
    _event_bus: EventBus
    _shared_budget: Any | None
    _policy_engine: PolicyEngine | None
    _telemetry: TelemetryFacade | None
    _agent_policy_resolver: Callable[[str], AgentModelPolicy] | None
    _tool_catalog_resolver: Callable[[str, ToolCatalogSelection | None], ToolCatalog] | None

    async def _complete(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        route_operation_identity_digest: str | None,
        soft_approved: bool,
        actor: IdentityContext | None,
        approved_grant: ModelApprovalGrantLike | None,
        tool_catalog: ToolCatalog | None = None,
        tool_catalog_selection: ToolCatalogSelection | None = None,
        tool_loop_id: str | None = None,
        tool_turn_ordinal: int | None = None,
        expected_tool_replay_seed: ToolIntentReplaySeed | None = None,
        loop_token_bound: int | None = None,
        loop_cost_bound: float | None = None,
    ) -> ModelResponse | ModelTurnResult:
        """执行一次 model 调用；任何 provider 副作用都晚于 durable 预约。"""

        # 重放检查和预约必须先于 provider 副作用，才能在重试中保持幂等结算。
        call_id = usage_call_id
        tool_mode = validate_loop_reservation_bounds(
            tool_loop_id=tool_loop_id,
            tool_turn_ordinal=tool_turn_ordinal,
            operation_identity_digest=route_operation_identity_digest,
            token_bound=loop_token_bound,
            cost_bound=loop_cost_bound,
        )
        bound_operation_identity_digest = route_operation_identity_digest
        replay = await self._replay_settlement_before_current_snapshot(
            request=request,
            context=context,
            usage_call_id=call_id,
        )
        if replay is not None:
            if tool_mode and replay.safe_to_start:
                if replay.tool_intent_replay_seed is None:
                    raise RuntimeError("tool-intent replay lost its durable catalog seed")
                tool_catalog = replay.tool_intent_replay_seed.tool_catalog
            elif tool_mode:
                return await self._resume_existing_turn_settlement(
                    claim=replay.usage,
                    usage_call_id=call_id,
                )
            else:
                return await self._resume_existing_settlement(
                    claim=replay.usage,
                    usage_call_id=call_id,
                )
        if tool_mode and tool_catalog is None:
            if self._tool_catalog_resolver is None:
                raise RuntimeError("tool catalog resolver is not configured")
            tool_catalog = self._tool_catalog_resolver(
                context.agent_id,
                tool_catalog_selection,
            )
        await self._event_bus.reconcile_local_capacity(run_id=context.run_id)
        started_evidence: ModelUsageEvidence | None = None
        tool_replay_seed: ToolIntentReplaySeed | None = None
        try:
            route_plan = await self._plan(
                request=request,
                context=context,
                approved=soft_approved,
                tool_catalog=tool_catalog,
            )
            if isinstance(route_plan, ModelRouteChainPlan):
                if tool_catalog is not None:
                    raise ValueError("tool-intent route cannot use a route chain")
                if route_operation_identity_digest is None:
                    raise ValueError("bound route-chain operation identity is required")
                return await self._complete_chain(
                    request,
                    chain=route_plan,
                    context=context,
                    usage_call_id=call_id,
                    operation_identity_digest=route_operation_identity_digest,
                    soft_approved=soft_approved,
                    actor=actor,
                    approved_grant=approved_grant,
                )
            plan = route_plan
            if loop_token_bound is not None:
                if plan.reserved_token_bound > loop_token_bound:
                    raise ModelLoopReservationError("model.tool_loop_limit_exceeded")
                if loop_cost_bound is not None:
                    if plan.reserved_cost_bound is None:
                        raise ModelLoopReservationError("model.tool_loop_needs_review")
                    if plan.reserved_cost_bound > Decimal(str(loop_cost_bound)):
                        raise ModelLoopReservationError("model.tool_loop_limit_exceeded")
            if tool_catalog is not None:
                if bound_operation_identity_digest is None:
                    raise ValueError("tool-intent plan is missing operation identity")
                if (
                    plan.tool_request_identity is None
                    or plan.provider_tool_catalog_json is None
                    or tool_loop_id is None
                    or tool_turn_ordinal is None
                ):
                    raise ValueError("tool-intent plan is missing replay identity")
                tool_replay_seed = ToolIntentReplaySeed(
                    usage_call_id=call_id,
                    loop_id=tool_loop_id,
                    turn_ordinal=tool_turn_ordinal,
                    bound_operation_identity_digest=bound_operation_identity_digest,
                    operation_identity_digest=tool_intent_operation_identity_digest(
                        usage_call_id=call_id,
                        bound_operation_identity_digest=bound_operation_identity_digest,
                        loop_id=tool_loop_id,
                        turn_ordinal=tool_turn_ordinal,
                        tool_request_identity_digest=plan.tool_request_identity.digest,
                    ),
                    tool_catalog=tool_catalog,
                    request_identity=plan.tool_request_identity,
                    provider_tool_catalog_json=plan.provider_tool_catalog_json,
                )
                if (
                    expected_tool_replay_seed is not None
                    and tool_replay_seed != expected_tool_replay_seed
                ):
                    raise ValueError("tool-intent approved route identity has drifted")
            approval_request = await model_policy_approval_request(
                policy_engine=self._policy_engine,
                soft_approved=soft_approved,
                actor=actor,
                context=context,
                plan=plan,
                request=request,
                usage_call_id=call_id,
                replay_seed=tool_replay_seed,
            )
            if approval_request is not None:
                raise ModelApprovalRequired(approval_request)
            started_evidence = self._started_evidence(
                context=context,
                provider=plan.provider,
                model=plan.model,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
                    {"route": self._route_evidence(plan)},
                    {"provider_called": plan.decision.action != "policy_required"},
                ),
            )
            started_evidence = self._tool_loop_correlated_evidence(
                started_evidence,
                replay_seed=tool_replay_seed,
                turn_result=None,
            )
            settlement = await self._start_settlement(
                evidence=started_evidence,
                usage_call_id=call_id,
                request=request,
                plan=plan,
                tool_intent_replay_seed=tool_replay_seed,
            )
        except BudgetReservationRejected as exc:
            if tool_mode:
                # Tool-enabled preflight 的冻结 delta 明确要求零 usage claim；共享
                # reservation UoW 已回滚，不能再用普通模型的 rejection row 反向
                # 制造一个本轮 claim。稳定错误仍由原 BudgetReservationRejected 提供。
                raise
            if started_evidence is None:
                started_evidence = self._started_evidence(
                    context=context,
                    provider=request.provider or self._router.config.default_provider,
                    model=request.model or self._router.config.default_model,
                    decision={"provider_called": False},
                )
            try:
                await self._record_budget_rejection(
                    evidence=started_evidence,
                    usage_call_id=call_id,
                    reason=exc.reason,
                )
            except EventCapacityExceeded:
                # Hard budget 的公开优先级高于 capacity exhaustion；容量不足时
                # 无法再新增 rejection event，但不能用较低优先级错误覆盖它。
                pass
            raise
        # 已存在但尚不可安全启动时只恢复其确定性结果，绝不能再次调用 provider。
        if not settlement.usage.created and not settlement.safe_to_start:
            if tool_catalog is not None:
                return await self._resume_existing_turn_settlement(
                    claim=settlement.usage,
                    usage_call_id=call_id,
                )
            return await self._resume_existing_settlement(
                claim=settlement.usage,
                usage_call_id=call_id,
            )
        lifecycle = UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=started_evidence,
            usage_call_id=call_id,
        )
        started = await lifecycle.publish_started()
        if self._telemetry is not None:
            await self._telemetry.publish_event(started)

        invoked_at = perf_counter()
        prepared = None
        turn_result: ModelTurnResult | None = None
        send_started = False
        mark_in_progress = False
        try:
            if plan.decision.action == "policy_required":
                provider_response = await self._router.execute(request, plan=plan)
            else:
                # permit/client 构造必须晚于 reservation、早于 durable mark，且自身不触网。
                prepared = (
                    await self._router.prepare_tool_intent(request, plan=plan)
                    if tool_catalog is not None
                    else await self._router.prepare(request, plan=plan)
                )
                mark_in_progress = True
                await self._mark_side_effect_started(
                    context=context,
                    usage_call_id=call_id,
                    ownership=settlement.ownership,
                )
                mark_in_progress = False
                send_started = True
                if tool_catalog is None:
                    prepared_model = cast(PreparedModelCall, prepared)
                    provider_response = self._router.normalize_response(
                        await prepared_model.send(), plan=plan
                    )
                else:
                    if tool_loop_id is None or tool_turn_ordinal is None:
                        raise RuntimeError("bound tool loop identity is required")
                    prepared_tool = cast(PreparedModelToolIntentCall, prepared)
                    # Adapter 返回在 exact DTO 重验前始终视为不受信 object。
                    raw_turn = cast(object, await prepared_tool.send_tool_intent())
                    provider_response, turn_result = normalize_tool_turn(
                        raw_turn,
                        plan=plan,
                        catalog=tool_catalog,
                        loop_id=tool_loop_id,
                        turn_ordinal=tool_turn_ordinal,
                        usage_call_id=call_id,
                    )
                    if turn_result is None:
                        provider_response = self._router.normalize_response(
                            provider_response,
                            plan=plan,
                        )
            # adapter 可能错误地用 model_construct 绕过 DTO；副作用后必须重新校验。
            response = ModelResponse.model_validate(provider_response.model_dump(mode="python"))
            if response.provider != plan.provider or response.model != plan.model:
                raise ValueError("model response identity does not match routing plan")
            provider_called = plan.decision.action != "policy_required"
            controlled_real = plan.provider == "openai-compatible"
            attempt_summary = (
                self._attempt_summary(
                    attempts=response.attempts,
                    plan=plan,
                    provider_called=provider_called,
                )
                if controlled_real
                else None
            )
            evidence = successful_usage_evidence(
                context=context,
                response=response,
                attempt_summary=attempt_summary,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
                    response.decision.to_payload(),
                    {"route": self._route_evidence(plan)},
                    {"provider_called": provider_called},
                    (
                        {
                            "attempts": attempt_summary["attempts"],
                            "budget_charge": attempt_summary["budget_charge"],
                        }
                        if attempt_summary is not None
                        else {}
                    ),
                ),
            )
            if tool_catalog is not None and turn_result is None:
                turn_result = FinalTextTurnResult(response=response)
        except asyncio.CancelledError as exc:
            # mark 的 commit 可能已经生效，但 await 尚未正常返回。此时即使 send
            # 尚未在本协程可见，也不能按确定性 pre-mark cancel 释放 reservation。
            provider_called = send_started or isinstance(exc, DurableMarkStateUnknown)
            controlled_real = plan.provider == "openai-compatible"
            error_code = (
                "model.provider_side_effect_unknown"
                if provider_called
                else "model.invocation_cancelled"
            )
            attempt_summary = (
                self._attempt_summary(
                    attempts=(
                        [
                            ModelAttemptEvidence(
                                attempt=1,
                                outcome="cancelled",
                                side_effect_state="unknown",
                                latency_ms=int((perf_counter() - invoked_at) * 1000),
                                error_code=error_code,
                            )
                        ]
                        if provider_called
                        else []
                    ),
                    plan=plan,
                    provider_called=provider_called,
                )
                if controlled_real
                else None
            )
            failure_latency_ms = int((perf_counter() - invoked_at) * 1000)
            evidence = self._started_evidence(
                context=context,
                provider=plan.provider,
                model=plan.model,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
                    {"route": self._route_evidence(plan)},
                    {"provider_called": provider_called},
                    (
                        {
                            "attempts": attempt_summary["attempts"],
                            "budget_charge": attempt_summary["budget_charge"],
                        }
                        if attempt_summary is not None
                        else {}
                    ),
                ),
                latency_ms=failure_latency_ms,
            )
            await self._finalize(
                evidence=evidence,
                usage_call_id=call_id,
                outcome="failed",
                error_code=error_code,
                ownership=settlement.ownership,
                response=None,
                tool_intent_replay_seed=tool_replay_seed,
            )
            raise ModelProviderInvocationError(
                error_code,
                provider_called=provider_called,
                attempt_count=1 if provider_called else 0,
                latency_ms=failure_latency_ms,
            ) from None
        except Exception as exc:
            if mark_in_progress:
                # mark 内异常可能发生在耐久提交前或提交后；这里不能猜测状态并写入
                # provider_failed settlement。保留原异常，让恢复路径从存储事实判定
                # not_started 可重放或 started 必须围栏。
                raise
            error_code = str(getattr(exc, "code", "model.provider_failed"))
            provider_called = send_started
            controlled_real = plan.provider == "openai-compatible"
            raw_attempts = getattr(exc, "attempts", ())
            safe_attempts = [
                item for item in raw_attempts if isinstance(item, ModelAttemptEvidence)
            ]
            if controlled_real and provider_called and not safe_attempts:
                safe_attempts = [
                    ModelAttemptEvidence(
                        attempt=1,
                        outcome="unknown",
                        side_effect_state="unknown",
                        latency_ms=int((perf_counter() - invoked_at) * 1000),
                        error_code=error_code,
                    )
                ]
            attempt_summary = (
                self._attempt_summary(
                    attempts=safe_attempts,
                    plan=plan,
                    provider_called=provider_called,
                )
                if controlled_real
                else None
            )
            failure_latency_ms = int((perf_counter() - invoked_at) * 1000)
            evidence = self._started_evidence(
                context=context,
                provider=plan.provider,
                model=plan.model,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
                    {"route": self._route_evidence(plan)},
                    {"provider_called": provider_called},
                    (
                        {
                            "attempts": attempt_summary["attempts"],
                            "budget_charge": attempt_summary["budget_charge"],
                        }
                        if attempt_summary is not None
                        else {}
                    ),
                ),
                latency_ms=failure_latency_ms,
                input_tokens=(
                    cast(int | None, attempt_summary["input_tokens"])
                    if attempt_summary is not None
                    else None
                ),
                output_tokens=(
                    cast(int | None, attempt_summary["output_tokens"])
                    if attempt_summary is not None
                    else None
                ),
                cost_usd=(
                    cast(float | None, attempt_summary["cost_usd"])
                    if attempt_summary is not None
                    else None
                ),
                cost_status=(
                    cast(Any, attempt_summary["cost_status"])
                    if attempt_summary is not None
                    else "unavailable"
                ),
            )
            await self._finalize(
                evidence=evidence,
                usage_call_id=call_id,
                outcome="failed",
                error_code=error_code,
                ownership=settlement.ownership,
                response=None,
                tool_intent_replay_seed=tool_replay_seed,
            )
            raise ModelProviderInvocationError(
                error_code,
                provider_called=provider_called,
                attempt_count=(len(safe_attempts) if controlled_real else int(provider_called)),
                latency_ms=failure_latency_ms,
            ) from None
        finally:
            if prepared is not None:
                await prepared.aclose()

        rejected = response.decision.action == "policy_required"
        await self._finalize(
            evidence=evidence,
            usage_call_id=call_id,
            outcome="rejected" if rejected else "completed",
            error_code="model.policy_required" if rejected else None,
            ownership=settlement.ownership,
            response=response if tool_catalog is None else None,
            turn_result=turn_result,
            settlement_attempts=(
                cast(list[ModelAttemptEvidence], response.attempts)
                if tool_catalog is not None
                else None
            ),
            tool_intent_replay_seed=tool_replay_seed,
        )
        return turn_result if turn_result is not None else response
