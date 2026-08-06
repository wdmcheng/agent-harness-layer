"""普通完成与文本流调用的公开模型 invocation 门面。"""

from __future__ import annotations

from typing import cast

from agent_harness.identity import IdentityContext
from agent_harness.models._invocation_basic_requirements import (
    ModelInvocationBasicRequirements,
)
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.usage import UsageEvidenceContext


class ModelInvocationBasicMixin:
    """公开普通 completion/stream seam，内部仍复用唯一执行状态机。"""

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

        runtime = cast(ModelInvocationBasicRequirements, self)
        return cast(
            ModelResponse,
            await runtime._complete(  # pyright: ignore[reportPrivateUsage]
                request,
                context=context,
                usage_call_id=usage_call_id,
                route_operation_identity_digest=route_operation_identity_digest,
                soft_approved=False,
                actor=actor,
                approved_grant=None,
            ),
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

        runtime = cast(ModelInvocationBasicRequirements, self)
        return await runtime._stream(  # pyright: ignore[reportPrivateUsage]
            request,
            context=context,
            usage_call_id=usage_call_id,
            route_operation_identity_digest=route_operation_identity_digest,
            soft_approved=False,
            actor=actor,
            approved_grant=None,
        )


__all__ = ["ModelInvocationBasicMixin"]
