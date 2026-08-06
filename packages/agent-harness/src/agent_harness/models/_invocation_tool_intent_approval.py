"""Tool-intent审批预映像、continuation与耐久恢复身份。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, cast

from agent_harness.models._invocation_approval_identity import ApprovalIdentityGrant
from agent_harness.models.providers import ModelRequest
from agent_harness.models.structured import canonical_structured_json
from agent_harness.models.tool_intent import ToolIntentReplaySeed
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.storage.adapters.sqlalchemy import SQLAlchemyStorage

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CONTINUATION_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "usage_call_id",
        "operation_identity_digest",
        "request_arguments_hash",
        "tool_request_identity_digest",
        "tool_intent_replay_seed",
        "arguments_hash",
    }
)


@dataclass(frozen=True)
class ToolIntentApprovalIdentity:
    """从durable approval/checkpoint恢复的完整tool turn调用身份。"""

    usage_call_id: str
    replay_seed: ToolIntentReplaySeed
    arguments_hash: str


def tool_intent_approval_arguments(
    *,
    usage_call_id: str,
    operation_identity_digest: str,
    request_arguments_hash: str,
    tool_request_identity_digest: str,
) -> dict[str, Any]:
    """构造冻结契约规定的五字段exact tool-intent审批预映像。"""

    digests = (
        usage_call_id,
        operation_identity_digest,
        request_arguments_hash,
        tool_request_identity_digest,
    )
    if not all(_DIGEST.fullmatch(item) for item in digests):
        raise ValueError("tool-intent approval identity is invalid")
    return {
        "schema_version": "tool-intent-policy-approval-arguments-v1",
        "usage_call_id": usage_call_id,
        "operation_identity_digest": operation_identity_digest,
        "request_arguments_hash": request_arguments_hash,
        "tool_request_identity_digest": tool_request_identity_digest,
    }


def model_request_arguments_hash(request: ModelRequest) -> str:
    """保留既有model.invoke request hash算法，供普通与tool审批逐值兼容。"""

    return hashlib.sha256(
        canonical_structured_json(request.to_payload()).encode("utf-8")
    ).hexdigest()


def tool_intent_approval_arguments_hash(arguments: dict[str, Any]) -> str:
    """使用既有canonical structured JSON规则生成tool-intent审批摘要。"""

    return hashlib.sha256(canonical_structured_json(arguments).encode("utf-8")).hexdigest()


def tool_intent_approval_continuation(
    *,
    arguments: dict[str, Any],
    replay_seed: ToolIntentReplaySeed,
) -> dict[str, Any]:
    """从已校验预映像生成record/checkpoint共用的exact continuation。"""

    usage_call_id = arguments["usage_call_id"]
    if (
        not isinstance(usage_call_id, str)
        or not _DIGEST.fullmatch(usage_call_id)
        or replay_seed.operation_identity_digest != arguments["operation_identity_digest"]
        or replay_seed.request_identity.digest != arguments["tool_request_identity_digest"]
    ):
        raise ValueError("tool-intent approval continuation identity is invalid")
    return {
        "schema_version": "tool-intent-policy-approval-continuation-v1",
        "kind": "tool_intent_policy_approval",
        "usage_call_id": usage_call_id,
        "operation_identity_digest": arguments["operation_identity_digest"],
        "request_arguments_hash": arguments["request_arguments_hash"],
        "tool_request_identity_digest": arguments["tool_request_identity_digest"],
        "tool_intent_replay_seed": replay_seed.to_payload(),
        "arguments_hash": tool_intent_approval_arguments_hash(arguments),
    }


def _tool_intent_continuation(value: object) -> dict[str, Any] | None:
    """只接受exact tool-intent continuation，损坏或扩展字段均关闭失败。"""

    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    if raw.get("kind") != "tool_intent_policy_approval":
        return None
    if raw.keys() != _CONTINUATION_KEYS:
        raise ValueError("tool-intent approval continuation shape is invalid")
    if raw.get("schema_version") != "tool-intent-policy-approval-continuation-v1":
        raise ValueError("tool-intent approval continuation version is invalid")
    usage_call_id = raw.get("usage_call_id")
    arguments_hash = raw.get("arguments_hash")
    operation_identity_digest = raw.get("operation_identity_digest")
    request_arguments_hash = raw.get("request_arguments_hash")
    tool_request_identity_digest = raw.get("tool_request_identity_digest")
    try:
        replay_seed = ToolIntentReplaySeed.model_validate(raw.get("tool_intent_replay_seed"))
    except (TypeError, ValueError):
        raise ValueError("tool-intent approval replay seed is invalid") from None
    if (
        not all(
            isinstance(item, str) and _DIGEST.fullmatch(item)
            for item in (
                usage_call_id,
                operation_identity_digest,
                request_arguments_hash,
                tool_request_identity_digest,
                arguments_hash,
            )
        )
        or replay_seed.operation_identity_digest != operation_identity_digest
        or replay_seed.request_identity.digest != tool_request_identity_digest
    ):
        raise ValueError("tool-intent approval continuation identity is invalid")
    return {
        "schema_version": raw["schema_version"],
        "kind": raw["kind"],
        "usage_call_id": usage_call_id,
        "operation_identity_digest": operation_identity_digest,
        "request_arguments_hash": request_arguments_hash,
        "tool_request_identity_digest": tool_request_identity_digest,
        "tool_intent_replay_seed": replay_seed.to_payload(),
        "arguments_hash": arguments_hash,
    }


async def resolve_tool_intent_approved_invocation_identity(
    *,
    storage: SQLAlchemyStorage,
    context: UsageEvidenceContext,
    identity_id: str,
    grant: ApprovalIdentityGrant,
    request: ModelRequest,
) -> ToolIntentApprovalIdentity:
    """交叉校验grant、lease、record、checkpoint与冻结tool turn预映像。"""

    async with storage.uow() as uow:
        lease = await uow.approvals.get_resolution(grant.approval_id)
        if (
            lease is None
            or lease.lease_id != grant.lease_id
            or lease.state not in {"claimed", "execution_owned", "recovery_pending"}
            or lease.approval.status != "waiting"
        ):
            raise ValueError("tool-intent approval grant does not match an active lease")
        approval = lease.approval
        persisted_identity = str(approval.metadata.get("identity_id") or approval.requested_by)
        if (
            approval.tenant_id != context.tenant_id
            or approval.run_id != context.run_id
            or approval.agent_id != context.agent_id
            or grant.tenant_id != context.tenant_id
            or grant.run_id != context.run_id
            or grant.agent_id != context.agent_id
            or persisted_identity != identity_id
            or grant.identity_id != identity_id
            or approval.action != grant.action
            or grant.action != "model.invoke"
            or approval.resource != grant.resource
            or grant.resource != f"agent:{context.agent_id}:model"
        ):
            raise ValueError("tool-intent approval does not match bound invocation")
        metadata_continuation = _tool_intent_continuation(
            cast(object, approval.metadata.get("continuation"))
        )
        if metadata_continuation is None:
            raise ValueError("tool-intent approval continuation is missing")
        if approval.resume_token is not None:
            checkpoint = await uow.checkpoints.get_by_resume_token(approval.resume_token)
            if (
                checkpoint is None
                or checkpoint.tenant_id != context.tenant_id
                or checkpoint.run_id != context.run_id
                or checkpoint.state.get("kind") != "agent_executor_approval"
            ):
                raise ValueError("tool-intent approval checkpoint is missing or mismatched")
            checkpoint_continuation = _tool_intent_continuation(
                cast(object, checkpoint.state.get("continuation"))
            )
            if checkpoint_continuation != metadata_continuation:
                raise ValueError("tool-intent approval checkpoint continuation is inconsistent")
        replay_seed = ToolIntentReplaySeed.model_validate(
            metadata_continuation["tool_intent_replay_seed"]
        )
        arguments = tool_intent_approval_arguments(
            usage_call_id=metadata_continuation["usage_call_id"],
            operation_identity_digest=metadata_continuation["operation_identity_digest"],
            request_arguments_hash=model_request_arguments_hash(request),
            tool_request_identity_digest=replay_seed.request_identity.digest,
        )
        expected_hash = tool_intent_approval_arguments_hash(arguments)
        if (
            metadata_continuation["arguments_hash"] != expected_hash
            or approval.metadata.get("arguments_hash") != expected_hash
            or grant.arguments_hash != expected_hash
        ):
            raise ValueError("tool-intent approval arguments binding is invalid")
        return ToolIntentApprovalIdentity(
            usage_call_id=metadata_continuation["usage_call_id"],
            replay_seed=replay_seed,
            arguments_hash=expected_hash,
        )


__all__ = [
    "ToolIntentApprovalIdentity",
    "model_request_arguments_hash",
    "resolve_tool_intent_approved_invocation_identity",
    "tool_intent_approval_arguments",
    "tool_intent_approval_arguments_hash",
    "tool_intent_approval_continuation",
]
