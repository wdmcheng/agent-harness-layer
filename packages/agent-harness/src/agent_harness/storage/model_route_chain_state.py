"""显式模型 route chain 的耐久推进状态与封闭 transition 合同。"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class _ExactRouteStateDTO(HarnessDTO):
    """耐久状态必须保留 nullable 字段，不能沿用公共摘要的省略语义。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    def to_payload(self) -> dict[str, Any]:
        """返回包含显式 null 的 exact JSON-compatible shape。"""

        return self.model_dump(mode="json", exclude_none=False)


class ModelRouteNotStartedProof(_ExactRouteStateDTO):
    """一个全局 attempt 的可信零副作用证明。"""

    attempt: int = Field(ge=1, strict=True)
    reason: Literal["client_not_started", "trusted_business_not_started"]
    side_effect_state: Literal["not_started", "started"]
    request_sent: bool
    http_response_observed: bool
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    response_identity_observed: bool
    usage_observed: bool
    text_observed: bool
    delta_observed: bool
    completion_observed: bool | None
    endpoint_policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    classifier_ref: str | None
    classifier_version: str | None
    proof_digest: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def validate_proof_kind(self) -> ModelRouteNotStartedProof:
        """两类证明的观察事实互斥且都禁止响应 identity、usage、文本或 delta。"""

        if any(
            (
                self.response_identity_observed,
                self.usage_observed,
                self.text_observed,
                self.delta_observed,
            )
        ):
            raise ValueError("not-started proof contains a provider result observation")
        if self.reason == "client_not_started":
            if (
                self.side_effect_state != "not_started"
                or self.request_sent
                or self.http_response_observed
                or self.http_status is not None
                or self.completion_observed is not None
                or self.classifier_ref is not None
                or self.classifier_version is not None
            ):
                raise ValueError("client-not-started proof shape is invalid")
        elif (
            self.side_effect_state != "started"
            or not self.request_sent
            or not self.http_response_observed
            or self.http_status is None
            or self.completion_observed is not False
            or not self.classifier_ref
            or not self.classifier_version
        ):
            raise ValueError("trusted-business-not-started proof shape is invalid")
        return self


class ModelRouteAttemptLifecycle(_ExactRouteStateDTO):
    """全链不可删除、不可重排的 provider attempt identity 与单调观察位。"""

    attempt: int = Field(ge=1, strict=True)
    candidate_ordinal: int = Field(ge=1, le=8, strict=True)
    attempt_identity_digest: str = Field(pattern=_DIGEST_PATTERN)
    lifecycle_state: Literal["started", "not_started_proven", "unknown", "settled"]
    side_effect_state: Literal["not_started", "started", "unknown", "result_committed"]
    request_sent: bool
    http_response_observed: bool
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    response_identity_observed: bool
    usage_observed: bool
    text_observed: bool
    delta_observed: bool
    completion_observed: bool | None
    not_started_proof_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def validate_lifecycle_terminal(self) -> ModelRouteAttemptLifecycle:
        """只有 `not_started_proven` 可绑定 proof；started 与其他终态不得伪造证明。"""

        if self.lifecycle_state == "not_started_proven":
            if self.not_started_proof_digest is None:
                raise ValueError("proven lifecycle requires a proof digest")
        elif self.not_started_proof_digest is not None:
            raise ValueError("only proven lifecycle may carry a proof digest")
        if (self.lifecycle_state == "settled") != (self.side_effect_state == "result_committed"):
            raise ValueError("settled lifecycle and result-committed side effect must match")
        return self


class ModelRouteCandidateState(_ExactRouteStateDTO):
    """逐候选聚合状态；attempt 事实仍以 lifecycle/proof 列表为权威。"""

    ordinal: int = Field(ge=1, le=8, strict=True)
    deployment_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    route_digest: str = Field(pattern=_DIGEST_PATTERN)
    state: Literal[
        "pending",
        "active",
        "waiting_approval",
        "static_ineligible",
        "budget_ineligible",
        "not_started",
        "completed",
        "cancelled",
        "denied",
        "unknown",
    ]
    side_effect_state: Literal["not_started", "started", "unknown", "result_committed"]
    reason: str | None
    request_sent: bool
    http_response_observed: bool
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    response_identity_observed: bool
    usage_observed: bool
    text_observed: bool
    delta_observed: bool
    completion_observed: bool | None
    not_started_proofs: tuple[ModelRouteNotStartedProof, ...]
    approval_request_binding_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    approval_grant_binding_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def validate_candidate_state(self) -> ModelRouteCandidateState:
        """普通 skip 与 waiting 保持零 attempt/观察，approval binding 按阶段单调增加。"""

        from agent_harness.storage._model_route_candidate_validation import (
            validate_model_route_candidate_state,
        )

        return validate_model_route_candidate_state(self)


def candidate_has_no_approval_bindings(candidate: ModelRouteCandidateState) -> bool:
    """普通未授权候选不能携带审批摘要。"""

    return (
        candidate.approval_request_binding_digest is None
        and candidate.approval_grant_binding_digest is None
    )


def candidate_has_zero_provider_facts(candidate: ModelRouteCandidateState) -> bool:
    """零副作用候选必须保持初始高水位和空观察事实。"""

    return (
        candidate.side_effect_state == "not_started"
        and not candidate.not_started_proofs
        and not candidate.request_sent
        and not candidate.http_response_observed
        and candidate.http_status is None
        and not candidate.response_identity_observed
        and not candidate.usage_observed
        and not candidate.text_observed
        and not candidate.delta_observed
        and candidate.completion_observed is None
    )


def _candidate_matches_lifecycle_aggregate(
    candidate: ModelRouteCandidateState,
    lifecycles: tuple[ModelRouteAttemptLifecycle, ...],
) -> bool:
    """按权威 lifecycle 推导候选观察高水位，拒绝独立改写的摘要事实。"""

    if not lifecycles:
        return candidate_has_zero_provider_facts(candidate)

    def latest_non_null(field: Literal["http_status", "completion_observed"]) -> int | bool | None:
        for lifecycle in reversed(lifecycles):
            value = getattr(lifecycle, field)
            if value is not None:
                return value
        return None

    if candidate.state in {"completed", "cancelled"}:
        side_effect_state = "result_committed"
    elif candidate.state == "unknown":
        side_effect_state = "unknown"
    elif any(lifecycle.side_effect_state == "started" for lifecycle in lifecycles):
        side_effect_state = "started"
    else:
        side_effect_state = "not_started"

    if candidate.state == "cancelled":
        reason = "invocation_cancelled"
    elif candidate.state == "unknown":
        reason = "provider_side_effect_unknown"
    elif candidate.state == "completed":
        reason = None
    else:
        reason = candidate.not_started_proofs[-1].reason if candidate.not_started_proofs else None

    return (
        candidate.side_effect_state == side_effect_state
        and candidate.reason == reason
        and candidate.request_sent == any(item.request_sent for item in lifecycles)
        and candidate.http_response_observed
        == any(item.http_response_observed for item in lifecycles)
        and candidate.http_status == latest_non_null("http_status")
        and candidate.response_identity_observed
        == any(item.response_identity_observed for item in lifecycles)
        and candidate.usage_observed == any(item.usage_observed for item in lifecycles)
        and candidate.text_observed == any(item.text_observed for item in lifecycles)
        and candidate.delta_observed == any(item.delta_observed for item in lifecycles)
        and candidate.completion_observed == latest_non_null("completion_observed")
    )


class ModelRouteReservation(_ExactRouteStateDTO):
    """当前 owner impact 对应的唯一候选 reservation。"""

    candidate_ordinal: int | None = Field(default=None, ge=1, le=8, strict=True)
    token_bound: int = Field(ge=0, strict=True)
    cost_bound: float | None = None

    @field_validator("cost_bound")
    @classmethod
    def validate_cost_bound(cls, value: float | None) -> float | None:
        """成本必须有限且非负；bool 不能借 float 强制转换通过。"""

        if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value < 0):
            raise ValueError("route reservation cost must be finite and non-negative")
        return value


class ModelRouteTransition(_ExactRouteStateDTO):
    """source-anchor 到目标候选的唯一 reservation 变化 tuple。"""

    sequence: int = Field(ge=1, strict=True)
    from_ordinal: int | None = Field(default=None, ge=1, le=8, strict=True)
    to_ordinal: int | None = Field(default=None, ge=1, le=8, strict=True)
    state: Literal[
        "activated",
        "transferred",
        "waiting_approval",
        "approved",
        "terminated",
    ]
    reason: str
    released_token_bound: int = Field(ge=0, strict=True)
    released_cost_bound: float | None = None
    reserved_token_bound: int = Field(ge=0, strict=True)
    reserved_cost_bound: float | None = None

    @field_validator("released_cost_bound", "reserved_cost_bound")
    @classmethod
    def validate_transition_cost(cls, value: float | None) -> float | None:
        """transition 中所有成本边界都要求有限非负。"""

        if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value < 0):
            raise ValueError("route transition cost must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_transition_tuple(self) -> ModelRouteTransition:
        """封闭关键 transition 的 from/to/reason/bound 组合。"""

        if self.state == "activated" and (
            self.from_ordinal is not None or self.to_ordinal is None or self.reason != "initial"
        ):
            raise ValueError("initial activation tuple is invalid")
        if self.state == "transferred" and (
            self.from_ordinal is None
            or self.to_ordinal is None
            or self.from_ordinal == self.to_ordinal
            or self.reason
            not in {
                "client_not_started",
                "trusted_business_not_started",
                "balance",
            }
        ):
            raise ValueError("route transfer tuple is invalid")
        if self.state == "waiting_approval" and (
            self.to_ordinal is None
            or self.reason != "approval_required"
            or self.reserved_token_bound != 0
            or self.reserved_cost_bound is not None
        ):
            raise ValueError("waiting approval tuple is invalid")
        if self.state == "approved" and (
            self.from_ordinal is None
            or self.from_ordinal != self.to_ordinal
            or self.reason != "approval_granted"
            or self.released_token_bound != 0
            or self.released_cost_bound is not None
        ):
            raise ValueError("approved activation tuple is invalid")
        if self.state == "terminated" and (
            self.to_ordinal is not None
            or self.reason not in {"policy_denied", "route_exhausted"}
            or self.reserved_token_bound != 0
            or self.reserved_cost_bound is not None
        ):
            raise ValueError("route termination tuple is invalid")
        return self


class ModelRouteChainState(_ExactRouteStateDTO):
    """一笔 usage claim/allocation 持有的完整 route-chain 推进事实。"""

    schema_version: Literal["model-route-chain-state-v1"] = "model-route-chain-state-v1"
    chain_id: str = Field(pattern=_DIGEST_PATTERN)
    candidate_count: int = Field(ge=1, le=8, strict=True)
    usage_call_id: str = Field(pattern=_DIGEST_PATTERN)
    operation_identity_digest: str = Field(pattern=_DIGEST_PATTERN)
    active_ordinal: int | None = Field(default=None, ge=1, le=8, strict=True)
    waiting_approval_ordinal: int | None = Field(default=None, ge=1, le=8, strict=True)
    selected_ordinal: int | None = Field(default=None, ge=1, le=8, strict=True)
    evidence_route_ordinal: int = Field(ge=1, le=8, strict=True)
    delta_fenced: bool
    attempt_lifecycle: tuple[ModelRouteAttemptLifecycle, ...]
    current_reservation: ModelRouteReservation
    candidates: tuple[ModelRouteCandidateState, ...] = Field(min_length=1, max_length=8)
    transitions: tuple[ModelRouteTransition, ...]

    @model_validator(mode="after")
    def validate_state_graph(self) -> ModelRouteChainState:
        """校验 ordinal、source anchor、reservation 与 proof/lifecycle 一致性。"""

        if self.candidate_count != len(self.candidates):
            raise ValueError("route-chain candidate count mismatch")
        if [item.ordinal for item in self.candidates] != list(range(1, self.candidate_count + 1)):
            raise ValueError("route-chain candidate ordinals must be continuous")
        if (
            sum(
                ordinal is not None
                for ordinal in (
                    self.active_ordinal,
                    self.waiting_approval_ordinal,
                    self.selected_ordinal,
                )
            )
            > 1
        ):
            raise ValueError("active, waiting, and selected ordinals are mutually exclusive")
        active = [item.ordinal for item in self.candidates if item.state in {"active", "unknown"}]
        waiting = [item.ordinal for item in self.candidates if item.state == "waiting_approval"]
        completed = [item.ordinal for item in self.candidates if item.state == "completed"]
        cancelled = [item.ordinal for item in self.candidates if item.state == "cancelled"]
        if active != ([] if self.active_ordinal is None else [self.active_ordinal]):
            raise ValueError("active candidate does not match active ordinal")
        if waiting != (
            [] if self.waiting_approval_ordinal is None else [self.waiting_approval_ordinal]
        ):
            raise ValueError("waiting candidate does not match waiting ordinal")
        if completed != ([] if self.selected_ordinal is None else [self.selected_ordinal]):
            raise ValueError("completed candidate does not match selected ordinal")
        if cancelled and (
            len(cancelled) != 1
            or completed
            or self.selected_ordinal is not None
            or self.active_ordinal is not None
            or self.waiting_approval_ordinal is not None
            or cancelled[0] != self.evidence_route_ordinal
        ):
            raise ValueError("cancelled candidate does not form the canonical terminal")
        reservation = self.current_reservation
        if self.active_ordinal is None:
            if (
                reservation.candidate_ordinal is not None
                or reservation.token_bound != 0
                or reservation.cost_bound is not None
            ):
                raise ValueError("inactive route chain must have a zero reservation")
        elif reservation.candidate_ordinal != self.active_ordinal:
            raise ValueError("reservation candidate does not match active ordinal")
        if [item.sequence for item in self.transitions] != list(
            range(1, len(self.transitions) + 1)
        ):
            raise ValueError("route transition sequences must be continuous")
        if any(item.state == "activated" and item.sequence != 1 for item in self.transitions):
            raise ValueError("initial activation may only be the first transition")
        if [item.attempt for item in self.attempt_lifecycle] != list(
            range(1, len(self.attempt_lifecycle) + 1)
        ):
            raise ValueError("route attempt lifecycle must be globally continuous")
        for item in self.attempt_lifecycle:
            if item.candidate_ordinal > self.candidate_count:
                raise ValueError("attempt references an unknown candidate")
            candidate = self.candidates[item.candidate_ordinal - 1]
            if item.lifecycle_state == "settled" and candidate.state not in {
                "completed",
                "cancelled",
            }:
                raise ValueError("settled lifecycle requires a terminal result candidate")
            if candidate.state == "cancelled" and (
                item.lifecycle_state != "settled"
                or item.side_effect_state != "result_committed"
                or not item.request_sent
                or item.http_response_observed
                or item.http_status is not None
                or item.response_identity_observed
                or not item.usage_observed
                or item.text_observed
                or item.delta_observed
                or item.completion_observed is not False
            ):
                raise ValueError("cancelled candidate lifecycle is invalid")

        lifecycle_by_candidate = {
            candidate.ordinal: tuple(
                item
                for item in self.attempt_lifecycle
                if item.candidate_ordinal == candidate.ordinal
            )
            for candidate in self.candidates
        }
        no_attempt_states = {
            "pending",
            "waiting_approval",
            "static_ineligible",
            "budget_ineligible",
            "denied",
        }
        for candidate in self.candidates:
            lifecycles = lifecycle_by_candidate[candidate.ordinal]
            if candidate.state in no_attempt_states and lifecycles:
                raise ValueError("zero-impact candidate cannot own an attempt lifecycle")
            if candidate.state == "active":
                if lifecycles and (
                    any(item.lifecycle_state != "not_started_proven" for item in lifecycles[:-1])
                    or lifecycles[-1].lifecycle_state not in {"started", "not_started_proven"}
                ):
                    raise ValueError("active candidate lifecycle history is invalid")
                if not lifecycles and not candidate_has_zero_provider_facts(candidate):
                    raise ValueError("active candidate facts require an attempt lifecycle")
            if candidate.state == "not_started" and (
                not lifecycles
                or any(item.lifecycle_state != "not_started_proven" for item in lifecycles)
            ):
                raise ValueError("not-started candidate requires proven lifecycles")
            if candidate.state == "unknown" and (
                not lifecycles
                or lifecycles[-1].lifecycle_state != "unknown"
                or any(item.lifecycle_state != "not_started_proven" for item in lifecycles[:-1])
            ):
                raise ValueError("unknown candidate lifecycle history is invalid")
            if lifecycles and not _candidate_matches_lifecycle_aggregate(candidate, lifecycles):
                raise ValueError("candidate aggregate does not match its attempt lifecycles")

        for candidate in self.candidates:
            if candidate.state != "budget_ineligible":
                continue
            has_bindings = not candidate_has_no_approval_bindings(candidate)
            waiting = [
                item
                for item in self.transitions
                if item.state == "waiting_approval" and item.to_ordinal == candidate.ordinal
            ]
            approved = [
                item
                for item in self.transitions
                if item.state == "approved" and item.to_ordinal == candidate.ordinal
            ]
            if has_bindings and (candidate.reason != "balance" or len(waiting) != 1 or approved):
                raise ValueError("approved balance skip lacks its canonical waiting history")
            if not has_bindings and waiting:
                raise ValueError("ordinary budget skip cannot carry waiting history")

        terminal_ordinals = completed + cancelled
        if terminal_ordinals:
            terminal_ordinal = terminal_ordinals[0]
            terminal_attempts = [
                item
                for item in self.attempt_lifecycle
                if item.candidate_ordinal == terminal_ordinal
            ]
            if (
                not terminal_attempts
                or terminal_attempts[-1].lifecycle_state != "settled"
                or any(
                    item.lifecycle_state != "not_started_proven" for item in terminal_attempts[:-1]
                )
                or any(
                    item.candidate_ordinal != terminal_ordinal
                    and item.lifecycle_state != "not_started_proven"
                    for item in self.attempt_lifecycle
                )
            ):
                raise ValueError("terminal route chain contains an unresolved lifecycle")

        proof_by_attempt: dict[int, tuple[int, ModelRouteNotStartedProof]] = {}
        for candidate in self.candidates:
            previous = 0
            for proof in candidate.not_started_proofs:
                if proof.attempt <= previous or proof.attempt in proof_by_attempt:
                    raise ValueError("candidate proof attempts must be strictly ordered")
                previous = proof.attempt
                proof_by_attempt[proof.attempt] = (candidate.ordinal, proof)
        lifecycle_by_attempt = {item.attempt: item for item in self.attempt_lifecycle}
        if set(proof_by_attempt) != {
            attempt
            for attempt, lifecycle in lifecycle_by_attempt.items()
            if lifecycle.lifecycle_state == "not_started_proven"
        }:
            raise ValueError("candidate proofs and lifecycle terminals are not one-to-one")
        for attempt, (candidate_ordinal, proof) in proof_by_attempt.items():
            lifecycle = lifecycle_by_attempt[attempt]
            if (
                lifecycle.candidate_ordinal != candidate_ordinal
                or lifecycle.not_started_proof_digest != proof.proof_digest
                or lifecycle.side_effect_state != proof.side_effect_state
                or lifecycle.request_sent != proof.request_sent
                or lifecycle.http_response_observed != proof.http_response_observed
                or lifecycle.http_status != proof.http_status
                or lifecycle.response_identity_observed != proof.response_identity_observed
                or lifecycle.usage_observed != proof.usage_observed
                or lifecycle.text_observed != proof.text_observed
                or lifecycle.delta_observed != proof.delta_observed
                or lifecycle.completion_observed != proof.completion_observed
            ):
                raise ValueError("proof does not match its attempt lifecycle")
        return self


def route_chain_can_start_active_candidate(state: ModelRouteChainState) -> bool:
    """判断耐久 chain 是否只缺当前 active 候选的全新 attempt identity。

    初始候选、已原子 transfer 的 successor，或当前候选仅有已 proof-close 的历史
    attempt时可以恢复下一次调用；前序未关闭、未来候选、unknown/settled或delta均失败。
    """

    from agent_harness.storage._model_route_chain_recovery import (
        route_chain_can_start_active_candidate as can_start,
    )

    return can_start(state)


__all__ = [
    "ModelRouteAttemptLifecycle",
    "ModelRouteCandidateState",
    "ModelRouteChainState",
    "ModelRouteNotStartedProof",
    "ModelRouteReservation",
    "ModelRouteTransition",
    "route_chain_can_start_active_candidate",
]
