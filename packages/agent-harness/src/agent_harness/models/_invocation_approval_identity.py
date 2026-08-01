"""审批续跑从 durable continuation 恢复调用身份。"""

from __future__ import annotations

import re
from typing import Protocol, cast

from agent_harness.models.route_chain_identity import model_route_operation_identity_digest
from agent_harness.models.usage import UsageEvidenceContext, stable_usage_call_id
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CHAIN_CONTINUATION_KEYS = frozenset(
    {
        "kind",
        "route_chain_id",
        "usage_call_id",
        "operation_identity_digest",
        "candidate_ordinal",
    }
)
_CHAIN_ONLY_KEYS = _CHAIN_CONTINUATION_KEYS - {"kind"}


class ApprovalIdentityGrant(Protocol):
    """身份恢复只需 approval id；完整 grant 仍由调用服务另行校验。"""

    @property
    def approval_id(self) -> str: ...


def _route_chain_continuation(value: object) -> dict[str, object] | None:
    """识别 exact route-chain continuation；部分字段视为持久化损坏。"""

    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    if not (_CHAIN_ONLY_KEYS & raw.keys()):
        return None
    if raw.keys() != _CHAIN_CONTINUATION_KEYS or raw.get("kind") != "policy_approval":
        raise ValueError("route-chain approval continuation shape is invalid")
    digests = (
        raw.get("route_chain_id"),
        raw.get("usage_call_id"),
        raw.get("operation_identity_digest"),
    )
    candidate_ordinal = raw.get("candidate_ordinal")
    if (
        not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in digests)
        or isinstance(candidate_ordinal, bool)
        or not isinstance(candidate_ordinal, int)
        or candidate_ordinal < 1
    ):
        raise ValueError("route-chain approval continuation identity is invalid")
    return {key: raw[key] for key in _CHAIN_CONTINUATION_KEYS}


async def resolve_approved_invocation_identity(
    *,
    storage: SQLAlchemyStorage,
    context: UsageEvidenceContext,
    grant: ApprovalIdentityGrant,
) -> tuple[str, str]:
    """从 checkpoint/private approval artifact 恢复身份，禁止读取 current policy。"""

    async with storage.uow() as uow:
        lease = await uow.approvals.get_resolution(grant.approval_id)
        if lease is None:
            raise ValueError("model approval grant does not match persisted approval")
        approval = lease.approval
        if (
            approval.tenant_id != context.tenant_id
            or approval.run_id != context.run_id
            or approval.agent_id != context.agent_id
        ):
            raise ValueError("model approval continuation does not match bound invocation")

        metadata_continuation: object = approval.metadata.get("continuation")
        continuation: object = metadata_continuation
        if approval.resume_token is not None:
            checkpoint = await uow.checkpoints.get_by_resume_token(approval.resume_token)
            if (
                checkpoint is None
                or checkpoint.tenant_id != context.tenant_id
                or checkpoint.run_id != context.run_id
                or checkpoint.state.get("kind") != "agent_executor_approval"
            ):
                raise ValueError("model approval checkpoint is missing or mismatched")
            checkpoint_continuation: object = checkpoint.state.get("continuation")
            if checkpoint_continuation != metadata_continuation:
                raise ValueError("model approval checkpoint continuation is inconsistent")
            continuation = checkpoint_continuation

        chain_continuation = _route_chain_continuation(cast(object, continuation))
        if chain_continuation is None:
            if await uow.shared_budget.has_waiting_model_route_chain_state(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
            ):
                raise ValueError("route-chain approval continuation is missing or invalid")
        if chain_continuation is not None:
            usage_call_id = str(chain_continuation["usage_call_id"])
            operation_identity_digest = str(chain_continuation["operation_identity_digest"])
            state = await uow.shared_budget.get_model_route_chain_state(
                tenant_id=context.tenant_id,
                run_id=context.run_id,
                usage_call_id=usage_call_id,
            )
            if (
                state is None
                or state.chain_id != chain_continuation["route_chain_id"]
                or state.operation_identity_digest != operation_identity_digest
                or state.waiting_approval_ordinal != chain_continuation["candidate_ordinal"]
            ):
                raise ValueError("route-chain approval continuation does not match durable state")
            return usage_call_id, operation_identity_digest

    continuation_key = f"approved:{grant.approval_id}"
    return (
        stable_usage_call_id(context=context, operation_key=continuation_key),
        model_route_operation_identity_digest(
            tenant_id=context.tenant_id,
            run_id=context.run_id,
            agent_id=context.agent_id,
            request_id=context.request_id,
            trace_id=context.trace_id,
            operation_key=continuation_key,
        ),
    )


__all__ = ["ApprovalIdentityGrant", "resolve_approved_invocation_identity"]
