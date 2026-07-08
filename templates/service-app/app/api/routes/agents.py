"""Agent registry API routes。"""

from __future__ import annotations

from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Request

from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.registry import AgentDescriptor
from app.api.dependencies import current_identity, get_policy_engine
from app.api.routes.runs import request_id_from

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ApiErrorEnvelope},
    403: {"model": ApiErrorEnvelope},
    409: {"model": ApiErrorEnvelope},
    422: {"model": ApiErrorEnvelope},
    500: {"model": ApiErrorEnvelope},
}

router = APIRouter(prefix="/api/v1", tags=["agents"], responses=ERROR_RESPONSES)


class AgentListResponse(HarnessDTO):
    request_id: str
    agents: list[AgentDescriptor]


class AgentRegistryDependency(Protocol):
    def list_agents(self) -> list[AgentDescriptor]: ...


def get_agent_registry() -> AgentRegistryDependency:
    """由应用工厂注入的 AgentRegistry 依赖。"""

    raise RuntimeError("AgentRegistry dependency is not configured")


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(
    request: Request,
    registry: Annotated[AgentRegistryDependency, Depends(get_agent_registry)],
    identity: Annotated[IdentityContext, Depends(current_identity)],
    policy: Annotated[PolicyEngine | None, Depends(get_policy_engine)],
) -> AgentListResponse:
    if policy is not None:
        await policy.require_allowed(
            PolicyCheck(
                actor=identity,
                action="agents.list",
                resource="agents",
                context={},
            )
        )
    return AgentListResponse(
        request_id=request_id_from(request),
        agents=registry.list_agents(),
    )
