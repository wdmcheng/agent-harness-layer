"""Experiment 原子认领、执行租约、结果落库与 provider 降级编排。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from agent_harness.evals.comparison import ExperimentComparisonBuilder
from agent_harness.evals.dataset_models import RegressionPolicy
from agent_harness.evals.errors import EvalExperimentError
from agent_harness.evals.experiment_execution_recovery import (
    ExperimentExecutionClaimLost,
    ExperimentExecutionRecoveryMixin,
    stop_experiment_heartbeat,
)
from agent_harness.evals.experiment_models import (
    ExperimentComparison,
    ExperimentCreateOutcome,
    ExperimentEvaluationFailure,
    ExperimentEvaluationResult,
    ExperimentEvaluator,
    ExperimentEvidencePublisher,
    ExperimentRequest,
)
from agent_harness.evals.experiment_persistence import ExperimentResultPersistence
from agent_harness.evals.experiment_publishers import publish_experiment_evidence
from agent_harness.evals.experiment_records import (
    experiment_create_data,
    request_hash,
    result_from_record,
)
from agent_harness.evals.experiment_validation import (
    local_refs,
    validate_partial_evaluation,
)
from agent_harness.storage import (
    EvalDatasetSplitCreate,
    EvalDatasetSplitRecord,
    EvalExperimentRecord,
    ExperimentStorageConcurrentConflict,
    ExperimentStorageConflict,
    ExperimentStorageNotFound,
    SQLAlchemyStorage,
)


class ExperimentExecutionCoordinator(ExperimentExecutionRecoveryMixin):
    """以数据库 claim 为 fencing token，保证同一 experiment 只有一个活跃执行者。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        evaluator: ExperimentEvaluator,
        publishers: list[ExperimentEvidencePublisher],
        comparison_builder: ExperimentComparisonBuilder,
        claim_ttl_seconds: float,
    ) -> None:
        """装配 evaluator、发布器与持久化协作者，并将执行租约下限限制为一秒。"""

        self.storage = storage
        self.evaluator = evaluator
        self.publishers = publishers
        self.comparison_builder = comparison_builder
        self.claim_ttl_seconds = max(1.0, claim_ttl_seconds)
        self.persistence = ExperimentResultPersistence(storage)

    async def run_with_status(
        self,
        request: ExperimentRequest,
        *,
        request_hash_override: str | None = None,
        split_create: EvalDatasetSplitCreate | None = None,
    ) -> ExperimentCreateOutcome:
        """创建或重放实验，并且只让持有唯一数据库 claim 的请求启动评测。

        split、experiment 和首个 execution claim 在同一事务内建立。已终态记录
        直接投影；运行中或未取得 claim 的记录会被原子标记为 ``needs_review``，
        因为新请求无法证明既有外部评测副作用是否完成。
        """

        hash_value = request_hash_override or request_hash(request)
        for attempt in range(2):
            try:
                async with self.storage.uow() as uow:
                    claim_id: str | None = None
                    split = (
                        await uow.eval_dataset_splits.create(split_create)
                        if split_create is not None
                        else await uow.eval_dataset_splits.get(request.tenant_id, request.split_id)
                    )
                    if split is None:
                        raise ExperimentStorageNotFound(
                            "eval.experiment.split_not_found",
                            "eval dataset split is not visible",
                        )
                    record, created = await uow.eval_experiments.create_with_status(
                        experiment_create_data(request, hash_value)
                    )
                    if created:
                        claim_id = str(uuid4())
                        claimed = await uow.eval_experiments.claim_execution(
                            tenant_id=request.tenant_id,
                            experiment_id=record.experiment_id,
                            claim_id=claim_id,
                            expires_at=self._claim_expiry(),
                        )
                        if not claimed:
                            raise ExperimentStorageConflict(
                                "eval.experiment.execution_claim_conflict",
                                "new experiment execution could not be claimed",
                            )
                        await uow.commit()
                if not created:
                    return await self.resume_or_replay(request=request, record=record)
                if claim_id is None:
                    raise AssertionError("created experiment must have an execution claim")
                return await self._execute_claimed(
                    request=request,
                    record=record,
                    split=split,
                    claim_id=claim_id,
                    created=True,
                )
            except ExperimentStorageConcurrentConflict as exc:
                winner = await self._idempotency_winner(request)
                if winner is not None:
                    if winner.request_hash != hash_value:
                        raise EvalExperimentError(
                            "eval.experiment.idempotency_conflict",
                            "idempotency key was already used with another request",
                            status_code=409,
                        ) from exc
                    return await self.resume_or_replay(request=request, record=winner)
                if attempt == 0:
                    continue
                raise EvalExperimentError(
                    "eval.experiment.idempotency_conflict",
                    "concurrent experiment could not be reconciled",
                    status_code=409,
                ) from exc
            except ExperimentStorageConflict as exc:
                raise EvalExperimentError(exc.code, str(exc), status_code=409) from exc
            except ExperimentStorageNotFound as exc:
                raise EvalExperimentError(exc.code, str(exc), status_code=404) from exc
        raise AssertionError("unreachable experiment claim loop")

    async def resume_or_replay(
        self,
        *,
        request: ExperimentRequest,
        record: EvalExperimentRecord,
    ) -> ExperimentCreateOutcome:
        """在持有数据库 fencing claim 时运行评测，并以心跳守护唯一执行权。

        claim 丢失或终态写入围栏冲突时收敛为人工复核，而不是由当前 worker 覆写
        新 owner 的结果。异常发生在 evaluator 之后也必须保留 running 风险，不可
        自动回退为可重跑状态。
        """

        split = await self._get_split(request.tenant_id, record.split_id)
        if record.status not in {"created", "running"}:
            return ExperimentCreateOutcome(
                result=result_from_record(record, split, request_id=request.request_id),
                created=False,
            )
        if record.status == "running":
            async with self.storage.uow() as uow:
                marked = await uow.eval_experiments.mark_expired_execution_needs_review(
                    tenant_id=request.tenant_id,
                    experiment_id=record.experiment_id,
                    now=datetime.now(tz=UTC),
                )
                if marked:
                    await uow.commit()
                latest = await uow.eval_experiments.get(request.tenant_id, record.experiment_id)
            if latest is None:
                raise EvalExperimentError(
                    "eval.experiment.not_found",
                    "eval experiment is not visible",
                    status_code=404,
                )
            return ExperimentCreateOutcome(
                result=result_from_record(latest, split, request_id=request.request_id),
                created=False,
            )
        async with self.storage.uow() as uow:
            marked = await uow.eval_experiments.mark_unclaimed_execution_needs_review(
                tenant_id=request.tenant_id,
                experiment_id=record.experiment_id,
            )
            if marked:
                await uow.commit()
            latest = await uow.eval_experiments.get(request.tenant_id, record.experiment_id)
        if latest is None:
            raise EvalExperimentError(
                "eval.experiment.not_found",
                "eval experiment is not visible",
                status_code=404,
            )
        return ExperimentCreateOutcome(
            result=result_from_record(latest, split, request_id=request.request_id),
            created=False,
        )

    async def _execute_claimed(
        self,
        *,
        request: ExperimentRequest,
        record: EvalExperimentRecord,
        split: EvalDatasetSplitRecord,
        claim_id: str,
        created: bool,
    ) -> ExperimentCreateOutcome:
        """在持有 execution claim 时续租，并把失权和围栏冲突收敛为人工复核。

        心跳存活期间才允许下游写终态；一旦失去 claim，当前 worker 不能覆盖
        新 owner 的结果，必须保留不确定状态而非自动重跑评测副作用。
        """

        claim_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._renew_claim_until_terminal(
                tenant_id=request.tenant_id,
                experiment_id=record.experiment_id,
                claim_id=claim_id,
                claim_lost=claim_lost,
            )
        )
        try:
            return await self._evaluate_and_persist(
                request=request,
                record=record,
                split=split,
                claim_id=claim_id,
                created=created,
                heartbeat=heartbeat,
                claim_lost=claim_lost,
            )
        except ExperimentExecutionClaimLost:
            return await self._needs_review_outcome(
                request=request,
                record=record,
                split=split,
                claim_id=claim_id,
                created=created,
            )
        except ExperimentStorageConflict as exc:
            if exc.code == "eval.experiment.execution_fenced":
                return await self._needs_review_outcome(
                    request=request,
                    record=record,
                    split=split,
                    claim_id=claim_id,
                    created=created,
                )
            await self._mark_needs_review(
                tenant_id=request.tenant_id,
                experiment_id=record.experiment_id,
                claim_id=claim_id,
            )
            raise
        except BaseException:
            # evaluator 一旦开始便可能已有外部副作用；无法落 terminal 时必须人工复核，
            # 不能把 running 回退成 created 后自动重跑。
            await self._mark_needs_review(
                tenant_id=request.tenant_id,
                experiment_id=record.experiment_id,
                claim_id=claim_id,
            )
            raise
        finally:
            await stop_experiment_heartbeat(heartbeat)

    async def _evaluate_and_persist(
        self,
        *,
        request: ExperimentRequest,
        record: EvalExperimentRecord,
        split: EvalDatasetSplitRecord,
        claim_id: str,
        created: bool,
        heartbeat: asyncio.Task[None],
        claim_lost: asyncio.Event,
    ) -> ExperimentCreateOutcome:
        """运行基线/候选评测，验证部分失败证据并先持久化本地终态。

        evaluator 异常会先转为受限失败记录；成功结果的比较和证据先落入可信
        存储，随后才调用允许降级的外部发布器，避免其故障阻断可审计结论。
        """

        baseline: ExperimentEvaluationResult | None = None
        candidate: ExperimentEvaluationResult | None = None
        comparison: ExperimentComparison | None = None
        try:
            baseline = await self._evaluate_version(
                request=request,
                split=split,
                version=request.baseline_harness_version,
            )
            if request.candidate_harness_version is not None:
                candidate = await self._evaluate_version(
                    request=request,
                    split=split,
                    version=request.candidate_harness_version,
                )
                comparison = self.comparison_builder.build(
                    experiment_id=record.experiment_id,
                    request_id=request.request_id,
                    requested_tags=split.tags,
                    baseline=baseline,
                    candidate=candidate,
                    regression_policy=RegressionPolicy.model_validate(split.regression_policy),
                    authoritative_case_tags=split.case_tags,
                )
        except ExperimentEvaluationFailure as exc:
            error: Exception = exc
            try:
                validate_partial_evaluation(
                    split=split,
                    result=exc.partial_result,
                    evaluator_profile=request.evaluator_profile,
                    metric_versions=request.metric_versions,
                )
            except Exception as validation_error:  # noqa: BLE001 - mismatch is failure evidence
                error = validation_error
            else:
                if (
                    exc.partial_result.harness_version_id
                    == request.baseline_harness_version.version_id
                ):
                    baseline = exc.partial_result
                elif (
                    request.candidate_harness_version is not None
                    and exc.partial_result.harness_version_id
                    == request.candidate_harness_version.version_id
                ):
                    candidate = exc.partial_result
            await self._prepare_terminal_write(heartbeat, claim_lost)
            return ExperimentCreateOutcome(
                result=await self.persistence.record_failure(
                    request=request,
                    record=record,
                    split=split,
                    baseline=baseline,
                    candidate=candidate,
                    error=error,
                    claim_id=claim_id,
                ),
                created=created,
            )
        except Exception as exc:  # noqa: BLE001 - local error evidence must survive
            await self._prepare_terminal_write(heartbeat, claim_lost)
            return ExperimentCreateOutcome(
                result=await self.persistence.record_failure(
                    request=request,
                    record=record,
                    split=split,
                    baseline=baseline,
                    candidate=candidate,
                    error=exc,
                    claim_id=claim_id,
                ),
                created=created,
            )

        score_summaries = {
            "baseline": baseline.to_payload(),
            **({} if candidate is None else {"candidate": candidate.to_payload()}),
            **(
                {}
                if comparison is None or not comparison.failure_details
                else {"comparison_failure_details": comparison.failure_details}
            ),
        }
        refs = local_refs(record.experiment_id, baseline, candidate, comparison)
        initial_status = "baseline_completed" if candidate is None else "completed"
        await self._prepare_terminal_write(heartbeat, claim_lost)
        stored = await self.persistence.update_record(
            request=request,
            record=record,
            status=initial_status,
            score_summaries=score_summaries,
            comparison=comparison,
            local_refs=refs,
            provider_statuses=[],
            claim_id=claim_id,
        )

        provider_statuses = await publish_experiment_evidence(
            publishers=self.publishers,
            record=stored,
            comparison=comparison,
        )
        if provider_statuses:
            degraded = any(status.get("status") == "degraded" for status in provider_statuses)
            final_status = f"{initial_status}_with_degradation" if degraded else initial_status
            if comparison is not None:
                comparison.provider_statuses = provider_statuses
            stored = await self.persistence.update_provider_record(
                request=request,
                record=record,
                expected_status=initial_status,
                status=final_status,
                comparison=comparison,
                provider_statuses=provider_statuses,
            )
        return ExperimentCreateOutcome(
            result=result_from_record(stored, split, request_id=request.request_id),
            created=created,
        )
