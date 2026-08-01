"""Route-chain attempt、proof 与审批 binding 的 exact canonical identities。"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models._router_contracts import (
    ModelRouteAgentPolicyIdentity,
    ModelRouteRequestBounds,
)
from agent_harness.models._router_identity import (
    canonical_decimal,
    model_route_canonical_json,
    model_route_digest,
)

_DIGEST = r"^[0-9a-f]{64}$"


class _CanonicalRouteIdentity(HarnessDTO):
    """所有 binding 拒绝 unknown/缺失字段，并复用唯一 UTF-8 serializer。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    def canonical_payload(self) -> dict[str, object]:
        """保留显式 null，返回唯一可摘要 JSON object。"""

        return self.model_dump(mode="python", exclude_none=False)

    def canonical_bytes(self) -> bytes:
        """返回 `model-route-canonical-json-v1` exact bytes。"""

        return model_route_canonical_json(self.canonical_payload())

    def digest(self) -> str:
        """返回 exact canonical bytes 的小写 SHA-256。"""

        return model_route_digest(self.canonical_payload())


class ModelRouteAttemptIdentity(_CanonicalRouteIdentity):
    """全链不可覆盖的 provider attempt started identity。"""

    schema_version: Literal["model-route-attempt-identity-v1"]
    chain_id: str = Field(pattern=_DIGEST)
    usage_call_id: str = Field(pattern=_DIGEST)
    operation_identity_digest: str = Field(pattern=_DIGEST)
    candidate_ordinal: int = Field(ge=1, le=8, strict=True)
    global_attempt: int = Field(ge=1, strict=True)
    route_digest: str = Field(pattern=_DIGEST)
    endpoint_policy_digest: str = Field(pattern=_DIGEST)
    retry_policy_digest: str = Field(pattern=_DIGEST)


class ModelRouteNotStartedProofIdentity(_CanonicalRouteIdentity):
    """两类可信实际零副作用证明的唯一摘要输入。"""

    schema_version: Literal["model-route-not-started-proof-v1"]
    chain_id: str = Field(pattern=_DIGEST)
    candidate_ordinal: int = Field(ge=1, le=8, strict=True)
    global_attempt: int = Field(ge=1, strict=True)
    reason: Literal["client_not_started", "trusted_business_not_started"]
    attempt_side_effect_state: Literal["not_started", "started"]
    request_sent: bool
    http_response_observed: bool
    http_status: int | None = Field(ge=100, le=599, strict=True)
    response_identity_observed: bool
    usage_observed: bool
    text_observed: bool
    delta_observed: bool
    completion_observed: bool | None
    endpoint_policy_digest: str = Field(pattern=_DIGEST)
    classifier_ref: str | None
    classifier_version: str | None

    @model_validator(mode="after")
    def validate_proof_union(self) -> ModelRouteNotStartedProofIdentity:
        """证明种类、请求/响应事实与 nullable classifier 必须逐值封闭。"""

        if any(
            (
                self.response_identity_observed,
                self.usage_observed,
                self.text_observed,
                self.delta_observed,
            )
        ):
            raise ValueError("not-started proof contains provider result observations")
        if self.reason == "client_not_started":
            valid = (
                self.attempt_side_effect_state == "not_started"
                and not self.request_sent
                and not self.http_response_observed
                and self.http_status is None
                and self.completion_observed is None
                and self.classifier_ref is None
                and self.classifier_version is None
            )
        else:
            valid = (
                self.attempt_side_effect_state == "started"
                and self.request_sent
                and self.http_response_observed
                and self.http_status is not None
                and self.completion_observed is False
                and bool(self.classifier_ref)
                and bool(self.classifier_version)
            )
        if not valid:
            raise ValueError("not-started proof identity union is invalid")
        return self


class ModelRouteApprovalRequestIdentity(_CanonicalRouteIdentity):
    """进入 waiting coordination 时冻结的审批请求 binding。"""

    schema_version: Literal["model-route-chain-approval-request-v1"]
    chain_id: str = Field(pattern=_DIGEST)
    candidate_ordinal: int = Field(ge=1, le=8, strict=True)
    route_digest: str = Field(pattern=_DIGEST)
    usage_call_id: str = Field(pattern=_DIGEST)
    operation_identity_digest: str = Field(pattern=_DIGEST)
    tenant_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    request_id: str | None
    trace_id: str | None
    action: Literal["model.invoke"]
    resource: str = Field(min_length=1)
    arguments_ref: str = Field(min_length=1)
    arguments_hash: str = Field(pattern=_DIGEST)


class ModelRouteApprovalGrantIdentity(_CanonicalRouteIdentity):
    """active lease 与原请求逐值绑定后的审批 grant identity。"""

    schema_version: Literal["model-route-chain-approval-grant-v1"]
    request_binding_digest: str = Field(pattern=_DIGEST)
    usage_call_id: str = Field(pattern=_DIGEST)
    operation_identity_digest: str = Field(pattern=_DIGEST)
    approval_id: str = Field(min_length=1)
    lease_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    identity_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    action: Literal["model.invoke"]
    resource: str = Field(min_length=1)
    arguments_hash: str = Field(pattern=_DIGEST)


class ModelRouteCandidateIdentity(_CanonicalRouteIdentity):
    """公开 chain identity 中不含 SDK、URL 或 credential value 的候选。"""

    ordinal: int = Field(ge=1, le=8, strict=True)
    deployment_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    route_digest: str = Field(pattern=_DIGEST)
    endpoint_policy_digest: str = Field(pattern=_DIGEST)
    model_catalog_digest: str = Field(pattern=_DIGEST)
    retry_policy_digest: str = Field(pattern=_DIGEST)
    bulkhead_policy_digest: str = Field(pattern=_DIGEST)
    credential_ref: str | None
    model_catalog_ref: str = Field(min_length=1)
    model_catalog_version: str = Field(min_length=1)
    reserved_token_bound: int = Field(ge=0, strict=True)
    reserved_cost_bound: Decimal | None = Field(ge=0)


class ModelRouteChainIdentity(_CanonicalRouteIdentity):
    """可独立复算 chain id 的完整公开 `model-route-chain-v1` identity。"""

    schema_version: Literal["model-route-chain-v1"]
    chain_id: str = Field(pattern=_DIGEST)
    capability: Literal["text_completion", "text_stream"]
    candidate_count: int = Field(ge=1, le=8, strict=True)
    agent_model_policy: ModelRouteAgentPolicyIdentity
    request_bounds: ModelRouteRequestBounds
    candidates: tuple[ModelRouteCandidateIdentity, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_chain_digest(self) -> ModelRouteChainIdentity:
        """候选 ordinal 与 chain-id preimage 必须在读取 current config 前闭合。"""

        if self.candidate_count != len(self.candidates) or [
            item.ordinal for item in self.candidates
        ] != list(range(1, self.candidate_count + 1)):
            raise ValueError("route chain candidate shape is invalid")
        payload: dict[str, object] = {
            "schema_version": "model-route-chain-id-v1",
            "capability": self.capability,
            "candidate_count": self.candidate_count,
            "agent_model_policy": self.agent_model_policy.model_dump(mode="json"),
            "request_bounds": self.request_bounds.model_dump(mode="json"),
            "candidates": [
                {
                    **candidate.model_dump(
                        mode="json",
                        exclude={"reserved_cost_bound"},
                    ),
                    "reserved_cost_bound": (
                        None
                        if candidate.reserved_cost_bound is None
                        else canonical_decimal(candidate.reserved_cost_bound)
                    ),
                }
                for candidate in self.candidates
            ],
        }
        if model_route_digest(payload) != self.chain_id:
            raise ValueError("route chain digest does not match canonical identity")
        return self


def validate_route_identity_digest(identity: _CanonicalRouteIdentity, digest: str) -> None:
    """对持久化摘要做 constant-shape exact 校验；不接受错误 Unicode 转义摘要。"""

    if identity.digest() != digest:
        raise ValueError("route identity digest does not match canonical bytes")


def model_route_operation_identity_digest(
    *,
    tenant_id: str,
    run_id: str,
    agent_id: str,
    request_id: str | None,
    trace_id: str | None,
    operation_key: str,
) -> str:
    """按冻结 bound 上下文与原始语义槽位计算 chain continuation identity。"""

    fields = (
        "model-route-chain-operation-v1",
        tenant_id,
        run_id,
        agent_id,
        request_id or "",
        trace_id or "",
        operation_key,
    )
    if not operation_key:
        raise ValueError("route-chain operation key must not be empty")
    return hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()


__all__ = [
    "ModelRouteApprovalGrantIdentity",
    "ModelRouteApprovalRequestIdentity",
    "ModelRouteAttemptIdentity",
    "ModelRouteCandidateIdentity",
    "ModelRouteChainIdentity",
    "ModelRouteNotStartedProofIdentity",
    "model_route_operation_identity_digest",
    "validate_route_identity_digest",
]
