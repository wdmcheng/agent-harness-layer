"""HITL approval API 路由。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from agent_harness.approvals import ApprovalResolveResult, ApprovalService
from agent_harness.contracts import ApiErrorEnvelope
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.delegation import DelegationService
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import RunResult, RunStatus
from agent_harness.storage import ApprovalRecord
from app.api.dependencies import current_identity, get_approval_service, get_policy_engine
from app.api.routes.runs import RunCreateResponse, get_delegation_service, request_id_from

ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ApiErrorEnvelope},
    403: {"model": ApiErrorEnvelope},
    404: {"model": ApiErrorEnvelope},
    409: {"model": ApiErrorEnvelope},
    422: {"model": ApiErrorEnvelope},
    500: {"model": ApiErrorEnvelope},
    503: {"model": ApiErrorEnvelope},
}

router = APIRouter(prefix="/api/v1", tags=["approvals"], responses=ERROR_RESPONSES)


class ApprovalPublicRecord(HarnessDTO):
    """对 HTTP/CLI 可见的脱敏审批记录。"""

    approval_id: str
    tenant_id: str
    run_id: str
    agent_id: str
    status: str
    action: str
    resource: str
    reason: str
    trace_id: str
    request_id: str | None = None
    requested_by: str | None = None
    resolved_by: str | None = None
    result: str | None = None
    created_at: str | None = None


class ApprovalListResponse(HarnessDTO):
    """APR-001 列表响应。"""

    request_id: str
    approvals: list[ApprovalPublicRecord]


class ApprovalDetailResponse(HarnessDTO):
    """单个 approval 读取响应。"""

    request_id: str
    approval: ApprovalPublicRecord


class ApprovalResolveRequest(HarnessDTO):
    """审批 resolve 请求；run_id 和 approval_id 来自 URL 边界。"""

    decision: Literal["approved", "denied"]
    comment: str | None = None


class ApprovalResolveResponse(HarnessDTO):
    """APR-002 resolve 后的审批记录和可选 run 摘要。"""

    request_id: str
    approval: ApprovalPublicRecord
    run: RunCreateResponse | None = None


@router.get("/runs/{run_id}/approvals", response_model=ApprovalListResponse)
async def list_approvals(
    http_request: Request,
    run_id: str,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    approvals: Annotated[ApprovalService, Depends(get_approval_service)],
    policy: Annotated[PolicyEngine | None, Depends(get_policy_engine)],
    status: str | None = Query(default=None),
) -> ApprovalListResponse:
    """列出当前身份可见的 run approvals。"""

    await _check_read_permission(policy=policy, identity=identity, run_id=run_id)
    rows = await approvals.list_for_run(actor=identity, run_id=run_id)
    if status is not None:
        rows = [row for row in rows if row.status == status]
    return ApprovalListResponse(
        request_id=request_id_from(http_request),
        approvals=[_public_approval(row) for row in rows],
    )


@router.get("/runs/{run_id}/approvals/{approval_id}", response_model=ApprovalDetailResponse)
async def get_approval(
    http_request: Request,
    run_id: str,
    approval_id: str,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    approvals: Annotated[ApprovalService, Depends(get_approval_service)],
    policy: Annotated[PolicyEngine | None, Depends(get_policy_engine)],
) -> ApprovalDetailResponse:
    """读取单个 approval，保持 run_id 与 approval_id 归属一致。"""

    await _check_read_permission(
        policy=policy,
        identity=identity,
        run_id=run_id,
        approval_id=approval_id,
    )
    row = await approvals.get(actor=identity, run_id=run_id, approval_id=approval_id)
    return ApprovalDetailResponse(
        request_id=request_id_from(http_request),
        approval=_public_approval(row),
    )


@router.post(
    "/runs/{run_id}/approvals/{approval_id}",
    response_model=ApprovalResolveResponse,
    responses={202: {"model": ApprovalResolveResponse}},
)
async def resolve_approval(
    http_request: Request,
    response: Response,
    run_id: str,
    approval_id: str,
    request: ApprovalResolveRequest,
    identity: Annotated[IdentityContext, Depends(current_identity)],
    approvals: Annotated[ApprovalService, Depends(get_approval_service)],
    policy: Annotated[PolicyEngine | None, Depends(get_policy_engine)],
    delegation_service: Annotated[
        DelegationService | None,
        Depends(get_delegation_service),
    ],
) -> ApprovalResolveResponse:
    """对 waiting approval 执行 approve/deny，并按策略推进 run。"""

    request_id = request_id_from(http_request)
    await _check_resolve_permission(
        policy=policy,
        identity=identity,
        run_id=run_id,
        approval_id=approval_id,
        decision=request.decision,
    )
    approval = await approvals.get_by_id(
        actor=identity,
        approval_id=approval_id,
        audit_read=False,
    )
    if approval.run_id != run_id:
        raise LookupError(f"approval not found: {approval_id}")
    if delegation_service is not None:
        # 上次 resolve 可能已提交 child terminal，却在聚合/事件确认处失败。
        # ownership 校验后先补偿，确保 approval conflict 不会截断恢复边沿。
        await delegation_service.reconcile_child_if_delegated(run_id)
    result: ApprovalResolveResult
    if request.decision == "approved":
        result = await approvals.approve(
            actor=identity,
            run_id=run_id,
            approval_id=approval_id,
            request_id=request_id,
            comment=request.comment,
        )
    else:
        result = await approvals.deny(
            actor=identity,
            run_id=run_id,
            approval_id=approval_id,
            request_id=request_id,
            comment=request.comment,
        )
    if (
        delegation_service is not None
        and result.run is not None
        and result.run.status
        in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ):
        # local approval 不经过 worker handler；在 HTTP 响应前补齐 delegated
        # child final，保持与 service queue 的终态边界一致。
        await delegation_service.reconcile_child_if_delegated(run_id)
    if (
        approvals.uses_queue
        and request.decision == "approved"
        and result.approval.status == "waiting"
    ):
        response.status_code = 202
    return ApprovalResolveResponse(
        request_id=request_id,
        approval=_public_approval(result.approval),
        run=_public_run(result.run, request_id=request_id),
    )


async def _check_resolve_permission(
    *,
    policy: PolicyEngine | None,
    identity: IdentityContext,
    run_id: str,
    approval_id: str,
    decision: str,
) -> None:
    engine = policy or PolicyEngine(provider=YamlPolicyProvider.default())
    await engine.require_allowed(
        PolicyCheck(
            actor=identity,
            action="approval.resolve",
            resource=f"run:{run_id}:approval:{approval_id}",
            context={"decision": decision},
        )
    )


async def _check_read_permission(
    *,
    policy: PolicyEngine | None,
    identity: IdentityContext,
    run_id: str,
    approval_id: str | None = None,
) -> None:
    engine = policy or PolicyEngine(provider=YamlPolicyProvider.default())
    resource = f"run:{run_id}:approval:{approval_id}" if approval_id else f"run:{run_id}:approvals"
    await engine.require_allowed(
        PolicyCheck(
            actor=identity,
            action="approval.read",
            resource=resource,
            context={"approval_id": approval_id} if approval_id else {},
        )
    )


def _public_approval(record: ApprovalRecord) -> ApprovalPublicRecord:
    return ApprovalPublicRecord(
        approval_id=record.approval_id,
        tenant_id=record.tenant_id,
        run_id=record.run_id,
        agent_id=record.agent_id,
        status=record.status,
        action=record.action,
        resource=record.resource,
        reason=record.reason,
        trace_id=record.trace_id,
        request_id=record.request_id,
        requested_by=record.requested_by,
        resolved_by=record.resolved_by,
        result=record.status if record.status in {"approved", "denied", "cancelled"} else None,
        created_at=record.created_at.isoformat() if record.created_at is not None else None,
    )


def _public_run(run: RunResult | None, *, request_id: str) -> RunCreateResponse | None:
    if run is None:
        return None
    return RunCreateResponse(
        request_id=request_id,
        run_id=run.run_id,
        status=run.status,
        terminal_event=run.terminal_event,
    )
