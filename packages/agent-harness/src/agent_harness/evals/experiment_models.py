"""Experiment evaluator、comparison 与 service 的公共 DTO/protocol。"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import Field

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.evals.harness_versions import HarnessVersionManifest
from agent_harness.storage import EvalDatasetSplitRecord

SubsetName = Literal["optimization", "holdout", "regression"]
Recommendation = Literal["accept", "reject", "needs_review"]
RecommendationReasonCode = Literal[
    "target_tag_improved",
    "named_failure_fixed",
    "no_target_improvement",
    "holdout_within_threshold",
    "holdout_regression_exceeded",
    "critical_regression_passed",
    "critical_regression_failed",
    "new_failures_present",
    "local_evidence_incomplete",
    "comparison_incomplete",
]


def _empty_provider_statuses() -> list[dict[str, object]]:
    return []


class ExperimentCaseResult(HarnessDTO):
    """单个 approved case 的 provider-neutral evaluator 结果。"""

    case_id: str
    subset: SubsetName
    tags: list[str]
    metric_scores: dict[str, float]
    passed: bool
    evidence_refs: list[str] = Field(default_factory=list)

    @property
    def aggregate_score(self) -> float:
        if not self.metric_scores:
            return 0.0
        return sum(self.metric_scores.values()) / len(self.metric_scores)


class ExperimentEvaluationResult(HarnessDTO):
    """一个 harness version 在固定 split/evaluator 上的完整结果。"""

    harness_version_id: str
    evaluator_profile: dict[str, Any]
    metric_versions: dict[str, str]
    case_results: list[ExperimentCaseResult]
    local_evidence_refs: list[str] = Field(default_factory=list)


class ExperimentEvaluationFailure(RuntimeError):
    """Evaluator 失败并携带已经完成、已落本地 evidence 的 case 结果。"""

    def __init__(self, message: str, *, partial_result: ExperimentEvaluationResult) -> None:
        super().__init__(message)
        self.partial_result = partial_result


class ExperimentEvaluator(Protocol):
    """生产 adapter 与 deterministic fake 共用的 evaluator seam。"""

    async def evaluate(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        dataset: str,
        split: EvalDatasetSplitRecord,
        harness_version: HarnessVersionManifest,
        evaluator_profile: dict[str, Any],
        metric_versions: dict[str, str],
    ) -> ExperimentEvaluationResult:
        ...


class ExperimentEvidencePublisher(Protocol):
    """可选 provider fan-out；本地 DB evidence 已先提交。"""

    provider_name: str

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        ...


class PerTagComparison(HarnessDTO):
    tag: str
    baseline_score: float
    candidate_score: float
    delta: float


class FailureDifference(HarnessDTO):
    case_id: str
    subset: SubsetName
    tags: list[str]
    baseline_score: float
    candidate_score: float
    evidence_refs: list[str] = Field(default_factory=list)


class ExperimentComparison(HarnessDTO):
    request_id: str | None = None
    experiment_id: str
    candidate_harness_version: str
    per_tag: list[PerTagComparison]
    holdout_delta: float
    regressions: list[FailureDifference]
    new_failures: list[FailureDifference]
    fixed_failures: list[FailureDifference]
    acceptance_recommendation: Recommendation
    recommendation_reason_codes: list[RecommendationReasonCode] = Field(min_length=1)
    local_evidence_refs: list[str]
    provider_statuses: list[dict[str, object]] = Field(default_factory=_empty_provider_statuses)
    failure_details_ref: str | None = None
    failure_details: dict[str, list[dict[str, object]]] = Field(
        default_factory=dict,
        exclude=True,
    )


class ExperimentRequest(HarnessDTO):
    """内部 service 输入；HTTP adapter 负责创建 split 并补默认 evaluator。"""

    request_id: str
    tenant_id: str
    idempotency_key: str
    agent_id: str
    dataset: str
    split_id: str
    baseline_harness_version: HarnessVersionManifest
    candidate_harness_version: HarnessVersionManifest | None = None
    evaluator_profile: dict[str, Any]
    metric_versions: dict[str, str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExperimentResult(HarnessDTO):
    request_id: str
    experiment_id: str
    status: str
    agent_id: str
    dataset: str
    tags: list[str]
    optimization_case_count: int
    holdout_case_count: int
    regression_case_count: int
    baseline_harness_version: str
    candidate_harness_version: str | None = None
    baseline_eval_run_ref: str | None = None
    candidate_eval_run_ref: str | None = None
    local_evidence_refs: list[str]
    provider_statuses: list[dict[str, object]] = Field(default_factory=_empty_provider_statuses)
    comparison: ExperimentComparison | None = None
