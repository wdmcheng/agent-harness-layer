"""同一 split 上 baseline/candidate experiment 的 local-first 编排。"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from agent_harness.evals.comparison import ExperimentComparisonBuilder
from agent_harness.evals.dataset_models import RegressionPolicy
from agent_harness.evals.errors import EvalExperimentError
from agent_harness.evals.experiment_models import (
    ExperimentComparison,
    ExperimentEvaluationFailure,
    ExperimentEvaluationResult,
    ExperimentEvaluator,
    ExperimentEvidencePublisher,
    ExperimentRequest,
    ExperimentResult,
)
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import (
    EvalDatasetSplitRecord,
    EvalExperimentCreate,
    EvalExperimentRecord,
    SQLAlchemyStorage,
)


class ExperimentService:
    """先持久化本地 experiment/comparison，再执行可降级 provider fan-out。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        evaluator: ExperimentEvaluator,
        publishers: list[ExperimentEvidencePublisher] | None = None,
        comparison_builder: ExperimentComparisonBuilder | None = None,
    ) -> None:
        self.storage = storage
        self.evaluator = evaluator
        self.publishers = publishers or []
        self.comparison_builder = comparison_builder or ExperimentComparisonBuilder()

    async def run(self, request: ExperimentRequest) -> ExperimentResult:
        split = await self._get_split(request.tenant_id, request.split_id)
        request_hash = _request_hash(request)
        async with self.storage.uow() as uow:
            record = await uow.eval_experiments.create(
                EvalExperimentCreate(
                    tenant_id=request.tenant_id,
                    idempotency_key=request.idempotency_key,
                    request_hash=request_hash,
                    request_id=request.request_id,
                    agent_id=request.agent_id,
                    dataset=request.dataset,
                    split_id=request.split_id,
                    evaluator_profile=request.evaluator_profile,
                    metric_versions=request.metric_versions,
                    baseline_harness=request.baseline_harness_version.to_payload(),
                    candidate_harness=(
                        None
                        if request.candidate_harness_version is None
                        else request.candidate_harness_version.to_payload()
                    ),
                    metadata=request.metadata,
                )
            )
            await uow.commit()
        if record.status != "created":
            return _result_from_record(record, split, request_id=request.request_id)

        baseline: ExperimentEvaluationResult | None = None
        candidate: ExperimentEvaluationResult | None = None
        comparison: ExperimentComparison | None = None
        try:
            baseline = await self.evaluator.evaluate(
                tenant_id=request.tenant_id,
                agent_id=request.agent_id,
                dataset=request.dataset,
                split=split,
                harness_version=request.baseline_harness_version,
                evaluator_profile=request.evaluator_profile,
                metric_versions=request.metric_versions,
            )
            _validate_evaluation(
                split=split,
                result=baseline,
                expected_version=request.baseline_harness_version.version_id,
                evaluator_profile=request.evaluator_profile,
                metric_versions=request.metric_versions,
            )
            if request.candidate_harness_version is not None:
                candidate = await self.evaluator.evaluate(
                    tenant_id=request.tenant_id,
                    agent_id=request.agent_id,
                    dataset=request.dataset,
                    split=split,
                    harness_version=request.candidate_harness_version,
                    evaluator_profile=request.evaluator_profile,
                    metric_versions=request.metric_versions,
                )
                _validate_evaluation(
                    split=split,
                    result=candidate,
                    expected_version=request.candidate_harness_version.version_id,
                    evaluator_profile=request.evaluator_profile,
                    metric_versions=request.metric_versions,
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
            partial = exc.partial_result
            _validate_partial_evaluation(
                split=split,
                result=partial,
                evaluator_profile=request.evaluator_profile,
                metric_versions=request.metric_versions,
            )
            if partial.harness_version_id == request.baseline_harness_version.version_id:
                baseline = partial
            elif (
                request.candidate_harness_version is not None
                and partial.harness_version_id
                == request.candidate_harness_version.version_id
            ):
                candidate = partial
            return await self._record_failure(
                request=request,
                record=record,
                split=split,
                baseline=baseline,
                candidate=candidate,
                error=exc,
            )
        except Exception as exc:  # noqa: BLE001 - local error evidence must survive
            return await self._record_failure(
                request=request,
                record=record,
                split=split,
                baseline=baseline,
                candidate=candidate,
                error=exc,
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
        local_refs = _local_refs(record.experiment_id, baseline, candidate, comparison)
        initial_status = "baseline_completed" if candidate is None else "completed"
        stored = await self._update_record(
            request=request,
            record=record,
            status=initial_status,
            score_summaries=score_summaries,
            comparison=comparison,
            local_refs=local_refs,
            provider_statuses=[],
        )

        provider_statuses = await self._publish(stored, comparison)
        if provider_statuses:
            degraded = any(status.get("status") == "degraded" for status in provider_statuses)
            final_status = (
                f"{initial_status}_with_degradation" if degraded else initial_status
            )
            if comparison is not None:
                comparison.provider_statuses = provider_statuses
            stored = await self._update_record(
                request=request,
                record=record,
                status=final_status,
                score_summaries=score_summaries,
                comparison=comparison,
                local_refs=local_refs,
                provider_statuses=provider_statuses,
            )
        return _result_from_record(stored, split, request_id=request.request_id)

    async def get(
        self, *, tenant_id: str, experiment_id: str, request_id: str
    ) -> ExperimentResult:
        async with self.storage.uow() as uow:
            record = await uow.eval_experiments.get(tenant_id, experiment_id)
        if record is None:
            raise EvalExperimentError(
                "eval.experiment.not_found",
                "eval experiment is not visible",
                status_code=404,
            )
        split = await self._get_split(tenant_id, record.split_id)
        return _result_from_record(record, split, request_id=request_id)

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

    async def _update_record(
        self,
        *,
        request: ExperimentRequest,
        record: EvalExperimentRecord,
        status: str,
        score_summaries: dict[str, Any],
        comparison: ExperimentComparison | None,
        local_refs: list[str],
        provider_statuses: list[dict[str, object]],
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
            )
            await uow.commit()
        return updated

    async def _record_failure(
        self,
        *,
        request: ExperimentRequest,
        record: EvalExperimentRecord,
        split: EvalDatasetSplitRecord,
        baseline: ExperimentEvaluationResult | None,
        candidate: ExperimentEvaluationResult | None,
        error: Exception,
    ) -> ExperimentResult:
        redacted_error = str(redact_secrets(str(error)))
        scores = {
            **({} if baseline is None else {"baseline": baseline.to_payload()}),
            **({} if candidate is None else {"candidate": candidate.to_payload()}),
            "error": {"summary": redacted_error},
        }
        failure_refs: set[str] = {f"db://eval-experiments/{record.experiment_id}"}
        if baseline is not None:
            failure_refs.update(baseline.local_evidence_refs)
        if candidate is not None:
            failure_refs.update(candidate.local_evidence_refs)
        local_refs = sorted(failure_refs)
        stored = await self._update_record(
            request=request,
            record=record,
            status="failed",
            score_summaries=scores,
            comparison=None,
            local_refs=local_refs,
            provider_statuses=[],
        )
        return _result_from_record(stored, split, request_id=request.request_id)

    async def _publish(
        self,
        record: EvalExperimentRecord,
        comparison: ExperimentComparison | None,
    ) -> list[dict[str, object]]:
        payload = {
            "experiment_id": record.experiment_id,
            "status": record.status,
            "comparison": None if comparison is None else comparison.to_payload(),
            "local_evidence_refs": record.local_refs,
        }
        statuses: list[dict[str, object]] = []
        for publisher in self.publishers:
            try:
                status = await publisher.publish(payload)
                statuses.append(cast(dict[str, object], redact_secrets(status)))
            except Exception as exc:  # noqa: BLE001 - optional provider must degrade
                statuses.append(
                    {
                        "provider": publisher.provider_name,
                        "status": "degraded",
                        "detail": str(redact_secrets(str(exc))),
                    }
                )
        return statuses


def _request_hash(request: ExperimentRequest) -> str:
    payload = request.to_payload()
    payload.pop("request_id", None)
    payload.pop("idempotency_key", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_evaluation(
    *,
    split: EvalDatasetSplitRecord,
    result: ExperimentEvaluationResult,
    expected_version: str,
    evaluator_profile: dict[str, Any],
    metric_versions: dict[str, str],
) -> None:
    expected_subsets = {
        **{case_id: "optimization" for case_id in split.optimization_case_ids},
        **{case_id: "holdout" for case_id in split.holdout_case_ids},
        **{case_id: "regression" for case_id in split.regression_case_ids},
    }
    actual_subsets = {item.case_id: item.subset for item in result.case_results}
    expected_metrics = set(metric_versions)
    cases_valid = all(
        set(item.metric_scores) == expected_metrics
        and sorted(set(item.tags)) == sorted(set(split.case_tags.get(item.case_id, [])))
        for item in result.case_results
    )
    if (
        result.harness_version_id != expected_version
        or result.evaluator_profile != evaluator_profile
        or result.metric_versions != metric_versions
        or actual_subsets != expected_subsets
        or len(actual_subsets) != len(result.case_results)
        or not cases_valid
    ):
        raise EvalExperimentError(
            "eval.experiment.evaluation_mismatch",
            "evaluator result does not match the fixed experiment inputs",
            status_code=422,
        )


def _validate_partial_evaluation(
    *,
    split: EvalDatasetSplitRecord,
    result: ExperimentEvaluationResult,
    evaluator_profile: dict[str, Any],
    metric_versions: dict[str, str],
) -> None:
    expected_subsets = {
        **{case_id: "optimization" for case_id in split.optimization_case_ids},
        **{case_id: "holdout" for case_id in split.holdout_case_ids},
        **{case_id: "regression" for case_id in split.regression_case_ids},
    }
    expected_metrics = set(metric_versions)
    seen: set[str] = set()
    valid = (
        result.evaluator_profile == evaluator_profile
        and result.metric_versions == metric_versions
    )
    for item in result.case_results:
        if (
            item.case_id in seen
            or expected_subsets.get(item.case_id) != item.subset
            or set(item.metric_scores) != expected_metrics
            or sorted(set(item.tags))
            != sorted(set(split.case_tags.get(item.case_id, [])))
        ):
            valid = False
        seen.add(item.case_id)
    if not valid:
        raise EvalExperimentError(
            "eval.experiment.partial_evaluation_mismatch",
            "partial evaluator result does not match fixed experiment inputs",
            status_code=422,
        )


def _local_refs(
    experiment_id: str,
    baseline: ExperimentEvaluationResult,
    candidate: ExperimentEvaluationResult | None,
    comparison: ExperimentComparison | None,
) -> list[str]:
    return sorted(
        {
            f"db://eval-experiments/{experiment_id}",
            *baseline.local_evidence_refs,
            *(candidate.local_evidence_refs if candidate is not None else []),
            *(comparison.local_evidence_refs if comparison is not None else []),
        }
    )


def _result_from_record(
    record: EvalExperimentRecord,
    split: EvalDatasetSplitRecord,
    *,
    request_id: str,
) -> ExperimentResult:
    baseline_payload = record.baseline_harness
    candidate_payload = record.candidate_harness
    comparison = (
        None
        if not record.comparison
        else ExperimentComparison.model_validate(record.comparison)
    )
    if comparison is not None:
        comparison.request_id = request_id
        comparison.provider_statuses = record.provider_statuses
    return ExperimentResult(
        request_id=request_id,
        experiment_id=record.experiment_id,
        status=record.status,
        agent_id=record.agent_id,
        dataset=record.dataset,
        tags=split.tags,
        optimization_case_count=split.optimization_case_count,
        holdout_case_count=split.holdout_case_count,
        regression_case_count=split.regression_case_count,
        baseline_harness_version=str(baseline_payload["version_id"]),
        candidate_harness_version=(
            None if candidate_payload is None else str(candidate_payload["version_id"])
        ),
        baseline_eval_run_ref=record.baseline_run_ref,
        candidate_eval_run_ref=record.candidate_run_ref,
        local_evidence_refs=record.local_refs,
        provider_statuses=record.provider_statuses,
        comparison=comparison,
    )
