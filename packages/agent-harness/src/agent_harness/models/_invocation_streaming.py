"""受控普通文本流的策略入口与纵向编排。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from time import perf_counter
from typing import TYPE_CHECKING, Literal, cast

from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_chain import ModelApprovalGrantLike
from agent_harness.models._invocation_execution import ModelApprovalRequired
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import (
    IdentityRuntime,
    ModelProviderInvocationError,
    SettlementStart,
)
from agent_harness.models._streaming_consumption import consume_prepared_stream
from agent_harness.models._streaming_contracts import StreamingRuntime
from agent_harness.models._streaming_events import (
    persist_completed_and_final,
    publish_persisted_stream,
)
from agent_harness.models._streaming_settlement import handle_interrupted_stream
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelRequest,
    ModelResponse,
    ModelStreamCloseResult,
    PreparedModelStreamCall,
)
from agent_harness.models.router import ModelRoutePlan, ModelRouter
from agent_harness.models.streaming import StreamLimitExceeded, StreamSafetyError
from agent_harness.models.structured import StructuredOutputReplayIdentity
from agent_harness.models.usage import (
    CostStatus,
    ModelUsageEvidence,
    UsageEvidenceContext,
    UsageInvocationReplayError,
)
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage, SQLAlchemyUnitOfWork
from agent_harness.storage.evidence_repositories import UsageSettlementClaim
from agent_harness.storage.shared_budget import BudgetOperationOwnership

if TYPE_CHECKING:
    from agent_harness.registry.descriptor import AgentModelPolicy


class ModelInvocationStreamingMixin:
    """协调策略、双预留、provider 消费和原子结算，不承载各子域细节。"""

    _storage: SQLAlchemyStorage
    _router: ModelRouter
    _event_bus: EventBus
    _telemetry: TelemetryFacade | None
    _shared_budget: IdentityRuntime | None
    _policy_engine: PolicyEngine | None
    _agent_policy_resolver: Callable[[str], AgentModelPolicy] | None
    _stream_output_guardrail: Callable[[str], bool] | None
    _stream_timing_observer: Callable[[str], None] | None

    if TYPE_CHECKING:

        async def _replay_settlement_before_current_snapshot(
            self,
            *,
            request: ModelRequest,
            context: UsageEvidenceContext,
            usage_call_id: str,
        ) -> SettlementStart | None: ...

        async def _resume_existing_settlement(
            self,
            *,
            claim: UsageSettlementClaim,
            usage_call_id: str,
        ) -> ModelResponse: ...

        async def _plan(
            self,
            *,
            request: ModelRequest,
            context: UsageEvidenceContext,
            approved: bool,
        ) -> ModelRoutePlan | ModelRouteChainPlan: ...

        async def _stream_chain(
            self,
            request: ModelRequest,
            *,
            chain: ModelRouteChainPlan,
            context: UsageEvidenceContext,
            usage_call_id: str,
            operation_identity_digest: str,
            soft_approved: bool,
            actor: IdentityContext | None,
            approved_grant: ModelApprovalGrantLike | None = None,
        ) -> ModelResponse: ...

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
        ) -> ModelUsageEvidence: ...

        @staticmethod
        def _safe_decision(*parts: dict[str, object]) -> dict[str, object]: ...

        @staticmethod
        def _route_evidence(plan: ModelRoutePlan) -> dict[str, object]: ...

        async def _start_settlement(
            self,
            *,
            evidence: ModelUsageEvidence,
            usage_call_id: str,
            request: ModelRequest,
            plan: ModelRoutePlan,
            stream: bool = False,
        ) -> SettlementStart: ...

        @staticmethod
        def _attempt_summary(
            *,
            attempts: list[ModelAttemptEvidence],
            plan: ModelRoutePlan,
            provider_called: bool,
        ) -> dict[str, object]: ...

        async def _mark_side_effect_started(
            self,
            *,
            context: UsageEvidenceContext,
            usage_call_id: str,
            ownership: BudgetOperationOwnership | None,
        ) -> None: ...

        async def _persist_final_in_uow(
            self,
            *,
            uow: SQLAlchemyUnitOfWork,
            evidence: ModelUsageEvidence,
            usage_call_id: str,
            outcome: str,
            error_code: str | None,
            ownership: BudgetOperationOwnership | None,
            response: ModelResponse | None,
            structured_replay: StructuredOutputReplayIdentity | None = None,
        ) -> None: ...

        async def _finalize(
            self,
            *,
            evidence: ModelUsageEvidence,
            usage_call_id: str,
            outcome: str,
            error_code: str | None,
            ownership: BudgetOperationOwnership | None,
            response: ModelResponse | None,
            structured_replay: StructuredOutputReplayIdentity | None = None,
        ) -> None: ...

        async def _publish_final(
            self,
            *,
            evidence: ModelUsageEvidence,
            usage_call_id: str,
            outcome: str,
            error_code: str | None,
        ) -> None: ...

    def _streaming_runtime(self) -> StreamingRuntime:
        """为拆分后的子模块装配一次窄协作者视图，不暴露到公开 façade。"""

        return StreamingRuntime(
            storage=self._storage,
            router=self._router,
            event_bus=self._event_bus,
            telemetry=self._telemetry,
            output_guardrail=self._stream_output_guardrail,
            timing_observer=self._stream_timing_observer,
            mark_side_effect_started=self._mark_side_effect_started,
            persist_final_in_uow=self._persist_final_in_uow,
            finalize=self._finalize,
            attempt_summary=self._attempt_summary,
            safe_decision=self._safe_decision,
            route_evidence=self._route_evidence,
        )

    async def _stream(
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
        """执行一次无 retry/fallback 的普通文本流，并严格串行持久化公共事件。"""

        helpers = self._streaming_runtime()
        replay = await self._replay_settlement_before_current_snapshot(
            request=request,
            context=context,
            usage_call_id=usage_call_id,
        )
        if replay is not None:
            return await self._resume_existing_settlement(
                claim=replay.usage,
                usage_call_id=usage_call_id,
            )
        await self._event_bus.reconcile_local_capacity(run_id=context.run_id)
        plan = await self._plan(
            request=request,
            context=context,
            approved=soft_approved,
        )
        if isinstance(plan, ModelRouteChainPlan):
            if route_operation_identity_digest is None:
                raise ValueError("bound route-chain operation identity is required")
            return await self._stream_chain(
                request,
                chain=plan,
                context=context,
                usage_call_id=usage_call_id,
                operation_identity_digest=route_operation_identity_digest,
                soft_approved=soft_approved,
                actor=actor,
                approved_grant=approved_grant,
            )
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
                raise ModelProviderInvocationError(
                    "model.policy_denied",
                    failure_domain="runtime",
                )
            if policy.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
                # 延迟导入避免 models/runtime 初始化成环；审批 DTO 仍来自唯一状态机。
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
        # 纯校验必须早于双预留和 started；prepare 仍留到提交之后。
        self._router.validate_stream_route(request, plan=plan)
        started_evidence = self._started_evidence(
            context=context,
            provider=plan.provider,
            model=plan.model,
            decision=self._safe_decision(
                plan.decision.to_payload(),
                {"route": self._route_evidence(plan)},
                {"provider_called": True},
                {
                    "usage_event_identity": {
                        "ref": "stream-usage",
                        "version": "v1",
                    }
                },
            ),
        )
        settlement = await self._start_settlement(
            evidence=started_evidence,
            usage_call_id=usage_call_id,
            request=request,
            plan=plan,
            stream=True,
        )
        if not settlement.usage.created and not settlement.safe_to_start:
            return await self._resume_existing_settlement(
                claim=settlement.usage,
                usage_call_id=usage_call_id,
            )
        lifecycle = UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=started_evidence,
            usage_call_id=usage_call_id,
        )
        started = await lifecycle.publish_started()
        invoked_at = perf_counter()
        chunks: list[str] = []
        prepared: PreparedModelStreamCall | None = None
        close_result: ModelStreamCloseResult | None = None
        settlement_persisted = False
        try:
            # started 已经耐久；telemetry fan-out 仍处于首次 provider 迭代前的
            # not-started 取消窗口，必须复用下方统一结算而不能泄漏预留容量。
            if self._telemetry is not None:
                await self._telemetry.publish_event(started)
            route_deadline = asyncio.get_running_loop().time() + plan.total_timeout_ms / 1000
            # 这一个绝对 deadline 同时覆盖 prepare、SDK 消费、完整结果
            # guardrail、尾部分片和每个 delta 的持久化/发布；prepare 之后
            # 不得重新获得一整段 total_timeout_ms。
            async with asyncio.timeout_at(route_deadline):
                prepared = await self._router.prepare_stream(request, plan=plan)
                if self._stream_timing_observer is not None:
                    self._stream_timing_observer("origin")
                response = await consume_prepared_stream(
                    helpers,
                    prepared=prepared,
                    context=context,
                    usage_call_id=usage_call_id,
                    ownership=settlement.ownership,
                    plan=plan,
                    chunks=chunks,
                )
            controlled_real = plan.provider == "openai-compatible"
            attempt_summary = (
                self._attempt_summary(
                    attempts=response.attempts,
                    plan=plan,
                    provider_called=True,
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
                    cast(CostStatus, attempt_summary["cost_status"])
                    if attempt_summary is not None
                    else response.cost_status
                ),
                latency_ms=response.latency_ms,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
                    response.decision.to_payload(),
                    {"route": self._route_evidence(plan)},
                    {"provider_called": True},
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
            completed_intent = await persist_completed_and_final(
                helpers,
                context=context,
                usage_call_id=usage_call_id,
                chunks=chunks,
                evidence=evidence,
                outcome="completed",
                error_code=None,
                ownership=settlement.ownership,
                response=response,
            )
            settlement_persisted = True
            await publish_persisted_stream(helpers, completed_intent)
            await self._publish_final(
                evidence=evidence,
                usage_call_id=usage_call_id,
                outcome="completed",
                error_code=None,
            )
            return response
        except asyncio.CancelledError:
            if settlement_persisted:
                # completed 与 usage 已原子耐久；取消只能留给恢复补投，不能回退占位。
                raise
            observed_close_result = (
                await prepared.aclose()
                if prepared is not None
                else ModelStreamCloseResult(state="not_started")
            )
            close_result = observed_close_result
            await handle_interrupted_stream(
                helpers,
                context=context,
                usage_call_id=usage_call_id,
                plan=plan,
                ownership=settlement.ownership,
                used_delta_count=len(chunks),
                close_result=observed_close_result,
                latency_ms=int((perf_counter() - invoked_at) * 1000),
                error_code="model.invocation_cancelled",
                outcome="cancelled",
                failure_domain="runtime",
            )
            raise AssertionError("cancel handler must raise a stable invocation error") from None
        except Exception as exc:
            if settlement_persisted:
                # 公开发布是可补投副作用；耐久结果存在后不得再走 provider 中断清理。
                raise UsageInvocationReplayError("result_persisted") from exc
            observed_close_result = (
                await prepared.aclose()
                if prepared is not None
                else ModelStreamCloseResult(state="not_started")
            )
            close_result = observed_close_result
            raw_code = getattr(exc, "code", None)
            timeout_not_started = (
                isinstance(exc, TimeoutError) and observed_close_result.state == "not_started"
            )
            error_code = (
                "model.invocation_cancelled"
                if timeout_not_started
                else (
                    raw_code
                    if isinstance(raw_code, str)
                    and raw_code in ModelProviderInvocationError.stable_codes
                    else "model.provider_failed"
                )
            )
            failure_domain: Literal["provider", "runtime"]
            if timeout_not_started:
                failure_domain = "runtime"
            elif isinstance(exc, ModelProviderInvocationError):
                failure_domain = exc.failure_domain
            elif isinstance(exc, StreamLimitExceeded | StreamSafetyError):
                failure_domain = "runtime"
            elif error_code in {"model.bulkhead_saturated", "model.invocation_cancelled"}:
                failure_domain = "runtime"
            elif (
                isinstance(raw_code, str) and raw_code in ModelProviderInvocationError.stable_codes
            ):
                failure_domain = "provider"
            else:
                failure_domain = "runtime"
            settlement_close_result = (
                # 内容一致性已被破坏时，provider 的 stopped/complete 只证明本地关闭与
                # 计量形状，不能证明公开结果可安全收口；丢弃该证明并保留全部围栏。
                ModelStreamCloseResult(state="unknown")
                if error_code == "model.provider_side_effect_unknown"
                else observed_close_result
            )
            await handle_interrupted_stream(
                helpers,
                context=context,
                usage_call_id=usage_call_id,
                plan=plan,
                ownership=settlement.ownership,
                used_delta_count=len(chunks),
                close_result=settlement_close_result,
                latency_ms=int((perf_counter() - invoked_at) * 1000),
                error_code=error_code,
                outcome="cancelled" if timeout_not_started else "failed",
                failure_domain=failure_domain,
            )
            raise AssertionError("failure handler must raise a stable invocation error") from None
        finally:
            if close_result is None and prepared is not None:
                await prepared.aclose()


__all__ = ["ModelInvocationStreamingMixin"]
