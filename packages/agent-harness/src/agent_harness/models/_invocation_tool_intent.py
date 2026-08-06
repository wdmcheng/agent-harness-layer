"""Bound与raw模型调用服务共用的tool-intent公开编排。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, cast

from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_tool_intent_approval import (
    resolve_tool_intent_approved_invocation_identity,
)
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.route_chain_identity import model_route_operation_identity_digest
from agent_harness.models.tool_catalog import ToolCatalog, ToolCatalogSelection
from agent_harness.models.tool_intent import (
    ModelTurnResult,
    ToolIntentReplaySeed,
    tool_loop_identity_digest,
)
from agent_harness.models.usage import (
    ModelUsageEvidence,
    UsageEvidenceContext,
    UsageInvocationReplayError,
    stable_usage_call_id,
)
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage


class _ApprovedToolGrant(Protocol):
    """Tool审批恢复所需的最小grant结构。"""

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


class _SettlementResult(Protocol):
    """用量读取只依赖结算结果中的脱离ORM evidence。"""

    evidence: ModelUsageEvidence


class _ToolIntentRuntime(Protocol):
    """Tool编排从主调用服务使用的最小内部能力。"""

    _storage: SQLAlchemyStorage
    _tool_catalog_resolver: Callable[[str, ToolCatalogSelection | None], ToolCatalog] | None

    async def _complete(
        self,
        request: ModelRequest,
        **kwargs: Any,
    ) -> ModelResponse | ModelTurnResult: ...

    def _validated_settlement_result(
        self,
        payload: dict[str, Any],
        *,
        state: str,
        error_code: str | None,
    ) -> _SettlementResult: ...


class _BoundToolIntentRuntime(Protocol):
    """Bound facade可调用的raw tool入口。"""

    async def complete(self, request: ModelRequest, **kwargs: Any) -> ModelResponse: ...

    async def complete_structured(self, request: ModelRequest, **kwargs: Any) -> ModelResponse: ...

    async def approved_invocation_identity(self, **kwargs: Any) -> tuple[str, str]: ...

    async def complete_with_approval(
        self, request: ModelRequest, **kwargs: Any
    ) -> ModelResponse: ...

    async def complete_structured_with_approval(
        self, request: ModelRequest, **kwargs: Any
    ) -> ModelResponse: ...

    async def stream(self, request: ModelRequest, **kwargs: Any) -> ModelResponse: ...

    async def stream_with_approval(self, request: ModelRequest, **kwargs: Any) -> ModelResponse: ...

    async def complete_tool_intent(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
        operation_identity_digest: str,
        tool_selection: ToolCatalogSelection | None,
        actor: IdentityContext | None = None,
    ) -> ModelTurnResult: ...

    async def complete_tool_intent_with_approval(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        actor: IdentityContext,
        grant: _ApprovedToolGrant,
    ) -> ModelTurnResult: ...


class BoundModelToolIntentMixin:
    """由可信bound context派生首轮tool identity并恢复审批。"""

    async def complete_tool_intent(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        tool_selection: ToolCatalogSelection | None = None,
    ) -> ModelTurnResult:
        """冻结bound Agent目录并执行首个provider-neutral tool-intent turn。"""

        service = cast(_BoundToolIntentRuntime, getattr(self, "_service"))  # noqa: B009
        context = cast(UsageEvidenceContext, getattr(self, "_context"))  # noqa: B009
        identity = cast(IdentityContext, getattr(self, "_identity"))  # noqa: B009
        usage_call_id = stable_usage_call_id(context=context, operation_key=operation_key)
        operation_identity_digest = model_route_operation_identity_digest(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            operation_key=operation_key,
        )
        return await service.complete_tool_intent(
            request,
            context=context,
            usage_call_id=usage_call_id,
            loop_id=tool_loop_identity_digest(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                agent_id=context.agent_id,
                request_id=context.request_id,
                trace_id=context.trace_id,
                operation_key=operation_key,
            ),
            turn_ordinal=1,
            operation_identity_digest=operation_identity_digest,
            tool_selection=tool_selection,
            actor=identity,
        )

    async def complete_tool_intent_approved(
        self,
        request: ModelRequest,
        *,
        operation_key: str,
        grant: _ApprovedToolGrant,
    ) -> ModelTurnResult:
        """从durable continuation恢复完整tool turn，只绕过一次policy gate。"""

        del operation_key
        service = cast(_BoundToolIntentRuntime, getattr(self, "_service"))  # noqa: B009
        context = cast(UsageEvidenceContext, getattr(self, "_context"))  # noqa: B009
        identity = cast(IdentityContext, getattr(self, "_identity"))  # noqa: B009
        return await service.complete_tool_intent_with_approval(
            request,
            context=context,
            actor=identity,
            grant=grant,
        )


class ModelInvocationToolIntentMixin:
    """Raw调用服务上的tool turn执行、用量读取与审批恢复。"""

    async def complete_tool_intent(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
        operation_identity_digest: str,
        tool_selection: ToolCatalogSelection | None,
        actor: IdentityContext | None = None,
    ) -> ModelTurnResult:
        """从可信目录resolver取得冻结快照，并复用唯一模型结算状态机。"""

        runtime = cast(_ToolIntentRuntime, self)
        if runtime._tool_catalog_resolver is None:  # pyright: ignore[reportPrivateUsage]
            raise RuntimeError("tool catalog resolver is not configured")
        result = await runtime._complete(  # pyright: ignore[reportPrivateUsage]
            request,
            context=context,
            usage_call_id=usage_call_id,
            route_operation_identity_digest=operation_identity_digest,
            soft_approved=False,
            actor=actor,
            approved_grant=None,
            tool_catalog=None,
            tool_catalog_selection=tool_selection,
            tool_loop_id=loop_id,
            tool_turn_ordinal=turn_ordinal,
        )
        if isinstance(result, ModelResponse):
            raise RuntimeError("tool-intent execution returned legacy response")
        return result

    async def complete_tool_loop_turn(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
        operation_identity_digest: str,
        tool_catalog: ToolCatalog,
        actor: IdentityContext,
        loop_token_bound: int,
        loop_cost_bound: float | None,
    ) -> ModelTurnResult:
        """只供绑定loop owner使用冻结catalog和runtime派生ordinal执行单轮。"""

        result = await cast(_ToolIntentRuntime, self)._complete(  # pyright: ignore[reportPrivateUsage]
            request,
            context=context,
            usage_call_id=usage_call_id,
            route_operation_identity_digest=operation_identity_digest,
            soft_approved=False,
            actor=actor,
            approved_grant=None,
            tool_catalog=tool_catalog,
            tool_catalog_selection=None,
            tool_loop_id=loop_id,
            tool_turn_ordinal=turn_ordinal,
            loop_token_bound=loop_token_bound,
            loop_cost_bound=loop_cost_bound,
        )
        if isinstance(result, ModelResponse):
            raise RuntimeError("tool-loop turn returned legacy response")
        return result

    async def read_tool_loop_turn_usage(
        self,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        loop_id: str,
        turn_ordinal: int,
    ) -> ModelUsageEvidence:
        """从同一durable settlement读取并交叉验证单轮actual usage。"""

        runtime = cast(_ToolIntentRuntime, self)
        async with runtime._storage.uow() as uow:  # pyright: ignore[reportPrivateUsage]
            persisted = await uow.evidence_outbox.get_usage(
                tenant_id=context.tenant_id,
                usage_call_id=usage_call_id,
            )
            if persisted.result_json is None or persisted.state not in {
                "result_persisted",
                "published",
            }:
                raise UsageInvocationReplayError(persisted.state)
            validated = runtime._validated_settlement_result(  # pyright: ignore[reportPrivateUsage]
                persisted.result_json,
                state=persisted.state,
                error_code=persisted.error_code,
            )
            try:
                replay_seed = ToolIntentReplaySeed.model_validate(
                    persisted.result_json.get("tool_intent_replay_seed")
                )
            except ValueError:
                raise UsageInvocationReplayError(persisted.state) from None
            if (
                replay_seed.usage_call_id != usage_call_id
                or replay_seed.loop_id != loop_id
                or replay_seed.turn_ordinal != turn_ordinal
            ):
                raise UsageInvocationReplayError(persisted.state)
            evidence = validated.evidence.model_copy(deep=True)
        if (
            evidence.tenant_id != context.tenant_id
            or evidence.run_id != context.run_id
            or evidence.agent_id != context.agent_id
            or evidence.request_id != context.request_id
            or evidence.trace_id != context.trace_id
        ):
            raise UsageInvocationReplayError(persisted.state)
        return evidence

    async def complete_tool_intent_with_approval(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        actor: IdentityContext,
        grant: _ApprovedToolGrant,
    ) -> ModelTurnResult:
        """校验完整durable tool identity后复用唯一模型结算状态机续跑。"""

        runtime = cast(_ToolIntentRuntime, self)
        approved = await resolve_tool_intent_approved_invocation_identity(
            storage=runtime._storage,  # pyright: ignore[reportPrivateUsage]
            context=context,
            identity_id=actor.user_id,
            grant=grant,
            request=request,
        )
        result = await runtime._complete(  # pyright: ignore[reportPrivateUsage]
            request,
            context=context,
            usage_call_id=approved.usage_call_id,
            route_operation_identity_digest=approved.replay_seed.bound_operation_identity_digest,
            soft_approved=True,
            actor=actor,
            approved_grant=grant,
            tool_catalog=approved.replay_seed.tool_catalog,
            tool_catalog_selection=None,
            tool_loop_id=approved.replay_seed.loop_id,
            tool_turn_ordinal=approved.replay_seed.turn_ordinal,
            expected_tool_replay_seed=approved.replay_seed,
        )
        if isinstance(result, ModelResponse):
            raise RuntimeError("approved tool-intent execution returned legacy response")
        return result


__all__ = ["BoundModelToolIntentMixin", "ModelInvocationToolIntentMixin"]
