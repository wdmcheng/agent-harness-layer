"""审批续跑从 durable continuation 恢复调用身份。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Protocol, cast

from agent_harness.models.providers import ModelRequest
from agent_harness.models.route_chain_identity import model_route_operation_identity_digest
from agent_harness.models.structured import (
    OutputSchemaIdentity,
    canonical_structured_json,
)
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
_STRUCTURED_CONTINUATION_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "usage_call_id",
        "operation_identity_digest",
        "schema_identity",
        "repair_limit",
        "arguments_hash",
    }
)


class ApprovalIdentityGrant(Protocol):
    """身份恢复只需 approval id；完整 grant 仍由调用服务另行校验。"""

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


@dataclass(frozen=True)
class StructuredApprovalIdentity:
    """从两份耐久artifact交叉校验后恢复的structured调用身份。"""

    usage_call_id: str
    operation_identity_digest: str
    repair_limit: int
    arguments_hash: str


class ModelInvocationApprovalIdentityMixin:
    """从主调用服务的storage恢复普通route-chain或legacy审批身份。"""

    _storage: SQLAlchemyStorage

    async def approved_invocation_identity(
        self,
        *,
        context: UsageEvidenceContext,
        grant: ApprovalIdentityGrant,
    ) -> tuple[str, str]:
        """只信任durable approval artifact，不从业务operation key重派生。"""

        return await resolve_approved_invocation_identity(
            storage=self._storage,
            context=context,
            grant=grant,
        )


def structured_approval_arguments(
    *,
    request: ModelRequest,
    usage_call_id: str,
    operation_identity_digest: str,
    schema_identity: OutputSchemaIdentity,
    repair_limit: int,
) -> dict[str, Any]:
    """构造保留显式null的exact structured审批参数预映像。"""

    if not _DIGEST.fullmatch(usage_call_id) or not _DIGEST.fullmatch(operation_identity_digest):
        raise ValueError("structured approval invocation identity is invalid")
    if isinstance(repair_limit, bool) or not 0 <= repair_limit <= 2:
        raise ValueError("structured approval repair limit is invalid")
    structured_request = request.model_copy(update={"capability": "structured_output"})
    return {
        "schema_version": "structured-policy-approval-arguments-v1",
        "usage_call_id": usage_call_id,
        "operation_identity_digest": operation_identity_digest,
        "request": structured_request.model_dump(mode="json"),
        "schema_identity": schema_identity.model_dump(mode="json"),
        "repair_limit": repair_limit,
    }


def structured_approval_arguments_bytes(arguments: dict[str, Any]) -> bytes:
    """以唯一structured canonical JSON规则生成审批hash输入。"""

    return canonical_structured_json(arguments).encode("utf-8")


def structured_approval_arguments_hash(arguments: dict[str, Any]) -> str:
    """计算同时绑定请求、schema、repair与耐久调用身份的摘要。"""

    return hashlib.sha256(structured_approval_arguments_bytes(arguments)).hexdigest()


def structured_approval_continuation(
    *,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """从已校验参数生成exact continuation，供record/checkpoint同值保存。"""

    schema_identity = OutputSchemaIdentity.model_validate(arguments["schema_identity"])
    repair_limit = arguments["repair_limit"]
    usage_call_id = arguments["usage_call_id"]
    operation_identity_digest = arguments["operation_identity_digest"]
    arguments_hash = structured_approval_arguments_hash(arguments)
    return {
        "schema_version": "structured-policy-approval-continuation-v1",
        "kind": "structured_policy_approval",
        "usage_call_id": usage_call_id,
        "operation_identity_digest": operation_identity_digest,
        "schema_identity": schema_identity.model_dump(mode="json"),
        "repair_limit": repair_limit,
        "arguments_hash": arguments_hash,
    }


def _structured_continuation(value: object) -> dict[str, Any] | None:
    """只接受exact structured continuation；相似但损坏的形状立即拒绝。"""

    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    if raw.get("kind") != "structured_policy_approval":
        return None
    if raw.keys() != _STRUCTURED_CONTINUATION_KEYS:
        raise ValueError("structured approval continuation shape is invalid")
    if raw.get("schema_version") != "structured-policy-approval-continuation-v1":
        raise ValueError("structured approval continuation version is invalid")
    digests = (
        raw.get("usage_call_id"),
        raw.get("operation_identity_digest"),
        raw.get("arguments_hash"),
    )
    repair_limit = raw.get("repair_limit")
    try:
        schema = OutputSchemaIdentity.model_validate(raw.get("schema_identity"))
    except (TypeError, ValueError):
        raise ValueError("structured approval schema identity is invalid") from None
    if (
        not all(isinstance(item, str) and _DIGEST.fullmatch(item) for item in digests)
        or isinstance(repair_limit, bool)
        or not isinstance(repair_limit, int)
        or not 0 <= repair_limit <= 2
    ):
        raise ValueError("structured approval continuation identity is invalid")
    normalized = {key: raw[key] for key in _STRUCTURED_CONTINUATION_KEYS}
    normalized["schema_identity"] = schema.model_dump(mode="json")
    return cast(dict[str, Any], normalized)


async def resolve_structured_approved_invocation_identity(
    *,
    storage: SQLAlchemyStorage,
    context: UsageEvidenceContext,
    identity_id: str,
    grant: ApprovalIdentityGrant,
    request: ModelRequest,
    schema_identity: OutputSchemaIdentity,
    repair_limit: int,
) -> StructuredApprovalIdentity:
    """交叉校验grant、lease、record、checkpoint与当前hard-bound输入。"""

    async with storage.uow() as uow:
        lease = await uow.approvals.get_resolution(grant.approval_id)
        if (
            lease is None
            or lease.lease_id != grant.lease_id
            or lease.state not in {"claimed", "execution_owned", "recovery_pending"}
            or lease.approval.status != "waiting"
        ):
            raise ValueError("structured approval grant does not match an active lease")
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
            or approval.action != "model.invoke"
            or grant.action != "model.invoke"
            or approval.action != grant.action
            or approval.resource != f"agent:{context.agent_id}:model"
            or grant.resource != f"agent:{context.agent_id}:model"
            or approval.resource != grant.resource
        ):
            raise ValueError("structured approval does not match bound invocation")
        metadata_continuation = _structured_continuation(
            cast(object, approval.metadata.get("continuation"))
        )
        if metadata_continuation is None:
            raise ValueError("structured approval continuation is missing")
        if approval.resume_token is not None:
            checkpoint = await uow.checkpoints.get_by_resume_token(approval.resume_token)
            if (
                checkpoint is None
                or checkpoint.tenant_id != context.tenant_id
                or checkpoint.run_id != context.run_id
                or checkpoint.state.get("kind") != "agent_executor_approval"
            ):
                raise ValueError("structured approval checkpoint is missing or mismatched")
            checkpoint_continuation = _structured_continuation(
                cast(object, checkpoint.state.get("continuation"))
            )
            if checkpoint_continuation != metadata_continuation:
                raise ValueError("structured approval checkpoint continuation is inconsistent")
        arguments = structured_approval_arguments(
            request=request,
            usage_call_id=metadata_continuation["usage_call_id"],
            operation_identity_digest=metadata_continuation["operation_identity_digest"],
            schema_identity=schema_identity,
            repair_limit=repair_limit,
        )
        expected_hash = structured_approval_arguments_hash(arguments)
        if (
            metadata_continuation["schema_identity"] != schema_identity.model_dump(mode="json")
            or metadata_continuation["repair_limit"] != repair_limit
            or metadata_continuation["arguments_hash"] != expected_hash
            or approval.metadata.get("arguments_hash") != expected_hash
            or grant.arguments_hash != expected_hash
        ):
            raise ValueError("structured approval arguments binding is invalid")
        return StructuredApprovalIdentity(
            usage_call_id=metadata_continuation["usage_call_id"],
            operation_identity_digest=metadata_continuation["operation_identity_digest"],
            repair_limit=repair_limit,
            arguments_hash=expected_hash,
        )


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


__all__ = [
    "ApprovalIdentityGrant",
    "ModelInvocationApprovalIdentityMixin",
    "StructuredApprovalIdentity",
    "resolve_approved_invocation_identity",
    "resolve_structured_approved_invocation_identity",
    "structured_approval_arguments",
    "structured_approval_arguments_bytes",
    "structured_approval_arguments_hash",
    "structured_approval_continuation",
]
