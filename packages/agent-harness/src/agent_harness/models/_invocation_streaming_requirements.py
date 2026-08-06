"""Streaming mixin在主调用服务上依赖的类型检查契约。"""

from __future__ import annotations

from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_chain import ModelApprovalGrantLike
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._settlement_contracts import SettlementStart
from agent_harness.models.providers import ModelAttemptEvidence, ModelRequest, ModelResponse
from agent_harness.models.router import ModelRoutePlan
from agent_harness.models.structured import StructuredOutputReplayIdentity
from agent_harness.models.tool_catalog import ToolCatalog
from agent_harness.models.tool_intent import ModelTurnResult, ToolIntentReplaySeed
from agent_harness.models.usage import CostStatus, ModelUsageEvidence, UsageEvidenceContext
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyUnitOfWork
from agent_harness.storage.evidence_repositories import UsageSettlementClaim
from agent_harness.storage.shared_budget import BudgetOperationOwnership


class ModelInvocationStreamingRequirements:
    """只供Pyright解析跨mixin方法；运行时由真实mixin实现覆盖。"""

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
        tool_catalog: ToolCatalog | None = None,
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
        turn_result: ModelTurnResult | None = None,
        settlement_attempts: list[ModelAttemptEvidence] | None = None,
        tool_intent_replay_seed: ToolIntentReplaySeed | None = None,
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
        turn_result: ModelTurnResult | None = None,
        settlement_attempts: list[ModelAttemptEvidence] | None = None,
        tool_intent_replay_seed: ToolIntentReplaySeed | None = None,
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


__all__ = ["ModelInvocationStreamingRequirements"]
