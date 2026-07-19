"""Approved case 的本地确定性 experiment evaluator。"""

from __future__ import annotations

from typing import Any, cast

from agent_harness.evals.errors import EvalExperimentError
from agent_harness.evals.experiment_models import (
    ExperimentCaseResult,
    ExperimentEvaluationResult,
)
from agent_harness.evals.harness_versions import HarnessVersionManifest
from agent_harness.storage import EvalCaseRecord, EvalDatasetSplitRecord, SQLAlchemyStorage


class RecordedApprovedCaseEvaluator:
    """用 approved case 中的脱敏期望值或版本化分数生成本地 evidence。

    Harness manifest 只保存 checksum，不能反推出 prompt/config 原文。真实执行器可以
    通过同一 protocol 注入；模板默认 adapter 因此只消费 curation 阶段已落库的
    `metadata.experiment_scores[version_id]`，缺省的 `exact_match` 则比较 payload 中
    `output` 与 `expected`。这条边界不会自动修改或执行生产配置。
    """

    def __init__(self, *, storage: SQLAlchemyStorage) -> None:
        """绑定存储 seam；评测时只读取已批准 case，不修改 curation 数据。"""

        self.storage = storage

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
        """从冻结 split 读取已批准 case，并以版本化本地证据生成确定性评测结果。

        split 中任何 case 不再可见都应报未找到，而不是静默缩小样本；这样可阻止
        删除、跨租户或状态变化导致 comparison 看似完整。输出只携带受限引用，
        不将 case payload 复制进 experiment 结果。
        """

        async with self.storage.uow() as uow:
            rows = await uow.eval_cases.list(
                tenant_id=tenant_id,
                status="approved",
                dataset=dataset,
                agent_id=agent_id,
            )
        cases = {row.case_id: row for row in rows}
        subsets = {
            **{case_id: "optimization" for case_id in split.optimization_case_ids},
            **{case_id: "holdout" for case_id in split.holdout_case_ids},
            **{case_id: "regression" for case_id in split.regression_case_ids},
        }
        threshold = _pass_threshold(evaluator_profile)
        results: list[ExperimentCaseResult] = []
        for case_id, subset in subsets.items():
            case = cases.get(case_id)
            if case is None:
                raise EvalExperimentError(
                    "eval.experiment.case_not_found",
                    "approved eval case is not visible",
                    status_code=404,
                )
            scores = _metric_scores(case, harness_version.version_id, metric_versions)
            evidence_refs = sorted(
                {
                    f"db://eval-cases/{case_id}",
                    *case.source_refs,
                    *case.artifact_refs,
                }
            )
            results.append(
                ExperimentCaseResult(
                    case_id=case_id,
                    subset=cast(Any, subset),
                    tags=split.case_tags[case_id],
                    metric_scores=scores,
                    passed=all(score >= threshold for score in scores.values()),
                    evidence_refs=evidence_refs,
                )
            )
        return ExperimentEvaluationResult(
            harness_version_id=harness_version.version_id,
            evaluator_profile=evaluator_profile,
            metric_versions=metric_versions,
            case_results=results,
            local_evidence_refs=[
                f"db://eval-dataset-splits/{split.split_id}",
                f"db://eval-evaluations/{split.split_id}/{harness_version.version_id}",
            ],
        )


def _pass_threshold(profile: dict[str, Any]) -> float:
    """解析 evaluator 的可选通过阈值，并拒绝 bool 等伪数值配置。"""

    value = profile.get("pass_threshold", 1.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvalExperimentError(
            "eval.experiment.evaluator_profile_invalid",
            "evaluator pass_threshold must be numeric",
            status_code=422,
            field_path="evaluator_profile.pass_threshold",
        )
    return float(value)


def _metric_scores(
    case: EvalCaseRecord,
    version_id: str,
    metric_versions: dict[str, str],
) -> dict[str, float]:
    """取得指定 harness 版本的指标证据，必要时仅对 exact_match 使用本地回退。

    任一声明指标缺失时必须失败，不能以零分代替；否则版本化评测会把证据缺口误报
    为模型退化，并污染后续 comparison 的推荐结论。
    """

    raw_by_version = case.metadata.get("experiment_scores", {})
    version_scores: dict[str, object] = {}
    if isinstance(raw_by_version, dict):
        raw_scores = cast(dict[str, object], raw_by_version).get(version_id)
        if isinstance(raw_scores, dict):
            version_scores = cast(dict[str, object], raw_scores)

    scores: dict[str, float] = {}
    for metric in metric_versions:
        raw_score = version_scores.get(metric)
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            scores[metric] = float(raw_score)
            continue
        if metric == "exact_match" and case.payload.get("expected") is not None:
            scores[metric] = float(case.payload.get("output") == case.payload.get("expected"))
            continue
        raise EvalExperimentError(
            "eval.experiment.metric_evidence_missing",
            "approved eval case lacks local metric evidence for this harness version",
            status_code=422,
            field_path=f"metadata.experiment_scores.{version_id}.{metric}",
        )
    return scores
