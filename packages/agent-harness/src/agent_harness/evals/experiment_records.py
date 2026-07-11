"""Experiment 请求、存储记录与公共结果之间的确定性映射。"""

from __future__ import annotations

import hashlib
import json
from typing import cast

from agent_harness.evals.experiment_models import (
    ExperimentComparison,
    ExperimentCreateRequest,
    ExperimentRequest,
    ExperimentResult,
    ExperimentStatus,
)
from agent_harness.storage import (
    EvalDatasetSplitRecord,
    EvalExperimentCreate,
    EvalExperimentRecord,
)


def request_hash(request: ExperimentRequest) -> str:
    payload = request.to_payload()
    payload.pop("request_id", None)
    payload.pop("idempotency_key", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_request_hash(request: ExperimentCreateRequest) -> str:
    """幂等 body hash 不依赖 request id、key 或之后可能变化的 dataset。"""

    payload = request.to_payload()
    payload.pop("request_id", None)
    payload.pop("idempotency_key", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def experiment_request_from_create(
    request: ExperimentCreateRequest, split_id: str
) -> ExperimentRequest:
    return ExperimentRequest(
        request_id=request.request_id,
        tenant_id=request.tenant_id,
        idempotency_key=request.idempotency_key,
        agent_id=request.agent_id,
        dataset=request.dataset,
        split_id=split_id,
        baseline_harness_version=request.baseline_harness_version,
        candidate_harness_version=request.candidate_harness_version,
        evaluator_profile=request.evaluator_profile,
        metric_versions=request.metric_versions,
        metadata=request.metadata,
    )


def experiment_create_data(
    request: ExperimentRequest, request_hash_value: str
) -> EvalExperimentCreate:
    return EvalExperimentCreate(
        tenant_id=request.tenant_id,
        idempotency_key=request.idempotency_key,
        request_hash=request_hash_value,
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


def result_from_record(
    record: EvalExperimentRecord,
    split: EvalDatasetSplitRecord,
    *,
    request_id: str,
) -> ExperimentResult:
    baseline_payload = record.baseline_harness
    candidate_payload = record.candidate_harness
    comparison = (
        None if not record.comparison else ExperimentComparison.model_validate(record.comparison)
    )
    if comparison is not None:
        comparison.request_id = request_id
        comparison.provider_statuses = record.provider_statuses
    return ExperimentResult(
        request_id=request_id,
        experiment_id=record.experiment_id,
        status=cast(ExperimentStatus, record.status),
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
