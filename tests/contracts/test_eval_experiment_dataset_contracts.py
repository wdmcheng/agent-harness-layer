"""Eval experiment 行为标签与 dataset split 公共 seam 合同测试。"""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from agent_harness.storage import EvalCaseRecord


def _case(
    case_id: str,
    *,
    tags: list[str] | None,
    status: str = "approved",
    tenant_id: str = "default",
    agent_id: str = "examples.basic",
    dataset: str = "default",
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> EvalCaseRecord:
    case_metadata = dict(metadata or {})
    if tags is not None:
        case_metadata["behavior_tags"] = tags
    return EvalCaseRecord(
        case_id=case_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        name=case_id,
        status=status,
        dataset=dataset,
        payload=payload or {"expected": {"answer": case_id}},
        metadata=case_metadata,
    )


def _request(**updates: Any):
    from agent_harness.evals import DatasetSplitRequest

    values: dict[str, Any] = {
        "request_id": "req-split",
        "tenant_id": "default",
        "agent_id": "examples.basic",
        "dataset": "default",
        "tags": ["retrieval_quality", "tool_selection"],
        "split_strategy": "deterministic_multilabel_v1",
    }
    values.update(updates)
    return DatasetSplitRequest(**values)


def test_split_is_deterministic_disjoint_and_tracks_tag_distribution() -> None:
    from agent_harness.evals import BehaviorTag, DatasetSplitService, RegressionPolicy

    cases = [
        _case("case-1", tags=["retrieval_quality"], metadata={"regression": True}),
        _case("case-2", tags=["retrieval_quality"]),
        _case("case-3", tags=["retrieval_quality", "tool_selection"]),
        _case("case-4", tags=["retrieval_quality"]),
        _case("case-5", tags=["tool_selection"]),
        _case("case-6", tags=["tool_selection"]),
        _case("case-7", tags=["tool_selection"]),
        _case("case-8", tags=["tool_selection"]),
    ]
    request = _request(
        regression_policy=RegressionPolicy(
            case_ids=["case-1"],
            critical_tags=[BehaviorTag.RETRIEVAL_QUALITY],
            max_holdout_regression=0.05,
        )
    )

    first = DatasetSplitService().build(request, cases)
    second = DatasetSplitService().build(request, list(reversed(cases)))

    assert first.model_dump() == second.model_dump()
    optimization = set(first.optimization_case_ids)
    holdout = set(first.holdout_case_ids)
    regression = set(first.regression_case_ids)
    assert regression == {"case-1"}
    assert optimization and holdout
    assert not (optimization & holdout or optimization & regression or holdout & regression)
    assert optimization | holdout | regression == {f"case-{index}" for index in range(1, 9)}
    assert first.optimization_case_count == len(optimization)
    assert first.holdout_case_count == len(holdout)
    assert first.regression_case_count == 1
    assert first.tag_distribution["retrieval_quality"]["regression"] == 1
    assert first.tag_distribution["tool_selection"]["optimization"] >= 1
    assert first.tag_distribution["tool_selection"]["holdout"] >= 1
    assert first.regression_policy.critical_tags == [BehaviorTag.RETRIEVAL_QUALITY]
    assert first.regression_policy.max_holdout_regression == 0.05


def test_approved_cases_can_be_queried_filtered_and_counted_by_behavior_tag() -> None:
    from agent_harness.evals import BehaviorTag, BehaviorTagQuery, DatasetSplitService

    result = DatasetSplitService().query_approved(
        BehaviorTagQuery(
            tenant_id="default",
            agent_id="examples.basic",
            dataset="default",
            tag=BehaviorTag.RETRIEVAL_QUALITY,
        ),
        [
            _case("retrieval-2", tags=["retrieval_quality"]),
            _case("tool-only", tags=["tool_selection"]),
            _case("retrieval-1", tags=["retrieval_quality", "tool_selection"]),
            _case("retrieval-draft", tags=["retrieval_quality"], status="draft"),
        ],
    )

    assert result.case_ids == ["retrieval-1", "retrieval-2"]
    assert result.case_count == 2
    assert result.tag == "retrieval_quality"
    assert "payload" not in result.to_payload()

    with pytest.raises(ValidationError) as invalid:
        BehaviorTagQuery(
            tenant_id="default",
            agent_id="examples.basic",
            tag=cast(Any, "unknown_behavior"),
        )
    assert invalid.value.errors()[0]["loc"] == ("tag",)


def test_split_filters_drafts_and_unrequested_tags_without_scoring_them() -> None:
    from agent_harness.evals import DatasetSplitService

    result = DatasetSplitService().build(
        _request(tags=["tool_selection"]),
        [
            _case("approved-1", tags=["tool_selection"]),
            _case("approved-2", tags=["tool_selection"]),
            _case("draft-1", tags=["tool_selection"], status="draft"),
            _case("other-tag", tags=["retrieval_quality"]),
        ],
    )

    members = set(result.optimization_case_ids + result.holdout_case_ids)
    assert members == {"approved-1", "approved-2"}
    assert result.rejected_counts == {"not_approved": 1, "tag_mismatch": 1}


@pytest.mark.parametrize(
    ("case", "error_code"),
    [
        (
            _case(
                "secret-case",
                tags=["tool_selection"],
                payload={"api_key": "[REDACTED]"},
            ),
            "eval.split.secret_unsafe",
        ),
        (_case("missing-tags", tags=None), "eval.split.tags_required"),
    ],
)
def test_split_rejects_secret_or_untagged_approved_cases(
    case: EvalCaseRecord,
    error_code: str,
) -> None:
    from agent_harness.evals import DatasetSplitError, DatasetSplitService

    with pytest.raises(DatasetSplitError) as captured:
        DatasetSplitService().build(
            _request(tags=["tool_selection"]),
            [
                case,
                _case("safe-1", tags=["tool_selection"]),
                _case("safe-2", tags=["tool_selection"]),
            ],
        )

    assert captured.value.code == error_code
    assert "[REDACTED]" not in str(captured.value)


def test_split_hides_cross_tenant_case_and_rejects_invalid_regression_ref() -> None:
    from agent_harness.evals import DatasetSplitError, DatasetSplitService, RegressionPolicy

    cross_tenant = _case("private-case", tags=["tool_selection"], tenant_id="other")
    with pytest.raises(DatasetSplitError) as captured:
        DatasetSplitService().build(
            _request(tags=["tool_selection"]),
            [
                cross_tenant,
                _case("safe-1", tags=["tool_selection"]),
                _case("safe-2", tags=["tool_selection"]),
            ],
        )
    assert captured.value.code == "eval.split.case_not_found"
    assert "private-case" not in str(captured.value)

    with pytest.raises(DatasetSplitError) as captured:
        DatasetSplitService().build(
            _request(
                tags=["tool_selection"],
                regression_policy=RegressionPolicy(case_ids=["missing-case"]),
            ),
            [_case("safe-1", tags=["tool_selection"]), _case("safe-2", tags=["tool_selection"])],
        )
    assert captured.value.code == "eval.split.regression_case_not_found"
    assert "missing-case" not in str(captured.value)

    with pytest.raises(DatasetSplitError) as captured:
        DatasetSplitService().build(
            _request(tags=["tool_selection"]),
            [
                _case(
                    "private-draft",
                    tags=["tool_selection"],
                    tenant_id="other",
                    status="draft",
                ),
                _case("safe-1", tags=["tool_selection"]),
                _case("safe-2", tags=["tool_selection"]),
            ],
        )
    assert captured.value.code == "eval.split.case_not_found"


def test_split_request_rejects_unknown_tags_invalid_ratios_and_duplicate_regression_refs() -> None:
    from agent_harness.evals import RegressionPolicy

    with pytest.raises(ValidationError) as captured:
        _request(tags=["unknown-tag"])
    assert captured.value.errors()[0]["type"] == "eval.split.tags_invalid"
    assert captured.value.errors()[0]["loc"] == ("tags",)
    assert "tool_selection" in captured.value.errors()[0]["msg"]

    with pytest.raises(ValidationError) as captured:
        _request(optimization_ratio=0.9, holdout_ratio=0.2)
    assert captured.value.errors()[0]["type"] == "eval.split.ratios_invalid"
    assert captured.value.errors()[0]["loc"] == ("holdout_ratio",)

    with pytest.raises(ValidationError) as captured:
        RegressionPolicy(case_ids=["case-1", "case-1"])
    assert captured.value.errors()[0]["type"] == "eval.split.regression_refs_duplicate"
    assert captured.value.errors()[0]["loc"] == ("case_ids",)

    with pytest.raises(ValidationError) as captured:
        RegressionPolicy(case_ids=["case-1"], critical_case_ids=["case-1"])
    assert captured.value.errors()[0]["type"] == "eval.split.regression_refs_duplicate"
    assert captured.value.errors()[0]["loc"] == ()

    with pytest.raises(ValidationError) as captured:
        _request(split_strategy="random")
    assert captured.value.errors()[0]["type"] == "literal_error"
    assert captured.value.errors()[0]["loc"] == ("split_strategy",)
    assert "deterministic_multilabel_v1" in captured.value.errors()[0]["msg"]


def test_split_requires_nonempty_optimization_and_holdout() -> None:
    from agent_harness.evals import DatasetSplitError, DatasetSplitService

    with pytest.raises(DatasetSplitError) as captured:
        DatasetSplitService().build(
            _request(tags=["tool_selection"]),
            [_case("only-case", tags=["tool_selection"])],
        )

    assert captured.value.code == "eval.split.insufficient_cases"


def test_split_preserves_multilabel_coverage_when_a_valid_partition_exists() -> None:
    from agent_harness.evals import DatasetSplitService

    request = _request(
        tags=["retrieval_quality", "tool_selection"],
        holdout_ratio=0.4,
        optimization_ratio=0.6,
    )
    result = DatasetSplitService().build(
        request,
        [
            _case("multi-1", tags=["retrieval_quality", "tool_selection"]),
            _case("multi-2", tags=["retrieval_quality", "tool_selection"]),
            _case("tool-1", tags=["tool_selection"]),
            _case("tool-2", tags=["tool_selection"]),
            _case("tool-3", tags=["tool_selection"]),
        ],
    )

    for tag in ("retrieval_quality", "tool_selection"):
        assert result.tag_distribution[tag]["optimization"] >= 1
        assert result.tag_distribution[tag]["holdout"] >= 1


def test_split_backtracks_when_multitag_first_choice_would_block_valid_partition() -> None:
    from agent_harness.evals import DatasetSplitService

    request = _request(
        tags=["retrieval_quality", "tool_selection"],
        holdout_ratio=2 / 3,
        optimization_ratio=1 / 3,
    )
    result = DatasetSplitService().build(
        request,
        [
            _case("retrieval-only", tags=["retrieval_quality"]),
            _case("tool-only", tags=["tool_selection"]),
            _case("both", tags=["retrieval_quality", "tool_selection"]),
        ],
    )

    assert result.optimization_case_ids == ["both"]
    assert result.holdout_case_ids == ["retrieval-only", "tool-only"]


@pytest.mark.parametrize(
    "unsafe_ref",
    [
        "/Users/alice/private/eval.json",
        "C:\\Users\\alice\\private\\eval.json",
        "file:///tmp/eval.json",
        "sk-proj-secret123456789",
        "artifact://trace/[REDACTED]",
    ],
)
def test_split_rejects_secret_or_absolute_local_evidence_refs(unsafe_ref: str) -> None:
    from agent_harness.evals import DatasetSplitError, DatasetSplitService

    request = _request(evidence_refs=[unsafe_ref])
    with pytest.raises(DatasetSplitError) as captured:
        DatasetSplitService().build(
            request,
            [
                _case("case-a", tags=["tool_selection"]),
                _case("case-b", tags=["tool_selection"]),
            ],
        )

    assert captured.value.code == "eval.split.evidence_ref_unsafe"
    assert captured.value.field_path == "evidence_refs"


def test_invalid_critical_regression_ref_reports_its_actual_field() -> None:
    from agent_harness.evals import DatasetSplitError, DatasetSplitService, RegressionPolicy

    with pytest.raises(DatasetSplitError) as captured:
        DatasetSplitService().build(
            _request(regression_policy=RegressionPolicy(critical_case_ids=["missing-critical"])),
            [
                _case("case-a", tags=["tool_selection"]),
                _case("case-b", tags=["tool_selection"]),
            ],
        )

    assert captured.value.code == "eval.split.regression_case_not_found"
    assert captured.value.field_path == "regression_policy.critical_case_ids"


def test_regression_policy_roundtrips_with_stable_comparison_semantics() -> None:
    from agent_harness.evals import DatasetSplitRequest

    request = _request(
        regression_policy={
            "case_ids": ["regression-1"],
            "critical_case_ids": ["critical-1"],
            "critical_tags": ["policy_approval"],
            "max_holdout_regression": 0.05,
        }
    )
    restored = DatasetSplitRequest.model_validate(request.to_payload())

    assert restored == request
    assert restored.regression_policy.critical_tags[0].value == "policy_approval"
    assert restored.regression_policy.max_holdout_regression == 0.05


def test_split_search_handles_large_tagged_dataset_deterministically() -> None:
    from agent_harness.evals import DatasetSplitService

    tags = [
        "tool_selection",
        "retrieval_quality",
        "followup_quality",
        "policy_approval",
        "context_trust_boundary",
    ]
    cases = [
        _case(
            f"case-{index:03d}",
            tags=[tags[index % len(tags)], tags[(index + 1) % len(tags)]],
        )
        for index in range(500)
    ]
    request = _request(tags=tags)

    first = DatasetSplitService().build(request, cases)
    second = DatasetSplitService().build(request, list(reversed(cases)))

    assert first.optimization_case_count == 400
    assert first.holdout_case_count == 100
    assert first.model_dump() == second.model_dump()
