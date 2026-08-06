"""普通 invocation 门面跨 mixin 所需的静态类型契约。"""

from __future__ import annotations

from typing import Any, Protocol

from agent_harness.identity import IdentityContext
from agent_harness.models.providers import ModelRequest, ModelResponse
from agent_harness.models.usage import UsageEvidenceContext


class ModelInvocationBasicRequirements(Protocol):
    """只描述由执行与 streaming mixin 提供的内部入口。"""

    async def _complete(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        route_operation_identity_digest: str | None,
        soft_approved: bool,
        actor: IdentityContext | None,
        approved_grant: Any,
    ) -> object: ...

    async def _stream(
        self,
        request: ModelRequest,
        *,
        context: UsageEvidenceContext,
        usage_call_id: str,
        route_operation_identity_digest: str | None,
        soft_approved: bool,
        actor: IdentityContext | None,
        approved_grant: Any,
    ) -> ModelResponse: ...


__all__ = ["ModelInvocationBasicRequirements"]
