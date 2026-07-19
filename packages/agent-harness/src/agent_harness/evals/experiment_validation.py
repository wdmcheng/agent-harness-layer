"""Evaluator 输出与冻结 split 输入之间的边界校验。"""

from __future__ import annotations

from typing import Any

from agent_harness.evals.errors import EvalExperimentError
from agent_harness.evals.experiment_models import (
    ExperimentComparison,
    ExperimentEvaluationResult,
    bounded_public_evidence_refs,
    validate_safe_evidence_refs,
)
from agent_harness.storage import EvalDatasetSplitRecord


def validate_evaluation(
    *,
    split: EvalDatasetSplitRecord,
    result: ExperimentEvaluationResult,
    expected_version: str,
    evaluator_profile: dict[str, Any],
    metric_versions: dict[str, str],
) -> None:
    """验证完整 evaluator 输出严格对应冻结 split、harness、profile 与指标版本。

    此检查同时拒绝重复 case、子集错配、标签漂移和指标集合变化，防止外部 evaluator
    在实验已经创建后替换输入语义；证据引用先经独立边界校验，避免错误内容进入存储。
    """

    _validate_result_evidence(result)
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


def validate_partial_evaluation(
    *,
    split: EvalDatasetSplitRecord,
    result: ExperimentEvaluationResult,
    evaluator_profile: dict[str, Any],
    metric_versions: dict[str, str],
) -> None:
    """验证可恢复执行期间的局部输出，不要求覆盖全部 split 但禁止任何越界 case。

    恢复累计前可缺少部分 case；一旦已有条目，则其 subset、标签和指标版本必须与
    冻结输入完全一致，且不可重复，避免断点恢复拼接出互相矛盾的结果。
    """

    _validate_result_evidence(result)
    expected_subsets = {
        **{case_id: "optimization" for case_id in split.optimization_case_ids},
        **{case_id: "holdout" for case_id in split.holdout_case_ids},
        **{case_id: "regression" for case_id in split.regression_case_ids},
    }
    expected_metrics = set(metric_versions)
    seen: set[str] = set()
    valid = (
        result.evaluator_profile == evaluator_profile and result.metric_versions == metric_versions
    )
    for item in result.case_results:
        if (
            item.case_id in seen
            or expected_subsets.get(item.case_id) != item.subset
            or set(item.metric_scores) != expected_metrics
            or sorted(set(item.tags)) != sorted(set(split.case_tags.get(item.case_id, [])))
        ):
            valid = False
        seen.add(item.case_id)
    if not valid:
        raise EvalExperimentError(
            "eval.experiment.partial_evaluation_mismatch",
            "partial evaluator result does not match fixed experiment inputs",
            status_code=422,
        )


def local_refs(
    experiment_id: str,
    baseline: ExperimentEvaluationResult,
    candidate: ExperimentEvaluationResult | None,
    comparison: ExperimentComparison | None,
) -> list[str]:
    """合并实验的局部证据引用，并按公开 DTO 的数量、大小和真相源约束裁剪。"""

    truth_ref = f"db://eval-experiments/{experiment_id}"
    return bounded_public_evidence_refs(
        [
            *baseline.local_evidence_refs,
            *(candidate.local_evidence_refs if candidate is not None else []),
            *(comparison.local_evidence_refs if comparison is not None else []),
        ],
        truth_ref=truth_ref,
        field_path="experiment.local_evidence_refs",
    )


def _validate_result_evidence(result: ExperimentEvaluationResult) -> None:
    """检查局部与 case 级证据引用都安全、总量受限且不包含本机敏感路径。"""

    try:
        validate_safe_evidence_refs(
            result.local_evidence_refs,
            field_path="evaluation.local_evidence_refs",
        )
        for index, item in enumerate(result.case_results):
            validate_safe_evidence_refs(
                item.evidence_refs,
                field_path=f"evaluation.case_results.{index}.evidence_refs",
            )
        refs = [
            *result.local_evidence_refs,
            *(ref for item in result.case_results for ref in item.evidence_refs),
        ]
        validate_safe_evidence_refs(
            refs,
            field_path="evaluation.evidence_refs",
            max_items=2_000,
            max_bytes=65_536,
        )
    except ValueError as exc:
        raise EvalExperimentError(
            "eval.experiment.evidence_invalid",
            "evaluator evidence refs failed the safe bounded contract",
            status_code=422,
            field_path="evaluation.evidence_refs",
        ) from exc
