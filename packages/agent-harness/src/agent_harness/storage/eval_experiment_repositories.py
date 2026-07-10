"""Phase 12.5 eval experiment DTO 与 tenant-scoped repositories。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.eval_experiment_models import (
    EvalDatasetSplitModel,
    EvalExperimentModel,
    HarnessAcceptanceModel,
)


class ExperimentStorageConflict(RuntimeError):
    """持久化幂等键或不可变 decision 与既有记录冲突。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ExperimentStorageNotFound(LookupError):
    """请求 tenant 不可见的 split/experiment 关联。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _empty_provider_statuses() -> list[dict[str, object]]:
    return []


class EvalDatasetSplitCreate(HarnessDTO):
    """写入可复现 split membership 的 provider-neutral 输入。"""

    split_id: str
    tenant_id: str
    agent_id: str
    dataset: str
    request_id: str
    tags: list[str]
    strategy: str
    optimization_ratio: float
    holdout_ratio: float
    regression_policy: dict[str, Any] = Field(default_factory=dict)
    case_tags: dict[str, list[str]] = Field(default_factory=dict)
    optimization_case_ids: list[str]
    holdout_case_ids: list[str]
    regression_case_ids: list[str]
    tag_distribution: dict[str, Any] = Field(default_factory=dict)
    rejected_counts: dict[str, int] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class EvalDatasetSplitRecord(EvalDatasetSplitCreate):
    """不含完整 case payload 的持久化 split。"""

    optimization_case_count: int
    holdout_case_count: int
    regression_case_count: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvalExperimentCreate(HarnessDTO):
    """创建 baseline 或 baseline/candidate experiment 的持久化输入。"""

    tenant_id: str
    idempotency_key: str
    request_hash: str
    request_id: str
    agent_id: str
    dataset: str
    split_id: str
    status: str = "created"
    evaluator_profile: dict[str, Any]
    metric_versions: dict[str, str]
    baseline_harness: dict[str, Any]
    candidate_harness: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalExperimentRecord(EvalExperimentCreate):
    """实验状态、结果与 local-first evidence 记录。"""

    experiment_id: str
    baseline_run_ref: str | None = None
    candidate_run_ref: str | None = None
    score_summaries: dict[str, Any] = Field(default_factory=dict)
    comparison: dict[str, Any] = Field(default_factory=dict)
    local_refs: list[str] = Field(default_factory=list)
    provider_statuses: list[dict[str, object]] = Field(default_factory=_empty_provider_statuses)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class HarnessAcceptanceCreate(HarnessDTO):
    """人工 accepted/rejected review decision 的持久化输入。"""

    tenant_id: str
    experiment_id: str
    decision_request_hash: str
    reviewer_id: str
    reason: str
    decision: Literal["accepted", "rejected"]
    accepted_harness_version: str | None = None
    production_binding: dict[str, Any] | None = None
    policy_decision: dict[str, Any]
    audit_ref: str
    evidence_refs: list[str] = Field(default_factory=list)
    followup_issue_ref: str | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> HarnessAcceptanceCreate:
        if self.decision == "accepted" and (
            self.accepted_harness_version is None or self.production_binding is None
        ):
            raise ValueError("accepted decision requires harness version and production binding")
        if self.decision == "rejected" and (
            self.accepted_harness_version is not None or self.production_binding is not None
        ):
            raise ValueError("rejected decision cannot create a production binding")
        return self


class HarnessAcceptanceRecord(HarnessAcceptanceCreate):
    """每个 experiment 唯一且不可变的人工 decision。"""

    acceptance_id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


def _split_record(model: EvalDatasetSplitModel) -> EvalDatasetSplitRecord:
    return EvalDatasetSplitRecord(
        split_id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id,
        dataset=model.dataset,
        request_id=model.request_id,
        tags=model.tags_json,
        strategy=model.strategy,
        optimization_ratio=model.optimization_ratio,
        holdout_ratio=model.holdout_ratio,
        regression_policy=model.regression_policy_json,
        case_tags=model.case_tags_json,
        optimization_case_ids=model.optimization_case_ids_json,
        holdout_case_ids=model.holdout_case_ids_json,
        regression_case_ids=model.regression_case_ids_json,
        optimization_case_count=model.optimization_case_count,
        holdout_case_count=model.holdout_case_count,
        regression_case_count=model.regression_case_count,
        tag_distribution=model.tag_distribution_json,
        rejected_counts=model.rejected_counts_json,
        evidence_refs=model.evidence_refs_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _experiment_record(model: EvalExperimentModel) -> EvalExperimentRecord:
    return EvalExperimentRecord(
        experiment_id=model.id,
        tenant_id=model.tenant_id,
        idempotency_key=model.idempotency_key,
        request_hash=model.request_hash,
        request_id=model.request_id,
        agent_id=model.agent_id,
        dataset=model.dataset,
        split_id=model.split_id,
        status=model.status,
        evaluator_profile=model.evaluator_profile_json,
        metric_versions=model.metric_versions_json,
        baseline_harness=model.baseline_harness_json,
        candidate_harness=model.candidate_harness_json,
        baseline_run_ref=model.baseline_run_ref,
        candidate_run_ref=model.candidate_run_ref,
        score_summaries=model.score_summaries_json,
        comparison=model.comparison_json,
        local_refs=model.local_refs_json,
        provider_statuses=model.provider_status_json,
        metadata=model.metadata_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _acceptance_record(model: HarnessAcceptanceModel) -> HarnessAcceptanceRecord:
    return HarnessAcceptanceRecord(
        acceptance_id=model.id,
        tenant_id=model.tenant_id,
        experiment_id=model.experiment_id,
        decision_request_hash=model.decision_request_hash,
        reviewer_id=model.reviewer_id,
        reason=model.reason,
        decision=cast(Literal["accepted", "rejected"], model.decision),
        accepted_harness_version=model.accepted_harness_version,
        production_binding=model.production_binding_json,
        policy_decision=model.policy_decision_json,
        audit_ref=model.audit_ref,
        evidence_refs=model.evidence_refs_json,
        followup_issue_ref=model.followup_issue_ref,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class EvalDatasetSplitRepository:
    """split membership repository，不读取或返回 case payload。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: EvalDatasetSplitCreate) -> EvalDatasetSplitRecord:
        existing = await self._session.get(EvalDatasetSplitModel, data.split_id)
        if existing is not None:
            if not _split_matches(existing, data):
                raise ExperimentStorageConflict(
                    "eval.split.id_conflict", "split id was already used with other membership"
                )
            return _split_record(existing)
        model = EvalDatasetSplitModel(
            id=data.split_id,
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            dataset=data.dataset,
            request_id=data.request_id,
            tags_json=data.tags,
            strategy=data.strategy,
            optimization_ratio=data.optimization_ratio,
            holdout_ratio=data.holdout_ratio,
            regression_policy_json=data.regression_policy,
            case_tags_json=data.case_tags,
            optimization_case_ids_json=data.optimization_case_ids,
            holdout_case_ids_json=data.holdout_case_ids,
            regression_case_ids_json=data.regression_case_ids,
            optimization_case_count=len(data.optimization_case_ids),
            holdout_case_count=len(data.holdout_case_ids),
            regression_case_count=len(data.regression_case_ids),
            tag_distribution_json=data.tag_distribution,
            rejected_counts_json=data.rejected_counts,
            evidence_refs_json=data.evidence_refs,
        )
        self._session.add(model)
        await self._session.flush()
        return _split_record(model)

    async def get(self, tenant_id: str, split_id: str) -> EvalDatasetSplitRecord | None:
        model = await self._session.get(EvalDatasetSplitModel, split_id)
        if model is None or model.tenant_id != tenant_id:
            return None
        return _split_record(model)


class EvalExperimentRepository:
    """tenant-scoped experiment create/read/result update。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: EvalExperimentCreate) -> EvalExperimentRecord:
        result = await self._session.scalars(
            select(EvalExperimentModel).where(
                EvalExperimentModel.tenant_id == data.tenant_id,
                EvalExperimentModel.idempotency_key == data.idempotency_key,
            )
        )
        existing = result.first()
        if existing is not None:
            if not _experiment_matches(existing, data):
                raise ExperimentStorageConflict(
                    "eval.experiment.idempotency_conflict",
                    "idempotency key was already used with another request",
                )
            return _experiment_record(existing)
        split = await self._session.get(EvalDatasetSplitModel, data.split_id)
        if (
            split is None
            or split.tenant_id != data.tenant_id
            or split.agent_id != data.agent_id
            or split.dataset != data.dataset
        ):
            raise ExperimentStorageNotFound(
                "eval.experiment.split_not_found",
                "eval dataset split is not visible",
            )
        model = EvalExperimentModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            idempotency_key=data.idempotency_key,
            request_hash=data.request_hash,
            request_id=data.request_id,
            agent_id=data.agent_id,
            dataset=data.dataset,
            split_id=data.split_id,
            status=data.status,
            evaluator_profile_json=data.evaluator_profile,
            metric_versions_json=data.metric_versions,
            baseline_harness_json=data.baseline_harness,
            candidate_harness_json=data.candidate_harness,
            score_summaries_json={},
            comparison_json={},
            local_refs_json=[],
            provider_status_json=[],
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _experiment_record(model)

    async def get(self, tenant_id: str, experiment_id: str) -> EvalExperimentRecord | None:
        model = await self._session.get(EvalExperimentModel, experiment_id)
        if model is None or model.tenant_id != tenant_id:
            return None
        return _experiment_record(model)

    async def update_results(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        status: str,
        baseline_run_ref: str | None,
        candidate_run_ref: str | None,
        score_summaries: dict[str, Any],
        comparison: dict[str, Any],
        local_refs: list[str],
        provider_statuses: list[dict[str, object]],
    ) -> EvalExperimentRecord:
        model = await self._session.get(EvalExperimentModel, experiment_id)
        if model is None or model.tenant_id != tenant_id:
            raise ExperimentStorageNotFound(
                "eval.experiment.not_found", "eval experiment is not visible"
            )
        model.status = status
        model.baseline_run_ref = baseline_run_ref
        model.candidate_run_ref = candidate_run_ref
        model.score_summaries_json = score_summaries
        model.comparison_json = comparison
        model.local_refs_json = local_refs
        model.provider_status_json = provider_statuses
        await self._session.flush()
        # server-side updated_at 会在 UPDATE 后过期；refresh 避免 async ORM 在
        # DTO 转换时触发隐式 IO 和 MissingGreenlet。
        await self._session.refresh(model)
        return _experiment_record(model)


class HarnessAcceptanceRepository:
    """同一 experiment 的 decision 只允许一次，完全相同请求可重放。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: HarnessAcceptanceCreate) -> HarnessAcceptanceRecord:
        result = await self._session.scalars(
            select(HarnessAcceptanceModel).where(
                HarnessAcceptanceModel.tenant_id == data.tenant_id,
                HarnessAcceptanceModel.experiment_id == data.experiment_id,
            )
        )
        existing = result.first()
        if existing is not None:
            if not _acceptance_matches(existing, data):
                raise ExperimentStorageConflict(
                    "eval.experiment.decision_conflict",
                    "experiment already has another immutable review decision",
                )
            return _acceptance_record(existing)
        experiment = await self._session.get(EvalExperimentModel, data.experiment_id)
        if experiment is None or experiment.tenant_id != data.tenant_id:
            raise ExperimentStorageNotFound(
                "eval.experiment.not_found", "eval experiment is not visible"
            )
        model = HarnessAcceptanceModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            experiment_id=data.experiment_id,
            decision_request_hash=data.decision_request_hash,
            reviewer_id=data.reviewer_id,
            reason=data.reason,
            decision=data.decision,
            accepted_harness_version=data.accepted_harness_version,
            production_binding_json=data.production_binding,
            policy_decision_json=data.policy_decision,
            audit_ref=data.audit_ref,
            evidence_refs_json=data.evidence_refs,
            followup_issue_ref=data.followup_issue_ref,
        )
        self._session.add(model)
        await self._session.flush()
        return _acceptance_record(model)

    async def get_for_experiment(
        self, tenant_id: str, experiment_id: str
    ) -> HarnessAcceptanceRecord | None:
        result = await self._session.scalars(
            select(HarnessAcceptanceModel).where(
                HarnessAcceptanceModel.tenant_id == tenant_id,
                HarnessAcceptanceModel.experiment_id == experiment_id,
            )
        )
        model = result.first()
        return None if model is None else _acceptance_record(model)


def _split_matches(model: EvalDatasetSplitModel, data: EvalDatasetSplitCreate) -> bool:
    return (
        model.tenant_id == data.tenant_id
        and model.agent_id == data.agent_id
        and model.dataset == data.dataset
        and model.tags_json == data.tags
        and model.strategy == data.strategy
        and model.optimization_ratio == data.optimization_ratio
        and model.holdout_ratio == data.holdout_ratio
        and model.regression_policy_json == data.regression_policy
        and model.case_tags_json == data.case_tags
        and model.optimization_case_ids_json == data.optimization_case_ids
        and model.holdout_case_ids_json == data.holdout_case_ids
        and model.regression_case_ids_json == data.regression_case_ids
        and model.tag_distribution_json == data.tag_distribution
        and model.rejected_counts_json == data.rejected_counts
        and model.evidence_refs_json == data.evidence_refs
    )


def _experiment_matches(model: EvalExperimentModel, data: EvalExperimentCreate) -> bool:
    return (
        model.request_hash == data.request_hash
        and model.agent_id == data.agent_id
        and model.dataset == data.dataset
        and model.split_id == data.split_id
        and model.evaluator_profile_json == data.evaluator_profile
        and model.metric_versions_json == data.metric_versions
        and model.baseline_harness_json == data.baseline_harness
        and model.candidate_harness_json == data.candidate_harness
        and model.metadata_json == data.metadata
    )


def _acceptance_matches(
    model: HarnessAcceptanceModel, data: HarnessAcceptanceCreate
) -> bool:
    return (
        model.decision_request_hash == data.decision_request_hash
        and model.reviewer_id == data.reviewer_id
        and model.reason == data.reason
        and model.decision == data.decision
        and model.accepted_harness_version == data.accepted_harness_version
        and model.followup_issue_ref == data.followup_issue_ref
    )
