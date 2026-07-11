"""Eval experiment DTO 与 tenant-scoped repositories。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.eval_experiment_models import EvalDatasetSplitModel, EvalExperimentModel


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


class ExperimentStorageConcurrentConflict(RuntimeError):
    """唯一约束并发 loser；application service 可在新 UoW 回读 winner。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _empty_provider_statuses() -> list[dict[str, object]]:
    return []


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
    execution_claim_id: str | None = None
    execution_claim_expires_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


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
        execution_claim_id=model.execution_claim_id,
        execution_claim_expires_at=model.execution_claim_expires_at,
        metadata=model.metadata_json,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


class EvalExperimentRepository:
    """tenant-scoped experiment create/read/result update。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: EvalExperimentCreate) -> EvalExperimentRecord:
        record, _created = await self.create_with_status(data)
        return record

    async def create_with_status(
        self, data: EvalExperimentCreate
    ) -> tuple[EvalExperimentRecord, bool]:
        """创建 experiment，并把幂等 replay 与本次新建显式区分。"""

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
            return _experiment_record(existing), False
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
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ExperimentStorageConcurrentConflict(
                "eval.experiment.idempotency_conflict",
                "concurrent experiment create must be reconciled",
            ) from exc
        return _experiment_record(model), True

    async def get_by_idempotency_key(
        self, tenant_id: str, idempotency_key: str
    ) -> EvalExperimentRecord | None:
        """在执行 evaluator 前检查持久化幂等记录，避免 replay side effect。"""

        result = await self._session.scalars(
            select(EvalExperimentModel).where(
                EvalExperimentModel.tenant_id == tenant_id,
                EvalExperimentModel.idempotency_key == idempotency_key,
            )
        )
        model = result.first()
        return None if model is None else _experiment_record(model)

    async def get(self, tenant_id: str, experiment_id: str) -> EvalExperimentRecord | None:
        model = await self._session.get(EvalExperimentModel, experiment_id)
        if model is None or model.tenant_id != tenant_id:
            return None
        return _experiment_record(model)

    async def claim_execution(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        claim_id: str,
        expires_at: datetime,
    ) -> bool:
        """只原子 claim 尚未开始执行的 created experiment。"""

        claimed_id = await self._session.scalar(
            update(EvalExperimentModel)
            .where(
                EvalExperimentModel.tenant_id == tenant_id,
                EvalExperimentModel.id == experiment_id,
                EvalExperimentModel.status == "created",
            )
            .values(
                status="running",
                execution_claim_id=claim_id,
                execution_claim_expires_at=expires_at,
            )
            .returning(EvalExperimentModel.id)
        )
        return claimed_id is not None

    async def renew_execution_claim(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        claim_id: str,
        expires_at: datetime,
    ) -> bool:
        renewed_id = await self._session.scalar(
            update(EvalExperimentModel)
            .where(
                EvalExperimentModel.tenant_id == tenant_id,
                EvalExperimentModel.id == experiment_id,
                EvalExperimentModel.status == "running",
                EvalExperimentModel.execution_claim_id == claim_id,
                EvalExperimentModel.execution_claim_expires_at.is_not(None),
                EvalExperimentModel.execution_claim_expires_at > datetime.now(tz=UTC),
            )
            .values(execution_claim_expires_at=expires_at)
            .returning(EvalExperimentModel.id)
        )
        return renewed_id is not None

    async def mark_execution_needs_review(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        claim_id: str,
        reason_code: str,
    ) -> bool:
        """当前 owner 无法证明执行结果时，fenced 地转入人工复核并禁止自动重跑。"""

        marked_id = await self._session.scalar(
            update(EvalExperimentModel)
            .where(
                EvalExperimentModel.tenant_id == tenant_id,
                EvalExperimentModel.id == experiment_id,
                EvalExperimentModel.status == "running",
                EvalExperimentModel.execution_claim_id == claim_id,
            )
            .values(
                status="needs_review",
                score_summaries_json={"error": {"code": reason_code}},
                execution_claim_id=None,
                execution_claim_expires_at=None,
            )
            .returning(EvalExperimentModel.id)
        )
        return marked_id is not None

    async def mark_expired_execution_needs_review(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        now: datetime,
    ) -> bool:
        """过期 running 可能已经产生外部副作用，只能转人工复核而不能 takeover。"""

        marked_id = await self._session.scalar(
            update(EvalExperimentModel)
            .where(
                EvalExperimentModel.tenant_id == tenant_id,
                EvalExperimentModel.id == experiment_id,
                EvalExperimentModel.status == "running",
                EvalExperimentModel.execution_claim_expires_at.is_not(None),
                EvalExperimentModel.execution_claim_expires_at <= now,
            )
            .values(
                status="needs_review",
                score_summaries_json={
                    "error": {"code": "eval.experiment.execution_outcome_uncertain"}
                },
                execution_claim_id=None,
                execution_claim_expires_at=None,
            )
            .returning(EvalExperimentModel.id)
        )
        return marked_id is not None

    async def mark_unclaimed_execution_needs_review(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
    ) -> bool:
        """旧版可见 created 无法证明 evaluator 未启动，保守转人工复核。"""

        marked_id = await self._session.scalar(
            update(EvalExperimentModel)
            .where(
                EvalExperimentModel.tenant_id == tenant_id,
                EvalExperimentModel.id == experiment_id,
                EvalExperimentModel.status == "created",
                EvalExperimentModel.execution_claim_id.is_(None),
            )
            .values(
                status="needs_review",
                score_summaries_json={
                    "error": {"code": "eval.experiment.legacy_execution_outcome_uncertain"}
                },
                execution_claim_expires_at=None,
            )
            .returning(EvalExperimentModel.id)
        )
        return marked_id is not None

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
        execution_claim_id: str,
    ) -> EvalExperimentRecord:
        updated_id = await self._session.scalar(
            update(EvalExperimentModel)
            .where(
                EvalExperimentModel.tenant_id == tenant_id,
                EvalExperimentModel.id == experiment_id,
                EvalExperimentModel.status == "running",
                EvalExperimentModel.execution_claim_id == execution_claim_id,
                EvalExperimentModel.execution_claim_expires_at.is_not(None),
                EvalExperimentModel.execution_claim_expires_at > datetime.now(tz=UTC),
            )
            .values(
                status=status,
                baseline_run_ref=baseline_run_ref,
                candidate_run_ref=candidate_run_ref,
                score_summaries_json=score_summaries,
                comparison_json=comparison,
                local_refs_json=local_refs,
                provider_status_json=provider_statuses,
                execution_claim_id=None,
                execution_claim_expires_at=None,
            )
            .returning(EvalExperimentModel.id)
        )
        if updated_id is None:
            await self._raise_result_update_conflict(tenant_id, experiment_id)
        return await self._refreshed_record(experiment_id)

    async def update_provider_results(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        expected_status: str,
        status: str,
        comparison: dict[str, Any],
        provider_statuses: list[dict[str, object]],
    ) -> EvalExperimentRecord:
        """本地终态提交后只允许追加一次 provider 摘要，不再改写 eval evidence。"""

        updated_id = await self._session.scalar(
            update(EvalExperimentModel)
            .where(
                EvalExperimentModel.tenant_id == tenant_id,
                EvalExperimentModel.id == experiment_id,
                EvalExperimentModel.status == expected_status,
                EvalExperimentModel.execution_claim_id.is_(None),
            )
            .values(
                status=status,
                comparison_json=comparison,
                provider_status_json=provider_statuses,
            )
            .returning(EvalExperimentModel.id)
        )
        if updated_id is None:
            await self._raise_result_update_conflict(tenant_id, experiment_id)
        return await self._refreshed_record(experiment_id)

    async def _raise_result_update_conflict(self, tenant_id: str, experiment_id: str) -> None:
        model = await self._session.get(EvalExperimentModel, experiment_id)
        if model is None or model.tenant_id != tenant_id:
            raise ExperimentStorageNotFound(
                "eval.experiment.not_found", "eval experiment is not visible"
            )
        raise ExperimentStorageConflict(
            "eval.experiment.execution_fenced",
            "experiment execution claim is no longer owned by this worker",
        )

    async def _refreshed_record(self, experiment_id: str) -> EvalExperimentRecord:
        model = await self._session.get(EvalExperimentModel, experiment_id)
        if model is None:
            raise AssertionError("updated experiment disappeared in the same transaction")
        await self._session.refresh(model)
        return _experiment_record(model)


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
