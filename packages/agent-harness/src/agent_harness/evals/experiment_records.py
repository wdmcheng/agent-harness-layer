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
    """生成执行请求的语义哈希，排除传输级 request id 与幂等键。"""

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
    """将创建请求与已持久化的 split id 结合为可执行实验请求。"""

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
    """投影执行请求为 repository 写入 DTO，保留版本化 harness 的不可变载荷。"""

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
    """把持久化 experiment 与 split 还原为公开结果，并为本次响应注入 request id。

    comparison 中的 provider 状态来自数据库当前快照，而不是调用方传入值；这样
    terminal 后追加的外部 provider 信息不会被旧内存对象覆盖。
    """

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
