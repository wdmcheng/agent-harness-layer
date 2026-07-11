"""人工 harness decision、policy 与 audit 的原子 acceptance 门禁。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.contracts.trust import GuardrailDecisionStatus
from agent_harness.evals.errors import EvalExperimentError
from agent_harness.evals.experiments import ExperimentService
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyCheck, PolicyEngine
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import (
    AuditLogCreate,
    ExperimentStorageConcurrentConflict,
    ExperimentStorageConflict,
    HarnessAcceptanceCreate,
    HarnessAcceptanceRecord,
    SQLAlchemyStorage,
)


class ExperimentAcceptanceRequest(HarnessDTO):
    request_id: str
    decision: Literal["accepted", "rejected"]
    reason: str = Field(min_length=1)
    accepted_harness_version: str | None = None
    followup_issue_ref: str | None = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("acceptance reason must not be blank")
        if "[REDACTED]" in normalized or redact_secrets(normalized) != normalized:
            raise ValueError("acceptance reason contains secret-shaped data")
        return normalized

    @field_validator("followup_issue_ref")
    @classmethod
    def validate_followup_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if (
            not normalized
            or Path(normalized).is_absolute()
            or PureWindowsPath(normalized).is_absolute()
            or normalized.casefold().startswith("file://")
            or "[REDACTED]" in normalized
            or redact_secrets(normalized) != normalized
        ):
            raise ValueError("followup_issue_ref must be a safe logical reference")
        return normalized

    @model_validator(mode="after")
    def validate_version(self) -> ExperimentAcceptanceRequest:
        if self.decision == "accepted" and self.accepted_harness_version is None:
            raise ValueError("accepted decision requires accepted_harness_version")
        if self.decision == "rejected" and self.accepted_harness_version is not None:
            raise ValueError("rejected decision cannot include accepted_harness_version")
        return self


class ExperimentAcceptanceResult(HarnessDTO):
    request_id: str
    experiment_id: str
    decision_id: str
    decision: Literal["accepted", "rejected"]
    reviewer_id: str
    accepted_harness_version: str | None = None
    production_binding: bool
    policy_decision: dict[str, Any]
    audit_ref: str
    evidence_refs: list[str]
    followup_issue_ref: str | None = None


class _ConcurrentAcceptanceReplay(RuntimeError):
    def __init__(self, record: HarnessAcceptanceRecord) -> None:
        super().__init__("concurrent acceptance already committed")
        self.record = record


class AcceptanceService:
    """comparison 只给建议；本服务才允许人工写唯一 production binding。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        experiments: ExperimentService,
        policy: PolicyEngine,
    ) -> None:
        self.storage = storage
        self.experiments = experiments
        self.policy = policy

    async def decide(
        self,
        *,
        actor: IdentityContext,
        experiment_id: str,
        request: ExperimentAcceptanceRequest,
    ) -> ExperimentAcceptanceResult:
        decision_hash = _decision_hash(actor, request)
        existing = await self._existing(actor.tenant_id, experiment_id)
        if existing is not None:
            return _replay_or_conflict(existing, decision_hash, request.request_id)

        comparison = await self.experiments.compare(
            tenant_id=actor.tenant_id,
            experiment_id=experiment_id,
            request_id=request.request_id,
        )
        if request.decision == "accepted":
            if request.accepted_harness_version != comparison.candidate_harness_version:
                raise EvalExperimentError(
                    "eval.experiment.accepted_version_mismatch",
                    "accepted harness version does not match compared candidate",
                    status_code=409,
                    field_path="accepted_harness_version",
                )
            if comparison.acceptance_recommendation != "accept":
                raise EvalExperimentError(
                    "eval.experiment.acceptance_gate_failed",
                    "comparison does not satisfy the acceptance gate",
                    status_code=409,
                    hint=", ".join(comparison.recommendation_reason_codes),
                )

        policy = await self.policy.evaluate(
            PolicyCheck(
                actor=actor,
                action="eval.harness.accept",
                resource=f"eval-experiment:{experiment_id}",
                context={
                    "decision": request.decision,
                    "candidate_harness_version": comparison.candidate_harness_version,
                    "recommendation": comparison.acceptance_recommendation,
                    "reason_codes": comparison.recommendation_reason_codes,
                },
            )
        )
        if policy.decision == GuardrailDecisionStatus.DENY.value:
            raise EvalExperimentError(
                "eval.experiment.policy_denied",
                "policy denied the harness decision",
                status_code=403,
            )
        if policy.decision == GuardrailDecisionStatus.REQUIRE_APPROVAL.value:
            raise EvalExperimentError(
                "eval.experiment.approval_required",
                "policy requires a separate approval; nested approval was not created",
                status_code=409,
            )

        policy_payload = redact_secrets(policy.to_payload())
        evidence_refs = sorted(set(comparison.local_evidence_refs))
        binding = (
            None
            if request.decision == "rejected"
            else {
                "experiment_id": experiment_id,
                "agent_id": (
                    await self.experiments.get(
                        tenant_id=actor.tenant_id,
                        experiment_id=experiment_id,
                        request_id=request.request_id,
                    )
                ).agent_id,
                "version_id": request.accepted_harness_version,
            }
        )
        try:
            async with self.storage.uow() as uow:
                # 并发前再次检查；savepoint 保证唯一冲突时 loser audit 一并回滚。
                existing = await uow.harness_acceptance_records.get_for_experiment(
                    actor.tenant_id, experiment_id
                )
                if existing is not None:
                    return _replay_or_conflict(existing, decision_hash, request.request_id)
                try:
                    async with uow.session.begin_nested():
                        audit = await uow.audit_logs.create(
                            AuditLogCreate(
                                tenant_id=actor.tenant_id,
                                actor_user_id=actor.user_id,
                                action=f"eval.harness.{request.decision}",
                                resource=f"eval-experiment:{experiment_id}",
                                payload={
                                    "decision": request.decision,
                                    "reviewer_id": actor.user_id,
                                    "reason": request.reason,
                                    "followup_issue_ref": request.followup_issue_ref,
                                    "candidate_harness_version": (
                                        comparison.candidate_harness_version
                                    ),
                                    "accepted_harness_version": request.accepted_harness_version,
                                    "policy_decision": policy_payload,
                                    "evidence_refs": evidence_refs,
                                },
                            )
                        )
                        audit_ref = f"audit://{audit.id}"
                        record = await uow.harness_acceptance_records.create(
                            HarnessAcceptanceCreate(
                                tenant_id=actor.tenant_id,
                                experiment_id=experiment_id,
                                decision_request_hash=decision_hash,
                                reviewer_id=actor.user_id,
                                reason=request.reason,
                                decision=request.decision,
                                accepted_harness_version=request.accepted_harness_version,
                                production_binding=binding,
                                policy_decision=policy_payload,
                                audit_ref=audit_ref,
                                evidence_refs=evidence_refs,
                                followup_issue_ref=request.followup_issue_ref,
                            )
                        )
                        if record.audit_ref != audit_ref:
                            # repository 在 audit flush 后看到了 winner；抛出私有信号
                            # 回滚 savepoint，避免 loser audit 成为孤立记录。
                            raise _ConcurrentAcceptanceReplay(record)
                except _ConcurrentAcceptanceReplay as replay:
                    return _replay_or_conflict(replay.record, decision_hash, request.request_id)
                await uow.commit()
        except ExperimentStorageConflict as exc:
            raise EvalExperimentError(
                exc.code,
                "experiment already has another review decision",
                status_code=409,
            ) from exc
        except ExperimentStorageConcurrentConflict as exc:
            # 唯一约束的 winner 已提交后，loser 回读同一持久化 decision。
            winner = await self._existing(actor.tenant_id, experiment_id)
            if winner is None:
                raise EvalExperimentError(
                    "eval.experiment.decision_conflict",
                    "concurrent review decision could not be reconciled",
                    status_code=409,
                ) from exc
            return _replay_or_conflict(winner, decision_hash, request.request_id)
        return _acceptance_result(record, request_id=request.request_id)

    async def _existing(self, tenant_id: str, experiment_id: str) -> HarnessAcceptanceRecord | None:
        async with self.storage.uow() as uow:
            return await uow.harness_acceptance_records.get_for_experiment(tenant_id, experiment_id)


def _decision_hash(actor: IdentityContext, request: ExperimentAcceptanceRequest) -> str:
    payload = request.to_payload()
    payload.pop("request_id", None)
    payload["reviewer_id"] = actor.user_id
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _replay_or_conflict(
    record: HarnessAcceptanceRecord,
    decision_hash: str,
    request_id: str,
) -> ExperimentAcceptanceResult:
    if record.decision_request_hash != decision_hash:
        raise EvalExperimentError(
            "eval.experiment.decision_conflict",
            "experiment already has another immutable review decision",
            status_code=409,
        )
    return _acceptance_result(record, request_id=request_id)


def _acceptance_result(
    record: HarnessAcceptanceRecord, *, request_id: str
) -> ExperimentAcceptanceResult:
    return ExperimentAcceptanceResult(
        request_id=request_id,
        experiment_id=record.experiment_id,
        decision_id=record.acceptance_id,
        decision=record.decision,
        reviewer_id=record.reviewer_id,
        accepted_harness_version=record.accepted_harness_version,
        production_binding=record.production_binding is not None,
        policy_decision=record.policy_decision,
        audit_ref=record.audit_ref,
        evidence_refs=record.evidence_refs,
        followup_issue_ref=record.followup_issue_ref,
    )
