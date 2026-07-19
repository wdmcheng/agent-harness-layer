"""评测实验执行 claim 的续租、栅栏与不确定结果恢复辅助。"""

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
        """调用评测器并立即校验其结果与请求、切分和版本清单完全一致。

        校验位于结果持久化之前，避免 Provider 返回了不同版本、错误切分或缺少
        指标的结果时仍被后续比较误认为可信实验事实。
        """
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
        """读取租户可见的持久化切分；缺失时映射为稳定的领域 404 错误。"""
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
        """按租户和幂等键读取唯一实验记录，用于恢复后返回既有结果。"""
        async with self.storage.uow() as uow:
            return await uow.eval_experiments.get_by_idempotency_key(
                request.tenant_id, request.idempotency_key
            )

    def _claim_expiry(self) -> datetime:
        """计算下一次 claim 到期时间；统一使用 UTC 避免多时区 worker 产生歧义。"""
        return datetime.now(tz=UTC) + timedelta(seconds=self.claim_ttl_seconds)

    async def _renew_claim_until_terminal(
        self,
        *,
        tenant_id: str,
        experiment_id: str,
        claim_id: str,
        claim_lost: asyncio.Event,
    ) -> None:
        """在执行期间定期续租，并在任何无法证明所有权的情形下标记 claim 丢失。

        续租失败不能被视为可重试的普通日志：终态写入必须停止，因为可能已有
        其他 worker 接管同一实验。取消则向上传播，让调用方按正常收口顺序处理。
        """
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
        """将不确定执行标记为需人工复核，并返回幂等获胜记录而非猜测结果。"""
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
        """在持有匹配 claim 时原子标记实验为需复核；丢失所有权时不写任何状态。"""
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
