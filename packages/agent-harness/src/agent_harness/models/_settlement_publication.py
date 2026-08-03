"""模型 settlement 的预算结算与 final event/telemetry 发布协调。"""

from __future__ import annotations

from decimal import Decimal
from typing import cast

from agent_harness.events import EventBus
from agent_harness.models._invocation_evidence import ModelInvocationEvidenceMixin
from agent_harness.models._settlement_contracts import ModelProviderInvocationError
from agent_harness.models.providers import ModelResponse
from agent_harness.models.structured import StructuredOutputReplayIdentity
from agent_harness.models.usage import ModelUsageEvidence
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage, SQLAlchemyUnitOfWork
from agent_harness.storage.shared_budget import BudgetOperationOwnership


class SettlementPublicationMixin(ModelInvocationEvidenceMixin):
    """只在完整验证后结算预算并发布 final；不负责解析不可信耐久 payload。"""

    _storage: SQLAlchemyStorage
    _event_bus: EventBus
    _telemetry: TelemetryFacade | None

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
    ) -> None:
        """原子持久化最终 usage 结果并结算共享预算，提交后才发布最终事件。

        先完成 outbox 与账本结算，保证 event 重试永远可从耐久事实恢复；最终事件属于
        可补投副作用，不能先于结果与预算事实对外可见。
        """

        async with self._storage.uow() as uow:
            await self._persist_final_in_uow(
                uow=uow,
                evidence=evidence,
                usage_call_id=usage_call_id,
                outcome=outcome,
                error_code=error_code,
                ownership=ownership,
                response=response,
                structured_replay=structured_replay,
            )
            await uow.commit()
        await self._publish_final(
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome=outcome,
            error_code=error_code,
        )

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
    ) -> None:
        """在调用方提供的事务中原子保存 usage 结果与共享预算，不提交或发布事件。

        普通 completion 由 ``_finalize`` 独占事务；stream 则把 completed intent、尾部
        槽位释放与本方法放进同一 UoW，避免 completed 已公开而 usage 尚不存在的窗口。
        """

        provider_called = evidence.decision.get("provider_called") is True
        raw_attempts = evidence.decision.get("attempts")
        attempt_count = (
            len(cast(list[object], raw_attempts))
            if isinstance(raw_attempts, list)
            else int(provider_called)
        )
        result = {
            "evidence": evidence.to_payload(),
            "outcome": outcome,
            "route": evidence.decision.get("route"),
            # 内部 outbox 投影复用公开 decision 的同一 5.29 结构，避免两套
            # attempts/budget-charge schema 随恢复路径独立漂移。
            "attempts": evidence.decision.get("attempts", []),
            "budget_charge": (
                evidence.decision["budget_charge"]
                if isinstance(evidence.decision.get("budget_charge"), dict)
                else {
                    "reserved_tokens": (
                        evidence.decision.get("route", {}).get("reserved_token_bound")
                        if isinstance(evidence.decision.get("route"), dict)
                        else None
                    ),
                    "reserved_cost_usd": (
                        evidence.decision.get("route", {}).get("reserved_cost_bound")
                        if isinstance(evidence.decision.get("route"), dict)
                        else None
                    ),
                    "actual_input_tokens": evidence.input_tokens,
                    "actual_output_tokens": evidence.output_tokens,
                    "actual_cost_usd": evidence.cost_usd,
                    "cost_status": evidence.cost_status,
                }
            ),
            **(
                {
                    "failure": {
                        "error_code": error_code,
                        "provider_called": provider_called,
                        "attempt_count": attempt_count,
                        "latency_ms": evidence.latency_ms,
                        **(
                            {"detail": evidence.decision.get("route_chain_exhausted")}
                            if error_code == "model.route_chain_exhausted"
                            else {}
                        ),
                    }
                }
                if error_code in ModelProviderInvocationError.stable_codes
                else {}
            ),
            **({"response": self._durable_response(response)} if response is not None else {}),
            **(
                {"structured_replay": structured_replay.model_dump(mode="json")}
                if structured_replay is not None
                else {}
            ),
        }
        persisted = await uow.evidence_outbox.persist_result(
            tenant_id=evidence.tenant_id,
            usage_call_id=usage_call_id,
            result=result,
            error_code=error_code,
        )
        if structured_replay is not None:
            from agent_harness.models._settlement_validation import (
                validate_durable_model_settlement,
            )

            if persisted.result_json is None:
                raise RuntimeError("structured settlement persistence lost its result")
            validate_durable_model_settlement(
                persisted.result_json,
                state="result_persisted",
                error_code=error_code,
            )
        if ownership is None:
            return
        input_tokens = evidence.input_tokens
        output_tokens = evidence.output_tokens
        actual_tokens: int | None
        actual_cost: Decimal | None
        if structured_replay is not None and structured_replay.final_status == "needs_review":
            # Structured needs-review不论provider request是否已知，都代表至少一个
            # 结算维度不可确认；以null actual让direct/allocation/owner保留预约并围栏。
            actual_tokens = None
            actual_cost = None
        elif provider_called and response is not None and response.attempts:
            started_attempts = [
                attempt
                for attempt in response.attempts
                if attempt.side_effect_state in {"started", "unknown"}
            ]
            attempts_have_usage = all(
                attempt.side_effect_state == "started"
                and attempt.input_tokens is not None
                and attempt.output_tokens is not None
                for attempt in started_attempts
            )
            actual_tokens = (
                sum(
                    (attempt.input_tokens or 0) + (attempt.output_tokens or 0)
                    for attempt in started_attempts
                )
                if attempts_have_usage
                else None
            )
            attempts_have_cost = all(
                attempt.side_effect_state == "started" and attempt.cost_usd is not None
                for attempt in started_attempts
            )
            actual_cost = (
                sum(
                    (Decimal(str(attempt.cost_usd)) for attempt in started_attempts),
                    Decimal("0"),
                )
                if attempts_have_cost
                else None
            )
        else:
            actual_tokens = (
                (input_tokens or 0) + (output_tokens or 0)
                if not provider_called or (input_tokens is not None and output_tokens is not None)
                else None
            )
            actual_cost = None if evidence.cost_usd is None else Decimal(str(evidence.cost_usd))
        # 未实际调用 provider 的拒绝/短路可确定为零用量；调用后缺少任一维度则
        # 保持未知，不能伪造总 token 以换取账本结算通过。
        if ownership.kind == "direct":
            await uow.shared_budget.settle_direct(
                tenant_id=evidence.tenant_id,
                budget_owner_run_id=ownership.budget_owner_run_id,
                usage_call_id=usage_call_id,
                actual_tokens=actual_tokens,
                actual_cost=actual_cost,
                cost_status=evidence.cost_status,
                result=result,
            )
        else:
            assert ownership.delegation_id is not None
            await uow.shared_budget.settle_allocation(
                tenant_id=evidence.tenant_id,
                budget_owner_run_id=ownership.budget_owner_run_id,
                delegation_id=ownership.delegation_id,
                usage_call_id=usage_call_id,
                actual_tokens=actual_tokens,
                actual_cost=actual_cost,
                cost_status=evidence.cost_status,
                result=result,
            )

    async def _publish_final(
        self,
        *,
        evidence: ModelUsageEvidence,
        usage_call_id: str,
        outcome: str,
        error_code: str | None,
    ) -> None:
        """幂等发布 usage 最终事件，并在本地容量模式下同步已发布的耐久进度。"""

        lifecycle = UsageEvidenceLifecycle(
            event_bus=self._event_bus,
            evidence=evidence,
            usage_call_id=usage_call_id,
        )
        final = await lifecycle.publish_final(outcome=outcome, error_code=error_code)
        if self._telemetry is not None:
            await self._telemetry.publish_event(final)
        async with self._storage.uow() as uow:
            item = await uow.evidence_outbox.get_usage(
                tenant_id=evidence.tenant_id,
                usage_call_id=usage_call_id,
            )
            if not self._event_bus.capacity_managed:
                # 远程容量适配器自行提交进度；本地模式必须在 outbox 状态转换前补记它。
                await uow.event_capacity.record_local_published(
                    run_id=evidence.run_id,
                    reserved_event_count=item.reserved_event_count,
                    highest_persisted_seq=final.seq,
                )
            await uow.evidence_outbox.mark_published(
                tenant_id=evidence.tenant_id,
                usage_call_id=usage_call_id,
            )
            await uow.commit()
