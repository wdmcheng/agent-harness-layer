"""Model 调用的 durable usage 预约、结算与补投 seam。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from agent_harness.events import CanonicalEvent, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_execution import (
    ModelApprovalRequired,
    ModelInvocationExecutionMixin,
)
from agent_harness.models._invocation_settlement import (
    ModelProviderInvocationError,
)
from agent_harness.models._invocation_streaming import ModelInvocationStreamingMixin
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.router import ModelRouter, ModelRouterConfig
from agent_harness.models.usage import (
    UsageEvidenceContext,
    stable_usage_call_id,
)
from agent_harness.observability.facade import TelemetryFacade
from agent_harness.policy import PolicyEngine
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage
from agent_harness.storage.evidence_repositories import (
    EvidenceOperationKind,
)
from agent_harness.storage.shared_budget import (
    OperationIdentity,
)

if TYPE_CHECKING:
    from agent_harness.registry.descriptor import AgentModelPolicy


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
    lease_id: str
    tenant_id: str
    identity_id: str
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
        identity: IdentityContext,
    ) -> None:
        """绑定可信服务与单一运行上下文，后续调用不再接收可伪造身份字段。"""

        self._service = service
        self._context = context
        self._identity = identity

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
            actor=self._identity,
        )

    async def complete_approved(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        grant: _ApprovedModelGrant,
    ) -> ModelResponse:
        """审批 continuation 只绕过 soft gate，硬上限与当前余额必须重新检查。"""

        return await self._service.complete_with_approval(
            request,
            context=self._context,
            usage_call_id=stable_usage_call_id(
                context=self._context,
                # approval_id 是该 lease 唯一的模型操作槽位；不能让业务 executor
                # 通过更换 operation_key 把一次批准扩成多次 provider 调用。
                operation_key=f"approved:{grant.approval_id}",
            ),
            actor=self._identity,
            grant=grant,
        )

    async def stream(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
    ) -> ModelResponse:
        """绑定可信运行身份执行普通文本流，业务侧不能传 call identity。"""

        return await self._service.stream(
            request,
            context=self._context,
            usage_call_id=stable_usage_call_id(
                context=self._context,
                operation_key=operation_key,
            ),
            actor=self._identity,
        )

    async def stream_approved(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        grant: _ApprovedModelGrant,
    ) -> ModelResponse:
        """匹配 durable grant 的 continuation 才能绕过一次 stream soft gate。"""

        del operation_key
        return await self._service.stream_with_approval(
            request,
            context=self._context,
            usage_call_id=stable_usage_call_id(
                context=self._context,
                operation_key=f"approved:{grant.approval_id}",
            ),
            actor=self._identity,
            grant=grant,
        )


class ModelInvocationService(ModelInvocationStreamingMixin, ModelInvocationExecutionMixin):
    """在 provider 副作用前建立 settlement，并只补投 evidence。"""

    def __init__(
        self,
        *,
        router: ModelRouter,
        storage: SQLAlchemyStorage,
        event_bus: EventBus,
        telemetry: TelemetryFacade | None = None,
        shared_budget: _SharedBudgetIdentityRuntime | None = None,
        agent_policy_resolver: Callable[[str], AgentModelPolicy] | None = None,
        policy_engine: PolicyEngine | None = None,
        stream_output_guardrail: Callable[[str], bool] | None = None,
        stream_timing_observer: Callable[[str], None] | None = None,
    ) -> None:
        """保存路由、持久化、事件和可选共享预算协作者。"""

        self._router = router
        self._storage = storage
        self._event_bus = event_bus
        self._telemetry = telemetry
        self._shared_budget = shared_budget
        self._agent_policy_resolver = agent_policy_resolver
        self._policy_engine = policy_engine
        # 该可信 composition seam 一旦存在，就声明必须观察完整结果；业务请求
        # 不能自行关闭它或要求 speculative delta。
        self._stream_output_guardrail = stream_output_guardrail
        # 只暴露阶段名，供受控 live smoke 采集 monotonic 时延；不得传递文本、
        # provider DTO 或异常对象。
        self._stream_timing_observer = stream_timing_observer

    async def aclose(self) -> None:
        """由组合根关闭 provider-neutral 路由链，不暴露 vendor client。"""

        await self._router.aclose()

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

        return BoundModelInvocationService(
            service=self,
            identity=identity,
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
        actor: IdentityContext | None = None,
    ) -> ModelResponse:
        """执行普通策略路径；公开调用面不接受布尔型审批旁路。"""

        return await self._complete(
            request,
            context=context,
            usage_call_id=usage_call_id,
            soft_approved=False,
            actor=actor,
        )

    async def stream(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        actor: IdentityContext | None = None,
    ) -> ModelResponse:
        """执行受控普通文本流；增量只写 CanonicalEvent，不返回第二个 iterator。"""

        return await self._stream(
            request,
            context=context,
            usage_call_id=usage_call_id,
            soft_approved=False,
            actor=actor,
        )

    async def _validate_approved_grant(
        self,
        *,
        request: ModelRequest,
        context: UsageEvidenceContext,
        identity: IdentityContext,
        grant: _ApprovedModelGrant,
    ) -> None:
        """校验 bound 语义与 durable lease，拒绝业务 executor 伪造批准能力。"""

        expected_hash = hashlib.sha256(
            json.dumps(
                request.to_payload(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            grant.tenant_id != context.tenant_id
            or grant.identity_id != identity.user_id
            or grant.agent_id != context.agent_id
            or grant.run_id != context.run_id
            or grant.action != "model.invoke"
            or grant.resource != f"agent:{context.agent_id}:model"
            or grant.arguments_hash != expected_hash
        ):
            raise ValueError("model approval grant does not match bound invocation")
        async with self._storage.uow() as uow:
            lease = await uow.approvals.get_resolution(grant.approval_id)
        if (
            lease is None
            or lease.lease_id != grant.lease_id
            or lease.state not in {"claimed", "execution_owned", "recovery_pending"}
            or lease.approval.status != "waiting"
        ):
            raise ValueError("model approval grant does not match an active approval lease")
        approval = lease.approval
        persisted = (
            approval.tenant_id,
            str(approval.metadata.get("identity_id") or approval.requested_by),
            approval.agent_id,
            approval.run_id,
            approval.action,
            approval.resource,
            str(approval.metadata.get("arguments_hash") or ""),
        )
        actual = (
            grant.tenant_id,
            grant.identity_id,
            grant.agent_id,
            grant.run_id,
            grant.action,
            grant.resource,
            grant.arguments_hash,
        )
        if actual != persisted:
            raise ValueError("model approval grant does not match persisted approval")

    async def complete_with_approval(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        actor: IdentityContext,
        grant: _ApprovedModelGrant,
    ) -> ModelResponse:
        """只接受全绑定 durable grant，并在校验后绕过一次 soft policy gate。"""

        await self._validate_approved_grant(
            request=request,
            context=context,
            identity=actor,
            grant=grant,
        )
        return await self._complete(
            request,
            context=context,
            usage_call_id=usage_call_id,
            soft_approved=True,
            actor=actor,
        )

    async def stream_with_approval(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        actor: IdentityContext,
        grant: _ApprovedModelGrant,
    ) -> ModelResponse:
        """校验全绑定 grant 后只绕过 stream soft policy gate，硬上限仍重算。"""

        await self._validate_approved_grant(
            request=request,
            context=context,
            identity=actor,
            grant=grant,
        )
        return await self._stream(
            request,
            context=context,
            usage_call_id=usage_call_id,
            soft_approved=True,
            actor=actor,
        )

    async def recover_pending(self, *, run_id: str) -> int:
        """只补投已有确定性结果；started/未知结果继续阻止 terminal。"""

        async with self._storage.uow() as uow:
            pending_rows = await uow.evidence_outbox.pending(run_id=run_id)
            stream_pending = sorted(
                [
                    (
                        int(item.sequence_in_group or 0),
                        item.result_json,
                    )
                    for item in pending_rows
                    if item.state == "result_persisted"
                    and item.operation_kind == EvidenceOperationKind.MODEL_STREAM.value
                ],
                key=lambda item: item[0],
            )
            pending = [
                (
                    item.state,
                    item.operation_kind,
                    item.result_json,
                    item.usage_call_id,
                    item.error_code,
                )
                for item in pending_rows
            ]
        recovered = 0
        for _sequence, result in stream_pending:
            raw_intent = result.get("event") if isinstance(result, dict) else None
            if not isinstance(raw_intent, dict):
                raise RuntimeError("stream recovery is missing its durable event intent")
            intent = CanonicalEvent.model_validate(raw_intent)
            await self._publish_persisted_stream(intent)
            recovered += 1
        for state, operation_kind, result, usage_call_id, error_code in pending:
            if (
                state != "result_persisted"
                or operation_kind != EvidenceOperationKind.MODEL_USAGE.value
                or result is None
            ):
                continue
            validated = self._validated_settlement_result(
                result,
                state=state,
                error_code=error_code,
            )
            await self._publish_final(
                evidence=validated.evidence,
                usage_call_id=str(usage_call_id),
                outcome=validated.outcome,
                error_code=error_code,
            )
            recovered += 1
        return recovered


__all__ = [
    "BoundModelInvocationService",
    "ModelInvocationService",
    "ModelApprovalRequired",
    "ModelProviderInvocationError",
]
