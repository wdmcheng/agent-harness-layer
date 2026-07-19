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
    """为 Pydantic 提供独立的默认列表，避免不同实验结果共享可变状态。"""

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
        """限制单个 case 暴露的证据引用，防止把本地路径或秘密带入结果。"""

        validate_safe_evidence_refs(value, field_path="case_result.evidence_refs")
        return value

    @property
    def aggregate_score(self) -> float:
        """返回指标分数的简单均值；没有可比较指标时稳定地返回零。"""

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
        """校验本地耐久证据引用仍适合出现在评估结果的公共边界。"""

        validate_safe_evidence_refs(value, field_path="evaluation.local_evidence_refs")
        return value


class ExperimentEvaluationFailure(RuntimeError):
    """Evaluator 失败并携带已经完成、已落本地 evidence 的 case 结果。"""

    def __init__(self, message: str, *, partial_result: ExperimentEvaluationResult) -> None:
        """保留可恢复的局部结果，供调用方落证据后再报告原始失败原因。"""

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
        """在固定数据切分和版本上执行评估，并返回可比较的完整 case 结果。"""

        ...


class ExperimentEvidencePublisher(Protocol):
    """可选 provider fan-out；本地 DB evidence 已先提交。"""

    provider_name: str

    async def publish(self, payload: dict[str, Any]) -> dict[str, object]:
        """在本地证据已经提交后向单个外部 provider 发布脱敏摘要。"""

        ...


class ExperimentProviderStatus(HarnessDTO):
    """公共/provider persistence 只允许小型、脱敏、闭合状态摘要。"""

    provider: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    status: Literal["completed", "degraded"]
    detail: str | None = Field(default=None, max_length=500)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_safe_summary(self) -> ExperimentProviderStatus:
        """在状态摘要入库或扇出前复用公共值的大小与脱敏校验。"""

        _validate_safe_public_value(self.to_payload(), field_path="provider_status")
        return self


class PerTagComparison(HarnessDTO):
    """同一标签在基线与候选版本间的可审计分数对比。"""

    tag: str
    baseline_score: float
    candidate_score: float
    delta: float


class FailureDifference(HarnessDTO):
    """描述某个 case 在两份评估结果间的失败变化，并只携带安全证据引用。"""

    case_id: str
    subset: SubsetName
    tags: list[str]
    baseline_score: float
    candidate_score: float
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: list[str]) -> list[str]:
        """确保失败变化不会因调试证据而突破公共结果边界。"""

        validate_safe_evidence_refs(value, field_path="failure_difference.evidence_refs")
        return value


class ExperimentComparison(HarnessDTO):
    """候选版本相对基线的汇总比较结果。

    大型失败明细刻意不参与公开 DTO 序列化，只通过稳定引用指向本地证据，避免接口
    响应和外部发布通道承载未经脱敏的调试内容。
    """

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
        """约束比较汇总中可公开的本地证据引用集合。"""

        validate_safe_evidence_refs(value, field_path="comparison.local_evidence_refs")
        return value

    @field_validator("failure_details_ref")
    @classmethod
    def validate_failure_details_ref(cls, value: str | None) -> str | None:
        """若提供明细引用，按单元素证据集合复用同一安全规则。"""

        if value is not None:
            validate_safe_evidence_refs([value], field_path="comparison.failure_details_ref")
        return value


class ExperimentRequest(HarnessDTO):
    """内部 service 输入；HTTP adapter 负责创建 split 并补默认 evaluator。

    与入口 body 分离后，执行服务不必信任 HTTP 层传来的身份、幂等键或评估器配置。
    """

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
        """去重并排序标签，保证数据切分和幂等身份不依赖请求数组顺序。"""

        return sorted(set(value), key=str)

    @field_validator("metadata")
    @classmethod
    def validate_safe_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        """仅允许可序列化、脱敏且有明确大小上限的调用方元数据。"""

        _validate_safe_public_value(value, field_path="metadata", max_bytes=16_384)
        return value

    @model_validator(mode="after")
    def validate_ratios(self) -> ExperimentCreateBody:
        """要求优化集与留出集覆盖完整数据集，避免悄悄遗失或重复 case。"""

        # 使用小容差兼容十进制字面量转为二进制浮点后的表示误差。
        if abs(self.optimization_ratio + self.holdout_ratio - 1.0) > 1e-9:
            raise ValueError("optimization_ratio and holdout_ratio must sum to 1")
        return self


class ExperimentCreateRequest(ExperimentCreateBody):
    """入口补入 identity、幂等与固定 evaluator profile 后的 service 输入。

    默认 profile 只用于未显式配置的受控入口，持久化后由请求记录冻结，不能在重放时
    根据当前默认值重新计算。
    """

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
    """创建或查询实验时返回的稳定摘要，不直接嵌入大型本地失败证据。"""

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
        """校验实验摘要引用，保证 API、存储和 provider 扇出使用相同边界。"""

        validate_safe_evidence_refs(value, field_path="experiment.local_evidence_refs")
        return value


class ExperimentCreateOutcome(HarnessDTO):
    """让 adapter 区分 201 新建与 200 幂等重放，不暴露持久化细节。

    ``created`` 只描述本次请求是否新建记录，不能被客户端当作实验执行是否成功的状态。
    """

    result: ExperimentResult
    created: bool


def _validate_safe_public_value(
    value: object,
    *,
    field_path: str,
    max_bytes: int = 4_096,
) -> None:
    """验证任意将跨存储、接口或 provider 边界传递的值可安全内联。

    这里先以稳定 JSON 序列化测量真实字节数，再拒绝秘密形态和本地绝对路径；不能只
    依赖调用方字段名，因为嵌套 metadata 或 provider 明细也可能携带敏感内容。
    """

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
    """统一约束公开、持久化和 provider fan-out 共用的逻辑 evidence refs。

    除逐项长度外，整个集合也要通过公共值校验；这样既限制响应体膨胀，也阻止引用中
    混入秘密、绝对路径或其他仅能留在本地诊断记录中的内容。
    """

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
    """合并结果超出公共边界时，只保留指向完整数据库证据的稳定引用。

    truth reference 必须始终保留：截断后调用方仍能沿着该引用取得完整事实，不能因
    为发布通道的长度限制而丢失审计可追溯性。
    """

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
        # 明细超过公共边界并非执行失败；降级为单一真相引用，完整证据仍在本地。
        validate_safe_evidence_refs([truth_ref], field_path=field_path)
        return [truth_ref]
    return refs


def _contains_unsafe_string(value: object) -> bool:
    """递归识别会泄露本地位置或与脱敏结果混淆的文本形态。"""

    if isinstance(value, dict):
        mapping = cast(Mapping[object, object], value)
        # 字典键也可能包含路径或秘密标签，不能只检查值。
        return any(
            _contains_unsafe_string(key) or _contains_unsafe_string(item)
            for key, item in mapping.items()
        )
    if isinstance(value, list):
        # 只沿 JSON 可表示的容器递归；其他对象已由序列化校验拒绝。
        return any(_contains_unsafe_string(item) for item in cast(list[object], value))
    if not isinstance(value, str):
        return False
    return (
        "[REDACTED]" in value
        # 两套 Path 判断覆盖运行环境和调用方可能传入的 Windows 文本路径。
        or Path(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or value.casefold().startswith("file://")
    )
