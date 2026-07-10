"""Behavior tag 与 dataset split 的公共 DTO。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, cast

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError

from agent_harness.contracts.dto import HarnessDTO


class BehaviorTag(StrEnum):
    """可独立优化和汇总的初始行为类别。"""

    TOOL_SELECTION = "tool_selection"
    RETRIEVAL_QUALITY = "retrieval_quality"
    FOLLOWUP_QUALITY = "followup_quality"
    POLICY_APPROVAL = "policy_approval"
    CONTEXT_TRUST_BOUNDARY = "context_trust_boundary"


def _empty_behavior_tags() -> list[BehaviorTag]:
    return []


class RegressionPolicy(HarnessDTO):
    """Regression subset 与 comparison 门禁共用的稳定策略。

    `case_ids`、`critical_case_ids` 和 `metadata_flag` 决定 subset membership；
    `critical_case_ids` 以及命中 `critical_tags` 的 regression case 必须通过。
    `max_holdout_regression` 是允许的 holdout aggregate score 绝对下降值；
    comparison 的 `holdout_delta < -max_holdout_regression` 时视为超过阈值。
    """

    case_ids: list[str] = Field(default_factory=list)
    critical_case_ids: list[str] = Field(default_factory=list)
    metadata_flag: str = Field(default="regression", min_length=1)
    critical_tags: list[BehaviorTag] = Field(default_factory=_empty_behavior_tags)
    max_holdout_regression: float = Field(default=0.0, ge=0.0)

    @field_validator("case_ids", "critical_case_ids")
    @classmethod
    def reject_duplicate_case_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise PydanticCustomError(
                "eval.split.regression_refs_duplicate",
                "regression case refs must be unique",
            )
        return value

    @field_validator("critical_tags")
    @classmethod
    def normalize_critical_tags(cls, value: list[BehaviorTag]) -> list[BehaviorTag]:
        return sorted(set(value), key=str)

    @model_validator(mode="after")
    def reject_cross_field_duplicates(self) -> RegressionPolicy:
        if set(self.case_ids).intersection(self.critical_case_ids):
            raise PydanticCustomError(
                "eval.split.regression_refs_duplicate",
                "regression case refs cannot appear in multiple fields",
            )
        return self


class DatasetSplitRequest(HarnessDTO):
    """从 approved cases 创建 experiment split 的公共输入。"""

    request_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    dataset: str = Field(default="default", min_length=1)
    tags: list[BehaviorTag] = Field(min_length=1)
    split_strategy: Literal["deterministic_multilabel_v1"] = "deterministic_multilabel_v1"
    optimization_ratio: float = Field(default=0.8, gt=0.0, lt=1.0)
    holdout_ratio: float = Field(default=0.2, gt=0.0, lt=1.0)
    regression_policy: RegressionPolicy = Field(default_factory=RegressionPolicy)
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def parse_tags(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        allowed = ", ".join(tag.value for tag in BehaviorTag)
        parsed: list[BehaviorTag] = []
        for item in cast(list[object], value):
            try:
                parsed.append(BehaviorTag(str(item)))
            except ValueError as exc:
                raise PydanticCustomError(
                    "eval.split.tags_invalid",
                    "unsupported behavior tag; allowed: {allowed}",
                    {"allowed": allowed},
                ) from exc
        return parsed

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, value: list[BehaviorTag]) -> list[BehaviorTag]:
        return sorted(set(value), key=str)

    @field_validator("holdout_ratio")
    @classmethod
    def validate_ratios(cls, value: float, info: ValidationInfo) -> float:
        optimization_ratio = info.data.get("optimization_ratio")
        if (
            isinstance(optimization_ratio, float)
            and abs(optimization_ratio + value - 1.0) > 1e-9
        ):
            raise PydanticCustomError(
                "eval.split.ratios_invalid",
                "optimization_ratio and holdout_ratio must sum to 1",
            )
        return value


class DatasetSplitPlan(HarnessDTO):
    """可持久化且不携带完整 case payload 的 split 结果。"""

    split_id: str
    request_id: str
    tenant_id: str
    agent_id: str
    dataset: str
    tags: list[BehaviorTag]
    split_strategy: str
    optimization_ratio: float
    holdout_ratio: float
    regression_policy: RegressionPolicy
    optimization_case_ids: list[str]
    holdout_case_ids: list[str]
    regression_case_ids: list[str]
    optimization_case_count: int
    holdout_case_count: int
    regression_case_count: int
    case_tags: dict[str, list[BehaviorTag]]
    tag_distribution: dict[str, dict[str, int]]
    rejected_counts: dict[str, int]
    evidence_refs: list[str]
