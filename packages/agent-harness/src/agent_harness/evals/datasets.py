"""Approved eval case 合格性门禁与 dataset split service。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PureWindowsPath
from typing import cast

from agent_harness.evals.dataset_models import (
    BehaviorTag,
    DatasetSplitPlan,
    DatasetSplitRequest,
)
from agent_harness.evals.errors import DatasetSplitError
from agent_harness.evals.split_strategy import select_holdout, split_id, tag_distribution
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import EvalCaseRecord


class DatasetSplitService:
    """先做安全/归属门禁，再生成确定性多标签 split。"""

    def build(
        self,
        request: DatasetSplitRequest,
        cases: list[EvalCaseRecord],
    ) -> DatasetSplitPlan:
        requested_tags = set(request.tags)
        rejected: dict[str, int] = {}
        eligible: list[EvalCaseRecord] = []
        case_tags: dict[str, list[BehaviorTag]] = {}

        for case in cases:
            # 归属检查必须早于 status/tag 过滤，否则跨租户 draft 会泄漏 count。
            if (
                case.tenant_id != request.tenant_id
                or case.agent_id != request.agent_id
                or case.dataset != request.dataset
            ):
                raise DatasetSplitError(
                    "eval.split.case_not_found",
                    "eval case is not visible",
                    status_code=404,
                )
            if case.status != "approved":
                _increment(rejected, "not_approved")
                continue
            tags = _behavior_tags(case)
            if _contains_secret(case):
                raise DatasetSplitError(
                    "eval.split.secret_unsafe",
                    "eval case did not pass secret eligibility",
                    status_code=422,
                    field_path="cases",
                )
            if not requested_tags.intersection(tags):
                _increment(rejected, "tag_mismatch")
                continue
            eligible.append(case)
            case_tags[case.case_id] = tags

        eligible_by_id = {case.case_id: case for case in eligible}
        for field_name, references in (
            ("case_ids", request.regression_policy.case_ids),
            ("critical_case_ids", request.regression_policy.critical_case_ids),
        ):
            if not set(references).issubset(eligible_by_id):
                raise DatasetSplitError(
                    "eval.split.regression_case_not_found",
                    "regression case is not visible",
                    status_code=404,
                    field_path=f"regression_policy.{field_name}",
                )
        explicit_regression = {
            *request.regression_policy.case_ids,
            *request.regression_policy.critical_case_ids,
        }
        regression_ids = {
            case.case_id
            for case in eligible
            if case.case_id in explicit_regression
            or case.metadata.get(request.regression_policy.metadata_flag) is True
        }
        remaining = [case for case in eligible if case.case_id not in regression_ids]
        if len(remaining) < 2:
            raise DatasetSplitError(
                "eval.split.insufficient_cases",
                "split requires nonempty optimization and holdout subsets",
                status_code=422,
                field_path="cases",
            )

        holdout_size = max(
            1,
            min(len(remaining) - 1, int(len(remaining) * request.holdout_ratio + 0.5)),
        )
        holdout_ids = select_holdout(
            request=request,
            case_ids=[case.case_id for case in remaining],
            case_tags=case_tags,
            target_size=holdout_size,
        )
        optimization_ids = {case.case_id for case in remaining} - holdout_ids
        optimization = sorted(optimization_ids)
        holdout = sorted(holdout_ids)
        regression = sorted(regression_ids)
        distribution = tag_distribution(
            request.tags,
            case_tags,
            optimization=optimization,
            holdout=holdout,
            regression=regression,
        )
        evidence_refs = _safe_evidence_refs(
            [
                *request.evidence_refs,
                *(
                    ref
                    for case in eligible
                    for ref in [*case.source_refs, *case.artifact_refs]
                ),
            ]
        )
        return DatasetSplitPlan(
            split_id=split_id(
                request=request,
                optimization=optimization,
                holdout=holdout,
                regression=regression,
            ),
            request_id=request.request_id,
            tenant_id=request.tenant_id,
            agent_id=request.agent_id,
            dataset=request.dataset,
            tags=request.tags,
            split_strategy=request.split_strategy,
            optimization_ratio=request.optimization_ratio,
            holdout_ratio=request.holdout_ratio,
            regression_policy=request.regression_policy,
            optimization_case_ids=optimization,
            holdout_case_ids=holdout,
            regression_case_ids=regression,
            optimization_case_count=len(optimization),
            holdout_case_count=len(holdout),
            regression_case_count=len(regression),
            case_tags={case_id: case_tags[case_id] for case_id in sorted(case_tags)},
            tag_distribution=distribution,
            rejected_counts={key: rejected[key] for key in sorted(rejected)},
            evidence_refs=evidence_refs,
        )


def _behavior_tags(case: EvalCaseRecord) -> list[BehaviorTag]:
    raw_tags = case.metadata.get("behavior_tags")
    if not isinstance(raw_tags, list) or not raw_tags:
        raise DatasetSplitError(
            "eval.split.tags_required",
            "approved eval case requires behavior tags",
            status_code=422,
            field_path="metadata.behavior_tags",
        )
    try:
        tags = [BehaviorTag(str(tag)) for tag in cast(list[object], raw_tags)]
    except ValueError as exc:
        allowed = ", ".join(tag.value for tag in BehaviorTag)
        raise DatasetSplitError(
            "eval.split.tags_invalid",
            "approved eval case contains an unsupported behavior tag",
            status_code=422,
            field_path="metadata.behavior_tags",
            hint=f"allowed: {allowed}",
        ) from exc
    return sorted(set(tags), key=str)


def _contains_secret(case: EvalCaseRecord) -> bool:
    payload = {"payload": case.payload, "metadata": case.metadata}
    if _contains_redaction_marker(payload):
        return True
    return redact_secrets(payload) != payload


def _contains_redaction_marker(value: object) -> bool:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return any(_contains_redaction_marker(item) for item in mapping.values())
    if isinstance(value, list):
        items = cast(list[object], value)
        return any(_contains_redaction_marker(item) for item in items)
    return isinstance(value, str) and "[REDACTED]" in value


def _safe_evidence_refs(refs: list[str]) -> list[str]:
    """公共 DTO 只接受脱敏的 URI/逻辑引用，不接受本机绝对路径。"""

    for ref in refs:
        unsafe_path = (
            Path(ref).is_absolute()
            or PureWindowsPath(ref).is_absolute()
            or ref.casefold().startswith("file://")
        )
        if unsafe_path or _contains_redaction_marker(ref) or redact_secrets(ref) != ref:
            raise DatasetSplitError(
                "eval.split.evidence_ref_unsafe",
                "evidence ref contains a secret or absolute local path",
                status_code=422,
                field_path="evidence_refs",
            )
    return sorted(set(refs))


def _increment(values: dict[str, int], key: str) -> None:
    values[key] = values.get(key, 0) + 1
