"""PolicyEngine API routes。"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyEngine, PolicyEvaluation
from app.api.dependencies import current_identity, get_policy_engine
from app.api.routes.runs import request_id_from

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ApiErrorEnvelope},
    403: {"model": ApiErrorEnvelope},
    422: {"model": ApiErrorEnvelope},
    500: {"model": ApiErrorEnvelope},
}

router = APIRouter(prefix="/api/v1", tags=["policies"], responses=ERROR_RESPONSES)


class PolicyCheckRequest(HarnessDTO):
    action: str
    resource: str
    context: dict[str, Any] = Field(default_factory=dict)


class PolicyDecisionResponse(HarnessDTO):
    request_id: str
    decision: str
    reason: str
    matched_rules: list[str]
    audit_ref: str
    approval: dict[str, Any] | None = None


@router.post("/policies/check", response_model=PolicyDecisionResponse)
async def check_policy(
    http_request: Request,
    request: PolicyCheckRequest,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    policy: Annotated[PolicyEngine | None, Depends(get_policy_engine)],
) -> PolicyDecisionResponse:
    if policy is None:
        raise RuntimeError("PolicyEngine dependency is not configured")
    request_id = request_id_from(http_request)
    decision = await policy.evaluate(
        PolicyCheck(
            actor=identity,
            action=request.action,
            resource=request.resource,
            context={**request.context, "request_id": request_id},
        )
    )
    return PolicyDecisionResponse(
        request_id=request_id,
        decision=decision.decision,
        reason=decision.reason,
        matched_rules=_matched_rules(decision),
        audit_ref=_audit_ref(decision),
        approval=decision.approval.to_payload() if decision.approval is not None else None,
    )


def _matched_rules(decision: PolicyEvaluation) -> list[str]:
    raw = decision.metadata.get("matched_rules", [])
    if not isinstance(raw, list):
        return []
    return [item for item in cast(list[object], raw) if isinstance(item, str)]


def _audit_ref(decision: PolicyEvaluation) -> str:
    raw = decision.metadata.get("audit_ref")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("PolicyEngine did not return audit_ref")
    return raw
