"""可信 structured invocation 的 planning、repair、结算与 replay 协调。"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, cast

from agent_harness.events import EventBus
from agent_harness.models._router_contracts import ModelRouteChainPlan
from agent_harness.models._router_identity import route_plan_identity_payload
from agent_harness.models._settlement_contracts import IdentityRuntime, SettlementStart
from agent_harness.models._structured_settlement_evidence_models import (
    StructuredSettlementRouteEvidence,
    StructuredSettlementSummary,
)
from agent_harness.models.providers import (
    ModelAttemptEvidence,
    ModelRequest,
    ModelResponse,
    PreparedStructuredModelCall,
    StructuredModelAttemptEvidence,
)
from agent_harness.models.router import ModelRouteError, ModelRoutePlan, ModelRouter
from agent_harness.models.structured import (
    OutputSchemaDefinition,
    StructuredOutputAttemptEvidence,
    StructuredOutputNotStartedProof,
    StructuredOutputReplayIdentity,
    StructuredOutputRequest,
    structured_digest,
)
from agent_harness.models.usage import CostStatus, ModelUsageEvidence, UsageEvidenceContext
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.policy import PolicyEngine
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage

if TYPE_CHECKING:
    from agent_harness.registry.descriptor import AgentModelPolicy
    from agent_harness.storage.evidence_repositories import UsageSettlementClaim
    from agent_harness.storage.shared_budget import BudgetOperationOwnership


class StructuredPromptBuilder(Protocol):
    """核心协调器允许调用的 provider prompt 构造窄接口。"""

    def __call__(
        self,
        *,
        business_prompt: str,
        schema: OutputSchemaDefinition,
        repair_ordinal: int,
        validation_codes: Sequence[str] = (),
    ) -> str: ...


class ModelInvocationStructuredSupportMixin:
    """只负责非流式单route结构化控制器。"""

    _storage: SQLAlchemyStorage
    _router: ModelRouter
    _event_bus: EventBus
    _telemetry: TelemetryFacade | None
    _shared_budget: IdentityRuntime | None
    _agent_policy_resolver: Callable[[str], AgentModelPolicy] | None
    _output_schema_resolver: Callable[[str], OutputSchemaDefinition] | None
    _policy_engine: PolicyEngine | None
    _structured_cleanup_tasks: set[asyncio.Future[None]]

    if TYPE_CHECKING:

        async def _plan(
            self,
            *,
            request: ModelRequest,
            context: UsageEvidenceContext,
            approved: bool,
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
            structured_replay: StructuredOutputReplayIdentity | None = None,
        ) -> None: ...

    @staticmethod
    def _structured_route_digest(plan: ModelRoutePlan) -> str:
        """复用完整脱敏route plan预映像计算structured replay identity。"""

        return structured_digest(route_plan_identity_payload(plan))

    @staticmethod
    def _structured_plan(
        plan: ModelRoutePlan,
        *,
        prompt_bytes: int,
        provider_request_limit: int,
    ) -> ModelRoutePlan:
        """把单次冻结上界提升为 transport×repair 联合 reservation。"""

        if provider_request_limit < 1:
            raise ModelRouteError("budget.reservation_rejected", "invalid structured limit")
        price_values = (plan.input_token_price_usd, plan.output_token_price_usd)
        price_source = (plan.price_source_ref, plan.price_source_version)
        price_values_enabled = any(value is not None for value in price_values)
        price_source_enabled = any(value not in {None, ""} for value in price_source)
        if (
            (price_values_enabled and any(value is None for value in price_values))
            or (price_source_enabled and any(value in {None, ""} for value in price_source))
            or (price_values_enabled and not price_source_enabled)
        ):
            raise ModelRouteError(
                "budget.reservation_rejected",
                "structured price identity is incomplete",
            )
        if plan.provider_kind == "fake":
            trusted_input = prompt_bytes
            per_attempt_tokens = trusted_input + plan.output_token_cap
            per_attempt_cost = plan.per_attempt_cost_bound
        else:
            trusted_input = prompt_bytes + plan.input_envelope_token_bound
            per_attempt_tokens = trusted_input + plan.output_token_cap
            if plan.input_token_price_usd is None:
                per_attempt_cost = None
            else:
                assert plan.output_token_price_usd is not None
                per_attempt_cost = (
                    Decimal(trusted_input) * plan.input_token_price_usd
                    + Decimal(plan.output_token_cap) * plan.output_token_price_usd
                )
        return plan.model_copy(
            update={
                "capability": "structured_output",
                "prompt_utf8_bytes": prompt_bytes,
                "trusted_input_token_bound": trusted_input,
                "per_attempt_token_bound": per_attempt_tokens,
                "per_attempt_cost_bound": per_attempt_cost,
                "reserved_token_bound": per_attempt_tokens * provider_request_limit,
                "reserved_cost_bound": (
                    None if per_attempt_cost is None else per_attempt_cost * provider_request_limit
                ),
                "trusted_token_bound": per_attempt_tokens * provider_request_limit,
                "trusted_cost_bound": (
                    None if per_attempt_cost is None else per_attempt_cost * provider_request_limit
                ),
                "repair_limit": provider_request_limit // plan.max_attempts - 1,
                "provider_request_limit": provider_request_limit,
            }
        )

    @staticmethod
    def _structured_attempt(
        local: ModelAttemptEvidence,
        *,
        attempt: int,
        schema: OutputSchemaDefinition,
        prompt_digest: str,
        repair_ordinal: int,
        transport_ordinal: int,
        trigger_codes: tuple[str, ...],
        validation_codes: tuple[str, ...] | None,
        cleanup_status: str,
        not_started_proof: StructuredOutputNotStartedProof | None = None,
    ) -> StructuredModelAttemptEvidence:
        """把 adapter-local attempt=1 映射到连续全局 controller ordinal。"""

        payload = local.model_dump(mode="python")
        payload.update(
            {
                "attempt": attempt,
                "structured_output": StructuredOutputAttemptEvidence(
                    schema_identity=schema.identity,
                    phase="initial" if repair_ordinal == 0 else "repair",
                    repair_ordinal=repair_ordinal,
                    transport_ordinal=transport_ordinal,
                    prompt_digest=prompt_digest,
                    repair_trigger_codes=trigger_codes,
                    validation_codes=validation_codes,
                    not_started_proof=not_started_proof,
                    cleanup_status=cast(Any, cleanup_status),
                ),
            }
        )
        return StructuredModelAttemptEvidence.model_validate(payload)

    @staticmethod
    def _structured_attempt_summary(
        *,
        attempts: list[StructuredModelAttemptEvidence],
        plan: ModelRoutePlan,
        provider_called: bool,
    ) -> dict[str, object]:
        """聚合所有 structured attempts，并保留 subtype 的耐久字段。"""

        cost_enabled = plan.input_token_price_usd is not None
        unresolved: list[int] = []
        normalized: list[dict[str, object]] = []
        total_input = 0
        total_output = 0
        total_cost = 0.0
        cost_status = "reported"
        for index, item in enumerate(attempts):
            if item.side_effect_state == "not_started":
                charge_tokens: int | None = 0
                charge_cost: float | None = 0.0 if cost_enabled else None
            elif item.side_effect_state == "started":
                known_tokens = item.input_tokens is not None and item.output_tokens is not None
                charge_tokens = (
                    cast(int, item.input_tokens) + cast(int, item.output_tokens)
                    if known_tokens
                    else None
                )
                charge_cost = item.cost_usd if cost_enabled else None
                if known_tokens:
                    total_input += cast(int, item.input_tokens)
                    total_output += cast(int, item.output_tokens)
                if cost_enabled and item.cost_usd is not None:
                    total_cost += item.cost_usd
                    if item.cost_status == "estimated":
                        cost_status = "estimated"
                if not known_tokens or cost_enabled and item.cost_usd is None:
                    unresolved.append(item.attempt)
            else:
                charge_tokens = None
                charge_cost = None
                unresolved.append(item.attempt)
            settled_item = item.model_copy(
                update={
                    "budget_charge_tokens": charge_tokens,
                    "budget_charge_cost_usd": charge_cost,
                }
            )
            attempts[index] = settled_item
            normalized_item = settled_item.model_dump(mode="json")
            normalized.append(cast(dict[str, object], normalized_item))
        charged_tokens = sum(
            cast(int, item["budget_charge_tokens"])
            for item in normalized
            if item["budget_charge_tokens"] is not None
        )
        charged_cost = sum(
            cast(float, item["budget_charge_cost_usd"])
            for item in normalized
            if item["budget_charge_cost_usd"] is not None
        )
        if charged_tokens > plan.reserved_token_bound or (
            cost_enabled
            and plan.reserved_cost_bound is not None
            and Decimal(str(charged_cost)) > plan.reserved_cost_bound
        ):
            unresolved = [item.attempt for item in attempts]
        actual = not unresolved
        return {
            "attempts": normalized,
            "budget_charge": {
                "charged_tokens": charged_tokens if actual else None,
                "charged_cost_usd": charged_cost if actual and cost_enabled else None,
                "charge_status": "actual" if actual else "unknown",
                "unresolved_attempts": sorted(set(unresolved)),
            },
            "input_tokens": total_input if actual and provider_called else None,
            "output_tokens": total_output if actual and provider_called else None,
            "cost_usd": total_cost if actual and provider_called and cost_enabled else None,
            "cost_status": (
                cost_status if actual and provider_called and cost_enabled else "unavailable"
            ),
        }

    @staticmethod
    def _structured_backoff_seconds(plan: ModelRoutePlan, *, transport_ordinal: int) -> float:
        """按冻结 retry policy 计算本次 prepare retry 的确定性等待时间。"""

        policy = plan.retry_policy
        if policy.backoff_initial_ms == 0 or policy.max_wait_ms == 0:
            return 0.0
        delay_ms = policy.backoff_initial_ms * (2 ** max(transport_ordinal - 1, 0))
        if policy.backoff_max_ms > 0:
            delay_ms = min(delay_ms, policy.backoff_max_ms)
        return min(delay_ms, policy.max_wait_ms) / 1000

    async def _close_structured_prepared(
        self,
        prepared: PreparedStructuredModelCall,
        *,
        deadline: float,
    ) -> str:
        """在调用总deadline内保护close，并把无法确认的清理结果显式围栏。"""

        try:
            close = prepared.aclose
            if not callable(close):
                return "failed"
            close_result = close()
        except asyncio.CancelledError:
            # 属性读取或同步调用阶段没有可等待的cleanup task；协议违规的
            # cancellation也不能被误报成已清理。
            return "unknown"
        except Exception:
            return "failed"
        if not inspect.isawaitable(close_result):
            return "failed"
        try:
            close_task = asyncio.ensure_future(cast(Awaitable[None], close_result))
        except asyncio.CancelledError:
            return "unknown"
        except Exception:
            return "failed"
        self._structured_cleanup_tasks.add(close_task)

        def cleanup_finished(task: asyncio.Future[None]) -> None:
            """释放组合根所有权，并显式观察延迟结束的cleanup异常。"""

            self._structured_cleanup_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        close_task.add_done_callback(cleanup_finished)

        async def wait_before_deadline() -> None:
            """只屏蔽close task本身，当前调用仍受同一绝对deadline约束。"""

            async with asyncio.timeout_at(deadline):
                await asyncio.shield(close_task)

        def stop_unfinished_close() -> None:
            """发出取消后立即返回；不响应取消的task继续由组合根显式持有。"""

            if not close_task.done():
                close_task.cancel()

        try:
            await wait_before_deadline()
        except asyncio.CancelledError:
            if close_task.cancelled():
                return "unknown"
            try:
                await wait_before_deadline()
            except TimeoutError:
                stop_unfinished_close()
                return "unknown"
            except asyncio.CancelledError:
                stop_unfinished_close()
                return "unknown"
            except Exception:
                return "failed"
            # 调用方已取消，即使本地 close 最终完成，也不能再发布 valid；该路径
            # 必须保留 send 后事实并进入 needs-review 围栏。
            return "unknown"
        except TimeoutError:
            stop_unfinished_close()
            return "unknown"
        except Exception:
            return "failed"
        return "completed"

    async def _recover_structured_started(
        self,
        *,
        usage_call_id: str,
        durable_started: dict[str, Any],
    ) -> bool:
        """把无 final 的 structured started 围栏提升为耐久 needs-review。

        恢复只消费首次 claim 事务冻结的 seed 与 started evidence；它不解析当前
        Registry、重建 prompt、创建 provider handle 或猜测实际请求计数。
        """

        raw_started = durable_started.get("started")
        raw_seed = durable_started.get("structured_replay_seed")
        if not isinstance(raw_started, dict) or raw_seed is None:
            return False
        try:
            started = ModelUsageEvidence.model_validate(raw_started)
            replay = StructuredOutputReplayIdentity.model_validate(raw_seed)
            raw_summary = started.decision.get("structured_output")
            raw_route = started.decision.get("route")
            raw_route_identity = started.decision.get("structured_route_identity")
            if (
                not isinstance(raw_summary, dict)
                or not isinstance(raw_route, dict)
                or not isinstance(raw_route_identity, dict)
            ):
                return False
            started_summary = StructuredSettlementSummary.model_validate(raw_summary)
            route = StructuredSettlementRouteEvidence.model_validate(raw_route)
            identity_plan = ModelRoutePlan.model_validate(raw_route_identity)
        except (TypeError, ValueError):
            return False
        if (
            started_summary.status != "started"
            or replay.final_status != "needs_review"
            or replay.repair_count is not None
            or replay.provider_request_count is not None
            or replay.usage_call_id != usage_call_id
            or replay.tenant_id != started.tenant_id
            or replay.run_id != started.run_id
            or replay.agent_id != started.agent_id
            or replay.request_id != started.request_id
            or replay.trace_id != started.trace_id
            or replay.provider != started.provider
            or replay.model != started.model
            or replay.schema_identity != started_summary.schema_identity
            or replay.repair_limit != started_summary.repair_limit
            or replay.transport_attempt_limit != route.max_attempts
            or route_plan_identity_payload(identity_plan) != raw_route_identity
            or replay.route_digest != structured_digest(cast(dict[str, Any], raw_route_identity))
        ):
            return False
        final_summary = StructuredSettlementSummary(
            schema_version="structured-output-evidence-v1",
            schema_identity=started_summary.schema_identity,
            status="needs_review",
            repair_limit=started_summary.repair_limit,
            repair_count=None,
            provider_request_limit=started_summary.provider_request_limit,
            provider_request_count=None,
            replay_identity=replay.digest,
            validation_issues=[],
            error_code="model.provider_side_effect_unknown",
        )
        decision = dict(started.decision)
        decision.update(
            {
                # 这是保留 reservation 的保守 side-effect 可能性，不是伪造 exact
                # provider request count；后者在 structured summary 中保持 null。
                "provider_called": True,
                "attempts": [],
                "budget_charge": {
                    "charged_tokens": None,
                    "charged_cost_usd": None,
                    "charge_status": "unknown",
                    "unresolved_attempts": [1],
                },
                "structured_output": final_summary.model_dump(mode="json"),
            }
        )
        evidence = started.model_copy(
            update={
                "input_tokens": None,
                "output_tokens": None,
                "cost_usd": None,
                "cost_status": "unavailable",
                "latency_ms": 0,
                "decision": decision,
            }
        )
        ownership = None
        if self._shared_budget is not None:
            async with self._storage.uow() as uow:
                ownership = await uow.shared_budget.resolve_operation_ownership(
                    tenant_id=started.tenant_id,
                    run_id=started.run_id,
                )
        await self._finalize(
            evidence=evidence,
            usage_call_id=usage_call_id,
            outcome="failed",
            error_code="model.provider_side_effect_unknown",
            ownership=ownership,
            response=None,
            structured_replay=replay,
        )
        return True
