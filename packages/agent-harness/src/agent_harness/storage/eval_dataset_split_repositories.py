"""可复现 dataset split DTO 与 repository。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.eval_experiment_models import EvalDatasetSplitModel
from agent_harness.storage.eval_experiment_repositories import (
    ExperimentStorageConcurrentConflict,
    ExperimentStorageConflict,
)


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
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ExperimentStorageConcurrentConflict(
                "eval.split.concurrent_conflict",
                "concurrent split create must be reconciled",
            ) from exc
        return _split_record(model)

    async def get(self, tenant_id: str, split_id: str) -> EvalDatasetSplitRecord | None:
        model = await self._session.get(EvalDatasetSplitModel, split_id)
        if model is None or model.tenant_id != tenant_id:
            return None
        return _split_record(model)


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
