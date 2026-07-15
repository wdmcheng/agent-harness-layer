"""Eval experiment execution claim 的续租、fencing 与恢复辅助。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from agent_harness.evals.errors import EvalExperimentError
from agent_harness.evals.experiment_models import (
    ExperimentCreateOutcome,
    ExperimentEvaluationResult,
    ExperimentEvaluator,
    ExperimentRequest,
)
from agent_harness.evals.experiment_records import result_from_record
from agent_harness.evals.experiment_validation import validate_evaluation
from agent_harness.evals.harness_versions import HarnessVersionManifest
from agent_harness.storage import (
    EvalDatasetSplitRecord,
    EvalExperimentRecord,
    SQLAlchemyStorage,
)


class ExperimentExecutionClaimLost(RuntimeError):
    """heartbeat 无法证明 claim 仍有效时，禁止写入可信终态。"""


class ExperimentExecutionRecoveryMixin:
    """集中维护 execution claim 的租约证明与不确定结果收口。"""

    storage: SQLAlchemyStorage
    evaluator: ExperimentEvaluator
    claim_ttl_seconds: float

    async def _evaluate_version(
        self,
        *,
        request: ExperimentRequest,
        split: EvalDatasetSplitRecord,
        version: HarnessVersionManifest,
    ) -> ExperimentEvaluationResult:
        result = await self.evaluator.evaluate(
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            dataset=request.dataset,
            split=split,
            harness_version=version,
            evaluator_profile=request.evaluator_profile,
            metric_versions=request.metric_versions,
        )
        validate_evaluation(
            split=split,
            result=result,
            expected_version=version.version_id,
            evaluator_profile=request.evaluator_profile,
            metric_versions=request.metric_versions,
        )
        return result

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

    async def _idempotency_winner(self, request: ExperimentRequest) -> EvalExperimentRecord | None:
        async with self.storage.uow() as uow:
            return await uow.eval_experiments.get_by_idempotency_key(
                request.tenant_id, request.idempotency_key
            )

    def _claim_expiry(self) -> datetime:
        return datetime.now(tz=UTC) + timedelta(seconds=self.claim_ttl_seconds)

    async def _renew_claim_until_terminal(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        claim_id: str,
        claim_lost: asyncio.Event,
    ) -> None:
        interval = max(0.5, self.claim_ttl_seconds / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                async with self.storage.uow() as uow:
                    renewed = await uow.eval_experiments.renew_execution_claim(
                        tenant_id=tenant_id,
                        experiment_id=experiment_id,
                        claim_id=claim_id,
                        expires_at=self._claim_expiry(),
                    )
                    if renewed:
                        await uow.commit()
                if not renewed:
                    claim_lost.set()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - storage/transport failure invalidates the lease proof
            claim_lost.set()

    async def _prepare_terminal_write(
        self,
        heartbeat: asyncio.Task[None],
        claim_lost: asyncio.Event,
    ) -> None:
        """先停止续租再判定所有权，终态写入随后由 repository 再原子校验。"""

        await stop_experiment_heartbeat(heartbeat)
        if claim_lost.is_set():
            raise ExperimentExecutionClaimLost

    async def _needs_review_outcome(
        self,
        *,
        request: ExperimentRequest,
        record: EvalExperimentRecord,
        split: EvalDatasetSplitRecord,
        claim_id: str,
        created: bool,
    ) -> ExperimentCreateOutcome:
        await self._mark_needs_review(
            tenant_id=request.tenant_id,
            experiment_id=record.experiment_id,
            claim_id=claim_id,
        )
        latest = await self._idempotency_winner(request)
        if latest is None:
            raise EvalExperimentError(
                "eval.experiment.not_found",
                "eval experiment is not visible",
                status_code=404,
            )
        return ExperimentCreateOutcome(
            result=result_from_record(latest, split, request_id=request.request_id),
            created=created,
        )

    async def _mark_needs_review(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        claim_id: str,
    ) -> None:
        async with self.storage.uow() as uow:
            marked = await uow.eval_experiments.mark_execution_needs_review(
                tenant_id=tenant_id,
                experiment_id=experiment_id,
                claim_id=claim_id,
                reason_code="eval.experiment.execution_outcome_uncertain",
            )
            if marked:
                await uow.commit()


async def stop_experiment_heartbeat(task: asyncio.Task[None]) -> None:
    """取消 heartbeat，并吞掉取消过程的预期异常。"""

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "ExperimentExecutionClaimLost",
    "ExperimentExecutionRecoveryMixin",
    "stop_experiment_heartbeat",
]
