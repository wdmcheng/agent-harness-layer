"""模型 policy、route、provider 执行与 settlement 协调。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from time import perf_counter
from typing import TYPE_CHECKING, Any, cast

from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_chain import (
    ModelApprovalGrantLike,
)
from agent_harness.models._invocation_settlement import (
    DurableMarkStateUnknown,
    ModelProviderInvocationError,
    _ModelSettlementMixin,
)
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models.providers import ModelAttemptEvidence, ModelRequest, ModelResponse
from agent_harness.models.router import ModelRouter
from agent_harness.models.usage import ModelUsageEvidence, UsageEvidenceContext
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.event_capacity_repositories import EventCapacityExceeded
from agent_harness.storage.shared_budget import BudgetReservationRejected

if TYPE_CHECKING:
    from agent_harness.registry.descriptor import AgentModelPolicy
    from agent_harness.runtime.executor import AgentApprovalRequest


class ModelApprovalRequired(RuntimeError):
    """策略要求 durable 审批时返回的受控暂停信号。"""

    code = "model.approval_required"

    def __init__(self, request: AgentApprovalRequest) -> None:
        super().__init__(self.code)
        self.request = request


class ModelInvocationExecutionMixin(_ModelSettlementMixin):
    """协调安全调用顺序；配置、路由和持久化细节由窄协作者提供。"""

    _storage: SQLAlchemyStorage
    _router: ModelRouter
    _event_bus: EventBus
    _shared_budget: Any | None
    _policy_engine: PolicyEngine | None
    _telemetry: TelemetryFacade | None
    _agent_policy_resolver: Callable[[str], AgentModelPolicy] | None

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
    ) -> ModelResponse:
        """执行一次 model 调用；任何 provider 副作用都晚于 durable 预约。"""

        # 重放检查和预约必须先于 provider 副作用，才能在重试中保持幂等结算。
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
        await self._event_bus.reconcile_local_capacity(run_id=context.run_id)
        started_evidence: ModelUsageEvidence | None = None
        try:
            route_plan = await self._plan(
                request=request,
                context=context,
                approved=soft_approved,
            )
            if isinstance(route_plan, ModelRouteChainPlan):
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
            if self._policy_engine is not None and not soft_approved:
                if actor is None:
                    raise RuntimeError("model policy requires bound identity")
                policy = await self._policy_engine.evaluate(
                    PolicyCheck(
                        actor=actor,
                        action="model.invoke",
                        resource=f"agent:{context.agent_id}:model",
                        context={
                            "tenant_id": context.tenant_id,
                            "agent_id": context.agent_id,
                            "run_id": context.run_id,
                            "request_id": context.request_id,
                            "trace_id": context.trace_id,
                            "deployment_id": plan.deployment_id,
                            "provider": plan.provider,
                            "model": plan.model,
                            "model_catalog_ref": plan.model_catalog_ref,
                            "model_catalog_version": plan.model_catalog_version,
                            "model_catalog_digest": plan.model_catalog_digest,
                            "reserved_token_bound": plan.reserved_token_bound,
                            "reserved_cost_bound": (
                                None
                                if plan.reserved_cost_bound is None
                                else float(plan.reserved_cost_bound)
                            ),
                            "soft_decision": plan.decision.action,
                        },
                    )
                )
                if policy.decision == GuardrailDecisionStatus.DENY.value:
                    raise ModelProviderInvocationError("model.policy_denied")
                if policy.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
                    # 延迟导入避免 models/runtime 公开 facade 初始化形成环；此时 runtime
                    # composition 已经完成，DTO 仍复用唯一的既有审批状态机类型。
                    from agent_harness.runtime.executor import AgentApprovalRequest

                    arguments_hash = hashlib.sha256(
                        json.dumps(
                            request.to_payload(),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest()
                    raise ModelApprovalRequired(
                        AgentApprovalRequest(
                            action="model.invoke",
                            resource=f"agent:{context.agent_id}:model",
                            reason=policy.reason,
                            arguments_ref=f"model-request:{arguments_hash}",
                            arguments_hash=arguments_hash,
                            continuation={
                                "kind": "policy_approval",
                                "deployment_id": plan.deployment_id,
                                "model": plan.model,
                            },
                        )
                    )
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
            settlement = await self._start_settlement(
                evidence=started_evidence,
                usage_call_id=call_id,
                request=request,
                plan=plan,
            )
        except BudgetReservationRejected as exc:
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
        send_started = False
        mark_in_progress = False
        try:
            if plan.decision.action == "policy_required":
                provider_response = await self._router.execute(request, plan=plan)
            else:
                # permit/client 构造必须晚于 reservation、早于 durable mark，且自身不触网。
                prepared = await self._router.prepare(request, plan=plan)
                mark_in_progress = True
                await self._mark_side_effect_started(
                    context=context,
                    usage_call_id=call_id,
                    ownership=settlement.ownership,
                )
                mark_in_progress = False
                send_started = True
                provider_response = self._router.normalize_response(
                    await prepared.send(), plan=plan
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
            evidence = ModelUsageEvidence(
                usage_kind="model",
                tenant_id=context.tenant_id,
                provider=response.provider,
                model=response.model,
                input_tokens=(
                    cast(int | None, attempt_summary["input_tokens"])
                    if attempt_summary is not None
                    else response.token_usage.get("input_tokens")
                ),
                output_tokens=(
                    cast(int | None, attempt_summary["output_tokens"])
                    if attempt_summary is not None
                    else response.token_usage.get("output_tokens")
                ),
                cost_usd=(
                    cast(float | None, attempt_summary["cost_usd"])
                    if attempt_summary is not None
                    else response.cost_usd
                ),
                cost_status=(
                    cast(Any, attempt_summary["cost_status"])
                    if attempt_summary is not None
                    else response.cost_status
                ),
                latency_ms=response.latency_ms,
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
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            )
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
            response=response,
        )
        return response
