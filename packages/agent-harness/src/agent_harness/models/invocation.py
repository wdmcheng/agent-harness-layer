"""Model 调用的 durable usage 预约、结算与补投 seam。"""

from __future__ import annotations

import hashlib
import json
from time import perf_counter
from typing import Any, Protocol, cast

from agent_harness.events import EventBus
from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_settlement import (
    ModelProviderInvocationError,
    _ModelSettlementMixin,
)
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.router import ModelRoutePlan, ModelRouter, ModelRouterConfig
from agent_harness.models.usage import (
    ModelUsageEvidence,
    UsageEvidenceContext,
    stable_usage_call_id,
)
from agent_harness.models.usage_events import UsageEvidenceLifecycle
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.event_capacity_repositories import EventCapacityExceeded
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
)
from agent_harness.storage.shared_budget import (
    BudgetReservationRejected,
    OperationIdentity,
)


class _SharedBudgetIdentityRuntime(Protocol):
    """向模型调用提供预算归属和快照派生配置的受限协作接口。"""

    def operation_identity(self, **values: Any) -> OperationIdentity:
        """从稳定业务字段派生可重放的共享预算操作身份。"""

        ...

    def model_router_config(
        self,
        *,
        snapshot: dict[str, Any],
        agent_id: str,
        base: ModelRouterConfig,
    ) -> ModelRouterConfig:
        """根据已冻结的树快照为当前 agent 派生模型路由配置。"""

        ...


class _ApprovedModelGrant(Protocol):
    """审批续跑时必须携带的不可变授权声明。

    模型请求在使用授权前会重新比对这些字段，防止一个审批被换 tenant、运行、资源或
    参数后复用。
    """

    approval_id: str
    tenant_id: str
    agent_id: str
    run_id: str
    action: str
    resource: str
    arguments_hash: str


class BoundModelInvocationService:
    """只向单个 run 的业务 executor 暴露请求与稳定操作槽位。"""

    def __init__(
        self,
        *,
        service: ModelInvocationService,
        context: UsageEvidenceContext,
    ) -> None:
        """绑定可信服务与单一运行上下文，后续调用不再接收可伪造身份字段。"""

        self._service = service
        self._context = context

    async def complete(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
    ) -> ModelResponse:
        """由可信 runtime 关联生成 call ID，业务输入不能覆盖身份。"""

        return await self._service.complete(
            request,
            context=self._context,
            usage_call_id=stable_usage_call_id(
                context=self._context,
                operation_key=operation_key,
            ),
        )

    async def complete_approved(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        grant: _ApprovedModelGrant,
    ) -> ModelResponse:
        """审批 continuation 只绕过 soft gate，硬上限与当前余额必须重新检查。"""

        expected_hash = hashlib.sha256(
            json.dumps(
                request.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            grant.tenant_id != self._context.tenant_id
            or grant.agent_id != self._context.agent_id
            or grant.run_id != self._context.run_id
            or grant.action != "model.invoke"
            or grant.resource != f"agent:{self._context.agent_id}:model"
            or grant.arguments_hash != expected_hash
        ):
            raise ValueError("model approval grant does not match bound invocation")
        return await self._service.complete(
            request,
            context=self._context,
            usage_call_id=stable_usage_call_id(
                context=self._context,
                operation_key=f"{operation_key}:approved:{grant.approval_id}",
            ),
            soft_approved=True,
        )


class ModelInvocationService(_ModelSettlementMixin):
    """在 provider 副作用前建立 settlement，并只补投 evidence。"""

    def __init__(
        self,
        *,
        router: ModelRouter,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        telemetry: TelemetryFacade | None = None,
        shared_budget: _SharedBudgetIdentityRuntime | None = None,
    ) -> None:
        """保存路由、持久化、事件和可选共享预算协作者。"""

        self._router = router
        self._storage = storage
        self._event_bus = event_bus
        self._telemetry = telemetry
        self._shared_budget = shared_budget

    def bind_execution(
        self,
        *,
        identity: IdentityContext,
        tenant_id: str,
        run_id: str,
        agent_id: str,
        request_id: str | None,
        trace_id: str,
    ) -> BoundModelInvocationService:
        """把原始 invocation seam 封闭为单个 runtime execution 的 facade。"""

        del identity
        return BoundModelInvocationService(
            service=self,
            context=UsageEvidenceContext(
                tenant_id=tenant_id,
                run_id=run_id,
                agent_id=agent_id,
                request_id=request_id,
                trace_id=trace_id,
            ),
        )

    async def complete(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        soft_approved: bool = False,
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
            plan = await self._plan(
                request=request,
                context=context,
                approved=soft_approved,
            )
            started_evidence = self._started_evidence(
                context=context,
                provider=plan.provider,
                model=plan.model,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
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

        if plan.decision.action != "policy_required":
            await self._mark_side_effect_started(
                context=context,
                usage_call_id=call_id,
                ownership=settlement.ownership,
            )

        invoked_at = perf_counter()
        try:
            provider_response = self._router.execute(request, plan=plan)
            # adapter 可能错误地用 model_construct 绕过 DTO；副作用后必须重新校验。
            response = ModelResponse.model_validate(provider_response.model_dump(mode="python"))
            if response.provider != plan.provider or response.model != plan.model:
                raise ValueError("model response identity does not match routing plan")
            provider_called = plan.decision.action != "policy_required"
            evidence = ModelUsageEvidence(
                usage_kind="model",
                tenant_id=context.tenant_id,
                provider=response.provider,
                model=response.model,
                input_tokens=response.token_usage.get("input_tokens"),
                output_tokens=response.token_usage.get("output_tokens"),
                cost_usd=response.cost_usd,
                cost_status=response.cost_status,
                latency_ms=response.latency_ms,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
                    response.decision.to_payload(),
                    {"provider_called": provider_called},
                ),
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
            )
        except Exception:
            evidence = self._started_evidence(
                context=context,
                provider=plan.provider,
                model=plan.model,
                decision=self._safe_decision(
                    plan.decision.to_payload(),
                    {"provider_called": True},
                ),
                latency_ms=int((perf_counter() - invoked_at) * 1000),
            )
            await self._finalize(
                evidence=evidence,
                usage_call_id=call_id,
                outcome="failed",
                error_code="model.provider_failed",
                ownership=settlement.ownership,
                response=None,
            )
            raise ModelProviderInvocationError("model provider invocation failed") from None

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

    async def _plan(
        self,
        *,
        request: ModelRequest,
        context: UsageEvidenceContext,
        approved: bool,
    ) -> ModelRoutePlan:
        """依据共享预算树快照或默认路由生成本次调用计划。

        快照、账本和归属在同一个 UoW 中读取，确保路由限制来自同一持久化视图；任何
        缺失或无效快照都转换成稳定预算拒绝，而不是悄悄回退到当前进程配置。
        """

        if self._shared_budget is None:
            return self._router.plan(request, approved=approved)
        # 三项读取必须保持一致，否则并发迁移预算树时可能组合出不存在的路由视图。
        async with self._storage.uow() as uow:
            ownership = await uow.shared_budget.resolve_operation_ownership(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
            )
            ledger = await uow.shared_budget.get_ledger(
                context.tenant_id,
                ownership.budget_owner_run_id,
            )
            snapshot = await uow.shared_budget.get_tree_snapshot(
                context.tenant_id,
                ownership.budget_owner_run_id,
            )
        if ledger is None:
            # `_start_settlement` 负责把 snapshot_invalid 保存为稳定拒绝 evidence。
            try:
                return self._router.plan(request, approved=approved)
            except KeyError as exc:
                raise BudgetReservationRejected(reason="snapshot_invalid") from exc
        if snapshot is None:
            try:
                return self._router.plan(request, approved=approved)
            except KeyError as exc:
                raise BudgetReservationRejected(reason="snapshot_invalid") from exc
        try:
            config = self._shared_budget.model_router_config(
                snapshot=snapshot,
                agent_id=context.agent_id,
                base=self._router.config,
            )
        except ValueError as exc:
            raise BudgetReservationRejected(reason="snapshot_invalid") from exc
        try:
            return self._router.plan(request, config=config, approved=approved)
        except KeyError as exc:
            raise BudgetReservationRejected(reason="snapshot_invalid") from exc

    async def recover_pending(self, *, run_id: str) -> int:
        """只补投已有确定性结果；started/未知结果继续阻止 terminal。"""

        async with self._storage.uow() as uow:
            pending = [
                (
                    item.state,
                    item.operation_kind,
                    item.result_json,
                    item.usage_call_id,
                    item.error_code,
                )
                for item in await uow.evidence_outbox.pending(run_id=run_id)
            ]
        recovered = 0
        for state, operation_kind, result, usage_call_id, error_code in pending:
            if (
                state != "result_persisted"
                or operation_kind != EvidenceOperationKind.MODEL_USAGE.value
                or result is None
            ):
                continue
            evidence = ModelUsageEvidence.model_validate(result["evidence"])
            await self._publish_final(
                evidence=evidence,
                usage_call_id=str(usage_call_id),
                outcome=str(result["outcome"]),
                error_code=error_code,
            )
            recovered += 1
        return recovered

    @staticmethod
    def _started_evidence(
        *,
        context: UsageEvidenceContext,
        provider: str,
        model: str,
        decision: dict[str, object],
        latency_ms: int = 0,
    ) -> ModelUsageEvidence:
        """构造 provider 尚未产生计量时的 started 或失败证据基础对象。"""

        return ModelUsageEvidence(
            usage_kind="model",
            tenant_id=context.tenant_id,
            provider=provider,
            model=model,
            input_tokens=None,
            output_tokens=None,
            cost_usd=None,
            cost_status="unavailable",
            latency_ms=latency_ms,
            decision=decision,
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
        )

    @staticmethod
    def _final_event_id(tenant_id: str, usage_call_id: str) -> str:
        """为每个租户调用槽位生成稳定终态事件标识，支持安全补投。"""

        return f"usage:{tenant_id}:{usage_call_id}:final"

    @staticmethod
    def _safe_decision(*parts: dict[str, object]) -> dict[str, Any]:
        """按后传入优先合并决策片段，并在进入持久化前整体脱敏。"""

        merged: dict[str, object] = {}
        for part in parts:
            merged.update(part)
        safe = redact_secrets(merged)
        if not isinstance(safe, dict):  # pragma: no cover - mapping input 保证输出形状
            raise RuntimeError("model decision redaction changed payload shape")
        return cast(dict[str, Any], safe)

    @staticmethod
    def _durable_response(response: ModelResponse) -> dict[str, Any]:
        """恢复所需 response 必须先整体脱敏，再进入内部 outbox/shared claim。"""

        safe = redact_secrets(response.to_payload())
        if not isinstance(safe, dict):  # pragma: no cover - DTO payload 保证 mapping
            raise RuntimeError("model response redaction changed payload shape")
        validated = ModelResponse.model_validate(safe)
        return validated.to_payload()


__all__ = [
    "BoundModelInvocationService",
    "ModelInvocationService",
    "ModelProviderInvocationError",
]
