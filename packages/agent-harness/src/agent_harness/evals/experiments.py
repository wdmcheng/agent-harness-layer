"""同一 split 上 baseline/candidate experiment 的 local-first 应用服务。"""

from __future__ import annotations

from agent_harness.evals.comparison import ExperimentComparisonBuilder
from agent_harness.evals.dataset_models import DatasetSplitRequest
from agent_harness.evals.datasets import DatasetSplitService
from agent_harness.evals.errors import EvalExperimentError
from agent_harness.evals.experiment_execution import ExperimentExecutionCoordinator
from agent_harness.evals.experiment_models import (
    ExperimentComparison,
    ExperimentCreateOutcome,
    ExperimentCreateRequest,
    ExperimentEvaluator,
    ExperimentEvidencePublisher,
    ExperimentRequest,
    ExperimentResult,
)
from agent_harness.evals.experiment_records import (
    create_request_hash,
    experiment_request_from_create,
    result_from_record,
)
from agent_harness.storage import EvalDatasetSplitCreate, EvalDatasetSplitRecord, SQLAlchemyStorage


class ExperimentService:
    """创建冻结 split，并把执行租约状态机委托给独立协调器。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        evaluator: ExperimentEvaluator,
        publishers: list[ExperimentEvidencePublisher] | None = None,
        comparison_builder: ExperimentComparisonBuilder | None = None,
        execution_claim_ttl_seconds: float = 30.0,
    ) -> None:
        self.storage = storage
        self.execution = ExperimentExecutionCoordinator(
            storage=storage,
            evaluator=evaluator,
            publishers=publishers or [],
            comparison_builder=comparison_builder or ExperimentComparisonBuilder(),
            claim_ttl_seconds=execution_claim_ttl_seconds,
        )

    async def create(self, request: ExperimentCreateRequest) -> ExperimentCreateOutcome:
        """从 approved cases 构建不可变 split，并执行或重放 experiment。"""

        hash_value = create_request_hash(request)
        async with self.storage.uow() as uow:
            existing = await uow.eval_experiments.get_by_idempotency_key(
                request.tenant_id, request.idempotency_key
            )
        if existing is not None:
            if existing.request_hash != hash_value:
                raise EvalExperimentError(
                    "eval.experiment.idempotency_conflict",
                    "idempotency key was already used with another request",
                    status_code=409,
                )
            return await self.execution.resume_or_replay(
                request=experiment_request_from_create(request, existing.split_id),
                record=existing,
            )

        async with self.storage.uow() as uow:
            cases = await uow.eval_cases.list(
                tenant_id=request.tenant_id,
                dataset=request.dataset,
                agent_id=request.agent_id,
            )
        split_plan = DatasetSplitService().build(
            DatasetSplitRequest(
                request_id=request.request_id,
                tenant_id=request.tenant_id,
                agent_id=request.agent_id,
                dataset=request.dataset,
                tags=request.tags,
                split_strategy=request.split_strategy,
                optimization_ratio=request.optimization_ratio,
                holdout_ratio=request.holdout_ratio,
                regression_policy=request.regression_policy,
            ),
            cases,
        )
        split_create = EvalDatasetSplitCreate(
            split_id=split_plan.split_id,
            tenant_id=split_plan.tenant_id,
            agent_id=split_plan.agent_id,
            dataset=split_plan.dataset,
            request_id=split_plan.request_id,
            tags=[tag.value for tag in split_plan.tags],
            strategy=split_plan.split_strategy,
            optimization_ratio=split_plan.optimization_ratio,
            holdout_ratio=split_plan.holdout_ratio,
            regression_policy=split_plan.regression_policy.to_payload(),
            case_tags={
                case_id: [tag.value for tag in tags]
                for case_id, tags in split_plan.case_tags.items()
            },
            optimization_case_ids=split_plan.optimization_case_ids,
            holdout_case_ids=split_plan.holdout_case_ids,
            regression_case_ids=split_plan.regression_case_ids,
            tag_distribution=split_plan.tag_distribution,
            rejected_counts=split_plan.rejected_counts,
            evidence_refs=split_plan.evidence_refs,
        )
        return await self.execution.run_with_status(
            experiment_request_from_create(request, split_plan.split_id),
            request_hash_override=hash_value,
            split_create=split_create,
        )

    async def run(self, request: ExperimentRequest) -> ExperimentResult:
        return (await self.execution.run_with_status(request)).result

    async def run_with_status(
        self,
        request: ExperimentRequest,
        *,
        request_hash_override: str | None = None,
        split_create: EvalDatasetSplitCreate | None = None,
    ) -> ExperimentCreateOutcome:
        return await self.execution.run_with_status(
            request,
            request_hash_override=request_hash_override,
            split_create=split_create,
        )

    async def get(self, *, tenant_id: str, experiment_id: str, request_id: str) -> ExperimentResult:
        async with self.storage.uow() as uow:
            record = await uow.eval_experiments.get(tenant_id, experiment_id)
        if record is None:
            raise EvalExperimentError(
                "eval.experiment.not_found",
                "eval experiment is not visible",
                status_code=404,
            )
        split = await self._get_split(tenant_id, record.split_id)
        return result_from_record(record, split, request_id=request_id)

    async def compare(
        self, *, tenant_id: str, experiment_id: str, request_id: str
    ) -> ExperimentComparison:
        result = await self.get(
            tenant_id=tenant_id,
            experiment_id=experiment_id,
            request_id=request_id,
        )
        if result.candidate_harness_version is None:
            raise EvalExperimentError(
                "eval.experiment.candidate_missing",
                "experiment has no candidate harness version",
                status_code=409,
            )
        if result.comparison is None:
            raise EvalExperimentError(
                "eval.experiment.comparison_incomplete",
                "experiment comparison is not complete",
                status_code=409,
            )
        result.comparison.request_id = request_id
        return result.comparison

    async def _get_split(self, tenant_id: str, split_id: str) -> EvalDatasetSplitRecord:
        async with self.storage.uow() as uow:
            split = await uow.eval_dataset_splits.get(tenant_id, split_id)
        if split is None:
            raise EvalExperimentError(
                "eval.experiment.split_not_found",
                "eval dataset split is not visible",
                status_code=404,
            )
        return split
