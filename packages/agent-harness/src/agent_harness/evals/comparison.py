"""基于持久化 evaluator 结果生成确定性 comparison。"""

from __future__ import annotations

import hashlib
import json

from agent_harness.evals.dataset_models import RegressionPolicy
from agent_harness.evals.experiment_models import (
    ExperimentCaseResult,
    ExperimentComparison,
    ExperimentEvaluationResult,
    FailureDifference,
    PerTagComparison,
    RecommendationReasonCode,
    bounded_public_evidence_refs,
)

REASON_CODE_ORDER: tuple[RecommendationReasonCode, ...] = (
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
)


class ExperimentComparisonBuilder:
    """按标签、holdout 与关键 regression 计算三态 recommendation。"""

    def __init__(self, *, failure_inline_limit: int = 100) -> None:
        self.failure_inline_limit = max(1, failure_inline_limit)

    def build(
        self,
        *,
        experiment_id: str,
        requested_tags: list[str],
        baseline: ExperimentEvaluationResult,
        candidate: ExperimentEvaluationResult,
        regression_policy: RegressionPolicy,
        authoritative_case_tags: dict[str, list[str]],
        request_id: str | None = None,
    ) -> ExperimentComparison:
        baseline_by_id = _by_case_id(baseline.case_results)
        candidate_by_id = _by_case_id(candidate.case_results)
        common_ids = sorted(set(baseline_by_id).intersection(candidate_by_id))
        structurally_complete = (
            set(baseline_by_id) == set(candidate_by_id)
            and baseline.evaluator_profile == candidate.evaluator_profile
            and baseline.metric_versions == candidate.metric_versions
            and set(authoritative_case_tags) == set(baseline_by_id)
            and len(baseline_by_id) == len(baseline.case_results)
            and len(candidate_by_id) == len(candidate.case_results)
        )
        declared_metrics = set(baseline.metric_versions)
        for case_id in common_ids:
            expected_tags = sorted(set(authoritative_case_tags.get(case_id, [])))
            if (
                sorted(set(baseline_by_id[case_id].tags)) != expected_tags
                or sorted(set(candidate_by_id[case_id].tags)) != expected_tags
                or set(baseline_by_id[case_id].metric_scores)
                != set(candidate_by_id[case_id].metric_scores)
                or set(baseline_by_id[case_id].metric_scores) != declared_metrics
            ):
                structurally_complete = False

        per_tag: list[PerTagComparison] = []
        for tag in requested_tags:
            tagged_ids = [
                case_id
                for case_id in common_ids
                if baseline_by_id[case_id].subset == "optimization"
                and candidate_by_id[case_id].subset == "optimization"
                and tag in authoritative_case_tags.get(case_id, [])
            ]
            if not tagged_ids:
                structurally_complete = False
                continue
            baseline_score = _mean(
                [baseline_by_id[case_id].aggregate_score for case_id in tagged_ids]
            )
            candidate_score = _mean(
                [candidate_by_id[case_id].aggregate_score for case_id in tagged_ids]
            )
            per_tag.append(
                PerTagComparison(
                    tag=tag,
                    baseline_score=baseline_score,
                    candidate_score=candidate_score,
                    delta=candidate_score - baseline_score,
                )
            )

        holdout_ids = [
            case_id
            for case_id in common_ids
            if baseline_by_id[case_id].subset == "holdout"
            and candidate_by_id[case_id].subset == "holdout"
        ]
        if not holdout_ids:
            structurally_complete = False
            holdout_delta = 0.0
        else:
            holdout_delta = _mean(
                [candidate_by_id[case_id].aggregate_score for case_id in holdout_ids]
            ) - _mean([baseline_by_id[case_id].aggregate_score for case_id in holdout_ids])

        regressions: list[FailureDifference] = []
        new_failures: list[FailureDifference] = []
        fixed_failures: list[FailureDifference] = []
        truth_ref = f"db://eval-experiments/{experiment_id}"
        for case_id in common_ids:
            baseline_case = baseline_by_id[case_id]
            candidate_case = candidate_by_id[case_id]
            difference = _failure_difference(
                baseline_case,
                candidate_case,
                tags=authoritative_case_tags.get(case_id, []),
                truth_ref=truth_ref,
            )
            if candidate_case.aggregate_score < baseline_case.aggregate_score:
                regressions.append(difference)
            if baseline_case.passed and not candidate_case.passed:
                new_failures.append(difference)
            if not baseline_case.passed and candidate_case.passed:
                fixed_failures.append(difference)

        critical_ids = set(regression_policy.critical_case_ids)
        critical_tags = {tag.value for tag in regression_policy.critical_tags}
        critical_cases = [
            candidate_by_id[case_id]
            for case_id in common_ids
            if candidate_by_id[case_id].subset == "regression"
            and (
                case_id in critical_ids
                or bool(critical_tags.intersection(authoritative_case_tags.get(case_id, [])))
            )
        ]
        expected_critical_ids = critical_ids.intersection(candidate_by_id)
        if expected_critical_ids != critical_ids:
            structurally_complete = False
        critical_passed = all(item.passed for item in critical_cases)

        local_refs = sorted({*baseline.local_evidence_refs, *candidate.local_evidence_refs})
        local_complete = bool(baseline.local_evidence_refs and candidate.local_evidence_refs)
        reason_codes: list[RecommendationReasonCode] = []
        if not local_complete or not structurally_complete:
            if not local_complete:
                reason_codes.append("local_evidence_incomplete")
            reason_codes.append("comparison_incomplete")
            recommendation = "needs_review"
        else:
            target_improved = any(item.delta > 0 for item in per_tag)
            reason_codes.append(
                "target_tag_improved" if target_improved else "no_target_improvement"
            )
            named_failure_ids = {
                *regression_policy.case_ids,
                *regression_policy.critical_case_ids,
            }
            named_failures_fixed = any(item.case_id in named_failure_ids for item in fixed_failures)
            if named_failures_fixed:
                reason_codes.append("named_failure_fixed")
            holdout_within_threshold = holdout_delta >= -regression_policy.max_holdout_regression
            reason_codes.append(
                "holdout_within_threshold"
                if holdout_within_threshold
                else "holdout_regression_exceeded"
            )
            reason_codes.append(
                "critical_regression_passed" if critical_passed else "critical_regression_failed"
            )
            if new_failures:
                reason_codes.append("new_failures_present")
            if not holdout_within_threshold or not critical_passed or new_failures:
                recommendation = "reject"
            elif target_improved or named_failures_fixed:
                recommendation = "accept"
            else:
                recommendation = "needs_review"

        full_failure_payload = {
            "regressions": [item.to_payload() for item in regressions],
            "new_failures": [item.to_payload() for item in new_failures],
            "fixed_failures": [item.to_payload() for item in fixed_failures],
        }
        failure_count = sum(len(items) for items in full_failure_payload.values())
        failure_details_ref: str | None = None
        if failure_count > self.failure_inline_limit:
            encoded = json.dumps(full_failure_payload, ensure_ascii=False, sort_keys=True).encode()
            checksum = hashlib.sha256(encoded).hexdigest()
            failure_details_ref = (
                f"db://eval-experiments/{experiment_id}/failure-details/{checksum}"
            )
            local_refs.append(failure_details_ref)
            regressions = regressions[: self.failure_inline_limit]
            new_failures = new_failures[: self.failure_inline_limit]
            fixed_failures = fixed_failures[: self.failure_inline_limit]

        ordered_reasons: list[RecommendationReasonCode] = []
        for code in REASON_CODE_ORDER:
            if code in reason_codes:
                ordered_reasons.append(code)
        return ExperimentComparison(
            request_id=request_id,
            experiment_id=experiment_id,
            candidate_harness_version=candidate.harness_version_id,
            per_tag=per_tag,
            holdout_delta=holdout_delta,
            regressions=regressions,
            new_failures=new_failures,
            fixed_failures=fixed_failures,
            acceptance_recommendation=recommendation,
            recommendation_reason_codes=ordered_reasons,
            local_evidence_refs=bounded_public_evidence_refs(
                local_refs,
                truth_ref=truth_ref,
                field_path="comparison.local_evidence_refs",
            ),
            failure_details_ref=failure_details_ref,
            failure_details=(
                {}
                if failure_details_ref is None
                else {
                    key: [dict(item) for item in items]
                    for key, items in full_failure_payload.items()
                }
            ),
        )


def _by_case_id(results: list[ExperimentCaseResult]) -> dict[str, ExperimentCaseResult]:
    return {result.case_id: result for result in results}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _failure_difference(
    baseline: ExperimentCaseResult,
    candidate: ExperimentCaseResult,
    *,
    tags: list[str],
    truth_ref: str,
) -> FailureDifference:
    return FailureDifference(
        case_id=baseline.case_id,
        subset=baseline.subset,
        tags=sorted(set(tags)),
        baseline_score=baseline.aggregate_score,
        candidate_score=candidate.aggregate_score,
        evidence_refs=bounded_public_evidence_refs(
            sorted(set(baseline.evidence_refs).union(candidate.evidence_refs)),
            truth_ref=truth_ref,
            field_path="comparison.failure_difference.evidence_refs",
        ),
    )
