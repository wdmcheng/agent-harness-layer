"""Experiment evaluator、comparison 与 service 的公共 DTO/protocol。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Protocol, cast

from pydantic import Field, field_validator, model_validator

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.evals.dataset_models import BehaviorTag, RegressionPolicy
from agent_harness.evals.harness_versions import HarnessVersionManifest
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import EvalDatasetSplitRecord

SubsetName = Literal["optimization", "holdout", "regression"]
ExperimentStatus = Literal[
    "running",
    "baseline_completed",
    "completed",
    "failed",
    "needs_review",
    "baseline_completed_with_degradation",
    "completed_with_degradation",
]
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
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        validate_safe_evidence_refs(value, field_path="case_result.evidence_refs")
        return value

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
    local_evidence_refs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("local_evidence_refs")
    @classmethod
    def validate_local_evidence_refs(cls, value: list[str]) -> list[str]:
        validate_safe_evidence_refs(value, field_path="evaluation.local_evidence_refs")
        return value


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
    ) -> ExperimentEvaluationResult: ...


class ExperimentEvidencePublisher(Protocol):
    """可选 provider fan-out；本地 DB evidence 已先提交。"""

    provider_name: str

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]: ...


class ExperimentProviderStatus(HarnessDTO):
    """公共/provider persistence 只允许小型、脱敏、闭合状态摘要。"""

    provider: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    status: Literal["completed", "degraded"]
    detail: str | None = Field(default=None, max_length=500)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_safe_summary(self) -> ExperimentProviderStatus:
        _validate_safe_public_value(self.to_payload(), field_path="provider_status")
        return self


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
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        validate_safe_evidence_refs(value, field_path="failure_difference.evidence_refs")
        return value


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

    @field_validator("local_evidence_refs")
    @classmethod
    def validate_local_evidence_refs(cls, value: list[str]) -> list[str]:
        validate_safe_evidence_refs(value, field_path="comparison.local_evidence_refs")
        return value

    @field_validator("failure_details_ref")
    @classmethod
    def validate_failure_details_ref(cls, value: str | None) -> str | None:
        if value is not None:
            validate_safe_evidence_refs([value], field_path="comparison.failure_details_ref")
        return value


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


class ExperimentCreateBody(HarnessDTO):
    """HTTP/CLI 共用的 EVL-004 create body，不接收 identity 或 evaluator 私有项。"""

    agent_id: str = Field(min_length=1)
    dataset: str = Field(default="default", min_length=1)
    tags: list[BehaviorTag] = Field(min_length=1)
    split_strategy: Literal["deterministic_multilabel_v1"] = "deterministic_multilabel_v1"
    baseline_harness_version: HarnessVersionManifest
    candidate_harness_version: HarnessVersionManifest | None = None
    optimization_ratio: float = Field(default=0.8, gt=0.0, lt=1.0)
    holdout_ratio: float = Field(default=0.2, gt=0.0, lt=1.0)
    regression_policy: RegressionPolicy = Field(default_factory=RegressionPolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[BehaviorTag]) -> list[BehaviorTag]:
        return sorted(set(value), key=str)

    @field_validator("metadata")
    @classmethod
    def validate_safe_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_safe_public_value(value, field_path="metadata", max_bytes=16_384)
        return value

    @model_validator(mode="after")
    def validate_ratios(self) -> ExperimentCreateBody:
        if abs(self.optimization_ratio + self.holdout_ratio - 1.0) > 1e-9:
            raise ValueError("optimization_ratio and holdout_ratio must sum to 1")
        return self


class ExperimentCreateRequest(ExperimentCreateBody):
    """入口补入 identity、幂等与固定 evaluator profile 后的 service 输入。"""

    request_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    evaluator_profile: dict[str, Any] = Field(
        default_factory=lambda: {
            "name": "recorded-approved-case",
            "version": "1",
            "pass_threshold": 1.0,
        }
    )
    metric_versions: dict[str, str] = Field(default_factory=lambda: {"exact_match": "1"})


class ExperimentResult(HarnessDTO):
    request_id: str
    experiment_id: str
    status: ExperimentStatus
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

    @field_validator("local_evidence_refs")
    @classmethod
    def validate_local_evidence_refs(cls, value: list[str]) -> list[str]:
        validate_safe_evidence_refs(value, field_path="experiment.local_evidence_refs")
        return value


class ExperimentCreateOutcome(HarnessDTO):
    """让 adapter 区分 201 新建与 200 幂等重放，不暴露持久化细节。"""

    result: ExperimentResult
    created: bool


def _validate_safe_public_value(
    value: object,
    *,
    field_path: str,
    max_bytes: int = 4_096,
) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_path} must be JSON serializable") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_path} exceeds the safe inline size")
    if redact_secrets(value) != value or _contains_unsafe_string(value):
        raise ValueError(f"{field_path} contains secret-shaped or local-path data")


def validate_safe_evidence_refs(
    value: list[str],
    *,
    field_path: str,
    max_items: int = 100,
    max_bytes: int = 16_384,
) -> None:
    """统一约束公开、持久化和 provider fan-out 共用的逻辑 evidence refs。"""

    if len(value) > max_items:
        raise ValueError(f"{field_path} exceeds the safe item count")
    if any(len(ref.encode()) > 2_048 for ref in value):
        raise ValueError(f"{field_path} contains an oversized evidence ref")
    _validate_safe_public_value(value, field_path=field_path, max_bytes=max_bytes)


def bounded_public_evidence_refs(
    value: list[str],
    *,
    truth_ref: str,
    field_path: str,
) -> list[str]:
    """合并结果超出公共边界时，只保留指向完整数据库证据的稳定引用。"""

    refs = sorted({truth_ref, *value})
    validate_safe_evidence_refs(
        refs,
        field_path=f"{field_path}.source",
        max_items=2_000,
        max_bytes=65_536,
    )
    try:
        validate_safe_evidence_refs(refs, field_path=field_path)
    except ValueError:
        validate_safe_evidence_refs([truth_ref], field_path=field_path)
        return [truth_ref]
    return refs


def _contains_unsafe_string(value: object) -> bool:
    if isinstance(value, dict):
        mapping = cast(Mapping[object, object], value)
        return any(
            _contains_unsafe_string(key) or _contains_unsafe_string(item)
            for key, item in mapping.items()
        )
    if isinstance(value, list):
        return any(_contains_unsafe_string(item) for item in cast(list[object], value))
    if not isinstance(value, str):
        return False
    return (
        "[REDACTED]" in value
        or Path(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or value.casefold().startswith("file://")
    )
