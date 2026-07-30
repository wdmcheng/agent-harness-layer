"""流式子模块共享的显式协作者视图。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from agent_harness.events import EventBus
from agent_harness.models.providers import ModelResponse
from agent_harness.models.router import ModelRoutePlan, ModelRouter
from agent_harness.models.usage import ModelUsageEvidence, UsageEvidenceContext
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage, SQLAlchemyUnitOfWork
from agent_harness.storage.shared_budget import BudgetOperationOwnership


class MarkSideEffectStarted(Protocol):
    """provider 首次迭代前的耐久副作用标记。"""

    async def __call__(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        ownership: BudgetOperationOwnership | None,
    ) -> None: ...


class PersistFinalInUow(Protocol):
    """把最终 usage 与预算结算写入调用方持有的同一事务。"""

    async def __call__(
        self,
        *,
        uow: SQLAlchemyUnitOfWork,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        outcome: str,
        error_code: str | None,
        ownership: BudgetOperationOwnership | None,
        response: ModelResponse | None,
    ) -> None: ...


class FinalizeSettlement(Protocol):
    """可信中断的最终 usage 持久化、预算结算与发布入口。"""

    async def __call__(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        outcome: str,
        error_code: str | None,
        ownership: BudgetOperationOwnership | None,
        response: ModelResponse | None,
    ) -> None: ...


@dataclass(frozen=True)
class StreamingRuntime:
    """只把三个流式子模块真正需要的能力暴露为公开字段。"""

    storage: SQLAlchemyStorage
    router: ModelRouter
    event_bus: EventBus
    telemetry: TelemetryFacade | None
    output_guardrail: Callable[[str], bool] | None
    timing_observer: Callable[[str], None] | None
    mark_side_effect_started: MarkSideEffectStarted
    persist_final_in_uow: PersistFinalInUow
    finalize: FinalizeSettlement
    attempt_summary: Callable[..., dict[str, object]]
    safe_decision: Callable[..., dict[str, object]]
    route_evidence: Callable[[ModelRoutePlan], dict[str, object]]


__all__ = ["StreamingRuntime"]
