"""Experiment 可信终态、失败摘要与 provider 追加写入。"""

from __future__ import annotations

import re
from typing import Any

from agent_harness.evals.experiment_models import (
    ExperimentComparison,
    ExperimentEvaluationResult,
    ExperimentRequest,
    ExperimentResult,
    bounded_public_evidence_refs,
)
from agent_harness.evals.experiment_records import result_from_record
from agent_harness.storage import (
    EvalDatasetSplitRecord,
    EvalExperimentRecord,
    SQLAlchemyStorage,
)


class ExperimentResultPersistence:
    """集中处理 claim-fenced 终态与 terminal 后的 provider 摘要追加。"""

    def __init__(self, storage: SQLAlchemyStorage) -> None:
        self.storage = storage

    async def update_record(
        self,
        *,
        request: ExperimentRequest,
        record: EvalExperimentRecord,
        status: str,
        score_summaries: dict[str, Any],
        comparison: ExperimentComparison | None,
        local_refs: list[str],
        provider_statuses: list[dict[str, object]],
        claim_id: str,
    ) -> EvalExperimentRecord:
        async with self.storage.uow() as uow:
            updated = await uow.eval_experiments.update_results(
                tenant_id=request.tenant_id,
                experiment_id=record.experiment_id,
                status=status,
                baseline_run_ref=f"eval-run://{record.experiment_id}/baseline",
                candidate_run_ref=(
                    None
                    if request.candidate_harness_version is None
                    else f"eval-run://{record.experiment_id}/candidate"
                ),
                score_summaries=score_summaries,
                comparison={} if comparison is None else comparison.to_payload(),
                local_refs=local_refs,
                provider_statuses=provider_statuses,
                execution_claim_id=claim_id,
            )
            await uow.commit()
        return updated

    async def record_failure(
        self,
        *,
        request: ExperimentRequest,
        record: EvalExperimentRecord,
        split: EvalDatasetSplitRecord,
        baseline: ExperimentEvaluationResult | None,
        candidate: ExperimentEvaluationResult | None,
        error: Exception,
        claim_id: str,
    ) -> ExperimentResult:
        error_code = getattr(error, "code", "eval.experiment.evaluation_failed")
        if not isinstance(error_code, str) or not re.fullmatch(
            r"[a-z][a-z0-9_.-]{0,127}", error_code
        ):
            error_code = "eval.experiment.evaluation_failed"
        scores = {
            **({} if baseline is None else {"baseline": baseline.to_payload()}),
            **({} if candidate is None else {"candidate": candidate.to_payload()}),
            "error": {
                "code": error_code,
                "summary": "evaluator failed; inspect local evidence refs",
            },
        }
        truth_ref = f"db://eval-experiments/{record.experiment_id}"
        failure_refs: set[str] = set()
        if baseline is not None:
            failure_refs.update(baseline.local_evidence_refs)
        if candidate is not None:
            failure_refs.update(candidate.local_evidence_refs)
        stored = await self.update_record(
            request=request,
            record=record,
            status="failed",
            score_summaries=scores,
            comparison=None,
            local_refs=bounded_public_evidence_refs(
                sorted(failure_refs),
                truth_ref=truth_ref,
                field_path="experiment.failure_local_evidence_refs",
            ),
            provider_statuses=[],
            claim_id=claim_id,
        )
        return result_from_record(stored, split, request_id=request.request_id)

    async def update_provider_record(
        self,
        *,
        request: ExperimentRequest,
        record: EvalExperimentRecord,
        expected_status: str,
        status: str,
        comparison: ExperimentComparison | None,
        provider_statuses: list[dict[str, object]],
    ) -> EvalExperimentRecord:
        async with self.storage.uow() as uow:
            updated = await uow.eval_experiments.update_provider_results(
                tenant_id=request.tenant_id,
                experiment_id=record.experiment_id,
                expected_status=expected_status,
                status=status,
                comparison={} if comparison is None else comparison.to_payload(),
                provider_statuses=provider_statuses,
            )
            await uow.commit()
        return updated
