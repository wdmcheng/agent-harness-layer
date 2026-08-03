"""Model 调用的 durable usage 预约、结算与补投 seam。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from agent_harness.events import CanonicalEvent, EventBus
from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_approval_identity import (
    resolve_approved_invocation_identity,
)
from agent_harness.models._invocation_execution import (
    ModelApprovalRequired,
    ModelInvocationExecutionMixin,
)
from agent_harness.models._invocation_settlement import (
    ModelProviderInvocationError,
)
from agent_harness.models._invocation_streaming import ModelInvocationStreamingMixin
from agent_harness.models._invocation_structured import ModelInvocationStructuredMixin
from agent_harness.models._streaming_events import publish_persisted_stream
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.route_chain_identity import model_route_operation_identity_digest
from agent_harness.models.router import ModelRouter, ModelRouterConfig
from agent_harness.models.structured import (
    OutputSchemaDefinition,
    structured_operation_identity_digest,
)
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
            route_operation_identity_digest=model_route_operation_identity_digest(
                tenant_id=self._context.tenant_id,
                run_id=self._context.run_id,
                agent_id=self._context.agent_id,
                request_id=self._context.request_id,
                trace_id=self._context.trace_id,
                operation_key=operation_key,
            ),
            actor=self._identity,
        )

    async def complete_structured(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        repair_limit: int = 0,
    ) -> ModelResponse:
        """使用 bound Agent 的已注册 schema 执行非流式 structured 调用。"""

        usage_call_id = stable_usage_call_id(context=self._context, operation_key=operation_key)
        return await self._service.complete_structured(
            request,
            context=self._context,
            usage_call_id=usage_call_id,
            operation_identity_digest=structured_operation_identity_digest(
                tenant_id=self._context.tenant_id,
                run_id=self._context.run_id,
                agent_id=self._context.agent_id,
                request_id=self._context.request_id,
                trace_id=self._context.trace_id,
                operation_key=operation_key,
            ),
            repair_limit=repair_limit,
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

        del operation_key  # 兼容公开签名；审批恢复身份只能来自 durable continuation。
        usage_call_id, operation_identity_digest = await self._service.approved_invocation_identity(
            context=self._context,
            grant=grant,
        )
        return await self._service.complete_with_approval(
            request,
            context=self._context,
            usage_call_id=usage_call_id,
            route_operation_identity_digest=operation_identity_digest,
            actor=self._identity,
            grant=grant,
        )

    async def complete_structured_approved(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        repair_limit: int = 0,
        grant: _ApprovedModelGrant,
    ) -> ModelResponse:
        """从耐久continuation恢复structured身份，只绕过一次soft gate。"""

        del operation_key  # structured批准恢复严禁从调用方槽位重新派生身份。
        return await self._service.complete_structured_with_approval(
            request,
            context=self._context,
            repair_limit=repair_limit,
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
            route_operation_identity_digest=model_route_operation_identity_digest(
                tenant_id=self._context.tenant_id,
                run_id=self._context.run_id,
                agent_id=self._context.agent_id,
                request_id=self._context.request_id,
                trace_id=self._context.trace_id,
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

        del operation_key  # streaming 与 completion 共用同一可信恢复边界。
        usage_call_id, operation_identity_digest = await self._service.approved_invocation_identity(
            context=self._context,
            grant=grant,
        )
        return await self._service.stream_with_approval(
            request,
            context=self._context,
            usage_call_id=usage_call_id,
            route_operation_identity_digest=operation_identity_digest,
            actor=self._identity,
            grant=grant,
        )


class ModelInvocationService(
    ModelInvocationStructuredMixin,
    ModelInvocationStreamingMixin,
    ModelInvocationExecutionMixin,
):
    """在 provider 副作用前建立 settlement，并只补投 evidence。"""

    async def approved_invocation_identity(
        self,
        *,
        context: UsageEvidenceContext,
        grant: _ApprovedModelGrant,
    ) -> tuple[str, str]:
        """从 durable approval artifact 恢复 route-chain 或 legacy 调用身份。"""

        return await resolve_approved_invocation_identity(
            storage=self._storage,
            context=context,
            grant=grant,
        )

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
        output_schema_resolver: Callable[[str], OutputSchemaDefinition] | None = None,
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
        self._output_schema_resolver = output_schema_resolver
        # 不响应取消的provider cleanup不能阻塞公开调用；组合根显式持有这些
        # 已围栏task，完成后由callback回收，避免静默orphan和未观察异常。
        self._structured_cleanup_tasks: set[asyncio.Future[None]] = set()

    async def aclose(self) -> None:
        """由组合根关闭 provider-neutral 路由链，不暴露 vendor client。"""

        for task in tuple(self._structured_cleanup_tasks):
            task.cancel()
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
        route_operation_identity_digest: str | None = None,
        actor: IdentityContext | None = None,
    ) -> ModelResponse:
        """执行普通策略路径；公开调用面不接受布尔型审批旁路。"""

        return await self._complete(
            request,
            context=context,
            usage_call_id=usage_call_id,
            route_operation_identity_digest=route_operation_identity_digest,
            soft_approved=False,
            actor=actor,
            approved_grant=None,
        )

    async def stream(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        route_operation_identity_digest: str | None = None,
        actor: IdentityContext | None = None,
    ) -> ModelResponse:
        """执行受控普通文本流；增量只写 CanonicalEvent，不返回第二个 iterator。"""

        return await self._stream(
            request,
            context=context,
            usage_call_id=usage_call_id,
            route_operation_identity_digest=route_operation_identity_digest,
            soft_approved=False,
            actor=actor,
            approved_grant=None,
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
        route_operation_identity_digest: str | None = None,
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
            route_operation_identity_digest=route_operation_identity_digest,
            soft_approved=True,
            actor=actor,
            approved_grant=grant,
        )

    async def stream_with_approval(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        route_operation_identity_digest: str | None = None,
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
            route_operation_identity_digest=route_operation_identity_digest,
            soft_approved=True,
            actor=actor,
            approved_grant=grant,
        )

    async def recover_pending(self, *, run_id: str) -> int:
        """补投确定结果，并把带冻结 seed 的 structured started 提升为 needs-review。"""

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
        for state, operation_kind, result, usage_call_id, _error_code in pending:
            if (
                state == "started"
                and operation_kind == EvidenceOperationKind.MODEL_USAGE.value
                and isinstance(result, dict)
                and await self._recover_structured_started(
                    usage_call_id=str(usage_call_id),
                    durable_started=result,
                )
            ):
                recovered += 1
        for _sequence, result in stream_pending:
            raw_intent = result.get("event") if isinstance(result, dict) else None
            if not isinstance(raw_intent, dict):
                raise RuntimeError("stream recovery is missing its durable event intent")
            intent = CanonicalEvent.model_validate(raw_intent)
            await publish_persisted_stream(self._streaming_runtime(), intent)
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
