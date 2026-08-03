"""Provider-neutral completion route-chain 候选控制器。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Literal, NoReturn, Protocol

from agent_harness.identity import IdentityContext
from agent_harness.models._route_chain_state import close_route_attempt
from agent_harness.models._router_contracts import ModelRouteCandidate, ModelRouteChainPlan
from agent_harness.models._settlement_contracts import (
    ModelProviderInvocationError,
    ModelRouteChainExhaustedDetail,
    RouteAttemptNotStartedFacts,
)
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelRequest,
    ModelResponse,
)
from agent_harness.models.structured import StructuredOutputReplayIdentity
from agent_harness.models.usage import CostStatus, ModelUsageEvidence, UsageEvidenceContext
from agent_harness.storage.model_route_chain_state import ModelRouteChainState


class ModelApprovalGrantLike(Protocol):
    """候选审批激活只依赖的只读 durable grant 字段。"""

    @property
    def approval_id(self) -> str: ...

    @property
    def lease_id(self) -> str: ...

    @property
    def tenant_id(self) -> str: ...

    @property
    def identity_id(self) -> str: ...

    @property
    def agent_id(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def action(self) -> str: ...

    @property
    def resource(self) -> str: ...

    @property
    def arguments_hash(self) -> str: ...


if TYPE_CHECKING:
    from agent_harness.events import EventBus
    from agent_harness.models._settlement_contracts import SettlementStart
    from agent_harness.models._streaming_contracts import StreamingRuntime
    from agent_harness.models.router import ModelRoutePlan, ModelRouter
    from agent_harness.observability.facade import TelemetryFacade
    from agent_harness.policy import PolicyEngine
    from agent_harness.runtime.executor import AgentApprovalRequest
    from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage, SQLAlchemyUnitOfWork
    from agent_harness.storage.evidence_repositories import UsageSettlementClaim
    from agent_harness.storage.shared_budget import BudgetOperationOwnership


@dataclass(frozen=True)
class ChainPolicyOutcome:
    """候选级 policy 的封闭结果；approval binding 只能随 approval 出现。"""

    decision: Literal["allow", "require_approval", "deny"]
    request: AgentApprovalRequest | None = None
    request_binding_digest: str | None = None


class ChainRuntimeBase:
    """只推进 frozen ordinal；任何切换都来自 durable not-started proof。"""

    _storage: SQLAlchemyStorage
    _router: ModelRouter
    _event_bus: EventBus
    _policy_engine: PolicyEngine | None
    _telemetry: TelemetryFacade | None

    @staticmethod
    def _route_attempt_observations(
        exc: Exception,
        *,
        delta_observed: bool = False,
    ) -> SimpleNamespace:
        """只从 provider-neutral 异常事实推导调用观察，不把 prepare 当成 send。"""

        attempts = tuple(getattr(exc, "attempts", ()))
        request_sent = bool(getattr(exc, "request_sent", False)) or any(
            attempt.side_effect_state in {"started", "unknown"} for attempt in attempts
        )
        response_observed = bool(getattr(exc, "http_response_observed", False)) or any(
            attempt.http_status is not None for attempt in attempts
        )
        usage_observed = bool(getattr(exc, "usage_observed", False)) or any(
            attempt.input_tokens is not None
            or attempt.output_tokens is not None
            or attempt.cost_usd is not None
            for attempt in attempts
        )
        completion_observed = getattr(exc, "completion_observed", None)
        if completion_observed is None and attempts:
            completion_observed = attempts[-1].completion_observed
        return SimpleNamespace(
            request_sent=request_sent,
            response_observed=response_observed,
            usage_observed=usage_observed,
            text_observed=bool(getattr(exc, "text_observed", False)),
            delta_observed=delta_observed or bool(getattr(exc, "delta_observed", False)),
            completion_observed=completion_observed,
        )

    @staticmethod
    def _route_chain_provider_called(state: ModelRouteChainState) -> bool:
        """公开调用摘要只能由耐久观察事实推出，started identity 本身不计调用。"""

        return any(
            item.request_sent
            or item.http_response_observed
            or item.response_identity_observed
            or item.usage_observed
            or item.text_observed
            or item.delta_observed
            for item in state.attempt_lifecycle
        )

    async def _raise_cancelled_route_attempt_unknown(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        state: ModelRouteChainState,
        candidate_ordinal: int,
        ownership: BudgetOperationOwnership | None,
        request_sent: bool,
        usage_observed: bool = False,
        delta_observed: bool = False,
        completion_observed: bool | None = None,
    ) -> NoReturn:
        """取消不能越过耐久 started；先关闭为 unknown 并提升人工复核。"""

        state = close_route_attempt(
            state,
            candidate_ordinal=candidate_ordinal,
            lifecycle_state="unknown",
            response_observed=False,
            delta_observed=delta_observed,
            request_sent=request_sent,
            usage_observed=usage_observed,
            text_observed=delta_observed,
            completion_observed=completion_observed,
        )
        state = await self._persist_route_chain_state(
            context=context,
            usage_call_id=usage_call_id,
            state=state,
            method="close_model_route_attempt",
        )
        if ownership is None:
            raise RuntimeError("route-chain settlement omitted budget ownership") from None
        async with self._storage.uow() as uow:
            await uow.shared_budget.recover_unknown_started(
                tenant_id=context.tenant_id,
                budget_owner_run_id=ownership.budget_owner_run_id,
            )
            await uow.commit()
        raise ModelProviderInvocationError(
            "model.provider_side_effect_unknown",
            provider_called=self._route_chain_provider_called(state),
            attempt_count=len(state.attempt_lifecycle),
        ) from None

    if TYPE_CHECKING:

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

        async def _start_chain_settlement(
            self,
            *,
            evidence: ModelUsageEvidence,
            usage_call_id: str,
            request: ModelRequest,
            chain: ModelRouteChainPlan,
            operation_identity_digest: str,
            waiting_approval_ordinal: int | None = None,
            approval_request_binding_digest: str | None = None,
            denied_ordinal: int | None = None,
            initial_active_ordinal: int = 1,
            initial_skips: dict[int, Literal["static_ineligible", "soft_budget", "balance"]]
            | None = None,
            initial_exhausted: bool = False,
            stream: bool = False,
        ) -> SettlementStart: ...

        async def _resume_existing_settlement(
            self,
            *,
            claim: UsageSettlementClaim,
            usage_call_id: str,
        ) -> ModelResponse: ...

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

        def _streaming_runtime(self) -> StreamingRuntime: ...

        async def _publish_final(
            self,
            *,
            evidence: ModelUsageEvidence,
            usage_call_id: str,
            outcome: str,
            error_code: str | None,
        ) -> None: ...

    if TYPE_CHECKING:

        async def _start_initial_chain(
            self,
            *,
            request: ModelRequest,
            chain: ModelRouteChainPlan,
            context: UsageEvidenceContext,
            usage_call_id: str,
            operation_identity_digest: str,
            soft_approved: bool,
            actor: IdentityContext | None,
            stream: bool,
        ) -> tuple[SettlementStart, ModelRouteChainState, ChainPolicyOutcome]: ...

        async def _complete_chain(
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

        def _stream_chain_evidence(self, evidence: ModelUsageEvidence) -> ModelUsageEvidence: ...

        async def _finalize_initial_chain_policy_denied(
            self,
            *,
            context: UsageEvidenceContext,
            chain: ModelRouteChainPlan,
            state: ModelRouteChainState,
            usage_call_id: str,
            settlement: SettlementStart,
            stream: bool,
        ) -> None: ...

        async def _advance_chain_successor(
            self,
            *,
            request: ModelRequest,
            context: UsageEvidenceContext,
            chain: ModelRouteChainPlan,
            state: ModelRouteChainState,
            current_ordinal: int,
            usage_call_id: str,
            operation_identity_digest: str,
            soft_approved: bool,
            actor: IdentityContext | None,
            cost_enabled: bool,
            settlement: SettlementStart,
            stream: bool,
        ) -> ModelRouteChainState: ...

        async def _activate_or_skip_approved_route(
            self,
            *,
            request: ModelRequest,
            context: UsageEvidenceContext,
            chain: ModelRouteChainPlan,
            state: ModelRouteChainState,
            approved_grant: ModelApprovalGrantLike,
            request_binding_digest: str,
            usage_call_id: str,
            operation_identity_digest: str,
            actor: IdentityContext | None,
            cost_enabled: bool,
            settlement: SettlementStart,
            stream: bool,
        ) -> ModelRouteChainState: ...

        async def _advance_after_approved_balance(
            self,
            *,
            request: ModelRequest,
            context: UsageEvidenceContext,
            chain: ModelRouteChainPlan,
            state: ModelRouteChainState,
            anchor_ordinal: int,
            usage_call_id: str,
            operation_identity_digest: str,
            actor: IdentityContext | None,
            cost_enabled: bool,
            settlement: SettlementStart,
            stream: bool,
        ) -> ModelRouteChainState: ...

        async def _finalize_chain_terminal(
            self,
            *,
            context: UsageEvidenceContext,
            chain: ModelRouteChainPlan,
            state: ModelRouteChainState,
            usage_call_id: str,
            settlement: SettlementStart,
            error_code: Literal["model.policy_denied", "model.route_chain_exhausted"],
            stream: bool,
            publish_started: bool,
        ) -> None: ...

        async def _require_chain_policy_allow(
            self,
            *,
            request: ModelRequest,
            candidate: ModelRouteCandidate,
            context: UsageEvidenceContext,
            actor: IdentityContext | None,
            chain: ModelRouteChainPlan,
            usage_call_id: str,
            operation_identity_digest: str,
        ) -> ChainPolicyOutcome: ...

        @staticmethod
        def _route_approval_grant_digest(
            *,
            approved_grant: ModelApprovalGrantLike,
            request_binding_digest: str,
            usage_call_id: str,
            operation_identity_digest: str,
        ) -> str: ...

        @staticmethod
        def _trusted_not_started_facts(
            exc: Exception, *, candidate: ModelRouteCandidate
        ) -> RouteAttemptNotStartedFacts | None: ...

        async def _load_route_chain_state(
            self, context: UsageEvidenceContext, usage_call_id: str
        ) -> ModelRouteChainState: ...

        async def _persist_route_chain_state(
            self,
            *,
            context: UsageEvidenceContext,
            usage_call_id: str,
            state: ModelRouteChainState,
            method: str,
            proof_state: ModelRouteChainState | None = None,
        ) -> ModelRouteChainState: ...

        async def _raise_cleanup_route_attempt_unknown(
            self,
            *,
            context: UsageEvidenceContext,
            usage_call_id: str,
            state: ModelRouteChainState,
            candidate_ordinal: int,
            ownership: BudgetOperationOwnership | None,
            response: ModelResponse,
            delta_observed: bool,
        ) -> NoReturn: ...

        @staticmethod
        def _global_response_attempt(
            response: ModelResponse, global_attempt: int
        ) -> ModelAttemptEvidence: ...

        def _chain_final_evidence(
            self,
            *,
            context: UsageEvidenceContext,
            chain: ModelRouteChainPlan,
            state: ModelRouteChainState,
            response: ModelResponse,
        ) -> ModelUsageEvidence: ...

        @staticmethod
        def _route_chain_exhausted_detail(
            *, chain: ModelRouteChainPlan, state: ModelRouteChainState
        ) -> ModelRouteChainExhaustedDetail: ...

        def _chain_failure_evidence(
            self,
            *,
            context: UsageEvidenceContext,
            chain: ModelRouteChainPlan,
            state: ModelRouteChainState,
            error_code: str,
        ) -> ModelUsageEvidence: ...

        @staticmethod
        def _chain_attempt_evidence(
            *,
            chain: ModelRouteChainPlan,
            state: ModelRouteChainState,
            response: ModelResponse | None,
        ) -> list[dict[str, object]]: ...

        @staticmethod
        def _chain_budget_charge(attempts: list[dict[str, object]]) -> dict[str, object]: ...


__all__ = ["ModelApprovalGrantLike"]
