"""可信 structured invocation 的 planning、repair、结算与 replay 协调。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_approval_identity import (
    ApprovalIdentityGrant,
    resolve_structured_approved_invocation_identity,
    structured_approval_arguments,
    structured_approval_arguments_hash,
    structured_approval_continuation,
)
from agent_harness.models._invocation_execution import ModelApprovalRequired
from agent_harness.models._invocation_structured_execution import (
    ModelInvocationStructuredExecutionMixin,
)
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._router_identity import route_plan_identity_payload
from agent_harness.models._settlement_contracts import (
    IdentityRuntime,
    ModelProviderInvocationError,
)
from agent_harness.models._structured_settlement_evidence_models import (
    StructuredSettlementRouteEvidence,
    StructuredSettlementSummary,
)
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelRequest,
    ModelResponse,
)
from agent_harness.models.router import ModelRouteError, ModelRoutePlan, ModelRouter
from agent_harness.models.structured import (
    OutputSchemaDefinition,
    StructuredOutputReplayIdentity,
    StructuredOutputRequest,
    StructuredSchemaResolutionError,
    maximum_structured_validation_codes,
    structured_provider_prompt,
)
from agent_harness.models.tool_catalog import ToolCatalog
from agent_harness.models.usage import CostStatus, ModelUsageEvidence, UsageEvidenceContext
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage

if TYPE_CHECKING:
    from agent_harness.models._settlement_contracts import SettlementStart
    from agent_harness.models.tool_intent import ModelTurnResult, ToolIntentReplaySeed
    from agent_harness.registry.descriptor import AgentModelPolicy
    from agent_harness.storage.evidence_repositories import UsageSettlementClaim
    from agent_harness.storage.shared_budget import BudgetOperationOwnership


class ModelInvocationStructuredMixin(
    ModelInvocationStructuredExecutionMixin,
):
    """只负责非流式单route结构化控制器。"""

    _storage: SQLAlchemyStorage
    _router: ModelRouter
    _event_bus: EventBus
    _telemetry: TelemetryFacade | None
    _shared_budget: IdentityRuntime | None
    _agent_policy_resolver: Callable[[str], AgentModelPolicy] | None
    _output_schema_resolver: Callable[[str], OutputSchemaDefinition] | None
    _policy_engine: PolicyEngine | None

    if TYPE_CHECKING:

        async def _plan(
            self,
            *,
            request: ModelRequest,
            context: UsageEvidenceContext,
            approved: bool,
            tool_catalog: ToolCatalog | None = None,
        ) -> ModelRoutePlan | ModelRouteChainPlan: ...

        @staticmethod
        def _route_evidence(plan: ModelRoutePlan) -> dict[str, object]: ...

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

        async def _start_settlement(
            self,
            *,
            evidence: ModelUsageEvidence,
            usage_call_id: str,
            request: ModelRequest,
            plan: ModelRoutePlan,
            stream: bool = False,
            structured_replay_seed: StructuredOutputReplayIdentity | None = None,
            structured_output_request: StructuredOutputRequest | None = None,
            tool_intent_replay_seed: ToolIntentReplaySeed | None = None,
        ) -> SettlementStart: ...

        async def _resume_existing_settlement(
            self,
            *,
            claim: UsageSettlementClaim,
            usage_call_id: str,
        ) -> ModelResponse: ...

        async def _mark_side_effect_started(
            self,
            *,
            context: UsageEvidenceContext,
            usage_call_id: str,
            ownership: BudgetOperationOwnership | None,
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
            turn_result: ModelTurnResult | None = None,
            settlement_attempts: list[ModelAttemptEvidence] | None = None,
            tool_intent_replay_seed: ToolIntentReplaySeed | None = None,
            structured_replay: StructuredOutputReplayIdentity | None = None,
        ) -> None: ...

    async def complete_structured(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        operation_identity_digest: str,
        repair_limit: int,
        actor: IdentityContext | None,
    ) -> ModelResponse:
        """供可信bound façade使用的service层结构化委托。"""

        return await self._complete_structured(
            request,
            context=context,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
            repair_limit=repair_limit,
            actor=actor,
            approved_grant=None,
        )

    async def complete_structured_with_approval(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        repair_limit: int,
        actor: IdentityContext,
        grant: ApprovalIdentityGrant,
    ) -> ModelResponse:
        """校验耐久structured grant后恢复原调用身份并执行一次批准续跑。"""

        return await self._complete_structured(
            request,
            context=context,
            usage_call_id=None,
            operation_identity_digest=None,
            repair_limit=repair_limit,
            actor=actor,
            approved_grant=grant,
        )

    async def _complete_structured(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str | None,
        operation_identity_digest: str | None,
        repair_limit: int,
        actor: IdentityContext | None,
        approved_grant: ApprovalIdentityGrant | None,
    ) -> ModelResponse:
        """在同一 durable claim 内执行有限 structured transport 与 repair。"""

        if isinstance(repair_limit, bool) or not 0 <= repair_limit <= 2:
            raise ModelProviderInvocationError("model.structured_policy_invalid")
        if self._output_schema_resolver is None:
            raise ModelProviderInvocationError("model.structured_schema_unknown")
        try:
            schema = self._output_schema_resolver(context.agent_id)
        except StructuredSchemaResolutionError as exc:
            raise ModelProviderInvocationError(exc.code) from None
        except Exception:
            raise ModelProviderInvocationError("model.structured_schema_unknown") from None

        initial_prompt = structured_provider_prompt(
            business_prompt=request.prompt,
            schema=schema,
            repair_ordinal=0,
        )
        structured_request = request.model_copy(
            update={"capability": "structured_output", "prompt": initial_prompt}
        )
        policy = (
            self._agent_policy_resolver(context.agent_id)
            if self._agent_policy_resolver is not None
            else None
        )
        if policy is not None and policy.fallback_routes:
            raise ModelProviderInvocationError("model.structured_route_not_allowed")
        try:
            plan = await self._plan(
                request=structured_request,
                context=context,
                approved=approved_grant is not None,
            )
            if not isinstance(plan, ModelRoutePlan):
                raise ModelRouteError(
                    "model.structured_route_not_allowed",
                    "structured output cannot use a route chain",
                )
            self._router.validate_structured_route(structured_request, plan=plan)
        except ModelRouteError as exc:
            # 通用 planning 同时服务 text/stream；structured 公开 seam 必须把
            # deployment capability 缺失收敛为唯一专用错误身份，且仍位于
            # usage claim、client 与 provider send 之前。
            code = (
                "model.structured_capability_unsupported"
                if exc.code == "model.capability_unsupported"
                else exc.code
            )
            raise ModelProviderInvocationError(code) from None
        deployment_limit = self._router.structured_repair_limit(plan)
        if repair_limit > deployment_limit:
            raise ModelProviderInvocationError("model.structured_policy_invalid")
        effective_limit = min(repair_limit, deployment_limit)
        structured_output_request = StructuredOutputRequest(
            schema=schema.identity,
            repair_limit=effective_limit,
        )
        prompt_limit = self._router.structured_prompt_byte_limit(plan)
        worst_prompts = [initial_prompt]
        for ordinal in range(1, effective_limit + 1):
            worst_prompts.append(
                structured_provider_prompt(
                    business_prompt=request.prompt,
                    schema=schema,
                    repair_ordinal=ordinal,
                    validation_codes=maximum_structured_validation_codes(),
                )
            )
        max_prompt_bytes = max(len(item.encode("utf-8")) for item in worst_prompts)
        if prompt_limit is not None and max_prompt_bytes > prompt_limit:
            raise ModelProviderInvocationError("model.input_too_large")
        transport_limit = plan.max_attempts
        provider_request_limit = transport_limit * (1 + effective_limit)
        try:
            plan = self._structured_plan(
                plan,
                prompt_bytes=(prompt_limit or max_prompt_bytes),
                provider_request_limit=provider_request_limit,
            )
        except ModelRouteError as exc:
            # 冻结快照的预算或价格身份畸形属于公共路由拒绝；不得让内部
            # ModelRouteError 越过 invocation seam，也不得在拒绝前创建 claim。
            raise ModelProviderInvocationError(exc.code) from None
        if approved_grant is not None:
            if actor is None:
                raise RuntimeError("structured approval requires bound identity")
            restored = await resolve_structured_approved_invocation_identity(
                storage=self._storage,
                context=context,
                identity_id=actor.user_id,
                grant=approved_grant,
                request=request,
                schema_identity=schema.identity,
                repair_limit=effective_limit,
            )
            usage_call_id = restored.usage_call_id
            operation_identity_digest = restored.operation_identity_digest
        elif usage_call_id is None or operation_identity_digest is None:
            raise ValueError("bound structured invocation identity is required")
        if self._policy_engine is not None and approved_grant is None:
            if actor is None:
                raise RuntimeError("model policy requires bound identity")
            policy_decision = await self._policy_engine.evaluate(
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
                        "schema_identity": schema.identity.model_dump(mode="json"),
                        "repair_limit": effective_limit,
                    },
                )
            )
            if policy_decision.decision == GuardrailDecisionStatus.DENY.value:
                raise ModelProviderInvocationError("model.policy_denied")
            if policy_decision.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
                arguments = structured_approval_arguments(
                    request=request,
                    usage_call_id=usage_call_id,
                    operation_identity_digest=operation_identity_digest,
                    schema_identity=schema.identity,
                    repair_limit=effective_limit,
                )
                arguments_hash = structured_approval_arguments_hash(arguments)
                continuation = structured_approval_continuation(arguments=arguments)
                from agent_harness.runtime.executor import AgentApprovalRequest

                raise ModelApprovalRequired(
                    AgentApprovalRequest(
                        action="model.invoke",
                        resource=f"agent:{context.agent_id}:model",
                        reason=policy_decision.reason,
                        arguments_ref=f"structured-model-request:{arguments_hash}",
                        arguments_hash=arguments_hash,
                        continuation=continuation,
                    )
                )
        assert usage_call_id is not None
        assert operation_identity_digest is not None
        route_evidence = self._route_evidence(plan)
        structured_route_identity = route_plan_identity_payload(plan)
        route_evidence.update(
            {
                "repair_limit": effective_limit,
                "provider_request_limit": provider_request_limit,
            }
        )
        # Producer 与 replay validator 共用同一 typed route 边界；这里保留原始
        # JSON-number 投影参与 digest，避免 Decimal 的序列化形式形成第二份 identity。
        try:
            StructuredSettlementRouteEvidence.model_validate(route_evidence)
        except (ValueError, TypeError):
            raise ModelProviderInvocationError("budget.reservation_rejected") from None
        route_digest = self._structured_route_digest(plan)
        expected_replay_fields = {
            "tenant_id": context.tenant_id,
            "run_id": context.run_id,
            "agent_id": context.agent_id,
            "request_id": context.request_id,
            "trace_id": context.trace_id,
            "usage_call_id": usage_call_id,
            "operation_identity_digest": operation_identity_digest,
            "prompt_digest": hashlib.sha256(initial_prompt.encode("utf-8")).hexdigest(),
            "deployment_id": plan.deployment_id,
            "provider": plan.provider,
            "model": plan.model,
            "route_digest": route_digest,
            "schema_identity": schema.identity.model_dump(mode="json"),
            "transport_attempt_limit": transport_limit,
            "repair_limit": effective_limit,
        }
        recovery_replay_seed = StructuredOutputReplayIdentity(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
            prompt_digest=hashlib.sha256(initial_prompt.encode("utf-8")).hexdigest(),
            deployment_id=plan.deployment_id,
            provider=plan.provider,
            model=plan.model,
            route_digest=route_digest,
            schema_identity=schema.identity,
            transport_attempt_limit=transport_limit,
            repair_limit=effective_limit,
            repair_count=None,
            provider_request_count=None,
            final_status="needs_review",
            value_digest=None,
        )
        async with self._storage.uow() as uow:
            try:
                existing = await uow.evidence_outbox.get_usage(
                    tenant_id=context.tenant_id,
                    usage_call_id=usage_call_id,
                )
            except LookupError:
                existing_result = None
                existing_record: tuple[str, str, dict[str, Any] | None, str | None] | None = None
            else:
                existing_result = existing.result_json
                existing_record = (
                    existing.state,
                    existing.operation_kind,
                    existing.result_json,
                    existing.error_code,
                )
        if existing_result is not None:
            raw_replay = existing_result.get("structured_replay")
            if raw_replay is not None:
                try:
                    durable_replay = StructuredOutputReplayIdentity.model_validate(raw_replay)
                except Exception:
                    raise ModelProviderInvocationError("model.structured_replay_conflict") from None
                durable_payload = durable_replay.model_dump(mode="json")
                if any(
                    durable_payload.get(key) != value
                    for key, value in expected_replay_fields.items()
                ):
                    raise ModelProviderInvocationError("model.structured_replay_conflict")
                if existing_record is not None and existing_record[0] in {
                    "result_persisted",
                    "published",
                }:
                    # Final record 先走完整 durable validator，再补投/恢复；不能先用
                    # 新 started identity 触发 repository binding 比较并泄露底层 ValueError。
                    from agent_harness.storage.evidence_repositories import UsageSettlementClaim

                    return await self._resume_existing_settlement(
                        claim=UsageSettlementClaim(
                            created=False,
                            state=existing_record[0],
                            operation_kind=existing_record[1],
                            result_json=existing_record[2],
                            error_code=existing_record[3],
                        ),
                        usage_call_id=usage_call_id,
                    )
        started_evidence = self._started_evidence(
            context=context,
            provider=plan.provider,
            model=plan.model,
            decision=self._safe_decision(
                plan.decision.to_payload(),
                {"route": route_evidence},
                {"structured_route_identity": structured_route_identity},
                {"provider_called": False},
                {
                    "structured_output": StructuredSettlementSummary(
                        schema_version="structured-output-evidence-v1",
                        schema_identity=schema.identity,
                        status="started",
                        repair_limit=effective_limit,
                        repair_count=0,
                        provider_request_limit=provider_request_limit,
                        provider_request_count=0,
                        replay_identity=None,
                        validation_issues=[],
                        error_code=None,
                    ).model_dump(mode="json")
                },
            ),
        )
        settlement = await self._start_settlement(
            evidence=started_evidence,
            usage_call_id=usage_call_id,
            request=structured_request,
            plan=plan,
            structured_replay_seed=recovery_replay_seed,
            structured_output_request=structured_output_request,
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
        started_event = await lifecycle.publish_started()
        if self._telemetry is not None:
            await self._telemetry.publish_event(started_event)

        return await self._execute_structured(
            request=request,
            structured_request=structured_request,
            context=context,
            usage_call_id=usage_call_id,
            operation_identity_digest=operation_identity_digest,
            schema=schema,
            plan=plan,
            initial_prompt=initial_prompt,
            prompt_limit=prompt_limit,
            effective_limit=effective_limit,
            transport_limit=transport_limit,
            provider_request_limit=provider_request_limit,
            route_evidence=route_evidence,
            structured_route_identity=structured_route_identity,
            route_digest=route_digest,
            settlement=settlement,
            prompt_builder=structured_provider_prompt,
        )
