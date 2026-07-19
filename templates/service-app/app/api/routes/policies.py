"""策略引擎 API：暴露可审计的决策摘要而不泄露内部 Provider 实现。"""

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
    """HTTP policy check 请求；actor 由认证 dependency 注入。"""

    action: str
    resource: str
    context: dict[str, Any] = Field(default_factory=dict)


class PolicyDecisionResponse(HarnessDTO):
    """POL-001 对外返回的三态决策摘要。"""

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
    """执行 policy check，并要求结果携带 audit_ref 证据。"""

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
    """从扩展元数据中提取字符串规则 ID，兼容缺失或旧版本的非列表字段。"""
    raw = decision.metadata.get("matched_rules", [])
    if not isinstance(raw, list):
        return []
    return [item for item in cast(list[object], raw) if isinstance(item, str)]


def _audit_ref(decision: PolicyEvaluation) -> str:
    """读取策略决策的必需审计引用；缺失时拒绝生成不可追溯的成功响应。"""
    raw = decision.metadata.get("audit_ref")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("PolicyEngine did not return audit_ref")
    return raw
